"""SQL 安全与权限模块。

职责：
1. 基于 sqlglot AST 的 SQL 校验与改写（表白名单、函数黑名单、行数上限、禁止加锁/多语句等）。
2. 亚马逊行级权限：ops 角色查询时，把每个带 store_id 的表替换成带 store_id 过滤的子查询。
3. 敏感列拦截（AST 级）与结果脱敏（mask_result，面向 customer_phone 等输出列）。
4. HMAC-SHA256 签名令牌的签发/校验，供 API 层做身份认证。

设计约束：
- 只做 AST 判断，不对 SQL 原文做关键词搜索，避免误杀 REPLACE()、字符串里的分号/关键字。
- 表白名单、函数黑名单、敏感列等策略全部走配置（security_policy.json + 环境变量），不写死在代码里。
- secure_sql / mask_result 的对外签名保持稳定，已调用方（main、api_service、evaluator）无需改动即可工作。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from config import APP_CONFIG

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

IMPLEMENTED_ROLES = {"admin", "ops"}
# 已知但未在新业务中实现的角色：出现时报"未配置的角色"，与"未知角色"区分开
DECLARED_UNCONFIGURED_ROLES = {"finance", "sales"}

DEFAULT_FORBIDDEN_SCHEMAS = ("information_schema", "mysql", "performance_schema", "sys")
DEFAULT_FORBIDDEN_FUNCTIONS = (
    "SLEEP",
    "BENCHMARK",
    "LOAD_FILE",
    "GET_LOCK",
    "RELEASE_LOCK",
    "UUID_SHORT",
)
DEFAULT_MASKED_COLUMNS = ("customer_phone", "customer_email")

_PROJECT_ROOT = Path(__file__).resolve().parent


class SecurityError(Exception):
    """权限校验或 SQL 安全检查失败。"""


# ---------------------------------------------------------------------------
# 用户上下文
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class UserContext:
    """当前请求的最小权限上下文。

    身份信息只信任服务端签发的签名令牌（见 verify_auth_token），不接受请求方自填。
    ops（运营）角色通过 operator_name 关联 dim_stores.owner，决定可见店铺。
    """

    user_id: str
    role: str = "admin"
    operator_name: str | None = None  # ops 角色的运营负责人（dim_stores.owner）

    @classmethod
    def demo_admin(cls) -> "UserContext":
        return cls(user_id="demo_admin", role="admin")


# ---------------------------------------------------------------------------
# 店铺归属目录（owner -> store_id 映射缓存）
# ---------------------------------------------------------------------------


class StoreDirectory:
    """owner（运营负责人）-> store_id 列表 的映射缓存。

    映射由数据层在启动时（及每 10 分钟 TTL 刷新）从 dim_stores 读出，不在代码里写死店铺清单。
    """

    def __init__(
        self,
        fetcher: Callable[[], dict[str, list[str]]] | None = None,
        ttl_seconds: float | None = None,
        time_fn: Callable[[], float] | None = None,
    ):
        self._fetcher = fetcher
        self._ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else float(APP_CONFIG.get("security", {}).get("store_cache_ttl_seconds", 600))
        )
        self._time_fn = time_fn or time.time
        self._cache: dict[str, list[str]] | None = None
        self._loaded_at = 0.0

    def attach_fetcher(self, fetcher: Callable[[], dict[str, list[str]]]) -> None:
        self._fetcher = fetcher

    def prime(self, mapping: dict[str, list[str]]) -> None:
        """直接灌入映射（测试或启动预热用）。"""
        self._cache = {owner: list(stores) for owner, stores in mapping.items()}
        self._loaded_at = self._time_fn()

    def refresh(self) -> dict[str, list[str]]:
        if self._fetcher is None:
            raise SecurityError("无法获取店铺归属映射：StoreDirectory 未配置数据源")
        self._cache = self._fetcher()
        self._loaded_at = self._time_fn()
        return self._cache

    def get_store_ids(self, operator_name: str) -> list[str]:
        mapping = self._load()
        return list(mapping.get(operator_name, []))

    def _load(self) -> dict[str, list[str]]:
        now = self._time_fn()
        if self._cache is not None and now - self._loaded_at < self._ttl:
            return self._cache
        if self._fetcher is None:
            if self._cache is not None:
                # 无数据源时允许使用过期缓存兜底（例如单测）
                return self._cache
            raise SecurityError("无法获取店铺归属映射：StoreDirectory 未配置数据源")
        return self.refresh()


# ---------------------------------------------------------------------------
# 令牌签发/校验（HMAC-SHA256）
# ---------------------------------------------------------------------------


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _resolve_secret(secret: str | None) -> str:
    resolved = secret or os.getenv("AUTH_SECRET") or APP_CONFIG.get("security", {}).get("auth_secret", "")
    if not resolved:
        raise SecurityError("服务端未配置 AUTH_SECRET，无法校验令牌")
    return resolved


def make_auth_token(
    user_id: str,
    role: str,
    secret: str | None = None,
    expires_in: int = 86400,
    operator_name: str | None = None,
    now: float | None = None,
) -> str:
    """签发签名令牌。载荷至少含 user_id、role、exp（过期时间，Unix 秒）。"""
    key = _resolve_secret(secret)
    issued_at = now if now is not None else time.time()
    payload: dict[str, Any] = {
        "user_id": user_id,
        "role": role,
        "iat": int(issued_at),
        "exp": int(issued_at) + int(expires_in),
    }
    if operator_name:
        payload["operator_name"] = operator_name
    payload_raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(key.encode("utf-8"), payload_raw, hashlib.sha256).digest()
    return f"{_b64url_encode(payload_raw)}.{_b64url_encode(signature)}"


def verify_auth_token(token: str, secret: str | None = None, now: float | None = None) -> UserContext:
    """校验签名令牌并还原 UserContext。签名错误/过期/缺字段一律抛 SecurityError（API 层映射 401）。"""
    key = _resolve_secret(secret)
    if not token or "." not in token:
        raise SecurityError("令牌格式错误")
    payload_part, signature_part = token.rsplit(".", 1)
    try:
        payload_raw = _b64url_decode(payload_part)
        signature = _b64url_decode(signature_part)
    except Exception as exc:  # noqa: BLE001 - base64 解码失败都视为非法令牌
        raise SecurityError("令牌格式错误") from exc

    expected = hmac.new(key.encode("utf-8"), payload_raw, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise SecurityError("令牌签名无效")

    try:
        payload = json.loads(payload_raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SecurityError("令牌载荷解析失败") from exc
    if not isinstance(payload, dict):
        raise SecurityError("令牌载荷解析失败")

    if "role" not in payload or not payload["role"]:
        raise SecurityError("令牌缺少 role 字段")
    if "user_id" not in payload or not payload["user_id"]:
        raise SecurityError("令牌缺少 user_id 字段")
    if "exp" not in payload:
        raise SecurityError("令牌缺少过期时间 exp")

    current = now if now is not None else time.time()
    try:
        expires_at = float(payload["exp"])
    except (TypeError, ValueError) as exc:
        raise SecurityError("令牌过期时间非法") from exc
    if current >= expires_at:
        raise SecurityError("令牌已过期")

    return UserContext(
        user_id=str(payload["user_id"]),
        role=str(payload["role"]),
        operator_name=payload.get("operator_name"),
    )


def resolve_request_user(
    authorization: str | None,
    *,
    secret: str | None = None,
    allow_demo_admin: bool | None = None,
    now: float | None = None,
) -> UserContext:
    """从 Authorization: Bearer <token> 解析身份。

    - 有令牌：校验签名/过期/必填字段，失败抛 SecurityError（API 层 401）。
    - 无令牌：仅当 ALLOW_DEMO_ADMIN=true 时放行为 demo_admin；默认拒绝。
    """
    header = (authorization or "").strip()
    if header:
        if not header.lower().startswith("bearer "):
            raise SecurityError("Authorization 头必须使用 Bearer 令牌")
        return verify_auth_token(header[7:].strip(), secret=secret, now=now)

    if allow_demo_admin is None:
        env_value = os.getenv("ALLOW_DEMO_ADMIN")
        if env_value is not None:
            allowed = env_value.lower() == "true"
        else:
            allowed = bool(APP_CONFIG.get("security", {}).get("allow_demo_admin", False))
    else:
        allowed = allow_demo_admin
    if allowed:
        return UserContext.demo_admin()
    raise SecurityError("缺少 Authorization: Bearer 令牌")


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------


def _resolve_policy_path(path: str | None) -> Path:
    raw = path or APP_CONFIG.get("security", {}).get("policy_path") or "security_policy.json"
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else _PROJECT_ROOT / candidate


def load_security_policy(path: str | None = None) -> dict[str, Any]:
    policy_file = _resolve_policy_path(path)
    if not policy_file.is_file():
        raise SecurityError(f"安全策略文件缺失：{policy_file}")
    try:
        with open(policy_file, encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise SecurityError(f"安全策略文件解析失败：{policy_file}") from exc
    if not isinstance(data, dict):
        raise SecurityError(f"安全策略文件格式非法：{policy_file}")
    return data


def load_table_columns(path: str | None = None) -> dict[str, list[str]]:
    """从 field_metadata.json（key 为 "table.field"）整理出 表 -> 列名列表，供 SELECT * 展开使用。"""
    cfg = APP_CONFIG.get("security", {})
    raw = path or cfg.get("table_columns_path") or "field_metadata.json"
    columns_file = Path(raw)
    if not columns_file.is_absolute():
        columns_file = _PROJECT_ROOT / columns_file
    if not columns_file.is_file():
        return {}
    try:
        with open(columns_file, encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError:
        return {}
    result: dict[str, list[str]] = {}
    for key, meta in data.items():
        if not isinstance(meta, dict):
            continue
        table = str(meta.get("table") or key.split(".", 1)[0]).lower()
        field = meta.get("field") or (key.split(".", 1)[1] if "." in key else None)
        if not field:
            continue
        result.setdefault(table, []).append(str(field))
    return result


# ---------------------------------------------------------------------------
# SQL 校验与改写
# ---------------------------------------------------------------------------


class QuerySecurityManager:
    """负责 SQL 拦截、行级过滤改写和结果脱敏。"""

    def __init__(
        self,
        policy: dict[str, Any] | None = None,
        *,
        store_directory: StoreDirectory | None = None,
        table_columns: dict[str, list[str]] | None = None,
        config: dict[str, Any] | None = None,
    ):
        cfg = config if config is not None else APP_CONFIG.get("security", {})
        self.policy = dict(policy) if policy is not None else load_security_policy(cfg.get("policy_path"))

        self.allowed_tables = {str(name).lower() for name in self.policy.get("allowed_tables", [])}
        self.forbidden_schemas = {
            str(name).lower()
            for name in self.policy.get("forbidden_schemas", DEFAULT_FORBIDDEN_SCHEMAS)
        }
        self.forbidden_functions = {
            str(name).upper()
            for name in self.policy.get("forbidden_functions", DEFAULT_FORBIDDEN_FUNCTIONS)
        }
        self.restricted_columns_by_table: dict[str, set[str]] = {
            str(table).lower(): {str(col).lower() for col in cols}
            for table, cols in self.policy.get("restricted_columns", {}).items()
        }
        self.restricted_columns: set[str] = {
            col for cols in self.restricted_columns_by_table.values() for col in cols
        }
        self.masked_columns = {
            str(name).lower() for name in self.policy.get("masked_columns", DEFAULT_MASKED_COLUMNS)
        }
        self.row_level_tables = {
            str(name).lower() for name in self.policy.get("row_level_tables", [])
        }
        self.row_level_column = str(self.policy.get("row_level_column", "store_id"))

        self.max_rows = int(self.policy.get("max_rows", cfg.get("max_rows", 1000)))
        self.max_tables = int(self.policy.get("max_tables_per_query", cfg.get("max_tables_per_query", 6)))
        self.max_depth = int(self.policy.get("max_subquery_depth", cfg.get("max_subquery_depth", 5)))

        self.store_directory = store_directory or StoreDirectory()
        self.table_columns = {
            str(table).lower(): [str(col) for col in cols]
            for table, cols in (table_columns if table_columns is not None else load_table_columns(cfg.get("table_columns_path"))).items()
        }

    # -- 对外入口 -----------------------------------------------------------

    def secure_sql(self, sql: str, user: UserContext | None = None) -> str:
        """校验并按角色改写 SQL，返回可执行的最终 SQL。拒绝时抛 SecurityError。"""
        user_context = user or UserContext.demo_admin()
        role = self._resolve_role(user_context)
        normalized_sql = self._normalize_sql(sql)
        tree = self._parse_single_statement(normalized_sql)

        self._check_statement_shape(tree)
        self._check_forbidden_nodes(tree)
        self._check_functions(tree)
        cte_names = self._collect_cte_names(tree)
        self._check_tables(tree, cte_names)
        self._check_size_limits(tree, cte_names)
        self._check_restricted_columns(tree, role)

        if role == "ops":
            store_ids = self._resolve_store_ids(user_context)
            self._expand_stars(tree, cte_names)
            self._wrap_row_level_tables(tree, cte_names, store_ids)

        self._enforce_limit(tree)
        return tree.sql(dialect="mysql")

    def mask_result(
        self,
        columns: list[str],
        rows: Iterable[tuple | list],
        user: UserContext | None = None,
    ) -> tuple[list[str], list[tuple]]:
        """对配置的脱敏列（如 customer_phone/customer_email）打码。admin 不脱敏。"""
        user_context = user or UserContext.demo_admin()
        role = self._resolve_role(user_context)
        if role == "admin" or not self.masked_columns:
            return columns, [tuple(row) for row in rows]

        sensitive_indexes = {
            index
            for index, column in enumerate(columns)
            if column.lower() in self.masked_columns
        }
        if not sensitive_indexes:
            return columns, [tuple(row) for row in rows]

        masked_rows: list[tuple] = []
        for row in rows:
            values = list(row)
            for index in sensitive_indexes:
                values[index] = "***"
            masked_rows.append(tuple(values))

        return columns, masked_rows

    # -- 角色 ---------------------------------------------------------------

    def _resolve_role(self, user: UserContext) -> str:
        role = (user.role or "").strip().lower()
        if role in IMPLEMENTED_ROLES:
            return role
        if role in DECLARED_UNCONFIGURED_ROLES:
            raise SecurityError(f"未配置的角色: {role}")
        raise SecurityError(f"未知角色: {role or '<空>'}")

    # -- 解析与语句结构 ------------------------------------------------------

    @staticmethod
    def _normalize_sql(sql: str) -> str:
        normalized = (sql or "").strip()
        normalized = normalized.removeprefix("```sql").removeprefix("```SQL")
        if normalized.endswith("```"):
            normalized = normalized[:-3]
        normalized = normalized.strip()
        return normalized[:-1].strip() if normalized.endswith(";") else normalized

    def _parse_single_statement(self, sql: str) -> exp.Expression:
        if not sql:
            raise SecurityError("SQL 不能为空。")
        try:
            statements = sqlglot.parse(sql, dialect="mysql")
        except (ParseError, TokenError) as exc:
            raise SecurityError(f"SQL 无法解析，禁止执行：{exc}") from exc
        statements = [stmt for stmt in statements if stmt is not None]
        if len(statements) == 0:
            raise SecurityError("SQL 不能为空。")
        if len(statements) > 1:
            raise SecurityError("检测到多语句执行风险，只允许单条 SELECT。")
        return statements[0]

    def _check_statement_shape(self, tree: exp.Expression) -> None:
        self._ensure_query_node(tree)

    def _ensure_query_node(self, node: exp.Expression) -> None:
        current = node
        while isinstance(current, (exp.Paren, exp.Subquery)):
            current = current.this
        if isinstance(current, exp.Select):
            return
        if isinstance(current, (exp.Union, exp.Except, exp.Intersect)):
            self._ensure_query_node(current.args["this"])
            self._ensure_query_node(current.args["expression"])
            return
        raise SecurityError(f"只允许执行 SELECT 查询，禁止 {type(current).__name__} 语句。")

    def _check_forbidden_nodes(self, tree: exp.Expression) -> None:
        for node in tree.walk():
            if isinstance(node, exp.SessionParameter):
                raise SecurityError("禁止引用 @@ 系统变量。")
            if isinstance(node, exp.Parameter):
                raise SecurityError("禁止使用用户变量（@var）。")
            if isinstance(node, exp.Into):
                raise SecurityError("禁止 SELECT ... INTO（OUTFILE/DUMPFILE/变量）。")
            if isinstance(node, exp.Select) and node.args.get("into"):
                raise SecurityError("禁止 SELECT ... INTO（OUTFILE/DUMPFILE/变量）。")
            if isinstance(node, exp.Select) and node.args.get("locks"):
                raise SecurityError("禁止 FOR UPDATE / LOCK IN SHARE MODE 加锁读取。")

    @staticmethod
    def _function_name(node: exp.Func) -> str:
        if isinstance(node, exp.Anonymous):
            return str(node.name).upper()
        names = node.sql_names()
        return str(names[0]).upper() if names else type(node).__name__.upper()

    def _check_functions(self, tree: exp.Expression) -> None:
        for node in tree.find_all(exp.Func):
            name = self._function_name(node)
            if name in self.forbidden_functions:
                raise SecurityError(f"禁止使用函数: {name}")

    @staticmethod
    def _collect_cte_names(tree: exp.Expression) -> set[str]:
        return {
            cte.alias_or_name.lower()
            for cte in tree.find_all(exp.CTE)
            if cte.alias_or_name
        }

    def _check_tables(self, tree: exp.Expression, cte_names: set[str]) -> None:
        for table in tree.find_all(exp.Table):
            qualified = table.sql(dialect="mysql")
            db_name = (table.db or "").lower()
            catalog_name = (table.catalog or "").lower()
            if catalog_name or db_name:
                if db_name in self.forbidden_schemas or catalog_name in self.forbidden_schemas:
                    raise SecurityError(f"禁止访问系统库中的表: {qualified}")
                raise SecurityError(f"禁止使用带库名前缀的表: {qualified}")
            name = table.name.lower()
            if name in self.forbidden_schemas:
                raise SecurityError(f"禁止访问系统库中的表: {qualified}")
            if name in cte_names:
                continue  # CTE 名称不是真实表
            if name not in self.allowed_tables:
                raise SecurityError(f"表 {table.name} 不在白名单内，禁止查询。")

    def _check_size_limits(self, tree: exp.Expression, cte_names: set[str]) -> None:
        real_tables = {
            table.name.lower()
            for table in tree.find_all(exp.Table)
            if table.name.lower() not in cte_names
        }
        if len(real_tables) > self.max_tables:
            raise SecurityError(
                f"查询引用了 {len(real_tables)} 张表，超过上限 {self.max_tables}，请拆分查询。"
            )
        max_depth = 0
        for select in tree.find_all(exp.Select):
            depth = 1
            parent = select.parent
            while parent is not None:
                if isinstance(parent, exp.Select):
                    depth += 1
                parent = parent.parent
            max_depth = max(max_depth, depth)
        if max_depth > self.max_depth:
            raise SecurityError(
                f"子查询嵌套深度 {max_depth} 超过上限 {self.max_depth}，请简化查询。"
            )

    def _check_restricted_columns(self, tree: exp.Expression, role: str) -> None:
        if role != "ops" or not self.restricted_columns:
            return
        for column in tree.find_all(exp.Column):
            if isinstance(column.this, exp.Star):
                continue  # SELECT * / t.* 由 _expand_stars 处理
            name = column.name.lower()
            if name in self.restricted_columns:
                owner_table = next(
                    (
                        table
                        for table, cols in self.restricted_columns_by_table.items()
                        if name in cols
                    ),
                    "?",
                )
                raise SecurityError(
                    f"受限列 {owner_table}.{name} 包含成本等敏感信息，当前角色无权引用。"
                )

    # -- 行级权限与 SELECT * 展开 --------------------------------------------

    def _resolve_store_ids(self, user: UserContext) -> list[str]:
        if not user.operator_name:
            raise SecurityError("运营角色缺少 operator_name 令牌字段，禁止查询。")
        store_ids = self.store_directory.get_store_ids(user.operator_name)
        if not store_ids:
            raise SecurityError(f"运营角色 {user.operator_name} 没有绑定任何店铺，禁止查询。")
        return store_ids

    @staticmethod
    def _table_alias(table: exp.Table) -> str:
        return table.alias or table.name

    def _sources_of_select(self, select: exp.Select) -> list[exp.Expression]:
        """取该 SELECT 直接引用的数据源（FROM/JOIN，不含下钻子查询内部）。"""
        sources: list[exp.Expression] = []
        from_clause = select.args.get("from_")
        if from_clause is not None and from_clause.this is not None:
            sources.append(from_clause.this)
        for join in select.args.get("joins") or []:
            if join.this is not None:
                sources.append(join.this)
        return sources

    def _expand_stars(self, tree: exp.Expression, cte_names: set[str]) -> None:
        """ops 角色下，把涉及敏感列表的 SELECT * / t.* 展开成非敏感列，而不是拒绝。"""
        if not self.restricted_columns:
            return
        alias_to_table: dict[str, str] = {}
        for table in tree.find_all(exp.Table):
            if table.name.lower() in cte_names:
                continue
            alias_to_table[self._table_alias(table).lower()] = table.name.lower()

        restricted_tables = set(self.restricted_columns_by_table)

        for select in list(tree.find_all(exp.Select)):
            sources = self._sources_of_select(select)
            source_aliases = {
                self._table_alias(source).lower()
                for source in sources
                if isinstance(source, exp.Table)
            }
            source_real_tables = {
                source.name.lower()
                for source in sources
                if isinstance(source, exp.Table) and source.name.lower() not in cte_names
            }
            touches_restricted = bool(source_real_tables & restricted_tables)

            new_expressions: list[exp.Expression] = []
            for item in select.expressions:
                if isinstance(item, exp.Star):
                    if not touches_restricted:
                        new_expressions.append(item)
                        continue
                    for source in sources:
                        new_expressions.extend(
                            self._expand_source_star(source, alias_to_table, cte_names)
                        )
                    continue
                if isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
                    qualifier = (item.table or "").lower()
                    target_table = alias_to_table.get(qualifier)
                    if target_table not in restricted_tables:
                        new_expressions.append(item)
                        continue
                    columns = self._columns_without_restricted(target_table)
                    alias = item.table
                    for column_name in columns:
                        new_expressions.append(
                            exp.column(column_name, table=alias, quoted=False)
                        )
                    continue
                new_expressions.append(item)
            select.set("expressions", new_expressions)

    def _expand_source_star(
        self,
        source: exp.Expression,
        alias_to_table: dict[str, str],
        cte_names: set[str],
    ) -> list[exp.Expression]:
        """把一个数据源在 SELECT * 中的份额展开：敏感表出非敏感列，其余源保留 t.*。"""
        if isinstance(source, exp.Table):
            table_name = source.name.lower()
            if table_name in cte_names or table_name not in self.restricted_columns_by_table:
                return [self._qualified_star(self._table_alias(source))]
            alias = self._table_alias(source)
            return [
                exp.column(column_name, table=alias, quoted=False)
                for column_name in self._columns_without_restricted(table_name)
            ]
        # 派生表 / 子查询等无法枚举列的源：保留 <别名>.*
        alias = getattr(source, "alias", "") or ""
        return [self._qualified_star(alias)]

    @staticmethod
    def _qualified_star(alias: str) -> exp.Expression:
        if not alias:
            return exp.Star()
        return exp.Column(this=exp.Star(), table=exp.to_identifier(alias))

    def _columns_without_restricted(self, table_name: str) -> list[str]:
        columns = self.table_columns.get(table_name.lower())
        if not columns:
            raise SecurityError(
                f"缺少表 {table_name} 的列元数据，无法安全展开 SELECT *，请显式列出列。"
            )
        restricted = self.restricted_columns_by_table.get(table_name.lower(), set())
        return [column for column in columns if column.lower() not in restricted]

    def _wrap_row_level_tables(
        self,
        tree: exp.Expression,
        cte_names: set[str],
        store_ids: list[str],
    ) -> None:
        """把每个带 store_id 的真实表替换成 (SELECT * FROM 表 WHERE store_id IN (...)) AS 原别名。

        在 AST 层替换（而不是在 SQL 末尾拼 WHERE），子查询/CTE/UNION/JOIN/逗号连接/自连接
        里出现的每一张表都会被分别包裹，无法绕过。
        """
        in_list = ", ".join(f"'{self._escape_sql_literal(store)}'" for store in store_ids)
        for table in list(tree.find_all(exp.Table)):
            if table.name.lower() not in self.row_level_tables:
                continue
            if table.name.lower() in cte_names:
                continue
            alias = self._table_alias(table)
            bare_table = exp.Table(this=table.this.copy())
            inner_select = (
                exp.select("*")
                .from_(bare_table, copy=False)
                .where(f"{self.row_level_column} IN ({in_list})", copy=False)
            )
            subquery = exp.Subquery(
                this=inner_select,
                alias=exp.TableAlias(this=exp.to_identifier(alias)),
            )
            table.replace(subquery)

    # -- 行数上限 -----------------------------------------------------------

    def _enforce_limit(self, tree: exp.Expression) -> None:
        target = tree
        while isinstance(target, (exp.Paren, exp.Subquery)):
            target = target.this
        if not isinstance(target, (exp.Select, exp.Union, exp.Except, exp.Intersect)):
            return
        limit_node = target.args.get("limit")
        if limit_node is None:
            target.set("limit", exp.Limit(expression=exp.Literal.number(self.max_rows)))
            return
        limit_value = limit_node.expression
        if isinstance(limit_value, exp.Literal) and not limit_value.is_string:
            try:
                current = int(limit_value.this)
            except (TypeError, ValueError):
                current = -1
            if 0 <= current <= self.max_rows:
                return
        # LIMIT 超上限、或无法静态求值：压到上限
        limit_node.set("expression", exp.Literal.number(self.max_rows))

    @staticmethod
    def _escape_sql_literal(value: str) -> str:
        return str(value).replace("\\", "\\\\").replace("'", "''")
