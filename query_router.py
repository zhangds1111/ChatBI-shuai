"""问题路由：决定一条自然语言问题走「直接查询」还是「多步分析」链路。

设计（见讨论结论）：
- 统一入口 + 自动路由为主，显式 mode 覆盖为辅；不要求用户提问前打标签。
- 第 0 层：mode 显式指定（simple/agent）→ 直接派发，作为用户逃生口。
- 第 1 层：规则快判（零成本、确定性、可测试）。
    - 强复杂信号（归因/多问句/拆解/建议等）→ agent，高置信。
    - 强查询信号（多少/排名/列出等）且无分析词 → simple，高置信。
    - 其余 → simple 低置信（不确定带）。
- 第 2 层（不确定带补救）：快速路径跑完后用 default_coverage_judge 做覆盖度质检，
    问题含分析信号但结果只有一行汇总 → 判定未覆盖，由 API 层升级到 agent。

全部为纯函数，不依赖 LLM；若后续规则维护不动，可把第 1 层换成轻量 LLM 分类，
route()/RoutingDecision 的契约保持不变。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

RoutingTarget = Literal["simple", "agent"]
RoutingMode = Literal["auto", "simple", "agent"]

# 置信度约定
CONFIDENCE_EXPLICIT = 1.0   # 用户显式指定
CONFIDENCE_STRONG = 0.9     # 强规则命中
CONFIDENCE_SIMPLE = 0.85    # 强查询信号命中
CONFIDENCE_UNCERTAIN = 0.5  # 不确定带：先走快速路径，由覆盖度质检兜底
# 低于该置信度的 simple 结果才做覆盖度质检（高置信查询不付这笔成本）
ESCALATION_CONFIDENCE_THRESHOLD = 0.75

# 强复杂信号：命中即走多步分析
_COMPLEX_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"为什么|为何|原因|归因|导致|影响因素|驱动因素|怎么造成|怎么回事", "归因分析"),
    (r"(对比|比较).{0,24}(原因|为什么|差异|变化|趋势|波动)", "对比归因"),
    (r"拆解|拆分|逐步分析|多步|全流程|链路|漏斗|一步步", "多步拆解"),
    (r"(分析|评估|诊断|洞察|研判).{0,24}(原因|因素|驱动|变化|趋势|波动|结构|贡献|占比)", "分析+分析对象"),
    (r"(建议|该怎么办|如何改进|怎样优化|优化方案|应对策略|制定策略)", "决策建议"),
    (r"(每个|各个|各|分别).{0,12}(原因|贡献|影响|驱动)", "分组归因"),
)

# 强查询信号：典型的单条 SQL 可答的指标查询
_SIMPLE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"多少|几\s*个|哪些|哪个|什么|是谁|何时|哪里|几个", "疑问词查询"),
    (r"列出|查询|显示|统计|汇总|求和|平均|总数|合计|明细|清单", "查询动词"),
    (r"(排名|排行|top\s*\d*|前\s*\d+\s*(名|个|位)?)", "排名类查询"),
)

# 弱分析词：单独出现不足以判定复杂，但其分析意图（形状类：要序列/多行对比）
# 无法被一个标量数值回答；不确定带走快速路径后，若结果只有单标量则触发升级。
ANALYSIS_SIGNALS: tuple[str, ...] = (
    "趋势", "走势", "变化", "波动", "对比", "比较", "差异", "涨跌", "增减", "走向",
)

_MODE_VALUES = {"auto", "simple", "agent"}


@dataclass(slots=True)
class RoutingDecision:
    """一次路由决策。"""

    target: RoutingTarget
    reason: str
    confidence: float
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "routed_to": self.target,
            "reason": self.reason,
            "confidence": self.confidence,
            "signals": self.signals,
        }


def route(question: str, mode: str = "auto") -> RoutingDecision:
    """对问题做路由决策。

    Args:
        question: 用户自然语言问题
        mode: auto=自动路由；simple/agent=显式覆盖（逃生口）

    Returns:
        RoutingDecision

    Raises:
        ValueError: mode 非法或问题为空
    """
    if mode not in _MODE_VALUES:
        raise ValueError(f"非法的 mode: {mode}，仅支持 auto / simple / agent")
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")

    # 第 0 层：显式覆盖
    if mode in ("simple", "agent"):
        return RoutingDecision(
            target=mode,
            reason=f"用户显式指定 mode={mode}",
            confidence=CONFIDENCE_EXPLICIT,
            signals=["mode_override"],
        )

    # 第 1 层：强复杂信号
    complex_signals: list[str] = []
    for pattern, label in _COMPLEX_PATTERNS:
        if re.search(pattern, question, flags=re.IGNORECASE):
            complex_signals.append(label)
    question_marks = question.count("？") + question.count("?")
    if question_marks >= 2:
        complex_signals.append("多问句")
    if complex_signals:
        return RoutingDecision(
            target="agent",
            reason=f"复杂分析信号：{'、'.join(complex_signals)}",
            confidence=CONFIDENCE_STRONG,
            signals=complex_signals,
        )

    simple_signals = [
        label
        for pattern, label in _SIMPLE_PATTERNS
        if re.search(pattern, question, flags=re.IGNORECASE)
    ]
    weak_signals = [f"分析类词:{token}" for token in ANALYSIS_SIGNALS if token in question]

    # 强查询信号且不带分析词 → 高置信 simple（不做覆盖度质检）
    if simple_signals and not weak_signals:
        return RoutingDecision(
            target="simple",
            reason=f"明确的指标查询：{'、'.join(simple_signals)}",
            confidence=CONFIDENCE_SIMPLE,
            signals=simple_signals,
        )

    # 不确定带：先走快速路径，结果由覆盖度质检兜底
    return RoutingDecision(
        target="simple",
        reason="无法明确判定问题类型，先走快速路径（结果不完整会自动升级）",
        confidence=CONFIDENCE_UNCERTAIN,
        signals=simple_signals + weak_signals or ["fallback"],
    )


def default_coverage_judge(
    question: str,
    columns: Sequence[str],
    rows: Sequence[Any],
) -> str | None:
    """覆盖度质检（第 2 层）：快速路径结果是否足以回答问题。

    仅对不确定带（低置信）的路由结果调用。返回升级原因；返回 None 表示结果已覆盖。

    判定规则（纯启发式，可解释）：
    - 问题含形状类分析信号（趋势/变化/对比/波动…——这些意图需要多行序列或多行对比），
      且结果只有一个标量（单行单列）→ 一个数值回答不了，升级多步分析。
    - 其余情况视为已覆盖（查询类问题返回单值/多列/多行都是正常答案）。
    """
    signals = [token for token in ANALYSIS_SIGNALS if token in (question or "")]
    if not signals:
        return None
    row_count = len(rows) if rows is not None else 0
    column_count = len(columns) if columns is not None else 0
    if row_count <= 1 and column_count <= 1:
        return (
            f"问题含分析信号（{'、'.join(signals[:3])}），"
            f"但结果只有一个标量数值，疑似未覆盖分析意图"
        )
    return None
