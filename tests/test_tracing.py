"""tracing 模块单测：开关判断、关闭时的透传行为。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tracing  # noqa: E402
from security import UserContext  # noqa: E402
from tracing import (
    is_tracing_enabled,
    reduce_sse,
    reduce_step_events,
    traceable,
    trim_check_outputs,
    trim_context_inputs,
    trim_execute_inputs,
    trim_execute_outputs,
    trim_prompt_outputs,
    trim_query_outputs,
    wrap_embeddings,
    wrap_openai_client,
)

_TRACING_ENV_KEYS = (
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING_V2",
    "LANGSMITH_TRACING",
    "LANGCHAIN_TRACING",
)


@pytest.fixture(autouse=True)
def _clean_tracing_env(monkeypatch: pytest.MonkeyPatch):
    for key in _TRACING_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


def test_tracing_disabled_by_default():
    assert is_tracing_enabled() is False


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [("true", True), ("True", True), ("false", False), ("1", False), ("", False)],
)
def test_is_tracing_enabled_reads_langsmith_tracing(env_value, expected):
    os.environ["LANGSMITH_TRACING"] = env_value
    assert is_tracing_enabled() is expected


def test_legacy_langchain_tracing_fallback():
    os.environ["LANGCHAIN_TRACING"] = "true"
    assert is_tracing_enabled() is True


def test_langsmith_namespace_takes_precedence():
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING"] = "true"
    assert is_tracing_enabled() is False


def test_wrap_openai_client_passthrough_when_disabled():
    sentinel = object()
    assert wrap_openai_client(sentinel) is sentinel


def test_wrap_embeddings_passthrough_when_disabled():
    class _Emb:
        def embed_query(self, text: str):
            return [0.0]

        def embed_documents(self, texts: list[str]):
            return [[0.0]]

    emb = _Emb()
    assert wrap_embeddings(emb) is emb


def test_traceable_preserves_behavior_when_disabled():
    @traceable(name="double", run_type="tool")
    def double(x: int) -> int:
        return x * 2

    assert double(21) == 42


def test_trim_query_outputs_drops_all_rows_by_default(monkeypatch):
    """默认配置：不保留任何行，只留 row_count（原断言「保留 5 行」的旧契约已废弃）。"""
    monkeypatch.delenv("LANGSMITH_TRACE_ROWS", raising=False)
    outputs = {"results": list(range(10)), "sql": "SELECT 1", "formatted": "table"}
    trimmed = trim_query_outputs(outputs)
    assert trimmed["results"] == []
    assert trimmed["row_count"] == 10
    assert trimmed["results_truncated"] is True
    assert trimmed["sql"] == "SELECT 1"
    assert "0" not in str(trimmed["results"])


def test_trim_query_outputs_keeps_short_results(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACE_ROWS", raising=False)
    outputs = {"results": [1, 2], "ok": True}
    trimmed = trim_query_outputs(outputs)
    assert trimmed["results"] == []
    assert trimmed["row_count"] == 2


def test_is_tracing_enabled_does_not_use_lru_cache(monkeypatch):
    """开关必须能随 env 变化（绕开 langsmith get_env_var 的缓存）。"""
    assert is_tracing_enabled() is False
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    assert is_tracing_enabled() is True


def test_trim_context_inputs_replaces_object_repr():
    class _Knowledge:
        pass

    inputs = {"user_question": "退货率", "indicator_knowledge": _Knowledge(), "n": 3}
    trimmed = trim_context_inputs(inputs)
    assert trimmed["user_question"] == "退货率"
    assert trimmed["indicator_knowledge"] == "<_Knowledge>"
    assert trimmed["n"] == 3


def test_trim_prompt_outputs_shortens_long_strings():
    outputs = ("sys", "p" * 2000)
    trimmed = trim_prompt_outputs(outputs)
    assert trimmed[0] == "sys"
    assert trimmed[1].startswith("ppp")
    assert "已截断" in trimmed[1]


def test_trim_check_outputs_marks_pass():
    assert trim_check_outputs(None) == "校验通过"
    assert trim_check_outputs("缺上界") == "缺上界"


def test_reduce_sse_counts_event_types():
    events = ["event: sql_chunk\ndata: {}\n\n"] * 3 + ["event: result\ndata: {}\n\n"]
    summary = reduce_sse(events)
    assert summary["event_count"] == 4
    assert summary["events_by_type"] == {"sql_chunk": 3, "result": 1}


def test_reduce_step_events_counts_kinds():
    summary = reduce_step_events([("step_result", 1), ("step_result", 2), ("error", 3)])
    assert summary == {"event_count": 3, "events_by_type": {"step_result": 2, "error": 1}}


# ---------------------------------------------------------------------------
# 任务 2：execute span 不得上传结果行 / 用户身份
# ---------------------------------------------------------------------------


def test_trim_execute_outputs_has_no_rows():
    result = trim_execute_outputs((["a"], [(1,), (2,)]))
    assert result == {"columns": ["a"], "row_count": 2}
    assert "rows" not in result


def test_trim_execute_inputs_drops_user_identity():
    user = UserContext(user_id="u_secret_123", role="ops", operator_name="张三")
    inputs = {
        "self": object(),
        "sql": "SELECT 1",
        "user": user,
        "question": "上月销售额",
    }
    trimmed = trim_execute_inputs(inputs)
    assert trimmed == {"sql": "SELECT 1", "question": "上月销售额", "user": "ops"}
    flat = json.dumps(trimmed, ensure_ascii=False, default=str)
    assert "u_secret_123" not in flat
    assert "张三" not in flat
    assert "user_id" not in flat
    assert "operator_name" not in flat


def test_trim_query_outputs_keeps_five_rows_only_when_env_true(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACE_ROWS", "true")
    outputs = {"results": list(range(10)), "sql": "SELECT 1"}
    trimmed = trim_query_outputs(outputs)
    assert trimmed["results"] == [0, 1, 2, 3, 4]
    assert trimmed["results_truncated"] is True
    assert trimmed["row_count"] == 10


def test_execute_span_records_no_rows_with_fake_trace_backend(monkeypatch):
    """执行 100 行查询时，被 trace 记录的 outputs 里没有任何行。

    做法：monkeypatch traceable 的 process_outputs（tracing.trim_execute_outputs）
    加一层记录后重载 database，使「SQL执行」span 绑定到带记录的裁剪函数；
    再用假连接跑 100 行查询，核对记录到的 outputs。
    """
    import importlib

    import database as database_module

    recorded: list = []
    original = tracing.trim_execute_outputs

    def spy(outputs):
        processed = original(outputs)
        recorded.append(processed)
        return processed

    monkeypatch.setattr(tracing, "trim_execute_outputs", spy)
    reloaded = importlib.reload(database_module)
    try:

        class _Cursor:
            def __init__(self):
                self.description = [("store_id",)]
                self._pos = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql):
                self._pos = 0

            def fetchmany(self, size):
                chunk = [(f"S{i:03d}",) for i in range(self._pos, min(self._pos + size, 100))]
                self._pos += len(chunk)
                return chunk

            def fetchall(self):
                return self.fetchmany(1000)

        class _Conn:
            def __init__(self):
                self._cursor = _Cursor()

            def cursor(self):
                return self._cursor

            def close(self):
                pass

            def ping(self, reconnect=False):
                return None

        client = reloaded.DatabaseClient(connection_factory=lambda: _Conn())
        columns, rows = client.execute(
            "SELECT store_id FROM amz_order_items",
            user=UserContext(user_id="u_a", role="admin"),
        )
    finally:
        monkeypatch.undo()
        importlib.reload(database_module)

    # 真实返回值不受影响
    assert columns == ["store_id"]
    assert len(rows) == 100

    # 被记录的 trace outputs 里没有任何行
    assert recorded, "process_outputs 未被 traceable 调用"
    processed = recorded[-1]
    assert processed == {"columns": ["store_id"], "row_count": 100}
    flat = json.dumps(processed, default=str)
    assert "S000" not in flat
