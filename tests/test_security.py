from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import ChatBISystem
from database import DatabaseClient
from security import QuerySecurityManager, SecurityError, StoreDirectory, UserContext


class FakeCursor:
    def __init__(self):
        self.description = [
            ("product_name",),
            ("customer_phone",),
            ("customer_email",),
        ]
        self.executed_sql = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str):
        self.executed_sql = sql

    def fetchmany(self, size: int):
        return [("折叠收纳箱", "13800000000", "a@b.com")]

    def fetchall(self):
        return [("折叠收纳箱", "13800000000", "a@b.com")]


class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def close(self):
        self.closed = True


def _manager() -> QuerySecurityManager:
    directory = StoreDirectory()
    directory.prime({"运营A": ["US01", "CA01"], "运营B": ["UK01", "DE01"]})
    return QuerySecurityManager(store_directory=directory)


def test_security_manager_rejects_non_select_sql():
    manager = _manager()
    user = UserContext(user_id="u_admin", role="admin")

    with pytest.raises(SecurityError, match="只允许执行 SELECT 查询"):
        manager.secure_sql("DELETE FROM amz_order_items", user)


def test_security_manager_rejects_unknown_role_instead_of_admin_fallback():
    manager = _manager()
    user = UserContext(user_id="u_hacker", role="hacker")

    with pytest.raises(SecurityError, match="未知角色"):
        manager.secure_sql("SELECT * FROM amz_order_items", user)


def test_security_manager_row_level_filter_for_ops_role():
    manager = _manager()
    user = UserContext(user_id="u_ops_a", role="ops", operator_name="运营A")

    secured_sql = manager.secure_sql(
        """
        SELECT s.marketplace, SUM(o.item_amount) AS revenue
        FROM amz_order_items o
        JOIN dim_stores s ON o.store_id = s.store_id
        GROUP BY s.marketplace
        """,
        user,
    )

    # 每张带 store_id 的表都被包成带过滤的子查询，而不是在 SQL 末尾拼 WHERE
    assert secured_sql.count("store_id IN ('US01', 'CA01')") == 2
    assert secured_sql.upper().count(" WHERE ") == 2  # 仅子查询内部各一个 WHERE


def test_security_manager_masks_sensitive_output_columns():
    manager = _manager()
    user = UserContext(user_id="u_ops_a", role="ops", operator_name="运营A")

    masked_columns, masked_rows = manager.mask_result(
        columns=["product_name", "customer_phone", "customer_email"],
        rows=[("折叠收纳箱", "13800000000", "a@b.com")],
        user=user,
    )

    assert masked_columns == ["product_name", "customer_phone", "customer_email"]
    assert masked_rows == [("折叠收纳箱", "***", "***")]


def test_admin_results_are_not_masked():
    manager = _manager()
    _, rows = manager.mask_result(
        columns=["customer_phone"],
        rows=[("13800000000",)],
        user=UserContext(user_id="u_admin", role="admin"),
    )
    assert rows == [("13800000000",)]


def test_database_client_applies_security_before_query_execution():
    fake_connection = FakeConnection()
    manager = _manager()
    client = DatabaseClient(
        connection_factory=lambda: fake_connection,
        security_manager=manager,
    )
    user = UserContext(user_id="u_ops_a", role="ops", operator_name="运营A")

    columns, rows = client.execute(
        """
        SELECT p.product_name, p.customer_phone, p.customer_email
        FROM dim_products p
        JOIN dim_stores s ON p.store_id = s.store_id
        """,
        user=user,
    )

    executed = fake_connection.cursor_instance.executed_sql
    assert "store_id IN ('US01', 'CA01')" in executed
    assert columns == ["product_name", "customer_phone", "customer_email"]
    assert rows == [("折叠收纳箱", "***", "***")]
    assert fake_connection.closed is True


def test_chatbi_system_returns_security_error_type():
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
            raise SecurityError(f"权限不足：{user.role}")

    system = ChatBISystem()
    system.parser = FakeParser()
    system.llm = FakeLLM()
    system.db = FakeDB()

    result = system.run(
        "查看订单明细",
        security_context=UserContext(user_id="u_ops_b", role="ops", operator_name="运营B"),
    )

    assert result["success"] is False
    assert result["error_type"] == "security"
    assert "ops" in result["error"]
