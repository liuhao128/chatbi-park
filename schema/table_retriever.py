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
from tools.config import LLM_CONFIG


# ==================== 表描述数据 ====================
# 每张表构建一段结构化的自然语言描述用于向量化检索。
# 表集合与 database/01_schema.sql 的智慧停车 MVP 六表保持一致。
TABLE_METADATA = {
    "dim_parking_lot": {
        "description": (
            "停车场维度表：一行代表一个停车场，保存停车场名称、运营商、城市、"
            "停车场类型、总车位数和运营状态。用于按停车场、城市或停车场类型"
            "分析停车收入、订单量、车位利用率、停车时长和异常情况，也用于停车场排行。"
        ),
        "domain": "维度表",
        "key_fields": "parking_lot_id, parking_lot_name, operator_id, city_name, parking_lot_type, total_spaces, operation_status, updated_at",
    },
    "fact_parking_order": {
        "description": (
            "停车订单事实表：一行代表一笔停车订单，记录停车场、订单类型、入场时间、"
            "出场时间、停车时长、订单状态、应收金额、优惠金额、实收金额、退款金额、"
            "支付状态、支付方式、人工抬杆和免费放行。用于订单明细、停车净收入、"
            "车流量、平均停车时长、支付成功率、退款和优惠分析。"
        ),
        "domain": "明细事实表",
        "key_fields": "order_id, parking_lot_id, order_type, entry_time, exit_time, parking_minutes, order_status, receivable_amount, discount_amount, paid_amount, refund_amount, payment_status, payment_method, manual_open_flag, free_release_flag, updated_at",
    },
    "fact_space_snapshot": {
        "description": (
            "车位状态快照事实表：一行代表某停车场某个时刻的车位状态，记录快照时间、"
            "可运营总车位数、已占用车位数和空闲车位数。用于当前空闲车位、历史占用、"
            "车位利用率、空闲率和停车高峰分析。利用率由 occupied_spaces 除以 total_spaces。"
        ),
        "domain": "明细事实表",
        "key_fields": "snapshot_id, parking_lot_id, snapshot_time, total_spaces, occupied_spaces, free_spaces",
    },
    "fact_operation_event": {
        "description": (
            "运营异常事件事实表：一行代表一次停车运营异常或人工操作事件，记录停车场、"
            "可选关联订单、事件时间、事件类型、严重程度、处理状态和预估收入损失。"
            "用于分析支付失败、设备离线、车牌识别失败、人工抬杆、异常数量和收入下降原因。"
        ),
        "domain": "明细事实表",
        "key_fields": "event_id, parking_lot_id, order_id, event_time, event_type, severity, event_status, estimated_loss, description",
    },
    "agg_parking_daily": {
        "description": (
            "停车场日经营汇总事实表：一行代表一个停车场一个自然日，包含完成订单量、"
            "停车净收入、平均停车时长、平均占用车位数、车位利用率、人工抬杆次数、"
            "免费放行次数和异常数量。优先用于今天、最近七天、最近三个月、月度趋势、"
            "停车场排名、收入下降和利用率变化等经营分析。"
        ),
        "domain": "日聚合事实表",
        "key_fields": "stat_date, parking_lot_id, order_count, net_revenue, average_parking_minutes, average_occupied_spaces, utilization_rate, manual_open_count, free_release_count, exception_count, updated_at",
    },
    "agg_parking_hourly": {
        "description": (
            "停车场小时经营汇总事实表：一行代表一个停车场某天某个小时，包含小时完成订单量、"
            "停车净收入、平均占用车位数、车位利用率和异常数量。优先用于几点最忙、"
            "高峰时段、小时收入、小时车流和小时利用率分析。"
        ),
        "domain": "小时聚合事实表",
        "key_fields": "stat_date, stat_hour, parking_lot_id, order_count, net_revenue, occupied_spaces, utilization_rate, exception_count, updated_at",
    },
}


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


# 阿里 MaaS OpenAI Compatible 接口兼容配置
# 避免 LangChain 内部 tokenizer 改写 input 格式
# 所以加了这个参数check_embedding_ctx_length=False,
def get_embeddings() -> OpenAIEmbeddings:
    """构建 LangChain OpenAI Embeddings 实例（使用项目统一配置）"""
    return OpenAIEmbeddings(
        model=LLM_CONFIG["embedding_model"],
        base_url=LLM_CONFIG["base_url"],
        api_key=LLM_CONFIG["api_key"],
        check_embedding_ctx_length=False,
    )


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

    # 检查是否已有数据；业务迁移后即使 collection 非空，也不能继续复用旧表 ID。
    existing = vectorstore._collection.count()
    existing_ids = set(vectorstore._collection.get()["ids"]) if existing > 0 else set()
    expected_ids = set(TABLE_METADATA)
    index_matches_metadata = existing_ids == expected_ids
    if existing > 0 and not force_rebuild and index_matches_metadata:
        print(f"已有索引数据（{existing} 条），跳过重建。如需重建请传入 force_rebuild=True")
        return vectorstore

    # 强制重建或索引 ID 与当前停车元数据不一致时清空旧数据。
    if existing > 0 and (force_rebuild or not index_matches_metadata):
        # ChromaDB 较新版本不支持空 where 过滤，用 delete 逐个删除
        if existing_ids:
            vectorstore._collection.delete(ids=list(existing_ids))
        print("已清空与当前停车表元数据不一致的旧索引数据")

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
        "今天停车收入是多少？",
        "最近三个月收入趋势？",
        "哪个停车场收入最高？",
        "哪个停车场利用率最低？",
        "平均停车时长是多少？",
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
    print("\n\n--- 元数据过滤演示：仅检索日聚合事实表 ---")
    results = retrieve_tables("最近三个月停车收入趋势", top_k=3, domain_filter="日聚合事实表")
    print("问题：最近三个月停车收入趋势（仅日聚合事实表）")
    for r in results:
        print(f"  ✓ {r['table_name']:30s} 相似度: {r['score']:.4f}  [{r['domain']}]")
