"""亚马逊业务的可用分析维度(替换 query_decomposer.py 中写死的 AVAILABLE_DIMENSIONS)。

用法(query_decomposer.py):
    from analysis_dimensions import AVAILABLE_DIMENSIONS, DIMENSION_ALIASES, normalize_dimensions, \
        build_dimension_catalog_text, build_strategy_text
    # 1) 提示词里的 {dimension_catalog} 换成 build_dimension_catalog_text()
    # 2) 提示词里的【常见分析类型拆解策略】换成 build_strategy_text()
    # 3) _validate_dimensions 之前先 plan 里的每个 task.dimensions = normalize_dimensions(task.dimensions),
    #    把「国家/商品/运营」等近义词归一成规范名;仍不在 AVAILABLE_DIMENSIONS 里的才算不支持。
    # 4) _max_tasks_for_question_type 的上限建议改为 7(综合经营分析要 7 步)。
"""
import json, os
_D = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_D, "analysis_dimensions.json"), encoding="utf-8") as _f:
    _DATA = json.load(_f)

DIMENSIONS = _DATA["dimensions"]
AVAILABLE_DIMENSIONS = [d["name"] for d in DIMENSIONS]
DIMENSION_ALIASES = {}
for _d in DIMENSIONS:
    DIMENSION_ALIASES[_d["name"]] = _d["name"]
    for _a in _d["aliases"]:
        DIMENSION_ALIASES.setdefault(_a, _d["name"])
METRIC_DIMENSION_SUPPORT = _DATA["metric_dimension_support"]
DECOMPOSITION_STRATEGIES = _DATA["decomposition_strategies"]
RULES = _DATA["rules"]


def normalize_dimensions(dims):
    """近义词归一为规范维度名,去重保序;未知词原样保留,交给校验报错。"""
    out = []
    for x in dims:
        n = DIMENSION_ALIASES.get(x, x)
        if n not in out:
            out.append(n)
    return out


def build_dimension_catalog_text():
    lines = []
    for d in DIMENSIONS:
        al = "、".join(d["aliases"])
        vals = ("(" + " / ".join(d["values"]) + ")") if d["values"] and len(d["values"]) <= 8 and d["kind"] != "concept" else ""
        tabs = "、".join(d["supported_tables"]) if d["supported_tables"] else "概念维度"
        line = f"- {d['name']}{vals}:{d['description']}。别名:{al}。可用于:{tabs}"
        if d["note"]:
            line += f"。注意:{d['note']}"
        lines.append(line)
    return "\n".join(lines)


def build_strategy_text():
    s = ["【常见分析类型拆解策略】"]
    for i, (k, v) in enumerate(DECOMPOSITION_STRATEGIES.items(), 1):
        s.append(f"{i}. {k}:{v}")
    s.append("【拆解约束】")
    for r in RULES:
        s.append("- " + r)
    return "\n".join(s)


def build_metric_dimension_text(metrics=None):
    """指标与可选维度的对应关系,可注入提示词,避免『按广告类型看销售额』这种无数据的拆解。"""
    items = metrics or list(METRIC_DIMENSION_SUPPORT)
    return "\n".join(f"- {m}:可按 {'、'.join(METRIC_DIMENSION_SUPPORT[m])} 分组" for m in items if m in METRIC_DIMENSION_SUPPORT)
