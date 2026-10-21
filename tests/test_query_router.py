"""query_router 路由规则与覆盖度质检的单元测试。"""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from query_router import (
    CONFIDENCE_EXPLICIT,
    CONFIDENCE_SIMPLE,
    CONFIDENCE_STRONG,
    CONFIDENCE_UNCERTAIN,
    default_coverage_judge,
    route,
)


# ---------------------------------------------------------------------------
# 第 0 层：显式覆盖（逃生口）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["simple", "agent"])
def test_explicit_mode_overrides_rules(mode: str):
    # 即使问题特征与 mode 相反，也必须听 mode 的
    question = "上月销售额为什么下降" if mode == "simple" else "上月销售额是多少"
    decision = route(question, mode=mode)
    assert decision.target == mode
    assert decision.confidence == CONFIDENCE_EXPLICIT
    assert "mode_override" in decision.signals


def test_invalid_mode_rejected():
    with pytest.raises(ValueError):
        route("上月销售额是多少", mode="whatever")


def test_empty_question_rejected():
    with pytest.raises(ValueError):
        route("   ")


# ---------------------------------------------------------------------------
# 第 1 层：强复杂信号 → agent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question,expected_signal",
    [
        ("最近三个月利润为什么下降", "归因分析"),
        ("广告花费是什么原因导致上升的", "归因分析"),
        ("对比一下美国和英国站点上月销售额的差异原因", "对比归因"),
        ("把上月销售下滑拆解一下", "多步拆解"),
        ("分析下退货率上升的原因", "分析+分析对象"),
        ("库存积压严重，我该怎么办", "决策建议"),
        ("各站点销售额下降的原因分别是什么", "分组归因"),
    ],
)
def test_complex_questions_route_to_agent(question: str, expected_signal: str):
    decision = route(question)
    assert decision.target == "agent"
    assert decision.confidence == CONFIDENCE_STRONG
    assert expected_signal in decision.signals


def test_multi_question_marks_route_to_agent():
    decision = route("上月销售额是多少？广告花费是多少？")
    assert decision.target == "agent"
    assert "多问句" in decision.signals


# ---------------------------------------------------------------------------
# 第 1 层：强查询信号 → simple（高置信）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "上月销售额是多少",
        "上月销量排名前10的ASIN有哪些",
        "ACME-US 店铺有多少个在售商品",
        "统计一下上月各站点的订单数",
        "上月成交订单总数",
    ],
)
def test_lookup_questions_route_to_simple_with_high_confidence(question: str):
    decision = route(question)
    assert decision.target == "simple"
    assert decision.confidence == CONFIDENCE_SIMPLE


# ---------------------------------------------------------------------------
# 不确定带：simple + 低置信（交给覆盖度质检兜底）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        "帮我看看最近的销售情况",
        "最近卖得怎么样",
        "近7天销售额趋势",
    ],
)
def test_uncertain_questions_route_to_simple_with_low_confidence(question: str):
    decision = route(question)
    assert decision.target == "simple"
    assert decision.confidence == CONFIDENCE_UNCERTAIN


# ---------------------------------------------------------------------------
# 第 2 层：覆盖度质检
# ---------------------------------------------------------------------------


def test_judge_flags_scalar_result_for_analysis_question():
    reason = default_coverage_judge("近7天销售额趋势", ["sales"], [(100.0,)])
    assert reason is not None
    assert "分析信号" in reason


def test_judge_passes_multi_row_result_for_analysis_question():
    # 趋势问题返回多行序列 → 已覆盖，不升级
    rows = [(d, 100.0) for d in range(7)]
    assert default_coverage_judge("近7天销售额趋势", ["day", "sales"], rows) is None


def test_judge_passes_multi_column_result():
    # 对比问题返回一行多列 → 已覆盖
    assert (
        default_coverage_judge("对比美国和英国的销售额", ["us_sales", "uk_sales"], [(1, 2)])
        is None
    )


def test_judge_passes_plain_lookup_scalar():
    # 普通查询问题返回单值是正常答案，不升级
    assert default_coverage_judge("上月销售额是多少", ["sales"], [(100.0,)]) is None


def test_judge_passes_when_no_analysis_signal():
    assert default_coverage_judge("帮我看看最近的销售情况", ["sales"], [(100.0,)]) is None
