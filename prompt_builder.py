"""
Prompt 构造模块

负责将 Schema 信息、Few-shot 示例和用户问题组装为完整 Prompt。
Schema 和示例在此集中维护，便于后续课程中动态扩展。

第7课增强：新增 RULES（业务规则注入层）与 ERROR_GUARDS（错误防护层），
通过 build_prompt 的可选参数控制是否注入，实现 Prompt 策略的灵活切换。
"""


# ==================== Schema 定义 ====================
# 业务背景：跨境电商（Amazon 多站点）。Schema 从 knowledge_loader 元数据渲染，
# 修改 table_metadata.json / field_metadata.json 后此处自动同步。
from knowledge_loader import FIELD_METADATA, TABLE_METADATA  # noqa: E402
from tracing import traceable, trim_prompt_outputs, trim_schema_outputs

_TABLE_ORDER = [
    "dim_stores",
    "dim_products",
    "exchange_rates",
    "amz_order_items",
    "amz_ads_daily",
    "amz_traffic_daily",
    "amz_fba_inventory",
    "amz_settlement",
    "amz_returns",
]


def _render_schema() -> str:
    """把表/字段元数据渲染成 Prompt 可读的 Schema 文本。"""
    blocks = []
    for table in _TABLE_ORDER:
        meta = TABLE_METADATA.get(table, {})
        title = meta.get("table_comment") or meta.get("domain") or ""
        fields = [
            key for key, fm in FIELD_METADATA.items()
            if fm.get("table") == table
        ]
        header = f"表：{table}（{title}）" if title else f"表：{table}"
        lines_out = [header]
        for key in fields:
            fm = FIELD_METADATA[key]
            field = fm.get("field", key.split(".", 1)[-1])
            ftype = (fm.get("type") or "").upper()
            desc = fm.get("comment") or fm.get("description") or ""
            pk = "（主键）" if fm.get("is_primary_key") else ""
            lines_out.append(f"- {field} {ftype} {desc}{pk}".rstrip())
        blocks.append("\n".join(lines_out))
    return "\n\n".join(blocks)


SCHEMA = _render_schema()


# ==================== 时间词规则（time_rules.json）====================
import json as _json
import os as _os
from datetime import date

_TIME_RULES_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "time_rules.json")
with open(_TIME_RULES_PATH, encoding="utf-8") as _f:
    TIME_RULES = _json.load(_f)
TIME_RULES_SNIPPET = TIME_RULES["prompt_snippet"]


# ==================== Few-shot 示例 ====================
FEW_SHOT_EXAMPLES = """
示例1：
问题：上个月一共出了多少单
SQL：SELECT COUNT(DISTINCT order_id) AS order_count FROM amz_order_items WHERE order_status NOT IN ('Canceled', 'Pending') AND purchase_date >= DATE_FORMAT(DATE_SUB(CURDATE(), INTERVAL 1 MONTH), '%Y-%m-01') AND purchase_date < DATE_FORMAT(CURDATE(), '%Y-%m-01');

示例2：
问题：最近三个月各月销售额（人民币）
SQL：SELECT
    DATE_FORMAT(o.purchase_date, '%Y-%m-01') AS month,
    SUM(o.item_amount * r.rate_to_cny) AS sales_cny
FROM amz_order_items o
JOIN exchange_rates r
  ON o.purchase_date = r.rate_date
 AND o.currency = r.currency
WHERE o.order_status NOT IN ('Canceled', 'Pending')
  AND o.purchase_date >= DATE_SUB(CURDATE(), INTERVAL 3 MONTH)
  AND o.purchase_date < CURDATE() + INTERVAL 1 DAY
GROUP BY DATE_FORMAT(o.purchase_date, '%Y-%m-01')
ORDER BY month;

示例3：
问题：各站点的销售额和销量
SQL：SELECT
    s.marketplace,
    SUM(o.item_amount * r.rate_to_cny) AS sales_cny,
    SUM(o.quantity) AS units
FROM amz_order_items o
JOIN dim_stores s ON o.store_id = s.store_id
JOIN exchange_rates r
  ON o.purchase_date = r.rate_date
 AND o.currency = r.currency
WHERE o.order_status NOT IN ('Canceled', 'Pending')
GROUP BY s.marketplace;

示例4：
问题：各广告活动的ACOS
SQL：SELECT
    campaign_name,
    SUM(a.spend * r.rate_to_cny) / NULLIF(SUM(a.sales_7d * r.rate_to_cny), 0) * 100 AS acos_pct
FROM amz_ads_daily a
JOIN exchange_rates r
  ON a.report_date = r.rate_date
 AND a.currency = r.currency
GROUP BY campaign_name;

示例5：
问题：上周的销售额是多少（人民币）
SQL：SELECT
    SUM(o.item_amount * r.rate_to_cny) AS sales_cny
FROM amz_order_items o
JOIN exchange_rates r
  ON o.purchase_date = r.rate_date
 AND o.currency = r.currency
WHERE o.order_status NOT IN ('Canceled', 'Pending')
  AND o.purchase_date >= DATE_SUB(DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY), INTERVAL 7 DAY)
  AND o.purchase_date < DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY);

示例6：
问题：本月的订单量是多少
SQL：SELECT COUNT(DISTINCT order_id) AS order_count
FROM amz_order_items
WHERE order_status NOT IN ('Canceled', 'Pending')
  AND purchase_date >= DATE_FORMAT(CURDATE(), '%Y-%m-01')
  AND purchase_date <= CURDATE();

示例7：
问题：2026年9月的销售额是多少（人民币）
SQL：SELECT
    SUM(o.item_amount * r.rate_to_cny) AS sales_cny
FROM amz_order_items o
JOIN exchange_rates r
  ON o.purchase_date = r.rate_date
 AND o.currency = r.currency
WHERE o.order_status NOT IN ('Canceled', 'Pending')
  AND o.purchase_date >= '2026-09-01'
  AND o.purchase_date < '2026-10-01';
"""


