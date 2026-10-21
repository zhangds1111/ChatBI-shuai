"""从 JSON 加载表/字段/规则/关系,替换 chatbi_mvp 中硬编码的元数据。
用法(放在各模块定义常量的位置):
    from knowledge_loader import TABLE_METADATA, FIELD_METADATA, BUSINESS_RULES, \
        TABLE_RELATIONSHIPS, TABLE_TYPES, TABLE_KEYWORDS
对应关系:
    table_retriever.TABLE_METADATA   <- table_metadata.json
    field_matcher.FIELD_METADATA     <- field_metadata.json
    field_matcher.BUSINESS_RULES     <- business_rules.json["BUSINESS_RULES"]
    join_resolver.TABLE_RELATIONSHIPS/TABLE_TYPES/TABLE_KEYWORDS <- table_relationships.json
    indicator_retriever.INDICATORS_FILE -> indicators_full.json
改完 JSON 后需重建向量库:build_index(force_rebuild=True)、build_field_index(force_rebuild=True)、build_indicator_index(force_rebuild=True)。
"""
import json, os
_D = os.path.dirname(os.path.abspath(__file__))
def _load(name):
    with open(os.path.join(_D, name), encoding="utf-8") as f:
        return json.load(f)
TABLE_METADATA = _load("table_metadata.json")
FIELD_METADATA = _load("field_metadata.json")
BUSINESS_RULES = _load("business_rules.json")["BUSINESS_RULES"]
_rel = _load("table_relationships.json")
TABLE_RELATIONSHIPS = _rel["TABLE_RELATIONSHIPS"]
TABLE_TYPES = _rel["TABLE_TYPES"]
TABLE_KEYWORDS = _rel["TABLE_KEYWORDS"]
