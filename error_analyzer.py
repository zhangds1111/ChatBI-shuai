"""
错误分析模块

负责分类 SQL 生成错误类型，提供错误根因分析与修复建议。
该模块在第7课引入，用于系统性地收集、分类和追踪 Text2SQL 系统的错误模式。
"""

import re
from datetime import date
from typing import Optional

from tracing import traceable, trim_check_outputs


class ErrorAnalyzer:
    """SQL 生成错误分析器"""

    # 错误类型常量
    FIELD_ERROR = "field_error"           # 字段选择错误
    JOIN_ERROR = "join_error"             # 关联路径错误
    TIME_ERROR = "time_error"             # 时间计算错误
    FILTER_ERROR = "filter_error"         # 过滤条件遗漏
    AGGREGATION_ERROR = "aggregation_error"  # 聚合逻辑错误
    SYNTAX_ERROR = "syntax_error"         # SQL 语法错误
    UNKNOWN = "unknown"                   # 未知错误

    def __init__(self):
        self.error_patterns = {
            self.FIELD_ERROR: [
                r"gross_amount",  # 错误使用含税金额
                r"standard_cost",  # 错误使用标准成本替代实际成本
            ],
            self.JOIN_ERROR: [
                r"exchange_rates",  # 关联汇率表的字段或条件错误
                r"JOIN.*ON.*=",     # 通用 Join 问题检测
            ],
            self.TIME_ERROR: [
                r"DATE_SUB",        # 时间函数使用问题
                r"CURDATE",         # 当前日期引用问题
            ],
            self.FILTER_ERROR: [
                r"order_status",    # 过滤条件相关
            ],
            self.AGGREGATION_ERROR: [
                r"GROUP BY",        # 聚合维度问题
                r"SUM\(|AVG\(|COUNT\(",  # 聚合函数问题
            ],
        }

    def categorize_error(
        self,
        sql: str,
        error_msg: Optional[str] = None,
        question: Optional[str] = None
    ) -> dict:
        """
        对 SQL 错误进行分类

        Args:
            sql: LLM 生成的 SQL 语句
            error_msg: 数据库执行返回的错误信息（如有）
            question: 用户的原始问题

        Returns:
            包含错误类型、根因分析和修复建议的字典
        """
        result = {
            "error_type": self.UNKNOWN,
            "root_cause": "",
            "suggestion": "",
            "sql": sql
        }

        # 如果有数据库执行错误，优先判断语法类错误
        if error_msg:
            if self._is_syntax_error(error_msg):
                result["error_type"] = self.SYNTAX_ERROR
                result["root_cause"] = f"SQL 语法错误：{error_msg}"
                result["suggestion"] = "检查 SQL 语句中的关键字拼写、括号匹配和引号使用，必要时在 Prompt 中增加语法约束。"
                return result

        # 基于 SQL 内容和问题语义进行启发式分类
        detected_types = self._detect_error_types(sql, question)

        if detected_types:
            primary_type = detected_types[0]
            result["error_type"] = primary_type
            result["root_cause"] = self._get_root_cause(primary_type, sql, question)
            result["suggestion"] = self._get_suggestion(primary_type)
        else:
            result["root_cause"] = "无法从 SQL 中直接识别错误模式，需人工审查"
            result["suggestion"] = "将该用例加入 Few-shot 示例或增强 Schema 描述中的相关字段说明"

        return result

    def _is_syntax_error(self, error_msg: str) -> bool:
        """判断是否为 SQL 语法错误"""
        syntax_keywords = [
            "syntax error", "parse error", "unexpected",
            "near", "expected", "invalid"
        ]
        return any(kw in error_msg.lower() for kw in syntax_keywords)

    def _detect_error_types(
        self,
        sql: str,
        question: Optional[str] = None
    ) -> list[str]:
        """基于启发式规则检测可能的错误类型"""
        detected = []
        sql_lower = sql.lower()

        # 字段选择错误检测
        if self._check_field_error(sql_lower, question):
            detected.append(self.FIELD_ERROR)

        # Join 错误检测
        if self._check_join_error(sql_lower, question):
            detected.append(self.JOIN_ERROR)

        # 时间错误检测
        if self._check_time_error(sql_lower, question):
            detected.append(self.TIME_ERROR)

        # 过滤遗漏检测
        if self._check_filter_error(sql_lower, question):
            detected.append(self.FILTER_ERROR)

        # 聚合错误检测
        if self._check_aggregation_error(sql_lower, question):
            detected.append(self.AGGREGATION_ERROR)

        return detected

    def _check_field_error(self, sql: str, question: Optional[str]) -> bool:
        """检测字段选择错误"""
        # 问题问销售额但 SQL 误用 tax_amount / shipping_amount 口径
        if question and ("收入" in question or "销售额" in question):
            if ("tax_amount" in sql or "shipping_amount" in sql) and "item_amount" not in sql:
                return True
        # 问题问成本但 SQL 只取采购成本、漏了头程运费
        if question and "成本" in question:
            if "unit_cost" in sql and "head_freight_cost" not in sql:
                return True
        return False

    def _check_join_error(self, sql: str, question: Optional[str]) -> bool:
        """检测 Join 关联错误"""
        # 涉及多币种金额但未关联汇率表
        if question and ("收入" in question or "销售额" in question):
            if "exchange_rates" not in sql and "currency" in sql:
                return True
        # 涉及站点/品类维度但未关联对应维度表
        if question and ("站点" in question or "品类" in question or "商品" in question):
            if "dim_stores" not in sql and "dim_products" not in sql:
                return True
        return False

    def _check_time_error(self, sql: str, question: Optional[str]) -> bool:
        """检测时间计算错误"""
        if question and ("最近" in question or "上个" in question or "本月" in question):
            # 未使用日期函数处理动态时间
            if "DATE_SUB" not in sql and "CURDATE" not in sql:
                return True
        return False

    def _check_filter_error(self, sql: str, question: Optional[str]) -> bool:
        """检测过滤条件遗漏"""
        # 统计订单但未过滤状态
        if question and ("订单" in question or "收入" in question or "销售额" in question):
            if "order_status" not in sql:
                return True
        return False

    def _check_aggregation_error(self, sql: str, question: Optional[str]) -> bool:
        """检测聚合逻辑错误"""
        # 使用了聚合函数但没有 GROUP BY
        has_agg = bool(re.search(r'sum\(|avg\(|count\(|max\(|min\(', sql))
        has_groupby = "group by" in sql
        if has_agg and not has_groupby:
            # 检查 SELECT 中是否有非聚合字段（简单启发式）
            select_part = sql.split("from")[0] if "from" in sql else sql
            non_agg_fields = re.findall(r'select\s+(.*?)\s+from', sql, re.DOTALL)
            if non_agg_fields:
                fields = non_agg_fields[0].split(",")
                # 如果有多于一个字段且没有 GROUP BY，可能存在聚合错误
                if len(fields) > 1:
                    return True
        return False

    def _get_root_cause(
        self,
        error_type: str,
        sql: str,
        question: Optional[str]
    ) -> str:
        """根据错误类型生成根因描述"""
        causes = {
            self.FIELD_ERROR: "模型未能正确理解业务语义，选择了错误的字段",
            self.JOIN_ERROR: "模型遗漏了必要的表关联或使用了错误的关联条件",
            self.TIME_ERROR: "时间范围计算不符合业务预期，动态时间边界处理有误",
            self.FILTER_ERROR: "遗漏了关键的业务过滤条件，导致统计范围不正确",
            self.AGGREGATION_ERROR: "聚合维度与 SELECT 字段不匹配，或聚合逻辑有误",
            self.SYNTAX_ERROR: "生成的 SQL 存在语法问题，无法被数据库解析",
        }
        return causes.get(error_type, "未知原因")

    def _get_suggestion(self, error_type: str) -> str:
        """根据错误类型生成修复建议"""
        suggestions = {
            self.FIELD_ERROR: "在 RULES 中强化字段语义定义，增加字段选择错误的 Few-shot 负例",
            self.JOIN_ERROR: "在 ERROR_GUARDS 中补充 Join 路径检查清单，增加多表查询 Few-shot 示例",
            self.TIME_ERROR: "增加时间计算相关的 Few-shot 示例，明确动态时间边界的 SQL 写法",
            self.FILTER_ERROR: "在 RULES 中明确各类查询的强制过滤条件，增加过滤遗漏的负例",
            self.AGGREGATION_ERROR: "增加 GROUP BY 正确用法的示例，强调 SELECT 非聚合字段必须出现在 GROUP BY 中",
            self.SYNTAX_ERROR: "检查 Prompt 中是否有格式混乱导致模型输出不完整 SQL 的情况",
        }
        return suggestions.get(error_type, "人工审查该用例并补充到测试集")

    def analyze_batch(
        self,
        cases: list[dict]
    ) -> list[dict]:
        """
        批量分析多个错误案例

        Args:
            cases: 每个元素为 {"question": str, "sql": str, "error_msg": str} 的字典列表

        Returns:
            分析结果列表
        """
        results = []
        for case in cases:
            result = self.categorize_error(
                sql=case.get("sql", ""),
                error_msg=case.get("error_msg"),
                question=case.get("question")
            )
            result["question"] = case.get("question", "")
            results.append(result)
        return results

    def generate_report(self, results: list[dict]) -> str:
        """
        生成错误分析汇总报告

        Args:
            results: analyze_batch 返回的结果列表

        Returns:
            格式化的文本报告
        """
        from collections import Counter
        types = [r["error_type"] for r in results]
        type_counts = Counter(types)

        lines = [
            "=" * 50,
            "SQL 生成错误分析汇总报告",
            "=" * 50,
            f"总案例数：{len(results)}",
            "",
            "错误类型分布：",
        ]
        for err_type, count in type_counts.most_common():
            lines.append(f"  {err_type}: {count} 例")

        lines.extend(["", "详细案例："])
        for i, r in enumerate(results, 1):
            lines.append(f"\n[{i}] 问题：{r.get('question', 'N/A')}")
            lines.append(f"    SQL：{r['sql'][:80]}...")
            lines.append(f"    类型：{r['error_type']}")
            lines.append(f"    根因：{r['root_cause']}")
            lines.append(f"    建议：{r['suggestion']}")

        return "\n".join(lines)


