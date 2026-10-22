"""执行报错 → 带错误信息让 LLM 重写 → 有限次重试 的验收测试（假 LLM + 假数据库）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import APP_CONFIG  # noqa: E402
from database import QueryExecutionError  # noqa: E402
from main import ChatBISystem  # noqa: E402
from result_formatter import ResultFormatter  # noqa: E402
from security import SecurityError  # noqa: E402

FEATURES_OFF = {
    "use_few_shot": False,
    "use_rules": False,
    "use_guards": False,
    "use_indicator_knowledge": False,
    "use_schema_linking": False,
    "use_indicator_rag": False,
}

FIXED_SQL = "SELECT item_amount FROM amz_order_items"
BROKEN_SQL = "SELECT bad_col FROM amz_order_items"


@pytest.fixture(autouse=True)
def _clean_repair_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SQL_REPAIR_MAX_RETRIES", raising=False)
    yield


class FakeParser:
    def parse(self, question: str) -> str:
        return question

    def validate(self, parsed: str) -> bool:
        return bool(str(parsed or "").strip())


class FakeIndicatorKnowledge:
    def get_indicator_context(self, question: str) -> dict:
        return {"detected_indicators": [], "indicator_block": ""}


class FakeLLM:
    """按脚本返回 SQL，记录每次调用的 prompt。"""

    def __init__(self, sql_script: list[str]):
        self.sql_script = list(sql_script)
        self.prompts: list[str] = []

    def _next(self, system_msg: str, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.sql_script:
            raise AssertionError("FakeLLM 脚本已耗尽")
        return self.sql_script.pop(0)

    def generate_sql(self, system_msg: str, prompt: str) -> str:
        return self._next(system_msg, prompt)

    def generate_sql_stream(self, system_msg: str, prompt: str):
        yield self._next(system_msg, prompt)


class FakeDB:
    """按脚本执行：("ok", columns, rows) 或 ("raise", exception)。"""

    def __init__(self, script: list[tuple]):
        self.script = list(script)
        self.calls: list[str] = []
        self.last_query_info: dict = {}

    def execute(self, sql: str, user=None, **kwargs):
        self.calls.append(sql)
        if not self.script:
            raise AssertionError("FakeDB 脚本已耗尽")
        entry = self.script.pop(0)
        if entry[0] == "raise":
            raise entry[1]
        _, columns, rows = entry
        self.last_query_info = {
            "duration_ms": 12.0,
            "slow_query": False,
            "explain_plan": [],
            "truncated": False,
        }
        return columns, rows


def _syntax_error(code: int = 1054) -> QueryExecutionError:
    return QueryExecutionError(
        "sql_syntax",
        "SQL 语法错误，请检查字段、聚合和别名是否正确",
        {"error_code": code, "raw_error": f"Unknown column 'bad_col' in 'field list' ({code})"},
    )


def _make_system(llm: FakeLLM, db: FakeDB, retries: int | None = None) -> ChatBISystem:
    runtime = SimpleNamespace(
        source_id="test_src",
        parser=FakeParser(),
        llm=llm,
        db=db,
        formatter=ResultFormatter(),
        indicator_knowledge=FakeIndicatorKnowledge(),
    )
    app_config = dict(APP_CONFIG)
    if retries is not None:
        app_config["sql_repair_max_retries"] = retries
    return ChatBISystem(runtime=runtime, app_config=app_config)


def _run(system: ChatBISystem, question: str = "各站点销售额") -> dict:
    return system.run(question, **FEATURES_OFF)


# ---------------------------------------------------------------------------
# 1. 第一次执行 sql_syntax 失败（1054），重写后成功
# ---------------------------------------------------------------------------


def test_repair_then_success():
    llm = FakeLLM([BROKEN_SQL, FIXED_SQL])
    db = FakeDB([("raise", _syntax_error()), ("ok", ["item_amount"], [(100,)])])
    system = _make_system(llm, db)

    result = _run(system)

    assert result["success"] is True
    assert result["sql"] == FIXED_SQL
    assert result["metadata"]["sql_repair_attempts"] == 1
    assert result["metadata"]["sql_repair_errors"] == ["sql_syntax"]
    assert len(llm.prompts) == 2
    assert len(db.calls) == 2
    # 第二次 prompt 包含上一次 SQL 与报错文本
    repair_prompt = llm.prompts[1]
    assert "【SQL 执行失败】" in repair_prompt
    assert BROKEN_SQL in repair_prompt
    assert "Unknown column 'bad_col'" in repair_prompt
    assert "请只修正导致报错的问题" in repair_prompt


# ---------------------------------------------------------------------------
# 2. 连续失败：1 次原始 + 2 次重试后停止
# ---------------------------------------------------------------------------


def test_stops_after_max_retries():
    llm = FakeLLM([BROKEN_SQL, BROKEN_SQL, BROKEN_SQL, FIXED_SQL])
    db = FakeDB([("raise", _syntax_error())] * 3)
    system = _make_system(llm, db)

    result = _run(system)

    assert result["success"] is False
    assert result["error_type"] == "database_sql_syntax"
    assert result["metadata"]["sql_repair_attempts"] == 2
    assert result["metadata"]["sql_repair_errors"] == ["sql_syntax"] * 3
    assert len(db.calls) == 3
    assert len(llm.prompts) == 3  # 1 次生成 + 2 次重写，第 4 条脚本不应被消费
    assert result["sql"] == BROKEN_SQL


# ---------------------------------------------------------------------------
# 3/4. connection_error / query_timeout 一律不重试
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_type,error_code",
    [("connection_error", 2003), ("query_timeout", 2013)],
)
def test_non_repairable_db_errors_not_retried(error_type, error_code):
    llm = FakeLLM([BROKEN_SQL, FIXED_SQL])
    error = QueryExecutionError(
        error_type,
        "数据库故障",
        {"error_code": error_code, "raw_error": "boom"},
    )
    db = FakeDB([("raise", error)])
    system = _make_system(llm, db)

    result = _run(system)

    assert result["success"] is False
    assert result["error_type"] == f"database_{error_type}"
    assert result["metadata"]["sql_repair_attempts"] == 0
    assert len(llm.prompts) == 1
    assert len(db.calls) == 1


# ---------------------------------------------------------------------------
# 5. SecurityError 不重试（避免用安全拒绝探测）
# ---------------------------------------------------------------------------


def test_security_error_not_retried():
    llm = FakeLLM([BROKEN_SQL, FIXED_SQL])
    db = FakeDB([("raise", SecurityError("禁止使用函数: SLEEP"))])
    system = _make_system(llm, db)

    result = _run(system)

    assert result["success"] is False
    assert result["error_type"] == "security"
    assert result["metadata"]["sql_repair_attempts"] == 0
    assert result["metadata"]["sql_repair_errors"] == ["security"]
    assert len(llm.prompts) == 1
    assert len(db.calls) == 1


def test_security_error_on_retry_stops():
    """重写出的新 SQL 触发安全拒绝时也不再重试。"""
    llm = FakeLLM([BROKEN_SQL, "SELECT SLEEP(3)"])
    db = FakeDB([("raise", _syntax_error()), ("raise", SecurityError("禁止使用函数: SLEEP"))])
    system = _make_system(llm, db)

    result = _run(system)

    assert result["success"] is False
    assert result["error_type"] == "security"
    assert result["metadata"]["sql_repair_attempts"] == 1
    assert len(db.calls) == 2


# ---------------------------------------------------------------------------
# 6. SQL_REPAIR_MAX_RETRIES=0：不重试，行为与改动前一致
# ---------------------------------------------------------------------------


def test_repair_disabled_when_max_retries_zero(monkeypatch):
    monkeypatch.setenv("SQL_REPAIR_MAX_RETRIES", "0")
    llm = FakeLLM([BROKEN_SQL, FIXED_SQL])
    db = FakeDB([("raise", _syntax_error()), ("ok", ["item_amount"], [(1,)])])
    system = _make_system(llm, db)

    result = _run(system)

    assert result["success"] is False
    assert result["error_type"] == "database_sql_syntax"
    assert result["metadata"]["sql_repair_attempts"] == 0
    assert len(llm.prompts) == 1
    assert len(db.calls) == 1


# ---------------------------------------------------------------------------
# 7. 重写后仍有时间边界问题 → metadata["time_bounds_issue"] 记录
# ---------------------------------------------------------------------------


def test_time_bounds_issue_recorded_after_repair():
    good_time_sql = (
        "SELECT bad_col FROM amz_order_items "
        "WHERE purchase_date >= DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 MONTH), '%Y-%m-01') "
        "AND purchase_date < DATE_FORMAT(CURDATE(), '%Y-%m-01')"
    )
    still_bad_time_sql = (
        "SELECT item_amount FROM amz_order_items "
        "WHERE purchase_date >= DATE_SUB(CURDATE(), INTERVAL 1 MONTH)"
    )
    llm = FakeLLM([good_time_sql, still_bad_time_sql])
    db = FakeDB([("raise", _syntax_error()), ("ok", ["item_amount"], [(100,)])])
    system = _make_system(llm, db)

    result = _run(system, question="上个月卖了多少钱?")

    assert result["success"] is True
    assert result["metadata"]["sql_repair_attempts"] == 1
    assert result["metadata"]["time_bounds_issue"]  # 重写引入的时间问题仍被记录


# ---------------------------------------------------------------------------
# 8. run_stream 路径：sql_rewrite 事件 + 事件序列正确
# ---------------------------------------------------------------------------


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for part in text.split("\n\n"):
        if not part.strip():
            continue
        event_type, data = "", {}
        for line in part.splitlines():
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if event_type:
            events.append((event_type, data))
    return events


def test_run_stream_emits_sql_rewrite_event():
    llm = FakeLLM([BROKEN_SQL, FIXED_SQL])
    db = FakeDB([("raise", _syntax_error()), ("ok", ["item_amount"], [(100,)])])
    system = _make_system(llm, db)

    events = _parse_sse(
        "".join(system.run_stream("各站点销售额", **FEATURES_OFF))
    )
    kinds = [kind for kind, _ in events]

    assert kinds[0].startswith("sql_chunk") or kinds[0] == "sql_chunk"
    assert "sql_done" in kinds
    assert kinds.count("sql_rewrite") == 1
    assert kinds[-1] == "result"

    rewrite_index = kinds.index("sql_rewrite")
    assert rewrite_index > kinds.index("sql_done")
    issue, rewrite_sql = events[rewrite_index][1]["issue"], events[rewrite_index][1]["sql"]
    assert "执行报错重写" in issue
    assert rewrite_sql == FIXED_SQL

    result_data = events[-1][1]
    assert result_data["metadata"]["sql_repair_attempts"] == 1
    assert result_data["metadata"]["sql_repair_errors"] == ["sql_syntax"]


def test_run_stream_final_error_carries_repair_metadata():
    llm = FakeLLM([BROKEN_SQL, BROKEN_SQL, BROKEN_SQL])
    db = FakeDB([("raise", _syntax_error())] * 3)
    system = _make_system(llm, db)

    events = _parse_sse(
        "".join(system.run_stream("各站点销售额", **FEATURES_OFF))
    )
    kinds = [kind for kind, _ in events]
    assert kinds[-1] == "error"
    assert kinds.count("sql_rewrite") == 2
    error_data = events[-1][1]
    assert error_data["error_type"] == "database_sql_syntax"
    assert error_data["metadata"]["sql_repair_attempts"] == 2
    assert error_data["metadata"]["sql_repair_errors"] == ["sql_syntax"] * 3


# ---------------------------------------------------------------------------
# 额外：error_code 命中可修复集合也重试（error_type 非 sql_syntax 的场景）
# ---------------------------------------------------------------------------


def test_repairable_error_code_retried_even_if_error_type_unknown():
    error = QueryExecutionError(
        "execution_error",
        "SQL 执行失败",
        {"error_code": 1055, "raw_error": "Expression #1 of SELECT list is not in GROUP BY clause"},
    )
    llm = FakeLLM([BROKEN_SQL, FIXED_SQL])
    db = FakeDB([("raise", error), ("ok", ["item_amount"], [(1,)])])
    system = _make_system(llm, db)

    result = _run(system)

    assert result["success"] is True
    assert result["metadata"]["sql_repair_attempts"] == 1


def test_unrepairable_execution_error_not_retried():
    error = QueryExecutionError(
        "execution_error",
        "SQL 执行失败",
        {"error_code": 1040, "raw_error": "Too many connections"},
    )
    llm = FakeLLM([BROKEN_SQL, FIXED_SQL])
    db = FakeDB([("raise", error)])
    system = _make_system(llm, db)

    result = _run(system)

    assert result["success"] is False
    assert result["metadata"]["sql_repair_attempts"] == 0
    assert len(llm.prompts) == 1
