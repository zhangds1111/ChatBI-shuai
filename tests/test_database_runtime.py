from pathlib import Path
import json
import sys

import pymysql
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import (
    DatabaseClient,
    DatabaseConnectionPool,
    QueryExecutionError,
)
from security import QuerySecurityManager, StoreDirectory, UserContext


def _primed_security_manager() -> QuerySecurityManager:
    directory = StoreDirectory()
    directory.prime({"运营A": ["US01", "CA01"], "运营B": ["UK01", "DE01"]})
    return QuerySecurityManager(store_directory=directory)


def _is_query(sql: str) -> bool:
    return sql.strip().upper().startswith(("SELECT", "WITH", "EXPLAIN"))


class ExplainableCursor:
    def __init__(self):
        self.description = [("store_id",), ("revenue",)]
        self.executed_sqls = []
        self._last_sql = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str):
        self.executed_sqls.append(sql)
        self._last_sql = sql
        if sql.strip().upper().startswith("EXPLAIN"):
            self.description = [("id",), ("select_type",), ("table",), ("type",), ("key",)]
        elif _is_query(sql):
            self.description = [("store_id",), ("revenue",)]

    def fetchall(self):
        return self.fetchmany(1000)

    def fetchmany(self, size: int):
        if self._last_sql.strip().upper().startswith("EXPLAIN"):
            return [(1, "SIMPLE", "amz_order_items", "ALL", None)]
        return [("US01", 1250000)]


class SyntaxErrorCursor:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str):
        if _is_query(sql) and not sql.strip().upper().startswith("EXPLAIN"):
            raise pymysql.err.ProgrammingError(1064, "You have an error in your SQL syntax")

    def fetchall(self):
        return []

    def fetchmany(self, size: int):
        return []


class TimedOutCursor:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str):
        if _is_query(sql) and not sql.strip().upper().startswith("EXPLAIN"):
            raise pymysql.err.OperationalError(
                2013,
                "Lost connection to MySQL server during query (The read operation timed out)",
            )

    def fetchall(self):
        return []

    def fetchmany(self, size: int):
        return []


class ManyRowsCursor:
    """返回超出 MAX_ROWS 的行，用于截断测试。"""

    def __init__(self, total_rows: int):
        self.description = [("store_id",)]
        self._rows = [(f"S{i:02d}",) for i in range(total_rows)]
        self._cursor = 0
        self.executed_sqls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str):
        self.executed_sqls.append(sql)
        self._cursor = 0

    def fetchmany(self, size: int):
        chunk = self._rows[self._cursor : self._cursor + size]
        self._cursor += len(chunk)
        return chunk

    def fetchall(self):
        return self.fetchmany(len(self._rows))


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True

    def ping(self, reconnect=False):
        return None


def test_database_client_classifies_sql_syntax_errors():
    client = DatabaseClient(connection_factory=lambda: FakeConnection(SyntaxErrorCursor()))

    with pytest.raises(QueryExecutionError) as exc_info:
        client.execute("SELECT broken_column FROM amz_order_items")

    assert exc_info.value.error_type == "sql_syntax"
    assert exc_info.value.metadata["error_code"] == 1064


def test_database_client_classifies_connection_factory_errors():
    client = DatabaseClient(
        connection_factory=lambda: (_ for _ in ()).throw(
            pymysql.err.OperationalError(
                1045,
                "Access denied for user 'chatbi'@'localhost' (using password: YES)",
            )
        )
    )

    with pytest.raises(QueryExecutionError) as exc_info:
        client.execute("SELECT * FROM amz_order_items")

    assert exc_info.value.error_type == "permission_denied"
    assert exc_info.value.metadata["error_code"] == 1045


def test_database_client_records_explain_for_slow_queries():
    time_points = iter([10.0, 10.25])
    client = DatabaseClient(
        connection_factory=lambda: FakeConnection(ExplainableCursor()),
        slow_query_threshold_ms=100,
        time_fn=lambda: next(time_points),
    )

    columns, rows = client.execute(
        "SELECT store_id, SUM(item_amount) AS revenue FROM amz_order_items GROUP BY store_id",
        user=UserContext(user_id="u_admin", role="admin"),
    )

    assert columns == ["store_id", "revenue"]
    assert rows == [("US01", 1250000)]
    assert client.last_query_info["duration_ms"] == 250.0
    assert client.last_query_info["slow_query"] is True
    assert client.last_query_info["explain_plan"][0]["table"] == "amz_order_items"


def test_database_client_sets_readonly_session_and_timeout():
    cursor = ExplainableCursor()
    client = DatabaseClient(connection_factory=lambda: FakeConnection(cursor))

    client.execute("SELECT * FROM amz_order_items", user=UserContext(user_id="u_admin", role="admin"))

    assert "SET SESSION TRANSACTION READ ONLY" in cursor.executed_sqls
    assert any(
        sql.startswith("SET SESSION max_execution_time") for sql in cursor.executed_sqls
    )


