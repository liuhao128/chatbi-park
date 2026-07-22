"""
向量检索演示 Demo

第 14 课教学代码：手写最简向量检索系统。
演示 Embedding 生成 → 向量存储 → 余弦相似度检索 的完整闭环。
本文件为教学演示用途，不纳入主系统模块。
"""

import numpy as np
from schema.table_retriever import TABLE_METADATA
from text2sql.llm_client import LLMClient


# ==================== 1. 表描述数据 ====================
# 教学 Demo 复用主链路的停车表描述，避免维护第二套 Schema 事实来源。
TABLE_DESCRIPTIONS = {
    table_name: metadata["description"]
    for table_name, metadata in TABLE_METADATA.items()
}


# ==================== 2. 余弦相似度计算 ====================
def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """
    计算两个向量的余弦相似度。

    余弦相似度 = (A·B) / (|A| × |B|)
    - 值域 [-1, 1]，越接近 1 表示越相似
    - 语义相近的文本，其 Embedding 向量的余弦相似度通常 > 0.7
    """
    a = np.array(vec_a)
    b = np.array(vec_b)
    dot_product = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot_product / (norm_a * norm_b))


# ==================== 3. 构建向量索引 ====================
def build_table_index(client: LLMClient) -> dict[str, list[float]]:
    """
    将所有表的描述文本转为 Embedding 向量，构建内存索引。

    Returns:
        {表名: embedding向量} 的字典
    """
    print("正在构建表描述向量索引...")
    index = {}
    for table_name, description in TABLE_DESCRIPTIONS.items():
        embedding = client.get_embedding(description)
        index[table_name] = embedding
        print(f"  ✓ {table_name} → 向量维度 {len(embedding)}")
    print(f"索引构建完成，共 {len(index)} 张表\n")
    return index


# ==================== 4. 向量检索 ====================
def search_tables(
    query: str,
    index: dict[str, list[float]],
    client: LLMClient,
    top_k: int = 3,
) -> list[tuple[str, float]]:
    """
    根据用户问题检索最相关的表。

    Args:
        query: 用户的自然语言问题
        index: 表名→向量 的索引字典
        client: LLM 客户端（用于生成 query embedding）
        top_k: 返回相似度最高的前 K 张表

    Returns:
        [(表名, 相似度分数)] 列表，按分数降序排列
    """
    # 将用户问题转为向量
    query_embedding = client.get_embedding(query)

    # 计算与每张表的相似度
    scores = []
    for table_name, table_embedding in index.items():
        score = cosine_similarity(query_embedding, table_embedding)
        scores.append((table_name, score))

    # 按相似度降序排序，取 Top-K
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[:top_k]

def test_cosine_similarity():
    vec_a = [1, 0, 0]
    vec_b = [0, 1, 0]
    vec_c = [3, 4, 0] # 3/5 = 0.6
    # print(cosine_similarity(vec_a, vec_b)) # 0.0
    print(cosine_similarity(vec_a, vec_c)) # 0.6
    # print(cosine_similarity(vec_b, vec_c)) # 0.8

# ==================== 5. 主程序：演示完整流程 ====================
if __name__ == "__main__":
    # test_cosine_similarity()
    # pass
    # 初始化 LLM 客户端（复用项目已有的配置）
    client = LLMClient()

    # 第一步：构建索引（将表描述向量化）
    table_index = build_table_index(client)

    # 第二步：用测试问题验证检索效果
    test_questions = [
        "今天停车收入是多少？",
        "最近三个月收入趋势？",
        "哪个停车场收入最高？",
        "哪个停车场利用率最低？",
        "平均停车时长是多少？",
    ]

    print("=" * 60)
    print("向量检索演示：根据用户问题召回相关表")
    print("=" * 60)

    for question in test_questions:
        print(f"\n问题：{question}")
        results = search_tables(question, table_index, client, top_k=6)
        print("召回结果：")
        for table_name, score in results:
            # 相似度 > 0.4 认为强相关，用 ✓ 标记；否则用 · 标记
            marker = "✓" if score > 0.4 else "·"
            print(f"  {marker} {table_name:20s} 相似度: {score:.4f}")
