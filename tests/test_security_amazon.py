"""亚马逊业务 SQL 安全验收测试。

覆盖任务验收标准 1-24：
- 1-14 必须拦截（SecurityError / 401）
- 15-18 必须放行（不能误杀）
- 19-22 必须改写（LIMIT / 行级权限包裹）
- 23 端到端：ops 只能看到自己店铺
- 24 令牌校验
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from security import (  # noqa: E402
    QuerySecurityManager,
    SecurityError,
    StoreDirectory,
    UserContext,
    load_security_policy,
    make_auth_token,
    resolve_request_user,
    verify_auth_token,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROW_LEVEL_TABLES = {
    "dim_stores",
    "dim_products",
    "amz_order_items",
    "amz_ads_daily",
    "amz_traffic_daily",
    "amz_fba_inventory",
    "amz_settlement",
    "amz_returns",
}

ADMIN = UserContext(user_id="u_admin", role="admin")
# 运营A：US01、CA01；运营B：UK01、DE01（与 mock_dataset/csv/dim_stores.csv 一致）
OPS_A = UserContext(user_id="u_ops_a", role="ops", operator_name="运营A")
OPS_B = UserContext(user_id="u_ops_b", role="ops", operator_name="运营B")


@pytest.fixture()
def store_directory() -> StoreDirectory:
    directory = StoreDirectory()
    directory.prime({"运营A": ["US01", "CA01"], "运营B": ["UK01", "DE01"]})
    return directory


@pytest.fixture()
def manager(store_directory: StoreDirectory) -> QuerySecurityManager:
    policy = load_security_policy()
    policy["max_rows"] = 1000  # 验收标准固定为默认上限
    return QuerySecurityManager(store_directory=store_directory, policy=policy)


# ---------------------------------------------------------------------------
# 验收 1-11：攻击语句必须拦截
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE amz_order_items",  # 1
        "SELECT 1; DROP TABLE x",  # 2
        "SELECT * FROM amz_order_items FOR UPDATE",  # 3
        "SELECT * FROM amz_order_items INTO OUTFILE '/tmp/a.txt'",  # 4
        "SELECT LOAD_FILE('/etc/passwd')",  # 5
        "SELECT SLEEP(3600)",  # 6
        "SELECT BENCHMARK(100000000, MD5('a'))",  # 7
        "SELECT user, authentication_string FROM mysql.user",  # 8
        "SELECT table_name FROM information_schema.tables",  # 9
        "SELECT * FROM other_db.salary",  # 10
        "SELECT * FROM some_unknown_table",  # 11
    ],
)
def test_attack_statements_are_blocked(manager: QuerySecurityManager, sql: str):
    with pytest.raises(SecurityError):
        manager.secure_sql(sql, ADMIN)


def test_multi_statement_blocked_with_reason(manager: QuerySecurityManager):
    with pytest.raises(SecurityError, match="多语句"):
        manager.secure_sql("SELECT 1; DROP TABLE x", ADMIN)


def test_unknown_table_blocked_with_reason(manager: QuerySecurityManager):
    with pytest.raises(SecurityError, match="some_unknown_table"):
        manager.secure_sql("SELECT * FROM some_unknown_table", ADMIN)


def test_db_prefixed_table_blocked_even_for_whitelisted_name(manager: QuerySecurityManager):
    with pytest.raises(SecurityError, match="库名前缀"):
        manager.secure_sql("SELECT * FROM chatbi_demo.amz_order_items", ADMIN)


# ---------------------------------------------------------------------------
# 验收 12：未知角色
# ---------------------------------------------------------------------------


def test_unknown_role_rejected(manager: QuerySecurityManager):
    with pytest.raises(SecurityError, match="未知角色"):
        manager.secure_sql("SELECT * FROM amz_order_items", UserContext(user_id="x", role="hacker"))


def test_declared_but_unconfigured_role_rejected(manager: QuerySecurityManager):
    with pytest.raises(SecurityError, match="未配置的角色"):
        manager.secure_sql("SELECT * FROM amz_order_items", UserContext(user_id="x", role="finance"))


# ---------------------------------------------------------------------------
# 验收 13：ops 引用敏感列
# ---------------------------------------------------------------------------


def test_ops_cannot_reference_restricted_column(manager: QuerySecurityManager):
    with pytest.raises(SecurityError, match="unit_cost"):
        manager.secure_sql("SELECT SUM(unit_cost) FROM dim_products", OPS_B)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT unit_cost FROM dim_products",
        "SELECT p.head_freight_cost FROM dim_products p",
        "SELECT 1 FROM dim_products WHERE unit_cost > 0",
        "SELECT 1 FROM dim_products ORDER BY head_freight_cost",
        "SELECT 1 FROM (SELECT unit_cost FROM dim_products) t",
    ],
)
def test_restricted_column_blocked_in_any_clause(manager: QuerySecurityManager, sql: str):
    with pytest.raises(SecurityError):
        manager.secure_sql(sql, OPS_A)


def test_admin_can_reference_restricted_columns(manager: QuerySecurityManager):
    secured = manager.secure_sql("SELECT SUM(unit_cost) FROM dim_products", ADMIN)
    assert "unit_cost" in secured


def test_ops_select_star_expands_to_non_sensitive_columns(manager: QuerySecurityManager):
    secured = manager.secure_sql("SELECT * FROM dim_products", OPS_A).lower()
    assert "unit_cost" not in secured
    assert "head_freight_cost" not in secured
    assert "product_name" in secured
    assert "store_id" in secured


# ---------------------------------------------------------------------------
# 验收 14：无法解析的 SQL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT FROM WHERE",
        "NOT SQL AT ALL",
        "SELECT * FROM amz_order_items WHERE",
    ],
)
def test_unparseable_sql_blocked(manager: QuerySecurityManager, sql: str):
    with pytest.raises(SecurityError):
        manager.secure_sql(sql, ADMIN)


# ---------------------------------------------------------------------------
# 验收 15-17：不能误杀
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT REPLACE(campaign_name, '_', '-') FROM amz_ads_daily LIMIT 5",  # 15
        "SELECT * FROM amz_ads_daily WHERE campaign_name LIKE '%;%' LIMIT 5",  # 16
        "SELECT * FROM amz_returns WHERE reason = 'CREATE' LIMIT 5",  # 17
    ],
)
def test_normal_queries_are_allowed(manager: QuerySecurityManager, sql: str):
    secured = manager.secure_sql(sql, ADMIN)
    assert secured
    sqlglot.parse_one(secured, dialect="mysql")


def test_replace_function_allowed_but_replace_into_rejected(manager: QuerySecurityManager):
    manager.secure_sql("SELECT REPLACE(campaign_name, '-', '_') FROM amz_ads_daily LIMIT 5", ADMIN)
    with pytest.raises(SecurityError):
        manager.secure_sql("REPLACE INTO amz_order_items VALUES (1)", ADMIN)


def test_string_literals_do_not_trigger_blocks(manager: QuerySecurityManager):
    """字符串里的分号 / 关键字 / 函数名都不应触发拦截（AST 判断，不做原文关键词搜索）。"""
    secured = manager.secure_sql(
        "SELECT * FROM amz_returns WHERE reason = 'CREATE; DROP TABLE x; SLEEP(1)' LIMIT 5",
        ADMIN,
    )
    assert "SLEEP(1)" in secured  # 字符串原样保留


# ---------------------------------------------------------------------------
# 验收 18：knowledge/test_cases.json 的 27 条 expected_sql 全部通过 admin 校验
# ---------------------------------------------------------------------------


def test_all_expected_sql_pass_admin_validation(manager: QuerySecurityManager):
    cases = json.loads((PROJECT_ROOT / "knowledge" / "test_cases.json").read_text(encoding="utf-8"))
    assert len(cases) == 27
    for case in cases:
        secured = manager.secure_sql(case["expected_sql"], ADMIN)
        # 放行且改写后仍是合法 SQL；LIMIT 之外不应有语义变化
        assert _strip_top_limit(secured) == _strip_top_limit(case["expected_sql"]), case.get("id")


def _strip_top_limit(sql: str) -> str:
    tree = sqlglot.parse_one(sql, dialect="mysql")
    node = tree
    while isinstance(node, (exp.Paren, exp.Subquery)):
        node = node.this
    if node.args.get("limit"):
        node.set("limit", None)
    return node.sql(dialect="mysql")


# ---------------------------------------------------------------------------
# 验收 19-20：LIMIT 强制
# ---------------------------------------------------------------------------


def test_limit_added_when_missing(manager: QuerySecurityManager):
    secured = manager.secure_sql("SELECT * FROM amz_settlement", ADMIN)
    assert "LIMIT 1000" in secured.upper()


def test_limit_clamped_to_max_rows(manager: QuerySecurityManager):
    secured = manager.secure_sql("SELECT * FROM amz_settlement LIMIT 5000000", ADMIN)
    assert "LIMIT 1000" in secured.upper()
    assert "5000000" not in secured


def test_limit_unaffected_when_already_small(manager: QuerySecurityManager):
    secured = manager.secure_sql("SELECT * FROM amz_settlement LIMIT 5", ADMIN)
    assert "LIMIT 5" in secured.upper()


def test_union_gets_single_outer_limit(manager: QuerySecurityManager):
    secured = manager.secure_sql(
        "SELECT store_id FROM amz_order_items UNION SELECT store_id FROM amz_settlement",
        ADMIN,
    )
    assert secured.upper().rstrip().endswith("LIMIT 1000")


# ---------------------------------------------------------------------------
# 验收 21-22：ops 行级权限 AST 包裹
# ---------------------------------------------------------------------------


def test_ops_query_wrapped_with_store_filter(manager: QuerySecurityManager):
    secured = manager.secure_sql("SELECT SUM(item_amount) FROM amz_order_items", OPS_A)
    tree = sqlglot.parse_one(secured, dialect="mysql")
    wrapped, total = _count_store_filtered_tables(tree)
    assert total == 1
    assert wrapped == total
    assert re.search(r"store_id IN \('US01',\s*'CA01'\)", secured)


@pytest.mark.parametrize(
    "sql,expected_tables",
    [
        # 反引号
        ("SELECT item_amount FROM `amz_order_items`", 1),
        # 逗号连接
        ("SELECT o1.item_amount FROM amz_order_items o1, dim_stores o2", 2),
        # UNION 第二段
        ("SELECT store_id FROM dim_stores UNION SELECT store_id FROM amz_order_items", 2),
        # 子查询
        ("SELECT * FROM (SELECT item_amount FROM amz_order_items) t", 1),
        # CTE
        ("WITH c AS (SELECT item_amount FROM amz_order_items) SELECT * FROM c", 1),
        # 自连接
        (
            "SELECT a.item_amount FROM amz_order_items a JOIN amz_order_items b ON a.store_id = b.store_id",
            2,
        ),
    ],
)
def test_ops_wraps_every_table_occurrence(
    manager: QuerySecurityManager,
    sql: str,
    expected_tables: int,
):
    secured = manager.secure_sql(sql, OPS_A)
    tree = sqlglot.parse_one(secured, dialect="mysql")
    wrapped, total = _count_store_filtered_tables(tree)
    assert total == expected_tables
    assert wrapped == total, f"存在未包裹的表: {secured}"


def test_exchange_rates_not_wrapped(manager: QuerySecurityManager):
    secured = manager.secure_sql("SELECT * FROM exchange_rates LIMIT 5", OPS_A)
    assert "store_id" not in secured


def test_ops_with_empty_store_list_rejected(store_directory: StoreDirectory):
    store_directory.prime({"运营C": []})
    manager = QuerySecurityManager(store_directory=store_directory)
    with pytest.raises(SecurityError, match="没有绑定任何店铺"):
        manager.secure_sql("SELECT * FROM amz_order_items", UserContext(user_id="x", role="ops", operator_name="运营C"))


def test_ops_without_operator_name_rejected(manager: QuerySecurityManager):
    with pytest.raises(SecurityError, match="operator_name"):
        manager.secure_sql("SELECT * FROM amz_order_items", UserContext(user_id="x", role="ops"))


def _count_store_filtered_tables(tree: exp.Expression) -> tuple[int, int]:
    """统计（带 store_id 过滤的表数, 总表数）。"""
    total = 0
    wrapped = 0
    for table in tree.find_all(exp.Table):
        if table.name.lower() not in ROW_LEVEL_TABLES:
            continue
        total += 1
        select = None
        node = table.parent
        while node is not None:
            if isinstance(node, exp.Select):
                select = node
                break
            node = node.parent
        if select is not None and _has_store_filter(select):
            wrapped += 1
    return wrapped, total


def _has_store_filter(select: exp.Select) -> bool:
    where = select.args.get("where")
    if where is None:
        return False
    for node in where.find_all(exp.In):
        if isinstance(node.this, exp.Column) and node.this.name.lower() == "store_id":
            if node.expressions and all(isinstance(e, exp.Literal) for e in node.expressions):
                return True
    return False


# ---------------------------------------------------------------------------
# 验收 23：端到端，ops 结果只包含自己的店铺（sqlite 加载 mock 数据）
# ---------------------------------------------------------------------------


def _load_mock_sqlite() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    csv_dir = PROJECT_ROOT / "mock_dataset" / "csv"
    for csv_file in sorted(csv_dir.glob("*.csv")):
        table = csv_file.stem
        with open(csv_file, encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            headers = next(reader)
            columns = ", ".join(f'"{col}"' for col in headers)
            conn.execute(f'CREATE TABLE "{table}" ({columns})')
            rows = [tuple(row) for row in reader]
            placeholders = ", ".join("?" for _ in headers)
            conn.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})', rows)
    conn.commit()
    return conn


def _store_ids_from_csv() -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    with open(PROJECT_ROOT / "mock_dataset" / "csv" / "dim_stores.csv", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            mapping.setdefault(row["owner"], []).append(row["store_id"])
    return mapping


def test_ops_end_to_end_sees_only_own_stores():
    directory = StoreDirectory()
    directory.prime(_store_ids_from_csv())
    manager = QuerySecurityManager(store_directory=directory)
    conn = _load_mock_sqlite()

    cases = [
        (OPS_A, {"US01", "CA01"}),
        (OPS_B, {"UK01", "DE01"}),
    ]
    probe_sqls = [
        "SELECT store_id FROM amz_order_items",
        "SELECT DISTINCT store_id FROM dim_products",
        "SELECT store_id FROM amz_settlement",
        "SELECT store_id FROM amz_ads_daily",
        "SELECT s.store_id FROM dim_stores s",
    ]

    for user, own_stores in cases:
        for sql in probe_sqls:
            secured = manager.secure_sql(sql, user)
            rows = conn.execute(secured).fetchall()
            seen = {row[0] for row in rows}
            assert seen, f"{user.operator_name} 查询无结果: {sql}"
            assert seen <= own_stores, (
                f"{user.operator_name} 越权看到店铺 {seen - own_stores}: {sql}"
            )

    # admin 不受限制：能看到全部店铺
    admin_rows = conn.execute(
        manager.secure_sql("SELECT DISTINCT store_id FROM amz_order_items", ADMIN)
    ).fetchall()
    assert {row[0] for row in admin_rows} == {"US01", "CA01", "UK01", "DE01"}
    conn.close()


# ---------------------------------------------------------------------------
# 验收 24：令牌
# ---------------------------------------------------------------------------

TEST_SECRET = "test-secret-for-acceptance"


def test_token_bad_signature_returns_error():
    token = make_auth_token("u1", "admin", secret=TEST_SECRET)
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    with pytest.raises(SecurityError):
        verify_auth_token(tampered, secret=TEST_SECRET)


def test_token_expired_returns_error():
    token = make_auth_token("u1", "admin", secret=TEST_SECRET, expires_in=10, now=time.time() - 100)
    with pytest.raises(SecurityError, match="过期"):
        verify_auth_token(token, secret=TEST_SECRET)


def test_token_missing_role_returns_error():
    payload_raw = json.dumps(
        {"user_id": "u1", "exp": int(time.time()) + 600},
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(TEST_SECRET.encode("utf-8"), payload_raw, hashlib.sha256).digest()
    token = (
        base64.urlsafe_b64encode(payload_raw).rstrip(b"=").decode()
        + "."
        + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    )
    with pytest.raises(SecurityError, match="role"):
        verify_auth_token(token, secret=TEST_SECRET)


def test_token_valid_roundtrip():
    token = make_auth_token(
        "u_ops_a", "ops", secret=TEST_SECRET, operator_name="运营A", expires_in=600
    )
    context = verify_auth_token(token, secret=TEST_SECRET)
    assert context.user_id == "u_ops_a"
    assert context.role == "ops"
    assert context.operator_name == "运营A"


def test_no_token_rejected_by_default():
    with pytest.raises(SecurityError):
        resolve_request_user(None, allow_demo_admin=False)


def test_demo_admin_only_when_enabled():
    context = resolve_request_user(None, allow_demo_admin=True)
    assert context.role == "admin"
    assert context.user_id == "demo_admin"


def test_api_rejects_bad_or_missing_token(monkeypatch):
    """签名错误、过期、缺少 role 都返回 401；请求体 user_role=admin 不起作用。"""
    monkeypatch.setenv("AUTH_SECRET", TEST_SECRET)
    monkeypatch.setenv("ALLOW_DEMO_ADMIN", "false")
    from fastapi.testclient import TestClient

    import api_service

    client = TestClient(api_service.app)

    # 无令牌
    response = client.post("/api/v1/query", json={"question": "上月销售额"})
    assert response.status_code == 401

    # 签名错误
    bad = make_auth_token("u1", "admin", secret="other-secret")
    response = client.post(
        "/api/v1/query",
        json={"question": "上月销售额"},
        headers={"Authorization": f"Bearer {bad}"},
    )
    assert response.status_code == 401

    # 过期
    expired = make_auth_token("u1", "admin", secret=TEST_SECRET, expires_in=-10)
    response = client.post(
        "/api/v1/query",
        json={"question": "上月销售额"},
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert response.status_code == 401

    # 缺少 role
    payload_raw = json.dumps({"user_id": "u1", "exp": int(time.time()) + 600}).encode("utf-8")
    sig = hmac.new(TEST_SECRET.encode("utf-8"), payload_raw, hashlib.sha256).digest()
    no_role = (
        base64.urlsafe_b64encode(payload_raw).rstrip(b"=").decode()
        + "."
        + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    )
    response = client.post(
        "/api/v1/query",
        json={"question": "上月销售额"},
        headers={"Authorization": f"Bearer {no_role}"},
    )
    assert response.status_code == 401


def test_api_ignores_payload_user_role(monkeypatch):
    """请求体里写 user_role=admin 不起作用：身份只来自令牌。"""
    monkeypatch.setenv("AUTH_SECRET", TEST_SECRET)
    monkeypatch.setenv("ALLOW_DEMO_ADMIN", "false")
    from fastapi.testclient import TestClient

    import api_service

    captured: dict = {}

    def fake_run(user_question, **kwargs):
        captured["user"] = kwargs.get("security_context")
        return {
            "success": True,
            "sql": "SELECT 1",
            "columns": ["x"],
            "results": [(1,)],
            "formatted": "ok",
            "metadata": {},
        }

    monkeypatch.setattr(api_service.system, "run", fake_run)
    client = TestClient(api_service.app)

    token = make_auth_token("u_ops_a", "ops", secret=TEST_SECRET, operator_name="运营A")
    response = client.post(
        "/api/v1/query",
        json={"question": "上月销售额", "user_role": "admin", "user_region": "US"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert captured["user"].role == "ops"
    assert captured["user"].user_id == "u_ops_a"
    assert captured["user"].operator_name == "运营A"

    # 无令牌时即使自称 admin 也拒绝
    response = client.post(
        "/api/v1/query",
        json={"question": "上月销售额", "user_role": "admin"},
    )
    assert response.status_code == 401