# ==================== 规则注入层（第7课新增）====================
RULES = """
【关键业务规则】
1. 销售额口径：使用 amz_order_items.item_amount（不含税商品销售额，不含运费和税费），tax_amount 不参与计算
2. 订单状态过滤：统计销售额、订单量、销量时必须排除 order_status IN ('Canceled', 'Pending')
3. 订单量口径：订单明细表是订单商品行粒度，订单量用 COUNT(DISTINCT order_id)，不能用 COUNT(*)
4. 汇率转换：金额折算人民币时按下单日（或报告日、入账日）关联 exchange_rates（rate_date + currency），使用 rate_to_cny
5. 成本口径：商品成本 = (dim_products.unit_cost + head_freight_cost) × 销量；dim_products 在 (store_id, msku) 上唯一
6. 毛利与利润：毛利 = 销售额 - 商品成本；利润 = 销售额 - 商品成本 - 平台费用 - 广告花费
7. 广告指标：广告销售额/订单用 7 天归因（sales_7d、orders_7d）；ACOS/ROAS/CPC 先聚合分子分母再相除，不能对每行结果求平均
8. 库存口径：amz_fba_inventory 是每日快照，只能取某一天（默认最新快照），不能跨天求和
9. 时间边界："最近N个月"使用 DATE_SUB(CURDATE(), INTERVAL N MONTH) 作为起始边界；"本月"指当月1日至当前日期
10. 口径陷阱：结算（amz_settlement）按入账日期、比下单晚 3-8 天；退货（amz_returns）滞后 5-30 天，近期退货率偏低
11. 不要引用 Schema 中不存在的字段；未建模维度优先回退到站点、品类、ASIN、MSKU、月份等已有维度
"""


# ==================== 错误防护层（第7课新增）====================
ERROR_GUARDS = """
【常见错误防护】
- 字段选择：销售额用 item_amount（不含税），不要把 tax_amount、shipping_amount 计入销售额
- Join 遗漏：多币种金额必须关联 exchange_rates 做汇率折算；商品成本必须通过 (store_id, msku) 关联 dim_products
- 过滤遗漏：销售额/订单量/销量统计必须排除 order_status IN ('Canceled', 'Pending')
- 跨表指标：TACOS、利润、退货率等跨表指标，分子分母分别在子查询中聚合后再相除，不能直接 JOIN 两张明细表（会重复放大）
- 聚合维度：GROUP BY 字段必须与 SELECT 中的非聚合字段完全一致
- 字段合法性：不要输出 Schema 中不存在的字段；订单量不要写 COUNT(*)
"""

