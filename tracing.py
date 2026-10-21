"""
LangSmith trace 接入模块

约定：
- 阶段 span 统一用 traceable 装饰（run_type 见 langsmith 枚举：chain/llm/tool/retriever/parser/prompt/embedding）。
- LLM 调用统一由 wrap_openai_client 包装 OpenAI client 自动记录，业务代码不手动打 LLM span。
- LANGSMITH_TRACING 未配/为 false 时：wrap 不生效、traceable 空转（无网络、无异常）。

环境变量（LANGSMITH_* 优先，LANGCHAIN_* 兜底）：
- LANGSMITH_TRACING=true        开启追踪
- LANGSMITH_API_KEY             API 密钥
- LANGSMITH_PROJECT             项目名
- LANGSMITH_ENDPOINT            自托管网关地址（可选）
"""

from __future__ import annotations

import os
from typing import Any, Callable, TypeVar

from langsmith import traceable

F = TypeVar("F", bound=Callable[..., Any])


def is_tracing_enabled() -> bool:
    """追踪开关。直读 os.environ（不走 langsmith 的 lru_cache），便于测试 monkeypatch。"""
    for name in (
        "LANGSMITH_TRACING_V2",
        "LANGCHAIN_TRACING_V2",
        "LANGSMITH_TRACING",
        "LANGCHAIN_TRACING",
    ):
        val = os.environ.get(name, "")
        if val.strip():
            return val.strip().lower() == "true"
    return False


def wrap_openai_client(client):
    """仅在 tracing 开启时包装 OpenAI client（含 chat.completions 与流式）。

    唯一 wrap 入口，只应在 LLMClient.__init__ 调用一次，避免双层 llm span。
    """
    if not is_tracing_enabled():
        return client
    from langsmith.wrappers import wrap_openai

    return wrap_openai(client, chat_name="大模型调用", completions_name="大模型调用")


def wrap_embeddings(emb):
    """包装 langchain Embeddings，使 embed_query/embed_documents 成为 embedding span。

    wrap_openai 不覆盖 embeddings.create，故需显式包装。
    包装类继承 langchain Embeddings 基类，兼容 Chroma 等调用方的类型要求。
    """
    if not is_tracing_enabled():
        return emb

    from langchain_core.embeddings import Embeddings

    @traceable(name="查询向量化", run_type="embedding")
    def _embed_query(text: str):
        return emb.embed_query(text)

    @traceable(name="文档向量化", run_type="embedding")
    def _embed_documents(texts: list[str]):
        return emb.embed_documents(texts)

    class _TracedEmbeddings(Embeddings):
        def embed_query(self, text: str):
            return _embed_query(text)

        def embed_documents(self, texts: list[str]):
            return _embed_documents(texts)

    return _TracedEmbeddings()


def trim_query_outputs(outputs: Any) -> Any:
    """裁剪链路 A 输出里的结果行，避免 trace 载荷过大。"""
    if isinstance(outputs, dict) and isinstance(outputs.get("results"), list):
        rows = outputs["results"]
        if len(rows) > 5:
            outputs = {**outputs, "results": rows[:5], "results_truncated": True}
    return outputs


def trim_schema_outputs(outputs: Any) -> Any:
    """裁剪 Schema 文本，避免 trace 载荷过大（仅影响 trace，不影响函数返回值）。"""
    if isinstance(outputs, dict) and isinstance(outputs.get("schema_text"), str):
        text = outputs["schema_text"]
        if len(text) > 1500:
            outputs = {
                **outputs,
                "schema_text": text[:1500] + f"...[已截断，共 {len(text)} 字符]",
            }
    return outputs


def _shorten(text: str, limit: int = 800) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[已截断，共 {len(text)} 字符]"


def trim_prompt_outputs(outputs: Any) -> Any:
    """裁剪 Prompt 构造结果里的长文本（system/prompt），避免与 LLM span 重复撑爆 trace。"""
    if isinstance(outputs, (list, tuple)) and all(isinstance(x, str) for x in outputs):
        return [_shorten(x) for x in outputs]
    return outputs


def trim_check_outputs(outputs: Any) -> Any:
    """校验函数通过时返回 None，trace 上补记结论便于一眼判断。"""
    return "校验通过" if outputs is None else outputs


def _trim_jsonable(value: Any, depth: int = 0) -> Any:
    """把嵌套结构里的非基本类型对象替换为类型名，避免 trace 里出现 object 地址。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _shorten(value) if depth > 0 else value
    if depth > 4:
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        return {str(k): _trim_jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_trim_jsonable(v, depth + 1) for v in value]
    return f"<{type(value).__name__}>"


def trim_context_inputs(inputs: dict) -> dict:
    """裁剪 span 输入：去掉对象 repr（如 IndicatorKnowledge 地址），只留类型名。"""
    return {k: _trim_jsonable(v) for k, v in inputs.items()}


def reduce_joined(parts: list[str]) -> str:
    """generator 产出的增量文本归并为完整字符串（SQL 流）。"""
    return "".join(parts)


def reduce_sse(events: list[str]) -> dict:
    """SSE/事件流归并：按事件类型计数，避免 outputs 膨胀（不再堆重复事件头）。"""
    kinds: dict[str, int] = {}
    for e in events:
        first = e.splitlines()[0] if e else ""
        kind = first.removeprefix("event: ").strip() or "(空)"
        kinds[kind] = kinds.get(kind, 0) + 1
    return {"event_count": len(events), "events_by_type": kinds}


def reduce_step_events(events: list[tuple]) -> dict:
    """Agent step 事件流归并：按事件类型计数。"""
    kinds: dict[str, int] = {}
    for t, _ in events:
        kinds[str(t)] = kinds.get(str(t), 0) + 1
    return {"event_count": len(events), "events_by_type": kinds}


__all__ = [
    "traceable",
    "is_tracing_enabled",
    "wrap_openai_client",
    "wrap_embeddings",
    "trim_query_outputs",
    "trim_schema_outputs",
    "trim_prompt_outputs",
    "trim_check_outputs",
    "trim_context_inputs",
    "reduce_joined",
    "reduce_sse",
    "reduce_step_events",
]
