"""重建 ChromaDB 向量索引（表 / 字段 / 指标）。

改动 table_metadata.json、field_metadata.json、indicators_full.json 等知识文件后，
或首次配置好 OPENAI_API_KEY 后运行：

    uv run rebuild_indexes.py

对应 knowledge_loader 中的说明：
    build_index(force_rebuild=True)、build_field_index(force_rebuild=True)、build_indicator_index(force_rebuild=True)
"""

from table_retriever import build_index
from field_matcher import build_field_index
from indicator_retriever import build_indicator_index


def main() -> None:
    print("重建表级索引 ...")
    print("  已写入文档数:", build_index(force_rebuild=True)._collection.count())
    print("重建字段级索引 ...")
    print("  已写入文档数:", build_field_index(force_rebuild=True)._collection.count())
    print("重建指标索引 ...")
    print("  已写入文档数:", build_indicator_index(force_rebuild=True)._collection.count())
    print("完成。")


if __name__ == "__main__":
    main()
