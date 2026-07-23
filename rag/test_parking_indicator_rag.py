"""智慧停车指标知识库的离线回归测试，不调用真实 Embedding 服务。"""

from unittest.mock import patch

from langchain_core.documents import Document

import rag.indicator_retriever as retriever
from rag.indicator_knowledge import IndicatorKnowledge


class FakeVectorStore:
    def __init__(self, results: list[tuple[Document, float]]):
        self.results = results

    def similarity_search_with_relevance_scores(self, *_args, **_kwargs):
        return self.results


def _vector_result(name: str, score: float) -> tuple[Document, float]:
    return (
        Document(page_content=name, metadata={"name": name}),
        score,
    )


def test_parking_metric_catalog_is_mvp_and_has_required_structure():
    catalog = retriever.load_indicator_catalog()
    indicators = catalog["indicators"]

    assert catalog["knowledge_version"] == "parking_metrics_v1"
    assert 1 <= len(indicators) <= 20
    assert len({item["metric_id"] for item in indicators}) == len(indicators)

    required_fields = {
        "metric_id",
        "name",
        "definition",
        "formula",
        "tables",
        "fields",
        "dimensions",
        "business_rules",
        "supported_questions",
    }
    assert all(required_fields <= set(item) for item in indicators)


def test_keyword_fallback_recognizes_four_core_parking_questions():
    knowledge = IndicatorKnowledge()

    assert knowledge.detect_indicators("最近一个月停车收入是多少") == ["停车净收入"]
    assert knowledge.detect_indicators("哪个停车场利用率最低") == ["车位利用率"]
    assert knowledge.detect_indicators("平均停车时间是多少") == ["平均停车时长"]
    assert knowledge.detect_indicators("收入下降原因") == ["停车净收入"]


def test_explicit_keyword_does_not_pull_weak_vector_metrics():
    fake_store = FakeVectorStore([
        _vector_result("停车净收入", 0.72),
        _vector_result("平均订单金额", 0.95),
        _vector_result("应收金额", 0.90),
    ])

    with patch.object(retriever, "get_vectorstore", return_value=fake_store):
        results = retriever.retrieve_indicators("最近一个月停车收入是多少")

    assert [item["name"] for item in results] == ["停车净收入"]
    assert results[0]["match_type"] == "keyword+vector"


def test_diagnosis_query_expands_revenue_drivers():
    fake_store = FakeVectorStore([_vector_result("停车净收入", 0.70)])

    with patch.object(retriever, "get_vectorstore", return_value=fake_store):
        results = retriever.retrieve_indicators("收入下降原因")

    names = [item["name"] for item in results]
    assert names == [
        "停车净收入",
        "完成订单量",
        "退款金额",
        "车位利用率",
        "异常事件数",
    ]
    assert all(item["is_related"] for item in results[1:])


def test_vector_retrieval_handles_colloquial_metric_question():
    fake_store = FakeVectorStore([
        _vector_result("平均订单金额", 0.71),
        _vector_result("停车净收入", 0.45),
    ])

    with patch.object(retriever, "get_vectorstore", return_value=fake_store):
        context = retriever.retrieve_indicator_context(
            "这个月每辆车平均花了多少钱",
            top_k=1,
        )

    assert context["detected_indicators"] == ["平均订单金额"]
    assert "关联字段：order_id, paid_amount, refund_amount" in context["indicator_block"]
    assert "支持维度：时间, 停车场, 订单类型, 支付方式" in context["indicator_block"]
