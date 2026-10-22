"""
主入口模块

整合所有模块，提供命令行交互界面。
这是 ChatBI Text2SQL 系统的统一入口，串联 query_parser → prompt_builder → llm_client → database → result_formatter 完整链路，
并整合第 7 课规则修复、第 9 课指标知识注入、第 18 课 Schema Linking 和第 19 课指标 RAG。

第11课增强：新增 run_stream 流式方法，按阶段 yield 事件，
为 SSE 推送提供业务层能力。保留原有 run 方法不动，确保向后兼容。

第20课增强：新增 use_schema_linking 和 use_indicator_rag 参数，
支持 Schema Linking（动态 Schema 注入）+ 指标 RAG（语义检索指标知识）。
两者可独立开关，均有 fallback 机制保障稳定性。
"""

import datetime
import json
import os
import re
import sys
from decimal import Decimal
from types import SimpleNamespace
from typing import Generator

from config import APP_CONFIG, LLM_CONFIG
from database import DatabaseClient, QueryExecutionError
from error_analyzer import check_date_year, check_time_bounds
from indicator_knowledge import IndicatorKnowledge
from llm_client import LLMClient
from prompt_builder import build_prompt
from query_parser import QueryParser
from result_formatter import ResultFormatter
from runtime_factory import build_runtime
from security import SecurityError, UserContext
from tracing import (
    reduce_sse,
    traceable,
    trim_context_inputs,
    trim_query_outputs,
    trim_run_inputs,
)