# ==================== SQL 校验：时间边界 ====================
# 封闭时间区间词（time_rules.json 的 must_have_both_bounds）：
# 必须同时有下界和上界，只有下界会把当期数据一起算进去。
_CLOSED_TIME_TERMS = ("上个月", "上月", "上周", "上季度", "去年")

# 日期字面量 / 日期函数调用，用于识别与时间字段比较的边界
_DATE_EXPR = (
    r"(?:CURDATE\s*\(|DATE_SUB\s*\(|DATE_ADD\s*\(|DATE_FORMAT\s*\(|"
    r"MAKEDATE\s*\(|STR_TO_DATE\s*\(|'\d{4}-\d{2}-\d{2}')"
)


@traceable(name="时间边界校验", run_type="tool", process_outputs=trim_check_outputs)
def check_time_bounds(question: Optional[str], sql: Optional[str]) -> Optional[str]:
    """校验封闭时间区间的上下界完整性。

    问题含「上个月/上周/上季度/去年」等封闭区间词，而 SQL 只有下界
    （如 ``>= DATE_SUB(...)``）没有上界时，返回问题描述，供调用方打回
    LLM 重写；合规或与时间无关时返回 None。
    """
    if not question or not sql:
        return None
    hit = next((t for t in _CLOSED_TIME_TERMS if t in question), None)
    if not hit:
        return None

    # BETWEEN a AND b 自带上下界
    if re.search(rf"BETWEEN\s+{_DATE_EXPR}\s+AND\s+{_DATE_EXPR}", sql, re.IGNORECASE):
        return None

    lower = re.search(rf"(?:>=|>)\s*{_DATE_EXPR}", sql, re.IGNORECASE) or re.search(
        rf"{_DATE_EXPR}\s*(?:>=|>)", sql, re.IGNORECASE
    )
    # < 的上界匹配需排除 <> 与 <= 之外的比较；<= 合法，<> 不算边界
    upper = re.search(rf"(?:<=|<(?!>))\s*{_DATE_EXPR}", sql, re.IGNORECASE) or re.search(
        rf"{_DATE_EXPR}\s*(?:<=|<(?!>))", sql, re.IGNORECASE
    )
    if lower and not upper:
        return (
            f"问题中的「{hit}」是封闭时间区间，SQL 只有下界没有上界。"
            f"必须同时写下界和上界（例如「上个月」："
            f">= DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 MONTH), '%Y-%m-01') "
            f"AND < DATE_FORMAT(CURDATE(), '%Y-%m-01')），"
            f"不能只写 >= DATE_SUB(CURDATE(), INTERVAL 1 MONTH) 这类滚动下界。"
        )
    return None


