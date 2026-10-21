"""
表级召回模块（LangChain + ChromaDB）

第 15 课：将手写向量检索升级为工程化方案。
使用 LangChain 统一抽象层 + ChromaDB 持久化向量数据库，
实现表级召回的可持久化、可扩展版本。

依赖安装：uv add langchain-openai langchain-chroma chromadb
"""

import os
from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_core.documents import Document
from config import LLM_CONFIG
from tracing import traceable, wrap_embeddings


# ==================== 表描述数据 ====================
# 业务背景：跨境电商（Amazon 多站点），元数据外置到 knowledge_loader（table_metadata.json）
from knowledge_loader import TABLE_METADATA  # noqa: E402


# ==================== ChromaDB 持久化目录 ====================
CHROMA_PERSIST_DIR = os.path.join(os.path.dirname(__file__), "chroma_db", "tables")


def _cosine_relevance_score_fn(distance: float) -> float:
    """
    将 ChromaDB 的 cosine 距离转换为余弦相似度。

    ChromaDB 在 hnsw:space=cosine 模式下，distance = 1 - cosine_similarity。
    因此 cosine_similarity = 1 - distance，值域 [-1, 1]，
    与第 14 课手写余弦相似度一致，方便对比。
    """
    return 1 - distance


def get_embeddings() -> OpenAIEmbeddings:
    """构建 LangChain OpenAI Embeddings 实例（使用项目统一配置）"""
    return wrap_embeddings(OpenAIEmbeddings(
        model=LLM_CONFIG["embedding_model"],
        base_url=LLM_CONFIG["base_url"],
        api_key=LLM_CONFIG["api_key"],
        # 本地 MaaS 兼容接口只接受字符串数组，不做 tiktoken 预切分，
        # 且单批最多 25 条
        check_embedding_ctx_length=False,
        chunk_size=25,
    ))


def get_vectorstore() -> Chroma:
    """
    获取或创建 ChromaDB 向量存储实例。

    关键配置：
    - collection_metadata={"hnsw:space": "cosine"}：使用 cosine 距离而非默认的 L2 距离
    - relevance_score_fn：将 cosine 距离转换回余弦相似度（与第 14 课一致）

    如果不指定 cosine 空间，ChromaDB 默认使用 L2 距离，
    LangChain 的默认分数转换 (1 - distance/2) 在 L2 距离超过 2 时会产生负值。
    """
    return Chroma(
        collection_name="table_descriptions",
        embedding_function=get_embeddings(),
        persist_directory=CHROMA_PERSIST_DIR,
        collection_metadata={"hnsw:space": "cosine"},
        relevance_score_fn=_cosine_relevance_score_fn,
    )


# ==================== 索引构建 ====================
def build_index(force_rebuild: bool = False) -> Chroma:
    """
    将表描述向量化并写入 ChromaDB。

    如果持久化目录已存在且 force_rebuild=False，则直接加载已有索引。
    如果需要重建（如表描述更新），传入 force_rebuild=True。

    Returns:
        Chroma 向量存储实例
    """
    vectorstore = get_vectorstore()

    # 检查是否已有数据
    existing = vectorstore._collection.count()
    if existing > 0 and not force_rebuild:
        print(f"已有索引数据（{existing} 条），跳过重建。如需重建请传入 force_rebuild=True")
        return vectorstore

    # 清空旧数据（如果 force_rebuild）
    if force_rebuild and existing > 0:
        # ChromaDB 较新版本不支持空 where 过滤，用 delete 逐个删除
        existing_ids = vectorstore._collection.get()["ids"]
        if existing_ids:
            vectorstore._collection.delete(ids=existing_ids)
        print("已清空旧索引数据")

    # 构建 Document 列表
    documents = []
    for table_name, meta in TABLE_METADATA.items():
        doc = Document(
            page_content=meta["description"],
            metadata={
                "table_name": table_name,
                "domain": meta["domain"],
                "key_fields": meta["key_fields"],
            },
        )
        documents.append(doc)

    # 写入向量数据库
    vectorstore.add_documents(documents, ids=list(TABLE_METADATA.keys()))
    print(f"索引构建完成：{len(documents)} 张表已写入 ChromaDB")
    print(f"持久化路径：{CHROMA_PERSIST_DIR}")
    return vectorstore


# ==================== 表召回检索 ====================
@traceable(name="表召回", run_type="retriever")
def retrieve_tables(
    query: str,
    top_k: int = 3,
    score_threshold: float = 0.2,
    domain_filter: str | None = None,
) -> list[dict]:
    """
    根据用户问题检索最相关的表。

    Args:
        query: 用户的自然语言问题
        top_k: 返回前 K 张最相关的表
        score_threshold: 相似度阈值，低于此值的结果会被过滤
        domain_filter: 可选的领域过滤（如 "维度表"、"事实表"）

    Returns:
        [{"table_name": str, "score": float, "description": str, "domain": str, "key_fields": str}]
    """
    vectorstore = get_vectorstore()

    # 构建过滤条件
    search_kwargs = {"k": top_k}
    if domain_filter:
        search_kwargs["filter"] = {"domain": domain_filter}

    # 执行相似度检索（带分数）
    # 分数为余弦相似度（由 _cosine_relevance_score_fn 转换），值域 [-1, 1]
    results_with_scores = vectorstore.similarity_search_with_relevance_scores(
        query, **search_kwargs
    )

    # 过滤低分结果并格式化输出
    output = []
    for doc, score in results_with_scores:
        if score >= score_threshold:
            output.append({
                "table_name": doc.metadata["table_name"],
                "score": round(score, 4),
                "description": doc.page_content,
                "domain": doc.metadata["domain"],
                "key_fields": doc.metadata["key_fields"],
            })

    return output


# ==================== 主程序：演示 ====================
if __name__ == "__main__":
    # 第一步：构建索引（首次运行会调 Embedding API，后续直接加载本地数据）
    print("=" * 60)
    print("LangChain + ChromaDB 表级召回演示")
    print("=" * 60)
    print()

    vectorstore = build_index(force_rebuild=True)

    # 第二步：测试检索
    test_questions = [
        "欧洲市场最近三个月的销售额是多少？",
        "各产品线的毛利率",
        "上个月的研发费用和销售费用对比",
        "查询已完成订单的总数量",
        "按客户类型统计收入，需要换算成人民币",
    ]

    print()
    for question in test_questions:
        print(f"\n问题：{question}")
        results = retrieve_tables(question, top_k=5)
        print("召回结果：")
        for r in results:
            # 相似度 > 0.4 标记为强相关，0.3~0.4 为弱相关，其余不标记
            marker = "✓" if r["score"] > 0.4 else "·"
            print(f"  {marker} {r['table_name']:30s} 相似度: {r['score']:.4f}  [{r['domain']}]")
        if not results:
            print("  （无满足阈值的结果）")

    # 第三步：演示元数据过滤
    print("\n\n--- 元数据过滤演示：仅检索事实表 ---")
    results = retrieve_tables("上个月的收入和利润", top_k=3, domain_filter="事实表")
    print("问题：上个月的收入和利润（仅事实表）")
    for r in results:
        print(f"  ✓ {r['table_name']:30s} 相似度: {r['score']:.4f}  [{r['domain']}]")