class ChatBISystem:
    """ChatBI 系统主类"""

    def __init__(
        self,
        runtime=None,
        app_config: dict | None = None,
        runtime_factory=build_runtime,
    ):
        self.app_config = app_config or APP_CONFIG
        self.runtime_factory = runtime_factory
        self.runtime = runtime or self.runtime_factory(self.app_config)
        self.parser = self.runtime.parser
        self.llm = self.runtime.llm
        self.db = self.runtime.db
        self.formatter = self.runtime.formatter
        self.indicator_knowledge = self.runtime.indicator_knowledge

    def _get_runtime(self, source_id: str | None = None):
        if source_id is None or source_id == self.runtime.source_id:
            return SimpleNamespace(
                source_id=self.runtime.source_id,
                parser=self.parser,
                llm=self.llm,
                db=self.db,
                formatter=self.formatter,
                indicator_knowledge=self.indicator_knowledge,
            )
        return self.runtime_factory(self.app_config, source_id=source_id)

    def _resolve_feature_options(self, overrides: dict[str, bool | None]) -> dict[str, bool]:
        feature_defaults = self.app_config.get("features", {})
        resolved = {}
        for option_name, value in overrides.items():
            feature_key = option_name.removeprefix("use_")
            resolved[option_name] = value if value is not None else feature_defaults.get(feature_key, False)
        return resolved

    @traceable(
        name="指标上下文解析",
        run_type="chain",
        process_inputs=trim_context_inputs,
    )
    def _resolve_indicator_context(
        self,
        user_question: str,
        use_indicator_knowledge: bool,
        use_indicator_rag: bool,
        indicator_knowledge: IndicatorKnowledge,
    ) -> tuple[list[str], str]:
        """统一解析指标上下文，避免 RAG/关键词路径重复执行同一检索逻辑。"""
        detected_indicators = []
        indicator_block = ""

        if use_indicator_rag:
            try:
                from indicator_retriever import retrieve_indicator_context

                context = retrieve_indicator_context(user_question)
                detected_indicators = context["detected_indicators"]
                indicator_block = context["indicator_block"]
            except Exception:
                detected_indicators = []
                indicator_block = ""

            if use_indicator_knowledge and not indicator_block:
                context = indicator_knowledge.get_indicator_context(user_question)
                detected_indicators = context["detected_indicators"]
                indicator_block = context["indicator_block"]
        elif use_indicator_knowledge:
            context = indicator_knowledge.get_indicator_context(user_question)
            detected_indicators = context["detected_indicators"]
            indicator_block = context["indicator_block"]

        return detected_indicators, indicator_block

    # ---- 执行报错 → 带错误信息让 LLM 重写 → 有限次重试 ----

    # 可修复错误码：字段不存在(1054)、字段有歧义(1052)、GROUP BY 不合规(1055)、聚合函数误用(1111)
    REPAIRABLE_ERROR_CODES = {1054, 1052, 1055, 1111}
    # 这些错误与 SQL 文本无关，重写无意义，一律不重试
    NON_REPAIRABLE_ERROR_TYPES = {"connection_error", "query_timeout", "permission_denied"}

    def _sql_repair_max_retries(self) -> int:
        raw = os.getenv("SQL_REPAIR_MAX_RETRIES")
        if raw is not None and raw.strip():
            try:
                return max(0, int(raw))
            except ValueError:
                pass
        return max(0, int(self.app_config.get("sql_repair_max_retries", 2)))

    def _is_repairable_error(self, exc: Exception) -> bool:
        """只有可修复错误（语法/字段/聚合类）才让 LLM 重写重试。

        SecurityError 不重试（避免用安全拒绝探测系统）；connection_error、
        query_timeout、permission_denied 和 LLM 调用失败一律不重试。
        """
        if isinstance(exc, SecurityError) or not isinstance(exc, QueryExecutionError):
            return False
        if exc.error_type in self.NON_REPAIRABLE_ERROR_TYPES:
            return False
        if exc.error_type == "sql_syntax":
            return True
        code = exc.metadata.get("error_code")
        try:
            return int(code) in self.REPAIRABLE_ERROR_CODES
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _build_repair_prompt(prompt: str, sql: str, exc: QueryExecutionError) -> str:
        raw_error = str(exc.metadata.get("raw_error") or exc)
        if len(raw_error) > 500:
            raw_error = raw_error[:500]
        return (
            f"{prompt}\n\n"
            f"【SQL 执行失败】\n"
            f"上一次生成的 SQL：{sql}\n"
            f"数据库报错：{raw_error}\n"
            f"请只修正导致报错的问题，重新输出完整 SQL。"
        )

    def _execute_sql_with_repair(
        self,
        *,
        runtime,
        user_question: str,
        sql: str,
        system_msg: str,
        prompt: str,
        user_context: UserContext,
        time_issue: str | None = None,
    ) -> Generator[tuple[str, dict], None, dict]:
        """执行 SQL；可修复报错时带错误信息让 LLM 重写并有限次重试。

        每次执行都走 db.execute 的完整安全校验与审计日志；每次重写后重新跑
        check_time_bounds / check_date_year，保证重写没有引入时间错误。

        生成器协议：每次重写 yield ("sql_rewrite", {"issue", "sql"}) 供流式路径
        推送事件；return 最终执行结果 dict（status / sql / columns / results /
        db_info / exception / error / error_type / error_meta / time_issue /
        sql_repair_attempts / sql_repair_errors）。
        """
        max_retries = self._sql_repair_max_retries()
        repair_attempts = 0
        repair_errors: list[str] = []
        current_sql = sql
        current_time_issue = time_issue

        while True:
            try:
                columns, results = runtime.db.execute(
                    current_sql, user=user_context, question=user_question
                )
                db_info = getattr(runtime.db, "last_query_info", {})
                return {
                    "status": "success",
                    "sql": current_sql,
                    "columns": columns,
                    "results": results,
                    "db_info": db_info,
                    "time_issue": current_time_issue,
                    "sql_repair_attempts": repair_attempts,
                    "sql_repair_errors": repair_errors,
                }
            except SecurityError as e:
                repair_errors.append("security")
                return {
                    "status": "security",
                    "sql": current_sql,
                    "exception": e,
                    "error": str(e),
                    "error_type": "security",
                    "time_issue": current_time_issue,
                    "sql_repair_attempts": repair_attempts,
                    "sql_repair_errors": repair_errors,
                }
            except QueryExecutionError as e:
                repair_errors.append(e.error_type)
                if repair_attempts >= max_retries or not self._is_repairable_error(e):
                    return {
                        "status": "query",
                        "sql": current_sql,
                        "exception": e,
                        "error": str(e),
                        "error_type": f"database_{e.error_type}",
                        "error_meta": e.metadata,
                        "time_issue": current_time_issue,
                        "sql_repair_attempts": repair_attempts,
                        "sql_repair_errors": repair_errors,
                    }
                try:
                    new_sql = runtime.llm.generate_sql(
                        system_msg,
                        self._build_repair_prompt(prompt, current_sql, e),
                    )
                except Exception:
                    # LLM 调用失败：不再重试，返回最后一次执行错误
                    return {
                        "status": "query",
                        "sql": current_sql,
                        "exception": e,
                        "error": str(e),
                        "error_type": f"database_{e.error_type}",
                        "error_meta": e.metadata,
                        "time_issue": current_time_issue,
                        "sql_repair_attempts": repair_attempts,
                        "sql_repair_errors": repair_errors,
                    }
                repair_attempts += 1
                current_sql = new_sql
                current_time_issue = check_time_bounds(
                    user_question, current_sql
                ) or check_date_year(user_question, current_sql)
                yield "sql_rewrite", {
                    "issue": f"执行报错重写：{e.error_type}",
                    "sql": current_sql,
                }
            except Exception as e:
                repair_errors.append("database")
                return {
                    "status": "error",
                    "sql": current_sql,
                    "exception": e,
                    "error": str(e),
                    "error_type": "database",
                    "time_issue": current_time_issue,
                    "sql_repair_attempts": repair_attempts,
                    "sql_repair_errors": repair_errors,
                }

    @traceable(
        name="问答链路",
        run_type="chain",
        process_inputs=trim_run_inputs,
        process_outputs=trim_query_outputs,
    )
    def run(
        self,
        user_question: str,
        use_few_shot: bool | None = None,
        use_rules: bool | None = None,
        use_guards: bool | None = None,
        use_indicator_knowledge: bool | None = None,
        use_schema_linking: bool | None = None,
        use_indicator_rag: bool | None = None,
        source_id: str | None = None,
        security_context: UserContext | None = None,
    ) -> dict:
        """
        运行完整链路

        Args:
            user_question: 用户自然语言问题
            use_few_shot: 是否使用 Few-shot
            use_rules: 是否启用业务规则
            use_guards: 是否启用错误防护
            use_indicator_knowledge: 是否启用指标知识注入（关键词匹配，第9课）
            use_schema_linking: 是否启用 Schema Linking 动态注入（第18课）
            use_indicator_rag: 是否启用指标 RAG 语义检索（第19课，替代关键词匹配）
            security_context: 当前请求的权限上下文

        Returns:
            包含 SQL、结果或错误信息的字典
        """
        runtime = self._get_runtime(source_id)
        options = self._resolve_feature_options(
            {
                "use_few_shot": use_few_shot,
                "use_rules": use_rules,
                "use_guards": use_guards,
                "use_indicator_knowledge": use_indicator_knowledge,
                "use_schema_linking": use_schema_linking,
                "use_indicator_rag": use_indicator_rag,
            }
        )
        use_few_shot = options["use_few_shot"]
        use_rules = options["use_rules"]
        use_guards = options["use_guards"]
        use_indicator_knowledge = options["use_indicator_knowledge"]
        use_schema_linking = options["use_schema_linking"]
        use_indicator_rag = options["use_indicator_rag"]
        user_context = security_context or UserContext.demo_admin()

        # 1. 解析问题
        parsed = runtime.parser.parse(user_question)
        if not runtime.parser.validate(parsed):
            return {
                "success": False,
                "error": "输入问题为空",
                "error_type": "validation"
            }

        # 2. 指标知识注入（二选一：RAG 优先，关键词匹配兜底）
        detected_indicators, indicator_block = self._resolve_indicator_context(
            user_question=user_question,
            use_indicator_knowledge=use_indicator_knowledge,
            use_indicator_rag=use_indicator_rag,
            indicator_knowledge=runtime.indicator_knowledge,
        )

        # 3. 构造 Prompt（Schema Linking 在 build_prompt 内部处理）
        system_msg, prompt = build_prompt(
            user_question,
            use_few_shot=use_few_shot,
            use_rules=use_rules,
            use_guards=use_guards,
            indicator_knowledge=indicator_block,
            use_schema_linking=use_schema_linking,
        )

        # 4. 生成 SQL
        try:
            sql = runtime.llm.generate_sql(system_msg, prompt)
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_type": "llm",
                "metadata": {
                    "detected_indicators": detected_indicators,
                    "model": LLM_CONFIG["model"],
                    "used_rules": use_rules,
                    "used_guards": use_guards,
                    "used_indicator_knowledge": use_indicator_knowledge,
                    "used_schema_linking": use_schema_linking,
                    "used_indicator_rag": use_indicator_rag,
                    "source_id": runtime.source_id,
                    "security_role": user_context.role,
                    "security_operator": user_context.operator_name,
                }
            }

        # 4.5 SQL 校验：时间边界缺上界 / 日期年份错误则打回重写（最多一次）
        time_issue = check_time_bounds(user_question, sql) or check_date_year(user_question, sql)
        time_bounds_rewritten = False
        if time_issue:
            try:
                sql = runtime.llm.generate_sql(
                    system_msg,
                    prompt + f"\n\n【SQL 校验打回】{time_issue}\n请修正后重新输出完整 SQL。",
                )
                time_bounds_rewritten = True
            except Exception:
                pass  # 重写失败则保留原 SQL，由执行层/评测暴露问题
            time_issue = check_time_bounds(user_question, sql) or check_date_year(user_question, sql)

        # 5. 执行 SQL（可修复报错时带错误信息让 LLM 重写并有限次重试）
        execution = _drain_execution_events(
            self._execute_sql_with_repair(
                runtime=runtime,
                user_question=user_question,
                sql=sql,
                system_msg=system_msg,
                prompt=prompt,
                user_context=user_context,
                time_issue=time_issue,
            )
        )
        sql = execution["sql"]
        time_issue = execution["time_issue"]
        repair_meta = {
            "sql_repair_attempts": execution["sql_repair_attempts"],
            "sql_repair_errors": execution["sql_repair_errors"],
        }
        base_meta = {
            "detected_indicators": detected_indicators,
            "model": LLM_CONFIG["model"],
            "used_few_shot": use_few_shot,
            "used_rules": use_rules,
            "used_guards": use_guards,
            "used_indicator_knowledge": use_indicator_knowledge,
            "used_schema_linking": use_schema_linking,
            "used_indicator_rag": use_indicator_rag,
            "source_id": runtime.source_id,
            "security_role": user_context.role,
            "security_operator": user_context.operator_name,
            **repair_meta,
        }
        if execution["status"] == "success":
            columns = execution["columns"]
            results = execution["results"]
            db_info = execution["db_info"]
            formatted = runtime.formatter.format(columns, results)
            return {
                "success": True,
                "sql": sql,
                "columns": columns,
                "results": results,
                "formatted": formatted,
                "metadata": {
                    **base_meta,
                    "row_count": len(results),
                    "db_duration_ms": db_info.get("duration_ms"),
                    "db_slow_query": db_info.get("slow_query", False),
                    "db_explain_plan": db_info.get("explain_plan", []),
                    "db_truncated": db_info.get("truncated", False),
                    "time_bounds_rewritten": time_bounds_rewritten,
                    "time_bounds_issue": time_issue,
                },
            }

        e = execution["exception"]
        error_meta = execution.get("error_meta") or {}
        metadata = dict(base_meta)
        if execution["status"] == "query":
            metadata.update(
                {
                    "db_duration_ms": error_meta.get("duration_ms"),
                    "db_error_code": error_meta.get("error_code"),
                    "db_raw_error": error_meta.get("raw_error"),
                }
            )
        return {
            "success": False,
            "sql": sql,
            "error": str(e),
            "error_type": execution["error_type"],
            "metadata": metadata,
        }

    @traceable(
        name="问答链路（流式）",
        run_type="chain",
        process_inputs=trim_run_inputs,
        reduce_fn=reduce_sse,
    )
    def run_stream(
        self,
        user_question: str,
        use_few_shot: bool | None = None,
        use_rules: bool | None = None,
        use_guards: bool | None = None,
        use_indicator_knowledge: bool | None = None,
        use_schema_linking: bool | None = None,
        use_indicator_rag: bool | None = None,
        source_id: str | None = None,
        security_context: UserContext | None = None,
    ) -> Generator[str, None, None]:
        """
        流式运行完整链路，按阶段 yield SSE 事件字符串

        事件类型：
        - sql_chunk: LLM 流式产出的 SQL 片段
        - sql_done: SQL 完整输出 + 执行结果
        - result: 查询结果（columns + rows）
        - error: 异常信息

        每个 yield 的字符串格式为 "event: <type>\\ndata: <json>\\n\\n"，
        可直接作为 SSE 推送内容。

        Args:
            user_question: 用户自然语言问题
            use_few_shot: 是否使用 Few-shot
            use_rules: 是否启用业务规则
            use_guards: 是否启用错误防护
            use_indicator_knowledge: 是否启用指标知识注入（关键词匹配，第9课）
            use_schema_linking: 是否启用 Schema Linking 动态注入（第18课）
            use_indicator_rag: 是否启用指标 RAG 语义检索（第19课）
            security_context: 当前请求的权限上下文

        Yields:
            SSE 格式的事件字符串
        """
        runtime = self._get_runtime(source_id)
        options = self._resolve_feature_options(
            {
                "use_few_shot": use_few_shot,
                "use_rules": use_rules,
                "use_guards": use_guards,
                "use_indicator_knowledge": use_indicator_knowledge,
                "use_schema_linking": use_schema_linking,
                "use_indicator_rag": use_indicator_rag,
            }
        )
        use_few_shot = options["use_few_shot"]
        use_rules = options["use_rules"]
        use_guards = options["use_guards"]
        use_indicator_knowledge = options["use_indicator_knowledge"]
        use_schema_linking = options["use_schema_linking"]
        use_indicator_rag = options["use_indicator_rag"]
        user_context = security_context or UserContext.demo_admin()

        # 1. 解析问题
        parsed = runtime.parser.parse(user_question)
        if not runtime.parser.validate(parsed):
            yield _sse_event("error", {
                "error": "输入问题为空",
                "error_type": "validation"
            })
            return

        # 2. 指标知识注入（二选一：RAG 优先，关键词匹配兜底）
        detected_indicators, indicator_block = self._resolve_indicator_context(
            user_question=user_question,
            use_indicator_knowledge=use_indicator_knowledge,
            use_indicator_rag=use_indicator_rag,
            indicator_knowledge=runtime.indicator_knowledge,
        )

        # 3. 构造 Prompt
        system_msg, prompt = build_prompt(
            user_question,
            use_few_shot=use_few_shot,
            use_rules=use_rules,
            use_guards=use_guards,
            indicator_knowledge=indicator_block,
            use_schema_linking=use_schema_linking,
        )

        # 4. 流式生成 SQL —— 逐 chunk 推送（过滤空内容）
        sql_parts = []
        try:
            for chunk_text in runtime.llm.generate_sql_stream(system_msg, prompt):
                if not chunk_text:
                    continue  # 跳过空 chunk（部分模型会返回空字符串的 delta）
                sql_parts.append(chunk_text)
                yield _sse_event("sql_chunk", {"content": chunk_text})
        except Exception as e:
            yield _sse_event("error", {
                "error": str(e),
                "error_type": "llm",
                "metadata": {
                    "detected_indicators": detected_indicators,
                    "model": LLM_CONFIG["model"],
                    "used_indicator_knowledge": use_indicator_knowledge,
                    "used_schema_linking": use_schema_linking,
                    "used_indicator_rag": use_indicator_rag,
                    "source_id": runtime.source_id,
                    "security_role": user_context.role,
                    "security_operator": user_context.operator_name,
                }
            })
            return

        # 5. 拼接完整 SQL 并清理 markdown 标记
        raw_sql = "".join(sql_parts)
        sql = re.sub(r'```sql|```', '', raw_sql).strip()

        # 5.5 SQL 校验：时间边界缺上界 / 日期年份错误则打回重写（非流式重生成一次）
        time_issue = check_time_bounds(user_question, sql) or check_date_year(user_question, sql)
        time_bounds_rewritten = False
        if time_issue:
            try:
                sql = runtime.llm.generate_sql(
                    system_msg,
                    prompt + f"\n\n【SQL 校验打回】{time_issue}\n请修正后重新输出完整 SQL。",
                )
                time_bounds_rewritten = True
                yield _sse_event("sql_rewrite", {"issue": time_issue, "sql": sql})
            except Exception:
                pass  # 重写失败则保留原 SQL
            time_issue = check_time_bounds(user_question, sql) or check_date_year(user_question, sql)

        yield _sse_event("sql_done", {"sql": sql})

        # 6. 执行 SQL（可修复报错时带错误信息让 LLM 重写并有限次重试）
        execution_gen = self._execute_sql_with_repair(
            runtime=runtime,
            user_question=user_question,
            sql=sql,
            system_msg=system_msg,
            prompt=prompt,
            user_context=user_context,
            time_issue=time_issue,
        )
        execution = None
        while True:
            try:
                _event_type, event_payload = next(execution_gen)
            except StopIteration as stop:
                execution = stop.value
                break
            yield _sse_event("sql_rewrite", event_payload)

        sql = execution["sql"]
        time_issue = execution["time_issue"]
        repair_meta = {
            "sql_repair_attempts": execution["sql_repair_attempts"],
            "sql_repair_errors": execution["sql_repair_errors"],
        }
        base_meta = {
            "detected_indicators": detected_indicators,
            "model": LLM_CONFIG["model"],
            "used_indicator_knowledge": use_indicator_knowledge,
            "used_schema_linking": use_schema_linking,
            "used_indicator_rag": use_indicator_rag,
            "source_id": runtime.source_id,
            "security_role": user_context.role,
            "security_operator": user_context.operator_name,
            **repair_meta,
        }
        if execution["status"] == "success":
            columns = execution["columns"]
            results = execution["results"]
            db_info = execution["db_info"]
            rows_dict = [dict(zip(columns, row)) for row in results]
            yield _sse_event("result", {
                "columns": columns,
                "rows": rows_dict,
                "row_count": len(results),
                "metadata": {
                    "time_bounds_rewritten": time_bounds_rewritten,
                    "time_bounds_issue": time_issue,
                    **base_meta,
                    "db_duration_ms": db_info.get("duration_ms"),
                    "db_slow_query": db_info.get("slow_query", False),
                    "db_explain_plan": db_info.get("explain_plan", []),
                    "db_truncated": db_info.get("truncated", False),
                }
            })
            return

        e = execution["exception"]
        error_meta = execution.get("error_meta") or {}
        metadata = dict(base_meta)
        if execution["status"] == "query":
            metadata.update(
                {
                    "db_duration_ms": error_meta.get("duration_ms"),
                    "db_error_code": error_meta.get("error_code"),
                    "db_raw_error": error_meta.get("raw_error"),
                }
            )
        yield _sse_event("error", {
            "error": str(e),
            "error_type": execution["error_type"],
            "sql": sql,
            "metadata": metadata,
        })