# ==================== SQL 校验：日期字面量年份 ====================
# 「9月」「8月」这类不带年份的月份，以及「上个月」，LLM 没有当前日期锚点时
# 会把年份猜错（如 2026 年问「9月」却生成 2025-09 的条件）。此校验按问题语义
# 推算出合法年份集合，不在集合内的日期字面量即打回重写。

_YEAR_LITERAL_RE = re.compile(r"'(20\d{2})-(\d{2})-(\d{2})'")
_YEAR_IN_QUESTION_RE = re.compile(r"(20\d{2})\s*年?")
_MONTH_IN_QUESTION_RE = re.compile(r"(?<![\d个])(\d{1,2})\s*月")


@traceable(name="日期年份校验", run_type="tool", process_outputs=trim_check_outputs)
def check_date_year(
    question: Optional[str], sql: Optional[str], today: Optional[date] = None
) -> Optional[str]:
    """校验 SQL 中硬编码日期字面量的年份是否符合问题语义。

    规则（``today`` 可注入以便测试）：
    - 问题写了明确年份（如「2025年9月」）→ 允许该年及其下界跨年的 +1 年
    - 问题含「去年」→ 允许去年、今年（12月下界会写到次年 1 月 1 日）
    - 问题含「上个月/上月」→ 允许上个自然月所在年（及其 +1 年跨年边界）
    - 问题含「N月」（不带年份）→ 允许「最近一个已过完的该月」所在年（及其 +1 年）
    - 问题无任何时间语义 → 不校验；SQL 全部用日期函数（无字面量）→ 不校验

    返回问题描述（供打回重写）或 None。
    """
    if not question or not sql:
        return None
    literals = _YEAR_LITERAL_RE.findall(sql)
    if not literals:
        return None  # 全动态日期，不猜年份，无从判错

    today = today or date.today()
    cur_year, cur_month = today.year, today.month

    allowed: set[int] = set()
    expected_desc = []

    explicit_years = sorted({int(y) for y in _YEAR_IN_QUESTION_RE.findall(question)})
    if explicit_years:
        for y in explicit_years:
            allowed.update({y, y + 1})  # y+1：12 月的上界会写成次年 1 月 1 日
            expected_desc.append(f"{y} 年")
    else:
        if "去年" in question:
            allowed.update({cur_year - 1, cur_year})
            expected_desc.append(f"去年（{cur_year - 1} 年）")
        if "上个月" in question or "上月" in question:
            last_month_year = cur_year - 1 if cur_month == 1 else cur_year
            allowed.update({last_month_year, last_month_year + 1})
            expected_desc.append("上个自然月")
        for m_raw in _MONTH_IN_QUESTION_RE.findall(question):
            month = int(m_raw)
            if not 1 <= month <= 12:
                continue
            # 最近一个已过完（或正在经历）的该月份
            month_year = cur_year if cur_month >= month else cur_year - 1
            allowed.update({month_year, month_year + 1})
            expected_desc.append(f"{month} 月（{month_year} 年）")

    if not allowed:
        return None  # 问题无时间语义，不拦

    bad_years = sorted({int(y) for y, _, _ in literals if int(y) not in allowed})
    if not bad_years:
        return None

    found = sorted({f"{y}-{m}-{d}" for y, m, d in literals if int(y) in bad_years})
    return (
        f"SQL 中硬编码的日期字面量年份与问题语义不符："
        f"问题时间应为 {'、'.join(expected_desc)}（今天是 {today.isoformat()}），"
        f"但 SQL 出现了 {found[0]} 等 {bad_years[0]} 年的日期"
        f"{'、' + found[-1] if len(found) > 1 else ''}。"
        f"请按正确的年份重新生成日期条件"
        f"（或改用 CURDATE()/DATE_FORMAT 等日期函数动态计算）。"
    )