# ==================== Prompt 构造函数 ====================
@traceable(
    name="Schema注入",
    run_type="tool",
    process_outputs=trim_schema_outputs,
)
def _build_schema_text(user_question: str, use_schema_linking: bool) -> dict:
    """选择注入 Prompt 的 Schema：优先动态 Schema Linking，失败回退全量 Schema。

    Returns:
        {"schema_type": "动态SchemaLinking"|"全量Schema", "schema_text": str}
    """
    schema_text = SCHEMA
    schema_type = "全量Schema"
    if use_schema_linking:
        try:
            from schema_linker import build_dynamic_prompt_schema
            dynamic_schema = build_dynamic_prompt_schema(user_question)
            if dynamic_schema:
                schema_text = dynamic_schema
                schema_type = "动态SchemaLinking"
        except Exception:
            pass  # 回退到全量 SCHEMA
    return {"schema_type": schema_type, "schema_text": schema_text}


@traceable(
    name="提示词构建",
    run_type="prompt",
    process_outputs=trim_prompt_outputs,
)
def build_prompt(
    user_question: str,
    use_few_shot: bool = True,
    use_rules: bool = False,
    use_guards: bool = False,
    indicator_knowledge: str = "",
    use_schema_linking: bool = False,
) -> tuple[str, str]:
    """
    构造发送给 LLM 的 Prompt

    Args:
        user_question: 用户的自然语言问题
        use_few_shot: 是否使用 Few-shot 示例
        use_rules: 是否注入业务规则层（第7课新增）
        use_guards: 是否注入错误防护层（第7课新增）
        indicator_knowledge: 指标知识文本块（第9课新增）
        use_schema_linking: 是否使用动态 Schema Linking（第18课新增）
                           为 True 时调用 schema_linker 动态生成精简 Schema，
                           失败时自动回退到全量 Schema。

    Returns:
        (system_message, user_message)
    """
    system_msg = "你是一个专业的 SQL 生成助手，擅长根据业务问题生成标准 MySQL 查询语句。"
    if use_rules or use_guards:
        system_msg = "你是一个专业的 SQL 生成助手，擅长根据业务问题生成标准 MySQL 查询语句。请严格遵守给定的业务规则，避免常见错误。"

    # 第18课新增：动态 Schema Linking（失败时自动回退全量 Schema）
    # Schema 注入过程单独成 span，Schema Linking 明细见其子节点
    schema_text = _build_schema_text(user_question, use_schema_linking)["schema_text"]

    prompt = f"""【数据库Schema】
{schema_text}
"""
    # 时间锚点：必须告诉 LLM 当前日期，否则「9月」这类不带年份的时间词会被猜错年份
    today = date.today()
    prompt += f"""
【当前日期】
今天是 {today.isoformat()}（{('星期' + '一二三四五六日'[today.weekday()])}）。
年份推断规则：问题中不带年份的月份（如「9月」「8月」）指以当前日期为基准，最近一个已经过完（或正在经历）的该月份；
例如今天是 {today.isoformat()} 时，「9月」指 {today.year if today.month >= 9 else today.year - 1} 年 9 月，
「8月」指 {today.year if today.month >= 8 else today.year - 1} 年 8 月。
不要臆造年份，不要使用与当前日期无关的年份。
"""
    # 时间词规则始终注入：封闭区间（上个月/上周/上季度/去年/某月）必须有上下界
    prompt += f"""
{TIME_RULES_SNIPPET}
"""
    if use_rules:
        prompt += f"""
{RULES}
"""

    if use_few_shot:
        prompt += f"""
【示例】
{FEW_SHOT_EXAMPLES}
"""

    if use_guards:
        prompt += f"""
{ERROR_GUARDS}
"""

    if indicator_knowledge:
        prompt += f"""
{indicator_knowledge}
"""

    prompt += f"""
【用户问题】
{user_question}

【要求】
1. 只输出 SQL 语句，不需要解释
2. 使用标准 MySQL 语法
3. 确保字段名和表名与 Schema 一致
4. 如果涉及多表查询，使用 JOIN 连接
5. 销售额使用 item_amount（不含税），排除 order_status IN ('Canceled', 'Pending')
6. 订单量使用 COUNT(DISTINCT order_id)；多币种金额按下单日汇率折算人民币
7. SQL 必须完整闭合，CTE、SELECT、GROUP BY、ORDER BY 不得省略，不要输出截断的半句 SQL
"""
    if use_rules or use_guards:
        prompt += """8. 优先遵循【关键业务规则】和【常见错误防护】中的约束
"""

    prompt += """
请直接输出 SQL：
"""
    return system_msg, prompt