def _sse_event(event_type: str, data: dict) -> str:
    """构造 SSE 格式的事件字符串"""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False, default=_json_serializer)}\n\n"


def _drain_execution_events(gen: Generator) -> dict:
    """排干 _execute_sql_with_repair：非流式路径丢弃中间重写事件，取回最终结果。"""
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def _json_serializer(obj):
    """JSON 序列化补充：处理 Decimal、日期等非标准类型"""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime.datetime):
        return obj.isoformat(sep=" ", timespec="seconds")
    if isinstance(obj, datetime.date):
        return obj.isoformat()
    if isinstance(obj, datetime.time):
        return obj.isoformat(timespec="seconds")
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def main():
    """命令行入口"""
    system = ChatBISystem()

    print("=" * 60)
    print("ChatBI Text2SQL 系统")
    print("=" * 60)

    # 命令行模式：直接传入问题
    if len(sys.argv) > 1:
        question = " ".join(sys.argv[1:])
        result = system.run(question)
        _print_result(question, result)
        return

    # 交互模式
    print("\n请输入问题（输入 exit / quit / q 退出）：")
    while True:
        try:
            question = input("\n> ")
            if question.strip().lower() in ["exit", "quit", "q"]:
                break
            result = system.run(question)
            _print_result(question, result)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"系统错误：{e}")

    print("\n感谢使用！")


def _print_result(question: str, result: dict):
    """打印执行结果"""
    print(f"\n问题：{question}")
    print(f"SQL：{result.get('sql', '')}")
    if result["success"]:
        print(f"\n{result['formatted']}")
    else:
        print(f"\n错误：{result['error']}")


if __name__ == "__main__":
    main()
