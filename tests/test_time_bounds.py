# -*- coding: utf-8 -*-
"""SQL 时间边界校验（check_time_bounds / check_date_year）单测"""
from datetime import date

from error_analyzer import check_date_year, check_time_bounds


Q = "上个月卖了多少钱?"

BAD_SQL = (
    "SELECT SUM(item_amount) FROM amz_order_items "
    "WHERE purchase_date >= DATE_SUB(CURDATE(), INTERVAL 1 MONTH)"
)
GOOD_SQL = (
    "SELECT SUM(item_amount) FROM amz_order_items "
    "WHERE purchase_date >= DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 MONTH), '%Y-%m-01') "
    "AND purchase_date < DATE_FORMAT(CURDATE(), '%Y-%m-01')"
)
BETWEEN_SQL = (
    "SELECT SUM(item_amount) FROM amz_order_items "
    "WHERE purchase_date BETWEEN '2026-09-01' AND '2026-09-30'"
)


def test_lower_bound_only_is_flagged():
    assert check_time_bounds(Q, BAD_SQL) is not None


def test_both_bounds_ok():
    assert check_time_bounds(Q, GOOD_SQL) is None


def test_between_has_both_bounds():
    assert check_time_bounds(Q, BETWEEN_SQL) is None


def test_non_time_question_ignored():
    assert check_time_bounds("各站点销售额", BAD_SQL) is None


def test_other_terms():
    assert check_time_bounds("上周的销售额", BAD_SQL) is not None
    assert check_time_bounds("去年的利润", BAD_SQL) is not None
    assert check_time_bounds("上季度广告花费", BAD_SQL) is not None


# ==================== check_date_year ====================

TODAY = date(2026, 10, 19)


def test_wrong_year_is_flagged():
    # 2026-10 问「9月」，SQL 却写死 2025 年 → 打回
    sql = "SELECT SUM(item_amount) FROM amz_order_items WHERE purchase_date >= '2025-09-01' AND purchase_date < '2025-10-01'"
    assert check_date_year("9月销售额比8月涨了多少?", sql, today=TODAY) is not None


def test_correct_year_passes():
    sql = "SELECT SUM(item_amount) FROM amz_order_items WHERE purchase_date >= '2026-09-01' AND purchase_date < '2026-10-01'"
    assert check_date_year("9月销售额比8月涨了多少?", sql, today=TODAY) is None


def test_month_before_current_month_uses_last_year():
    # 10 月问「2月」→ 最近一个已过完的 2 月是 2026-02（当前月 >= 2）
    sql_ok = "SELECT SUM(x) FROM t WHERE d >= '2026-02-01' AND d < '2026-03-01'"
    assert check_date_year("2月的销售额", sql_ok, today=TODAY) is None
    # 2 月问「9月」→ 最近一个已过完的 9 月是 2025-09
    sql_prev = "SELECT SUM(x) FROM t WHERE d >= '2025-09-01' AND d < '2025-10-01'"
    assert check_date_year("9月的销售额", sql_prev, today=date(2026, 2, 10)) is None


def test_explicit_year_wins():
    sql = "SELECT SUM(x) FROM t WHERE d >= '2025-09-01' AND d < '2025-10-01'"
    assert check_date_year("2025年9月的销售额", sql, today=TODAY) is None


def test_cross_year_upper_bound_allowed():
    # 2025年12月的上界是 2026-01-01，不应误杀
    sql = "SELECT SUM(x) FROM t WHERE d >= '2025-12-01' AND d < '2026-01-01'"
    assert check_date_year("2025年12月的销售额", sql, today=TODAY) is None


def test_dynamic_sql_passes():
    sql = "SELECT SUM(x) FROM t WHERE d >= DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 MONTH), '%Y-%m-01') AND d < DATE_FORMAT(CURDATE(), '%Y-%m-01')"
    assert check_date_year("上个月的销售额", sql, today=TODAY) is None


def test_no_time_semantics_skipped():
    sql = "SELECT marketplace, SUM(x) FROM t WHERE d >= '2025-01-01' GROUP BY marketplace"
    assert check_date_year("各站点销售额", sql, today=TODAY) is None
