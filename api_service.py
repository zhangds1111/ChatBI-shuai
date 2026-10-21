"""
FastAPI 服务入口

将命令行版 ChatBI 系统封装为 REST API，供前端或其他服务调用。

第11课增强：
- 新增 POST /api/v1/query/stream SSE 流式端点
- 新增 CORS 中间件（前后端分离必须）
- 挂载 static 目录为静态文件服务（前端 HTML）
- 保留原有 /api/v1/query 同步端点不动，确保向后兼容
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from time import perf_counter
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import APP_CONFIG
from main import ChatBISystem
from query_router import (
    ESCALATION_CONFIDENCE_THRESHOLD,
    default_coverage_judge,
    route,
)
from security import SecurityError, UserContext, resolve_request_user

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger("chatbi.api")

app = FastAPI(
    title="ChatBI MVP API",
    version="0.2.0",
    description="""
## ChatBI MVP API

企业级 ChatBI 最小可行产品的服务化接口。

### 接口概览

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/v1/ask` | POST | **统一入口**（推荐）：SSE 流式，自动路由到直接查询或多步分析 |
| `/api/v1/query` | POST | 同步查询，一次性返回完整结果 |
| `/api/v1/query/stream` | POST | SSE 流式查询，逐步推送 SQL 和结果 |
| `/api/v1/agent/stream` | POST | SSE 流式复杂问题分析（拆解→计划→执行→报告） |
| `/health` | GET | 健康检查 |

### 统一入口的路由（/api/v1/ask）

请求体 `mode` 可选：`auto`（默认，自动路由）/ `simple`（强制直接查询）/ `agent`（强制多步分析）。
自动路由按规则快判（归因/多问句/拆解 → 多步分析；明确指标查询 → 直接查询；其余先走快速路径），
快速路径结果只有一个标量且问题带分析意图时自动升级为多步分析（`escalated` 事件）。

新增 SSE 事件类型：

| 事件类型 | 说明 |
|----------|------|
| `routing` | 路由决策：`{"routed_to", "reason", "confidence", "signals"}` |
| `escalated` | 快速路径结果未覆盖问题，已自动升级多步分析：`{"reason", "from"}` |

### SSE 流式接口事件类型

`/api/v1/query/stream` 返回的事件类型：

| 事件类型 | 说明 | data 结构 |
|----------|------|-----------|
| `sql_chunk` | SQL 文本片段，前端逐字拼接展示 | `{"content": "SELECT..."}` |
| `sql_done` | SQL 完整输出，可复制或二次处理 | `{"sql": "SELECT COUNT(*)..."}` |
| `result` | 查询结果 | `{"columns": [...], "rows": [...], "row_count": N}` |
| `error` | 异常信息 | `{"error": "...", "error_type": "..."}` |

### Apifox 导入方式

1. 启动服务后访问 `http://localhost:8000/openapi.json`
2. 在 Apifox 中选择「导入」→「URL 导入」→ 粘贴上面的地址
3. 或直接复制 JSON 内容，选择「文件导入」
""",
    openapi_tags=[
        {"name": "查询", "description": "自然语言转 SQL 查询接口"},
        {"name": "Agent", "description": "复杂问题 Plan-and-Execute 分析接口"},
        {"name": "系统", "description": "系统运维与监控接口"},
    ],
)
system = ChatBISystem(app_config=APP_CONFIG)

# ==================== CORS 中间件 ====================
# 前后端分离时，浏览器从 localhost:3000/file:// 访问 localhost:8000 会触发跨域限制
# CORS 中间件允许跨域请求，是前后端联调的必要配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # 生产环境应限制为具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class QueryRequest(BaseModel):
    """查询请求体

    身份（user_id / role / operator_name）只从 Authorization: Bearer <签名令牌> 读取，
    请求体里不再接受任何身份字段（防止调用方自填 admin 提权）。
    """

    question: str = Field(..., min_length=1, description="业务人员的自然语言问题")
    use_few_shot: bool | None = Field(default=None, description="是否启用 Few-shot 示例")
    use_rules: bool | None = Field(default=None, description="是否启用业务规则约束")
    use_guards: bool | None = Field(default=None, description="是否启用错误防护")
    use_indicator_knowledge: bool | None = Field(default=None, description="是否注入指标知识")
    use_schema_linking: bool | None = Field(default=None, description="是否启用 Schema Linking")
    use_indicator_rag: bool | None = Field(default=None, description="是否启用指标 RAG")
    source_id: str | None = Field(default=None, description="数据源标识；未传时使用系统默认数据源")


class AgentRequest(BaseModel):
    """复杂问题 Agent 请求体"""

    question: str = Field(..., min_length=1, description="复杂分析问题，例如：最近三个月利润为什么下降？")
    max_steps: int | None = Field(default=None, ge=1, description="限制实际执行的子任务步骤数，便于分步调试")
    use_schema_linking: bool = Field(default=True, description="子任务执行时是否启用 Schema Linking")
    use_indicator_rag: bool = Field(default=True, description="子任务执行时是否启用指标 RAG")
    use_indicator_knowledge: bool = Field(default=True, description="子任务执行时是否注入指标知识")


class AskRequest(QueryRequest):
    """统一入口请求体（自动路由）

    mode 是可选逃生口，不是必填标签：
    - auto（默认）：路由规则自动判定走直接查询还是多步分析
    - simple / agent：显式强制指定链路
    """

    mode: str = Field(default="auto", description="auto=自动路由；simple=直接查询；agent=多步分析")
    auto_escalate: bool = Field(
        default=True,
        description="快速路径结果不完整时是否自动升级多步分析（仅 auto 路由生效）",
    )
    max_steps: int | None = Field(default=None, ge=1, description="多步分析时限制子任务步骤数")


class HealthResponse(BaseModel):
    """健康检查响应"""

    status: str
    database_connected: bool


class QuerySuccessResponse(BaseModel):
    """成功响应"""

    success: bool = True
    question: str
    sql: str
    columns: list[str]
    rows: list[dict[str, Any]]
    formatted: str
    metadata: dict[str, Any]


class ErrorResponse(BaseModel):
    """错误响应"""

    success: bool = False
    error: str
    error_type: str
    metadata: dict[str, Any] = Field(default_factory=dict)


def _rows_to_dicts(columns: list[str], results: list[tuple]) -> list[dict[str, Any]]:
    """将数据库元组结果转换为 JSON 可直接返回的字典列表"""
    return [dict(zip(columns, row)) for row in results]


def _parse_sse_event(event_str: str) -> tuple[str, dict[str, Any]]:
    """解析 run_stream 产出的 "event: X\\ndata: {...}\\n\\n" 事件字符串。"""
    event_type = ""
    data: dict[str, Any] = {}
    for line in event_str.splitlines():
        if line.startswith("event: "):
            event_type = line[len("event: "):].strip()
        elif line.startswith("data: "):
            try:
                data = json.loads(line[len("data: "):])
            except json.JSONDecodeError:
                data = {}
    return event_type, data


def _agent_event_stream(
    question: str,
    user_context: UserContext,
    max_steps: int | None = None,
    run_options: dict[str, Any] | None = None,
):
    """构造多步分析（Plan-and-Execute）的 SSE 事件流，供 /ask 与 /agent/stream 复用。"""
    from agent_planner import PlanAndExecuteAgent, StepExecutor

    executor = StepExecutor(
        chatbi_system=system,
        chatbi_run_options=run_options,
        security_context=user_context,
    )
    agent = PlanAndExecuteAgent(executor=executor)
    try:
        yield from agent.run_stream(
            question,
            max_steps=max_steps,
        )
    finally:
        executor.cleanup()


def _require_user_context(request: Request) -> UserContext:
    """取中间件解析出的身份；无有效令牌时返回 401。"""
    context = getattr(request.state, "user_context", None)
    if context is None:
        detail = getattr(request.state, "auth_error", None) or "缺少 Authorization: Bearer 令牌"
        raise HTTPException(status_code=401, detail=detail)
    return context


def _resolve_query_options(payload: QueryRequest, app_config: dict) -> dict[str, bool]:
    feature_defaults = app_config.get("features", {})
    return {
        "use_few_shot": payload.use_few_shot if payload.use_few_shot is not None else feature_defaults.get("few_shot", False),
        "use_rules": payload.use_rules if payload.use_rules is not None else feature_defaults.get("rules", False),
        "use_guards": payload.use_guards if payload.use_guards is not None else feature_defaults.get("guards", False),
        "use_indicator_knowledge": (
            payload.use_indicator_knowledge
            if payload.use_indicator_knowledge is not None
            else feature_defaults.get("indicator_knowledge", False)
        ),
        "use_schema_linking": (
            payload.use_schema_linking
            if payload.use_schema_linking is not None
            else feature_defaults.get("schema_linking", True)
        ),
        "use_indicator_rag": (
            payload.use_indicator_rag
            if payload.use_indicator_rag is not None
            else feature_defaults.get("indicator_rag", False)
        ),
    }


@app.middleware("http")
async def attach_user_context(request: Request, call_next):
    """解析 Authorization: Bearer <签名令牌>，把权限上下文挂到 request.state。

    - 令牌签名错误 / 过期 / 缺字段：user_context 为空，查询接口返回 401。
    - 无令牌：仅当 ALLOW_DEMO_ADMIN=true 时放行为 demo_admin（开发模式），默认拒绝。
    - 请求体/请求头里的 x-user-role 等身份字段一律忽略，不再信任调用方自填。
    """
    request.state.user_context = None
    request.state.auth_error = None
    try:
        request.state.user_context = resolve_request_user(
            request.headers.get("authorization"),
        )
    except SecurityError as exc:
        request.state.auth_error = str(exc)
    return await call_next(request)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError
):
    """统一处理请求体验证错误"""
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(
            error="请求参数校验失败",
            error_type="request_validation",
            metadata={
                "path": str(request.url.path),
                "details": exc.errors(),
            },
        ).model_dump()
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """统一处理业务异常"""
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            error=str(exc.detail),
            error_type="http_exception",
            metadata={"path": str(request.url.path)},
        ).model_dump()
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """兜底异常处理，避免把 Python Traceback 直接暴露给前端"""
    logger.exception("Unhandled server error: %s", exc)
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            error="服务内部异常",
            error_type="internal_server_error",
            metadata={"path": str(request.url.path)},
        ).model_dump()
    )


@app.get("/", tags=["系统"])
def read_root() -> dict[str, str]:
    """服务说明入口"""
    return {
        "name": "ChatBI MVP API",
        "docs": "/docs",
        "health": "/health",
        "query": "/api/v1/query",
        "query_stream": "/api/v1/query/stream",
        "agent_stream": "/api/v1/agent/stream",
    }


@app.get("/health", response_model=HealthResponse, tags=["系统"])
def health_check() -> HealthResponse:
    """检查 API 服务和数据库连通性"""
    runtime = system._get_runtime()
    return HealthResponse(
        status="ok",
        database_connected=runtime.db.validate_connection()
    )


@app.post(
    "/api/v1/query",
    response_model=QuerySuccessResponse,
    tags=["查询"],
    summary="同步查询（一次性返回）",
    responses={
        400: {"model": ErrorResponse, "description": "输入问题不合法"},
        403: {"model": ErrorResponse, "description": "权限不足或安全策略拒绝"},
        422: {"model": ErrorResponse, "description": "生成的 SQL 无法执行"},
        502: {"model": ErrorResponse, "description": "LLM 调用失败"},
        503: {"model": ErrorResponse, "description": "数据库连接异常"},
        504: {"model": ErrorResponse, "description": "数据库查询超时"},
        500: {"model": ErrorResponse, "description": "数据库或服务内部异常"},
    },
)
def query_chatbi(payload: QueryRequest, request: Request) -> QuerySuccessResponse:
    """执行自然语言查询，并返回标准化结果（同步，一次性返回）"""
    started_at = perf_counter()
    logger.info("Received question: %s", payload.question)
    user_context = _require_user_context(request)
    query_options = _resolve_query_options(payload, APP_CONFIG)

    result = system.run(
        user_question=payload.question,
        source_id=payload.source_id,
        security_context=user_context,
        **query_options,
    )

    duration_ms = round((perf_counter() - started_at) * 1000, 2)

    if not result["success"]:
        error_type = result.get("error_type", "internal_server_error")
        status_code = 500
        if error_type == "validation":
            status_code = 400
        elif error_type == "llm":
            status_code = 502
        elif error_type == "security":
            status_code = 403
        elif error_type == "database_sql_syntax":
            status_code = 422
        elif error_type == "database_connection_error":
            status_code = 503
        elif error_type == "database_query_timeout":
            status_code = 504
        raise HTTPException(status_code=status_code, detail=result["error"])

    metadata = {
        **result.get("metadata", {}),
        "duration_ms": duration_ms,
    }
    logger.info("Question handled successfully in %.2f ms", duration_ms)

    return QuerySuccessResponse(
        question=payload.question,
        sql=result["sql"],
        columns=result["columns"],
        rows=_rows_to_dicts(result["columns"], result["results"]),
        formatted=result["formatted"],
        metadata=metadata,
    )


@app.post("/api/v1/query/stream", tags=["查询"], summary="SSE 流式查询（逐步推送）")
async def query_chatbi_stream(payload: QueryRequest, request: Request) -> StreamingResponse:
    """
    执行自然语言查询，以 SSE 流式返回结果

    SSE 事件类型：
    - sql_chunk: LLM 流式产出的 SQL 文本片段，前端可逐字拼接展示
    - sql_done: SQL 完整输出，前端可用于复制或二次处理
    - result: 查询结果（columns + rows）
    - error: 异常信息

    每个事件的格式为：
        event: <type>
        data: <json>
    """
    logger.info("Stream request received: %s", payload.question)
    user_context = _require_user_context(request)
    query_options = _resolve_query_options(payload, APP_CONFIG)

    def event_generator():
        for event_str in system.run_stream(
            user_question=payload.question,
            source_id=payload.source_id,
            security_context=user_context,
            **query_options,
        ):
            yield event_str

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx 环境下禁止缓冲
        },
    )


@app.post("/api/v1/agent/stream", tags=["Agent"], summary="复杂问题 Agent 流式分析（拆解→计划→执行→报告）")
async def agent_stream(payload: AgentRequest, request: Request) -> StreamingResponse:
    """
    执行复杂分析问题，以 SSE 流式推送 Plan-and-Execute 全过程

    SSE 事件类型：
    - decompose_done: 问题差分（拆解出的子任务列表）
    - plan_done: 执行计划（步骤、依赖、预期输出）
    - step_start: 子任务开始执行
    - step_done: 子任务执行结果（含 SQL、数据、错误）
    - summary_done: 执行摘要
    - report_done: 最终分析报告
    - error: 链路级异常
    - done: 链路结束（含成功/失败/跳过步骤数）
    """
    logger.info("Agent stream request received: %s", payload.question)
    user_context = _require_user_context(request)

    run_options = {
        "use_schema_linking": payload.use_schema_linking,
        "use_indicator_rag": payload.use_indicator_rag,
        "use_indicator_knowledge": payload.use_indicator_knowledge,
    }

    def event_generator():
        yield from _agent_event_stream(
            payload.question,
            user_context,
            max_steps=payload.max_steps,
            run_options=run_options,
        )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/v1/ask", tags=["查询"], summary="统一入口：自动路由直接查询 / 多步分析（SSE）")
async def ask_stream(payload: AskRequest, request: Request) -> StreamingResponse:
    """统一问答入口：接收问题后自动路由，而不是要求调用方预先打标签。

    流程：
    1. 解析 Bearer 令牌身份（无有效令牌返回 401）；
    2. `route(question, mode)` 做路由决策，首帧推送 `routing` 事件（可解释原因）；
    3. simple → 走 /api/v1/query/stream 同款链路；agent → 走 /api/v1/agent/stream 同款链路；
    4. auto 模式下若快速路径结果只有一个标量、且问题带分析意图（覆盖度质检），
       推送 `escalated` 事件并自动升级多步分析（可用 auto_escalate=false 关闭）。

    事件流 = routing [→ simple 事件 | → escalated → agent 事件]，前端按事件类型渲染。
    """
    user_context = _require_user_context(request)
    try:
        decision = route(payload.question, mode=payload.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info(
        "Ask request received: mode=%s routed_to=%s question=%s",
        payload.mode,
        decision.target,
        payload.question,
    )
    query_options = _resolve_query_options(payload, APP_CONFIG)
    agent_options = {
        "use_schema_linking": query_options["use_schema_linking"],
        "use_indicator_rag": query_options["use_indicator_rag"],
        "use_indicator_knowledge": query_options["use_indicator_knowledge"],
    }

    from main import _sse_event

    def event_generator():
        yield _sse_event("routing", {"mode": payload.mode, **decision.to_dict()})

        if decision.target == "agent":
            yield from _agent_event_stream(
                payload.question,
                user_context,
                max_steps=payload.max_steps,
                run_options=agent_options,
            )
            return

        # 快速路径：流式透传；低置信结果做覆盖度质检，不完整则升级多步分析
        escalate_reason: str | None = None
        for event_str in system.run_stream(
            user_question=payload.question,
            source_id=payload.source_id,
            security_context=user_context,
            **query_options,
        ):
            event_type, event_data = _parse_sse_event(event_str)
            yield event_str
            if event_type != "result":
                continue
            if not payload.auto_escalate:
                continue
            if decision.confidence >= ESCALATION_CONFIDENCE_THRESHOLD:
                continue
            escalate_reason = default_coverage_judge(
                payload.question,
                event_data.get("columns") or [],
                event_data.get("rows") or [],
            )
            if escalate_reason:
                break  # 快速路径已给出结果事件，后续转多步分析

        if escalate_reason:
            yield _sse_event("escalated", {"reason": escalate_reason, "from": "simple"})
            yield from _agent_event_stream(
                payload.question,
                user_context,
                max_steps=payload.max_steps,
                run_options=agent_options,
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ==================== 静态文件挂载 ====================
# 将 static/ 目录挂载为 /static 路径，前端 HTML 页面可直接访问
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    uvicorn.run("api_service:app", host="0.0.0.0", port=8000, reload=True)
