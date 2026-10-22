"""
数据库模块

负责数据库连接、SQL 执行和结果获取。
将数据库操作封装为独立模块，便于后续扩展（如连接池、读写分离等）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from queue import Empty, LifoQueue
from time import perf_counter
from typing import Any, Callable

import pymysql

from config import APP_CONFIG, DB_RUNTIME_CONFIG, get_database_source_config
from security import QuerySecurityManager, SecurityError, UserContext
from tracing import traceable, trim_execute_inputs, trim_execute_outputs

logger = logging.getLogger("chatbi.database")
# 审计日志：JSON 行，只记录访问轨迹，不记录结果数据
audit_logger = logging.getLogger("chatbi.audit")


class QueryExecutionError(RuntimeError):
    """数据库执行失败后的统一异常。"""

    def __init__(
        self,
        error_type: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.metadata = metadata or {}


class DatabaseConnectionPool:
    """轻量连接池，优先复用空闲连接，避免每次查询重新建连。"""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        pool_size: int,
        max_overflow: int = 0,
        pool_timeout: float = 3.0,
    ):
        self.connection_factory = connection_factory
        self.pool_size = max(pool_size, 1)
        self.max_overflow = max(max_overflow, 0)
        self.pool_timeout = pool_timeout
        self._idle_connections: LifoQueue[Any] = LifoQueue(maxsize=self.pool_size)
        self._total_connections = 0

    def acquire(self) -> Any:
        try:
            conn = self._idle_connections.get_nowait()
        except Empty:
            if self._total_connections < self.pool_size + self.max_overflow:
                conn = self.connection_factory()
                self._total_connections += 1
                return conn
            conn = self._idle_connections.get(timeout=self.pool_timeout)

        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            self._discard_connection(conn)
            conn = self.connection_factory()
            self._total_connections += 1
            return conn

    def release(self, conn: Any) -> None:
        try:
            self._idle_connections.put_nowait(conn)
        except Exception:
            self._discard_connection(conn)

    def close_all(self) -> None:
        while True:
            try:
                conn = self._idle_connections.get_nowait()
            except Empty:
                break
            self._discard_connection(conn)

    def _discard_connection(self, conn: Any) -> None:
        try:
            conn.close()
        finally:
            self._total_connections = max(self._total_connections - 1, 0)


class DatabaseClient:
    """MySQL 数据库客户端"""

    def __init__(
        self,
        db_config: dict[str, Any] | None = None,
        source_id: str | None = None,
        connection_factory: Callable[[], Any] | None = None,
        security_manager: QuerySecurityManager | None = None,
        slow_query_threshold_ms: float | None = None,
        time_fn: Callable[[], float] | None = None,
        connection_pool: DatabaseConnectionPool | None = None,
    ):
        self.source_id = source_id or APP_CONFIG["database"]["default_source"]
        self.config = db_config or get_database_source_config(self.source_id)
        self.security = security_manager or QuerySecurityManager()
        self.time_fn = time_fn or perf_counter
        self.slow_query_threshold_ms = (
            slow_query_threshold_ms
            if slow_query_threshold_ms is not None
            else DB_RUNTIME_CONFIG["slow_query_threshold_ms"]
        )
        self.connection_factory = connection_factory
        self.connection_pool = connection_pool
        self.last_query_info: dict[str, Any] = {}
        self.max_rows = int(
            APP_CONFIG.get("security", {}).get("max_rows", 1000)
        )
        self.query_timeout_ms = int(
            APP_CONFIG.get("security", {}).get("query_timeout_ms", 30000)
        )

        if self.connection_factory is None and self.connection_pool is None:
            self.connection_pool = DatabaseConnectionPool(
                connection_factory=lambda: pymysql.connect(**self._connection_kwargs()),
                pool_size=DB_RUNTIME_CONFIG["pool_size"],
                max_overflow=DB_RUNTIME_CONFIG["max_overflow"],
                pool_timeout=DB_RUNTIME_CONFIG["pool_timeout"],
            )

        # 行级权限依赖 dim_stores.owner -> store_id 映射：启动时预热，之后按 TTL 刷新
        if getattr(self.security, "store_directory", None) is not None:
            self.security.store_directory.attach_fetcher(self._fetch_store_owner_map)
            if connection_factory is None:
                try:
                    self.security.store_directory.refresh()
                except Exception as exc:  # noqa: BLE001 - 数据库暂不可用时延迟到首次查询再取
                    logger.warning("Store owner map prefetch failed: %s", exc)

    @traceable(
        name="SQL执行",
        run_type="tool",
        process_inputs=trim_execute_inputs,
        process_outputs=trim_execute_outputs,
    )
    def execute(
        self,
        sql: str,
        user: UserContext | None = None,
        *,
        question: str | None = None,
    ) -> tuple[list[str], list[tuple]]:
        """
        执行 SQL 并返回结果

        Args:
            sql: 待执行的 SQL 语句
            user: 权限上下文；未传时使用 demo_admin（评测等内部调用）
            question: 原始自然语言问题，仅用于审计日志

        Returns:
            (列名列表, 结果行列表)
        """
        user_context = user or UserContext.demo_admin()
        started_at = self.time_fn()

        try:
            secured_sql = self.security.secure_sql(sql, user_context)
        except SecurityError as exc:
            duration_ms = round((self.time_fn() - started_at) * 1000, 2)
            self.last_query_info = {
                "sql": None,
                "duration_ms": duration_ms,
                "slow_query": False,
                "explain_plan": [],
                "truncated": False,
            }
            self._write_audit(
                user_context=user_context,
                question=question,
                original_sql=sql,
                final_sql=None,
                blocked=True,
                block_reason=str(exc),
                duration_ms=duration_ms,
                row_count=0,
                truncated=False,
            )
            raise

        conn = None
        try:
            conn = self._acquire_connection()
            with conn.cursor() as cursor:
                self._prepare_session(cursor)
                cursor.execute(secured_sql)
                columns = [desc[0] for desc in cursor.description] if cursor.description else []
                fetched = self._fetch_rows(cursor, self.max_rows + 1)
                truncated = len(fetched) > self.max_rows
                results = fetched[: self.max_rows] if truncated else fetched
                duration_ms = round((self.time_fn() - started_at) * 1000, 2)
                explain_plan = self._explain(cursor, secured_sql, duration_ms)
                self.last_query_info = {
                    "sql": secured_sql,
                    "duration_ms": duration_ms,
                    "slow_query": bool(explain_plan),
                    "explain_plan": explain_plan,
                    "truncated": truncated,
                }
                if explain_plan:
                    logger.warning(
                        "Slow query detected: duration_ms=%s sql=%s",
                        duration_ms,
                        secured_sql,
                    )
                self._write_audit(
                    user_context=user_context,
                    question=question,
                    original_sql=sql,
                    final_sql=secured_sql,
                    blocked=False,
                    block_reason=None,
                    duration_ms=duration_ms,
                    row_count=len(results),
                    truncated=truncated,
                )
                _, masked_rows = self.security.mask_result(columns, results, user_context)
                return columns, masked_rows
        except SecurityError:
            raise
        except (pymysql.MySQLError, OSError) as exc:
            duration_ms = round((self.time_fn() - started_at) * 1000, 2)
            self.last_query_info = {
                "sql": secured_sql,
                "duration_ms": duration_ms,
                "slow_query": False,
                "explain_plan": [],
                "truncated": False,
            }
            self._write_audit(
                user_context=user_context,
                question=question,
                original_sql=sql,
                final_sql=secured_sql,
                blocked=False,
                block_reason=str(exc),
                duration_ms=duration_ms,
                row_count=0,
                truncated=False,
            )
            raise self._translate_db_error(exc, secured_sql, duration_ms) from exc
        finally:
            if conn is not None:
                self._release_connection(conn)

    def _prepare_session(self, cursor: Any) -> None:
        """查询前设置会话级防护：只读事务 + 执行超时（毫秒）。"""
        cursor.execute("SET SESSION TRANSACTION READ ONLY")
        cursor.execute(f"SET SESSION max_execution_time = {int(self.query_timeout_ms)}")

    @staticmethod
    def _fetch_rows(cursor: Any, limit: int) -> list[tuple]:
        if hasattr(cursor, "fetchmany"):
            return list(cursor.fetchmany(limit))
        return list(cursor.fetchall())

    def _fetch_store_owner_map(self) -> dict[str, list[str]]:
        """从 dim_stores 读出 owner -> store_id 映射（固定 SQL，供行级权限缓存刷新用）。"""
        conn = self._acquire_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT store_id, owner FROM dim_stores")
                rows = cursor.fetchall()
        finally:
            self._release_connection(conn)
        mapping: dict[str, list[str]] = {}
        for store_id, owner in rows:
            if owner is None:
                continue
            mapping.setdefault(str(owner), []).append(str(store_id))
        return mapping

    def _write_audit(
        self,
        *,
        user_context: UserContext,
        question: str | None,
        original_sql: str,
        final_sql: str | None,
        blocked: bool,
        block_reason: str | None,
        duration_ms: float,
        row_count: int,
        truncated: bool,
    ) -> None:
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "user_id": user_context.user_id,
            "role": user_context.role,
            "operator_name": user_context.operator_name,
            "question": question,
            "original_sql": original_sql,
            "final_sql": final_sql,
            "blocked": blocked,
            "block_reason": block_reason,
            "duration_ms": duration_ms,
            "row_count": row_count,
            "truncated": truncated,
        }
        try:
            audit_logger.info(json.dumps(entry, ensure_ascii=False, default=str))
        except Exception:  # noqa: BLE001 - 审计失败不影响主流程
            logger.warning("Failed to write audit log entry")

    def validate_connection(self) -> bool:
        """验证数据库连接是否正常"""
        try:
            conn = self._acquire_connection()
            self._release_connection(conn)
            return True
        except Exception:
            return False

    def _acquire_connection(self) -> Any:
        if self.connection_pool is not None:
            return self.connection_pool.acquire()
        if self.connection_factory is None:
            raise RuntimeError("数据库连接工厂未初始化")
        return self.connection_factory()

    def _release_connection(self, conn: Any) -> None:
        if self.connection_pool is not None:
            self.connection_pool.release(conn)
        else:
            conn.close()

    def _connection_kwargs(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in self.config.items()
            if key not in {"driver", "name"}
        }

    def _explain(self, cursor: Any, sql: str, duration_ms: float) -> list[dict[str, Any]]:
        if duration_ms < self.slow_query_threshold_ms:
            return []
        if not sql.lstrip().upper().startswith(("SELECT", "WITH")):
            return []
        try:
            cursor.execute(f"EXPLAIN {sql}")
            explain_columns = [desc[0] for desc in cursor.description] if cursor.description else []
            explain_rows = cursor.fetchall()
            return [
                dict(zip(explain_columns, row))
                for row in explain_rows
            ]
        except Exception as exc:
            logger.warning("Failed to capture EXPLAIN plan: %s", exc)
            return []

    def _translate_db_error(
        self,
        exc: Exception,
        sql: str,
        duration_ms: float,
    ) -> QueryExecutionError:
        error_code = exc.args[0] if getattr(exc, "args", None) else None
        error_text = str(exc)
        metadata = {
            "sql": sql,
            "duration_ms": duration_ms,
            "error_code": error_code,
            "raw_error": error_text,
        }

        if isinstance(exc, pymysql.err.ProgrammingError) or error_code == 1064:
            return QueryExecutionError(
                "sql_syntax",
                "SQL 语法错误，请检查字段、聚合和别名是否正确",
                metadata,
            )
        if error_code in {1044, 1045, 1142, 1143, 1227}:
            return QueryExecutionError(
                "permission_denied",
                "数据库拒绝执行该查询，请检查当前账号权限",
                metadata,
            )
        if error_code in {1205, 3024} or (
            error_code in {2013}
            and "timed out" in error_text.lower()
        ):
            return QueryExecutionError(
                "query_timeout",
                "SQL 执行超时，请缩小时间范围或减少返回列后重试",
                metadata,
            )
        if isinstance(exc, OSError) or error_code in {2003, 2006, 2013}:
            return QueryExecutionError(
                "connection_error",
                "数据库连接失败，请检查连接池和数据库状态",
                metadata,
            )
        return QueryExecutionError(
            "execution_error",
            f"SQL 执行失败：{error_text}",
            metadata,
        )
