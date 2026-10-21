"""tracing 模块单测：开关判断、关闭时的透传行为。"""

from __future__ import annotations

import os

import pytest

import tracing
from tracing import (
    is_tracing_enabled,
    reduce_sse,
    reduce_step_events,
    traceable,
    trim_check_outputs,
    trim_context_inputs,
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


def test_trim_query_outputs_truncates_long_results():
    outputs = {"results": list(range(10)), "sql": "SELECT 1"}
    trimmed = trim_query_outputs(outputs)
    assert trimmed["results"] == [0, 1, 2, 3, 4]
    assert trimmed["results_truncated"] is True
    assert trimmed["sql"] == "SELECT 1"


def test_trim_query_outputs_keeps_short_results():
    outputs = {"results": [1, 2], "ok": True}
    assert trim_query_outputs(outputs) is outputs


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