def test_database_client_truncates_rows_beyond_max_rows():
    cursor = ManyRowsCursor(total_rows=1500)
    client = DatabaseClient(connection_factory=lambda: FakeConnection(cursor))
    client.max_rows = 1000  # 固定验收上限

    columns, rows = client.execute(
        "SELECT store_id FROM amz_order_items",
        user=UserContext(user_id="u_admin", role="admin"),
    )

    assert len(rows) == 1000
    assert client.last_query_info["truncated"] is True


def test_database_client_marks_not_truncated_for_small_results():
    cursor = ExplainableCursor()
    client = DatabaseClient(connection_factory=lambda: FakeConnection(cursor))

    client.execute("SELECT store_id FROM amz_order_items", user=UserContext(user_id="u_admin", role="admin"))

    assert client.last_query_info["truncated"] is False


def test_database_client_writes_audit_log(caplog):
    cursor = ExplainableCursor()
    manager_client = DatabaseClient(
        connection_factory=lambda: FakeConnection(cursor),
        security_manager=_primed_security_manager(),
    )

    with caplog.at_level("INFO", logger="chatbi.audit"):
        manager_client.execute(
            "SELECT store_id FROM amz_order_items",
            user=UserContext(user_id="u_ops_a", role="ops", operator_name="运营A"),
            question="店铺销售情况",
        )

    audit_records = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "chatbi.audit"
    ]
    assert audit_records
    entry = audit_records[-1]
    assert entry["user_id"] == "u_ops_a"
    assert entry["role"] == "ops"
    assert entry["question"] == "店铺销售情况"
    assert entry["blocked"] is False
    assert entry["final_sql"] and "store_id IN" in entry["final_sql"]
    assert entry["row_count"] == 1
    assert entry["truncated"] is False


def test_database_client_audits_blocked_queries(caplog):
    cursor = ExplainableCursor()
    client = DatabaseClient(connection_factory=lambda: FakeConnection(cursor))

    with caplog.at_level("INFO", logger="chatbi.audit"):
        with pytest.raises(Exception):
            client.execute(
                "DROP TABLE amz_order_items",
                user=UserContext(user_id="u1", role="admin"),
            )

    audit_records = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "chatbi.audit"
    ]
    assert audit_records
    entry = audit_records[-1]
    assert entry["blocked"] is True
    assert entry["block_reason"]
    assert entry["final_sql"] is None


def test_database_client_refreshes_store_owner_map():
    class StoreCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql: str):
            self.sql = sql

        def fetchall(self):
            return [("US01", "运营A"), ("CA01", "运营A"), ("UK01", "运营B"), ("DE01", "运营B")]

        def fetchmany(self, size: int):
            return self.fetchall()

    class StoreConnection:
        def cursor(self):
            return StoreCursor()

        def close(self):
            pass

        def ping(self, reconnect=False):
            return None

    client = DatabaseClient(connection_factory=lambda: StoreConnection())
    # DatabaseClient 初始化时已把 _fetch_store_owner_map 挂到 store_directory
    mapping = client.security.store_directory.refresh()
    assert mapping == {"运营A": ["US01", "CA01"], "运营B": ["UK01", "DE01"]}


def test_database_client_classifies_read_timeout_as_query_timeout():
    client = DatabaseClient(connection_factory=lambda: FakeConnection(TimedOutCursor()))

    with pytest.raises(QueryExecutionError) as exc_info:
        client.execute("SELECT * FROM amz_order_items")

    assert exc_info.value.error_type == "query_timeout"
    assert exc_info.value.metadata["error_code"] == 2013


def test_connection_pool_reuses_idle_connections():
    created = []

    def connection_factory():
        conn = FakeConnection(ExplainableCursor())
        created.append(conn)
        return conn

    pool = DatabaseConnectionPool(
        connection_factory=connection_factory,
        pool_size=1,
    )

    first = pool.acquire()
    pool.release(first)
    second = pool.acquire()

    assert first is second
    assert len(created) == 1
    pool.release(second)
    pool.close_all()


def test_chatbi_system_returns_granular_database_error_type_for_timeout():
    from main import ChatBISystem

    class FakeParser:
        def parse(self, question: str) -> str:
            return question

        def validate(self, parsed: str) -> bool:
            return True

    class FakeLLM:
        def generate_sql(self, _system_msg: str, _prompt: str) -> str:
            return "SELECT * FROM amz_order_items"

    class FakeDB:
        def execute(self, sql: str, user=None, **kwargs):
            raise QueryExecutionError(
                "query_timeout",
                "SQL 执行超时，请缩小时间范围后重试",
                metadata={"duration_ms": 3100.0},
            )

    system = ChatBISystem()
    system.parser = FakeParser()
    system.llm = FakeLLM()
    system.db = FakeDB()

    result = system.run("查看最近 12 个月订单明细")

    assert result["success"] is False
    assert result["error_type"] == "database_query_timeout"
    assert result["metadata"]["db_duration_ms"] == 3100.0
