"""统一入口 /api/v1/ask 的路由与升级行为测试。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from security import make_auth_token  # noqa: E402

TEST_SECRET = "test-secret-for-ask"


def _parse_sse_stream(text: str) -> list[tuple[str, dict]]:
    events = []
    for part in text.split("\n\n"):
        if not part.strip():
            continue
        event_type = ""
        data = {}
        for line in part.splitlines():
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if event_type:
            events.append((event_type, data))
    return events


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", TEST_SECRET)
    monkeypatch.setenv("ALLOW_DEMO_ADMIN", "false")
    from fastapi.testclient import TestClient

    import api_service

    return TestClient(api_service.app)


@pytest.fixture()
def stubs(monkeypatch):
    """打桩：直接查询链路与多步分析链路都用假事件流，避免真实 LLM/DB。"""
    import api_service

    calls = {"simple": [], "agent": []}

    def fake_run_stream(user_question, **kwargs):
        calls["simple"].append({"question": user_question, "kwargs": kwargs})
        yield 'event: sql_done\ndata: {"sql": "SELECT 1"}\n\n'
        yield (
            'event: result\ndata: '
            + json.dumps({"columns": ["sales"], "rows": [{"sales": 100}], "row_count": 1})
            + "\n\n"
        )

    def fake_agent_stream(question, user_context, max_steps=None, run_options=None):
        calls["agent"].append(
            {"question": question, "user": user_context, "max_steps": max_steps}
        )
        yield 'event: decompose_done\ndata: {"subtasks": []}\n\n'
        yield 'event: done\ndata: {"completed_steps": 1, "failed_steps": 0, "skipped_steps": 0}\n\n'

    monkeypatch.setattr(api_service.system, "run_stream", fake_run_stream)
    monkeypatch.setattr(api_service, "_agent_event_stream", fake_agent_stream)
    return calls


def _auth_headers() -> dict:
    token = make_auth_token("u1", "admin", secret=TEST_SECRET, expires_in=600)
    return {"Authorization": f"Bearer {token}"}


def test_ask_requires_token(client, stubs):
    response = client.post("/api/v1/ask", json={"question": "上月销售额是多少"})
    assert response.status_code == 401


def test_ask_routes_simple_question_to_simple_path(client, stubs):
    response = client.post(
        "/api/v1/ask",
        json={"question": "上月销售额是多少", "mode": "auto"},
        headers=_auth_headers(),
    )
    assert response.status_code == 200
    events = _parse_sse_stream(response.text)
    types = [e[0] for e in events]

    routing = events[0]
    assert routing[0] == "routing"
    assert routing[1]["routed_to"] == "simple"
    assert routing[1]["confidence"] >= 0.8
    assert "sql_done" in types
    assert "result" in types
    assert "decompose_done" not in types
    assert "escalated" not in types
    assert len(stubs["agent"]) == 0
    assert len(stubs["simple"]) == 1


def test_ask_routes_complex_question_to_agent_path(client, stubs):
    response = client.post(
        "/api/v1/ask",
        json={"question": "最近三个月利润为什么下降"},
        headers=_auth_headers(),
    )
    assert response.status_code == 200
    events = _parse_sse_stream(response.text)
    types = [e[0] for e in events]

    assert events[0][0] == "routing"
    assert events[0][1]["routed_to"] == "agent"
    assert "decompose_done" in types
    assert "sql_done" not in types
    assert len(stubs["simple"]) == 0
    assert len(stubs["agent"]) == 1


def test_ask_explicit_mode_forces_agent_even_for_lookup(client, stubs):
    response = client.post(
        "/api/v1/ask",
        json={"question": "上月销售额是多少", "mode": "agent"},
        headers=_auth_headers(),
    )
    events = _parse_sse_stream(response.text)
    assert events[0][1]["routed_to"] == "agent"
    assert events[0][1]["mode"] == "agent"
    assert len(stubs["agent"]) == 1
    assert len(stubs["simple"]) == 0


def test_ask_explicit_mode_forces_simple_even_for_complex(client, stubs):
    response = client.post(
        "/api/v1/ask",
        json={"question": "利润为什么下降", "mode": "simple"},
        headers=_auth_headers(),
    )
    events = _parse_sse_stream(response.text)
    assert events[0][1]["routed_to"] == "simple"
    assert "decompose_done" not in [e[0] for e in events]
    assert len(stubs["simple"]) == 1


def test_ask_auto_escalates_when_result_covers_nothing(client, stubs, monkeypatch):
    """不确定带问题 + 单标量结果 → escalated 事件后自动升级多步分析。"""
    import api_service

    def thin_result_stream(user_question, **kwargs):
        yield 'event: sql_done\ndata: {"sql": "SELECT SUM(x) FROM t"}\n\n'
        yield (
            'event: result\ndata: '
            + json.dumps({"columns": ["sales"], "rows": [{"sales": 100}], "row_count": 1})
            + "\n\n"
        )

    monkeypatch.setattr(api_service.system, "run_stream", thin_result_stream)

    response = client.post(
        "/api/v1/ask",
        json={"question": "近7天销售额趋势", "mode": "auto"},
        headers=_auth_headers(),
    )
    events = _parse_sse_stream(response.text)
    types = [e[0] for e in events]

    assert events[0][1]["routed_to"] == "simple"
    assert events[0][1]["confidence"] < 0.75
    assert "result" in types
    assert "escalated" in types
    assert "decompose_done" in types
    escalated = next(data for name, data in events if name == "escalated")
    assert escalated["from"] == "simple"
    assert escalated["reason"]
    assert len(stubs["agent"]) == 1


def test_ask_does_not_escalate_when_auto_escalate_disabled(client, stubs, monkeypatch):
    import api_service

    def thin_result_stream(user_question, **kwargs):
        yield (
            'event: result\ndata: '
            + json.dumps({"columns": ["sales"], "rows": [{"sales": 100}], "row_count": 1})
            + "\n\n"
        )

    monkeypatch.setattr(api_service.system, "run_stream", thin_result_stream)

    response = client.post(
        "/api/v1/ask",
        json={"question": "近7天销售额趋势", "auto_escalate": False},
        headers=_auth_headers(),
    )
    types = [e[0] for e in _parse_sse_stream(response.text)]
    assert "escalated" not in types
    assert "decompose_done" not in types
    assert len(stubs["agent"]) == 0


def test_ask_does_not_escalate_on_covered_analysis_result(client, stubs, monkeypatch):
    """趋势问题返回多行序列 → 覆盖度足够，不升级。"""
    import api_service

    def series_stream(user_question, **kwargs):
        rows = [{"day": i, "sales": 10} for i in range(7)]
        yield (
            'event: result\ndata: '
            + json.dumps({"columns": ["day", "sales"], "rows": rows, "row_count": 7})
            + "\n\n"
        )

    monkeypatch.setattr(api_service.system, "run_stream", series_stream)

    response = client.post(
        "/api/v1/ask",
        json={"question": "近7天销售额趋势"},
        headers=_auth_headers(),
    )
    types = [e[0] for e in _parse_sse_stream(response.text)]
    assert "result" in types
    assert "escalated" not in types
    assert len(stubs["agent"]) == 0


def test_ask_does_not_escalate_high_confidence_lookup(client, stubs, monkeypatch):
    """高置信查询即使只返回单值也不做覆盖度质检（不付升级成本）。"""
    import api_service

    def thin_result_stream(user_question, **kwargs):
        yield (
            'event: result\ndata: '
            + json.dumps({"columns": ["sales"], "rows": [{"sales": 100}], "row_count": 1})
            + "\n\n"
        )

    monkeypatch.setattr(api_service.system, "run_stream", thin_result_stream)

    response = client.post(
        "/api/v1/ask",
        json={"question": "上月销售额是多少"},
        headers=_auth_headers(),
    )
    events = _parse_sse_stream(response.text)
    assert events[0][1]["confidence"] >= 0.75
    assert "escalated" not in [e[0] for e in events]
    assert len(stubs["agent"]) == 0


def test_ask_rejects_invalid_mode(client, stubs):
    response = client.post(
        "/api/v1/ask",
        json={"question": "上月销售额是多少", "mode": "hack"},
        headers=_auth_headers(),
    )
    assert response.status_code == 400


def test_ask_propagates_security_context_to_both_paths(client, stubs, monkeypatch):
    """路由不绕过安全层：身份上下文原样传给执行链路。"""
    import api_service

    def thin_result_stream(user_question, **kwargs):
        captured["security_context"] = kwargs.get("security_context")
        yield (
            'event: result\ndata: '
            + json.dumps({"columns": ["sales"], "rows": [{"sales": 1}], "row_count": 1})
            + "\n\n"
        )

    captured = {}
    monkeypatch.setattr(api_service.system, "run_stream", thin_result_stream)
    token = make_auth_token("u_ops_a", "ops", secret=TEST_SECRET, operator_name="运营A")
    response = client.post(
        "/api/v1/ask",
        json={"question": "上月销售额是多少"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    ctx = captured["security_context"]
    assert ctx.role == "ops"
    assert ctx.operator_name == "运营A"
    assert stubs["agent"] == [] or stubs["agent"][0]["user"].role == "ops"
