"""智慧停车指标知识库 RAG 检索模块。

流程：指标 JSON Loader → 一指标一 Document 的语义切分 → Qwen-compatible
Embedding → ChromaDB → 关键词与向量混合召回 → 依赖/诊断指标扩展 → 知识块。

当前保持项目既有 LangChain + ChromaDB 技术栈。指标文档本身是完整、短小、
不可再拆的业务语义单元，因此不使用通用字符 Splitter，避免公式和规则被切散。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings

from tools.config import LLM_CONFIG


INDICATORS_FILE = Path(__file__).with_name("indicators_full.json")
CHROMA_PERSIST_DIR = os.path.join(os.path.dirname(__file__), "chroma_db", "indicators")
COLLECTION_NAME = "parking_indicator_definitions"
DIAGNOSIS_KEYWORDS = ("下降", "原因", "为什么", "异常", "波动", "下滑", "减少")


def _cosine_relevance_score_fn(distance: float) -> float:
    """将 Chroma cosine 距离还原为余弦相似度，值域为 [-1, 1]。"""
    return 1.0 - distance


def get_embeddings() -> OpenAIEmbeddings:
    """复用项目配置构建 Qwen/OpenAI-compatible Embedding 客户端。"""
    return OpenAIEmbeddings(
        model=LLM_CONFIG["embedding_model"],
        base_url=LLM_CONFIG["base_url"],
        api_key=LLM_CONFIG["api_key"],
        check_embedding_ctx_length=False,
        chunk_size=10,
    )


def get_vectorstore() -> Chroma:
    """获取智慧停车指标 Chroma 向量存储。"""
    return Chroma(
        collection_name=COLLECTION_NAME,
        embedding_function=get_embeddings(),
        persist_directory=CHROMA_PERSIST_DIR,
        collection_metadata={"hnsw:space": "cosine"},
        relevance_score_fn=_cosine_relevance_score_fn,
    )


def load_indicator_catalog() -> dict[str, Any]:
    """Document Loader：加载完整指标目录及知识版本。"""
    with INDICATORS_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def load_indicators() -> dict[str, dict]:
    """加载指标知识库，返回 {指标名称: 指标定义}。"""
    catalog = load_indicator_catalog()
    return {indicator["name"]: indicator for indicator in catalog["indicators"]}


def build_indicator_document(indicator: dict, knowledge_version: str) -> Document:
    """语义切分：把一项完整指标构造成一个不可再拆的检索 Document。"""
    aliases = "、".join(indicator.get("aliases", []))
    tables = "、".join(indicator.get("tables", []))
    fields = "、".join(indicator.get("fields", []))
    dimensions = "、".join(indicator.get("dimensions", []))
    rules = "；".join(indicator.get("business_rules", []))
    questions = "；".join(indicator.get("supported_questions", []))

    search_text = (
        f"指标名称：{indicator['name']}\n"
        f"指标类别：{indicator.get('category', '')}\n"
        f"别名与业务说法：{aliases}\n"
        f"业务定义：{indicator['definition']}\n"
        f"计算公式：{indicator['formula']}\n"
        f"关联表：{tables}\n"
        f"关联字段：{fields}\n"
        f"时间口径：{indicator.get('time_field', '')}\n"
        f"支持维度：{dimensions}\n"
        f"业务规则：{rules}\n"
        f"典型问题：{questions}"
    )

    return Document(
        page_content=search_text,
        metadata={
            "metric_id": indicator["metric_id"],
            "name": indicator["name"],
            "category": indicator.get("category", ""),
            "level": indicator.get("level", ""),
            "aliases": aliases,
            "knowledge_version": knowledge_version,
        },
    )


def build_indicator_index(force_rebuild: bool = False) -> Chroma:
    """将停车指标文档向量化并写入 Chroma，自动识别旧业务或旧版本索引。"""
    catalog = load_indicator_catalog()
    indicators = {item["name"]: item for item in catalog["indicators"]}
    knowledge_version = catalog.get("knowledge_version", "")
    expected_ids = {item["metric_id"] for item in indicators.values()}

    vectorstore = get_vectorstore()
    existing = vectorstore._collection.count()
    existing_data = (
        vectorstore._collection.get(include=["metadatas"])
        if existing > 0
        else {"ids": [], "metadatas": []}
    )
    existing_ids = set(existing_data.get("ids", []))
    existing_versions = {
        metadata.get("knowledge_version", "")
        for metadata in existing_data.get("metadatas", [])
        if metadata
    }
    index_matches = (
        existing_ids == expected_ids
        and existing_versions == {knowledge_version}
    )

    if existing > 0 and not force_rebuild and index_matches:
        print(f"停车指标索引已存在（{existing} 条，版本 {knowledge_version}），跳过重建。")
        return vectorstore

    if existing_ids:
        vectorstore._collection.delete(ids=list(existing_ids))
        print("已清空旧业务、旧版本或结构不一致的指标索引")

    documents = [
        build_indicator_document(indicator, knowledge_version)
        for indicator in indicators.values()
    ]
    ids = [indicator["metric_id"] for indicator in indicators.values()]
    vectorstore.add_documents(documents, ids=ids)
    print(f"指标索引构建完成：{len(documents)} 个停车指标已写入 ChromaDB")
    print(f"知识版本：{knowledge_version}")
    print(f"持久化路径：{CHROMA_PERSIST_DIR}")
    return vectorstore


def _keyword_matches(query: str, indicators: dict[str, dict]) -> dict[str, float]:
    """按标准名称和别名召回指标，为明确业务词提供确定性保障。"""
    normalized_query = query.lower()
    matches: dict[str, float] = {}
    for name, indicator in indicators.items():
        candidates = [name, *indicator.get("aliases", [])]
        matched_lengths = [
            len(candidate)
            for candidate in candidates
            if candidate.lower() in normalized_query
        ]
        if matched_lengths:
            # 标准名或较长别名比单字/短词更具体，分数略高。
            longest = max(matched_lengths)
            matches[name] = min(1.0, 0.85 + longest * 0.02)
    return matches


def _format_indicator_result(
    indicator: dict,
    score: float,
    match_type: str,
    *,
    is_dependency: bool = False,
    is_related: bool = False,
) -> dict:
    """把知识库指标统一格式化为检索结果。"""
    return {
        "metric_id": indicator.get("metric_id", ""),
        "name": indicator["name"],
        "score": round(score, 4),
        "match_type": match_type,
        "category": indicator.get("category", ""),
        "level": indicator.get("level", ""),
        "definition": indicator.get("definition", ""),
        "formula": indicator.get("formula", ""),
        "sql_template": indicator.get("sql_template", ""),
        "tables": indicator.get("tables", []),
        "fields": indicator.get("fields", []),
        "data_source": indicator.get("data_source", ""),
        "time_field": indicator.get("time_field", ""),
        "filters": indicator.get("filters", []),
        "dimensions": indicator.get("dimensions", []),
        "business_rules": indicator.get("business_rules", []),
        "supported_questions": indicator.get("supported_questions", []),
        "depends_on": indicator.get("depends_on", []),
        "related_metrics": indicator.get("related_metrics", []),
        "notes": indicator.get("notes", ""),
        "is_dependency": is_dependency,
        "is_related": is_related,
    }


def _expand_metric_names(
    matched: list[dict],
    indicators: dict[str, dict],
    query: str,
    expand_dependencies: bool,
    expand_related: bool,
    related_limit: int,
) -> list[dict]:
    """按依赖关系和诊断意图补充必要指标，避免简单查询无条件扩张。"""
    expanded = list(matched)
    included_names = {item["name"] for item in expanded}

    if expand_dependencies:
        for item in list(matched):
            for dependency_name in item["depends_on"]:
                if dependency_name in included_names:
                    continue
                dependency = indicators.get(dependency_name)
                if dependency:
                    expanded.append(
                        _format_indicator_result(
                            dependency,
                            0.0,
                            "dependency",
                            is_dependency=True,
                        )
                    )
                    included_names.add(dependency_name)

    is_diagnosis = any(keyword in query for keyword in DIAGNOSIS_KEYWORDS)
    if expand_related and is_diagnosis:
        related_names: list[str] = []
        for item in matched:
            for related_name in item["related_metrics"]:
                if related_name not in included_names and related_name not in related_names:
                    related_names.append(related_name)

        for related_name in related_names[:related_limit]:
            related = indicators.get(related_name)
            if related:
                expanded.append(
                    _format_indicator_result(
                        related,
                        0.0,
                        "diagnosis_related",
                        is_related=True,
                    )
                )
                included_names.add(related_name)

    return expanded


def retrieve_indicators(
    query: str,
    top_k: int = 3,
    score_threshold: float = 0.3,
    expand_dependencies: bool = True,
    expand_related: bool = True,
    related_limit: int = 4,
) -> list[dict]:
    """通过关键词与向量混合召回停车指标，并按需展开依赖和诊断指标。"""
    indicators = load_indicators()
    candidates: dict[str, dict[str, Any]] = {}

    # 第一步：明确名称/别名直接命中，保证“收入”“利用率”等核心词稳定召回。
    keyword_matches = _keyword_matches(query, indicators)
    for name, keyword_score in keyword_matches.items():
        candidates[name] = {
            "score": keyword_score,
            "match_types": {"keyword"},
        }

    # 第二步：向量检索负责理解“平均停车时间”“几点最忙”等语义表达。
    vectorstore = get_vectorstore()
    search_k = min(max(top_k * 4, 12), max(len(indicators), 1))
    results_with_scores = vectorstore.similarity_search_with_relevance_scores(
        query,
        k=search_k,
    )
    for document, vector_score in results_with_scores:
        if vector_score < score_threshold:
            continue
        name = document.metadata["name"]
        candidate = candidates.setdefault(
            name,
            {"score": vector_score, "match_types": set()},
        )
        candidate["score"] = max(candidate["score"], vector_score)
        candidate["match_types"].add("vector")

    # 如果问题已明确包含指标名称或别名，直接指标只保留关键词命中集合；
    # 向量分仍用于给这些命中项增强和排序，但不再用弱相关指标凑满 Top-K。
    # 没有明确业务词时，才完全依赖向量 Top-K 理解口语化表达。
    direct_candidate_names = (
        set(keyword_matches)
        if keyword_matches
        else set(candidates)
    )
    ranked_names = sorted(
        direct_candidate_names,
        key=lambda name: candidates[name]["score"],
        reverse=True,
    )[:top_k]
    matched = [
        _format_indicator_result(
            indicators[name],
            candidates[name]["score"],
            "+".join(sorted(candidates[name]["match_types"])),
        )
        for name in ranked_names
    ]

    return _expand_metric_names(
        matched,
        indicators,
        query,
        expand_dependencies,
        expand_related,
        related_limit,
    )


def build_indicator_knowledge_block_from_results(indicators: list[dict]) -> str:
    """把召回指标压缩成 Text2SQL 可消费的业务知识块。"""
    if not indicators:
        return ""

    blocks = ["【智慧停车指标知识】"]
    for indicator in indicators:
        if indicator["is_dependency"]:
            source_tag = "（依赖指标）"
        elif indicator["is_related"]:
            source_tag = "（诊断关联指标）"
        else:
            source_tag = ""

        lines = [
            f"指标：{indicator['name']}{source_tag}",
            f"  定义：{indicator['definition']}",
            f"  计算公式：{indicator['formula']}",
            f"  关联表：{', '.join(indicator['tables'])}",
            f"  关联字段：{', '.join(indicator['fields'])}",
            f"  时间口径：{indicator['time_field']}",
            f"  支持维度：{', '.join(indicator['dimensions'])}",
        ]
        if indicator["filters"]:
            lines.append(f"  强制过滤：{' AND '.join(indicator['filters'])}")
        if indicator["business_rules"]:
            lines.append(f"  业务规则：{'；'.join(indicator['business_rules'])}")
        # SQL 模板仅给直接召回指标，避免诊断扩展时 Prompt 体积快速增长。
        if (
            indicator["sql_template"]
            and not indicator["is_dependency"]
            and not indicator["is_related"]
        ):
            lines.append(f"  SQL参考：{indicator['sql_template']}")
        if indicator["notes"]:
            lines.append(f"  注意：{indicator['notes']}")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


def build_indicator_knowledge_block(query: str) -> str:
    """根据用户问题检索指标并生成知识块。"""
    return build_indicator_knowledge_block_from_results(retrieve_indicators(query))


def retrieve_indicator_context(
    query: str,
    top_k: int = 3,
    score_threshold: float = 0.3,
    expand_dependencies: bool = True,
    expand_related: bool = True,
) -> dict[str, list[str] | list[dict] | str]:
    """一次混合检索返回直接命中指标、完整结果和 Prompt 知识块。"""
    indicators = retrieve_indicators(
        query,
        top_k=top_k,
        score_threshold=score_threshold,
        expand_dependencies=expand_dependencies,
        expand_related=expand_related,
    )
    return {
        "detected_indicators": [
            item["name"]
            for item in indicators
            if not item["is_dependency"] and not item["is_related"]
        ],
        "retrieved_indicators": indicators,
        "indicator_block": build_indicator_knowledge_block_from_results(indicators),
    }


if __name__ == "__main__":
    print("=" * 70)
    print("智慧停车指标知识库 RAG 检索演示")
    print("=" * 70)
    build_indicator_index(force_rebuild=True)

    for question in [
        "最近一个月停车收入是多少",
        "哪个停车场利用率最低",
        "平均停车时间是多少",
        "收入下降原因",
    ]:
        print(f"\n问题：{question}")
        context = retrieve_indicator_context(question)
        for item in context["retrieved_indicators"]:
            tag = "依赖" if item["is_dependency"] else "诊断扩展" if item["is_related"] else item["match_type"]
            print(f"  {item['name']}  score={item['score']:.4f}  [{tag}]")
        print(context["indicator_block"])
