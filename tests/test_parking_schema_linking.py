from pathlib import Path
import sys
from unittest.mock import patch

from langchain_core.documents import Document


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import schema.field_matcher as field_matcher_module
from schema.field_matcher import FIELD_METADATA, evaluate_rules, match_fields
from schema.join_resolver import resolve_joins, select_anchor
from schema.schema_linker import schema_link
from schema.table_retriever import TABLE_METADATA


PARKING_TABLES = {
    "dim_parking_lot",
    "fact_parking_order",
    "fact_space_snapshot",
    "fact_operation_event",
    "agg_parking_daily",
    "agg_parking_hourly",
}


def _table_result(table_name: str, score: float = 0.9) -> dict:
    metadata = TABLE_METADATA[table_name]
    return {
        "table_name": table_name,
        "score": score,
        "description": metadata["description"],
        "domain": metadata["domain"],
        "key_fields": metadata["key_fields"],
    }


def _rule_backed_fields(query: str, candidate_tables: list[str], **_kwargs) -> list[dict]:
    include_fields = evaluate_rules(query)["force_include"]
    fields = []
    for field_key in include_fields:
        metadata = FIELD_METADATA.get(field_key)
        if metadata is None or metadata["table"] not in candidate_tables:
            continue
        fields.append({
            "field_key": field_key,
            "table": metadata["table"],
            "field": metadata["field"],
            "score": 0.3,
            "embedding_score": 0.0,
            "rule_applied": "强制包含（补充）",
            "description": metadata["description"],
        })
    return fields


def _simulate_schema_link(query: str, recalled_tables: list[str]) -> dict:
    def fake_retrieve_tables(_query: str, top_k: int = 3, **_kwargs) -> list[dict]:
        if top_k >= 10:
            return [_table_result(table) for table in PARKING_TABLES]
        return [_table_result(table) for table in recalled_tables]

    with (
        patch("schema.schema_linker.retrieve_tables", side_effect=fake_retrieve_tables),
        patch("schema.schema_linker.match_fields", side_effect=_rule_backed_fields),
    ):
        return schema_link(query)


def test_schema_metadata_contains_only_parking_mvp_tables():
    assert set(TABLE_METADATA) == PARKING_TABLES
    assert len(FIELD_METADATA) == 59
    assert all(field["table"] in PARKING_TABLES for field in FIELD_METADATA.values())
    assert not any("sales_orders" in field_key for field_key in FIELD_METADATA)
    assert not any("dim_customers" in field_key for field_key in FIELD_METADATA)


def test_parking_business_rules_map_core_terms_to_real_fields():
    revenue_fields = set(evaluate_rules("最近三个月停车收入趋势")["force_include"])
    utilization_fields = set(evaluate_rules("哪个停车场利用率最低")["force_include"])
    duration_fields = set(evaluate_rules("平均停车时长是多少")["force_include"])

    assert {"agg_parking_daily.net_revenue", "agg_parking_daily.stat_date"} <= revenue_fields
    assert {
        "fact_parking_order.paid_amount",
        "fact_parking_order.refund_amount",
        "fact_parking_order.exit_time",
    } <= revenue_fields
    assert {
        "fact_space_snapshot.occupied_spaces",
        "fact_space_snapshot.total_spaces",
        "agg_parking_daily.utilization_rate",
    } <= utilization_fields
    assert {
        "fact_parking_order.entry_time",
        "fact_parking_order.exit_time",
        "fact_parking_order.parking_minutes",
    } <= duration_fields


def test_blacklist_excludes_ambiguous_revenue_and_time_fields():
    rule_result = evaluate_rules("最近三个月停车收入趋势")
    excluded = set(rule_result["force_exclude"])

    assert {
        "fact_parking_order.receivable_amount",
        "fact_parking_order.discount_amount",
        "fact_operation_event.estimated_loss",
        "fact_parking_order.updated_at",
        "agg_parking_daily.updated_at",
    } <= excluded


def test_specific_whitelist_overrides_generic_blacklist():
    revenue_rules = evaluate_rules("最近三个月应收收入和优惠金额趋势")
    revenue_included = set(revenue_rules["force_include"])
    revenue_excluded = set(revenue_rules["force_exclude"])

    assert {
        "fact_parking_order.receivable_amount",
        "fact_parking_order.discount_amount",
    } <= revenue_included
    assert not revenue_included.intersection(revenue_excluded)

    utilization_rules = evaluate_rules("停车场总车位和利用率")
    assert "dim_parking_lot.total_spaces" in utilization_rules["force_include"]
    assert "dim_parking_lot.total_spaces" not in utilization_rules["force_exclude"]

    peak_rules = evaluate_rules("哪个时段是收入高峰")
    assert "agg_parking_hourly.net_revenue" in peak_rules["force_include"]
    assert "agg_parking_hourly.net_revenue" not in peak_rules["force_exclude"]


def test_match_fields_hard_excludes_blacklisted_field():
    class FakeVectorStore:
        def similarity_search_with_relevance_scores(self, *_args, **_kwargs):
            return [
                (
                    Document(
                        page_content="停车订单实收金额",
                        metadata={
                            "field_key": "fact_parking_order.paid_amount",
                            "table_name": "fact_parking_order",
                            "field_name": "paid_amount",
                        },
                    ),
                    1.0,
                ),
                (
                    Document(
                        page_content="停车时长，单位分钟",
                        metadata={
                            "field_key": "fact_parking_order.parking_minutes",
                            "table_name": "fact_parking_order",
                            "field_name": "parking_minutes",
                        },
                    ),
                    0.7,
                ),
            ]

    with patch.object(field_matcher_module, "get_vectorstore", return_value=FakeVectorStore()):
        fields = match_fields(
            "平均停车时长是多少？",
            candidate_tables=["fact_parking_order"],
            top_k=10,
            score_threshold=0.0,
        )

    field_names = {field["field"] for field in fields}
    assert "parking_minutes" in field_names
    assert "paid_amount" not in field_names


def test_parking_lot_ranking_selects_daily_fact_anchor_and_dimension_join():
    tables = ["agg_parking_daily", "dim_parking_lot"]
    anchor, _reason = select_anchor("哪个停车场收入最高", tables)
    join_path = resolve_joins(anchor, tables)

    assert anchor == "agg_parking_daily"
    assert join_path["unreachable"] == []
    assert join_path["joins"][0]["to_table"] == "dim_parking_lot"
    assert join_path["joins"][0]["on_clause"] == (
        "agg_parking_daily.parking_lot_id = dim_parking_lot.parking_lot_id"
    )


def test_today_revenue_links_daily_revenue_and_date():
    result = _simulate_schema_link("今天停车收入是多少？", ["agg_parking_daily"])
    field_keys = {field["field_key"] for field in result["fields"]}

    assert result["anchor"] == "agg_parking_daily"
    assert {"agg_parking_daily.net_revenue", "agg_parking_daily.stat_date"} <= field_keys


def test_three_month_revenue_trend_links_daily_fact():
    result = _simulate_schema_link("最近三个月收入趋势？", ["agg_parking_daily"])
    field_keys = {field["field_key"] for field in result["fields"]}

    assert result["anchor"] == "agg_parking_daily"
    assert {"agg_parking_daily.net_revenue", "agg_parking_daily.stat_date"} <= field_keys


def test_parking_lot_revenue_ranking_links_dimension_and_daily_fact():
    result = _simulate_schema_link(
        "哪个停车场收入最高？",
        ["agg_parking_daily", "dim_parking_lot"],
    )
    field_keys = {field["field_key"] for field in result["fields"]}

    assert result["anchor"] == "agg_parking_daily"
    assert {table["table_name"] for table in result["tables"]} == {
        "agg_parking_daily",
        "dim_parking_lot",
    }
    assert "dim_parking_lot.parking_lot_name" in field_keys
    assert result["join_path"]["unreachable"] == []


def test_parking_lot_utilization_ranking_links_dimension_and_daily_fact():
    result = _simulate_schema_link(
        "哪个停车场利用率最低？",
        ["agg_parking_daily", "dim_parking_lot"],
    )
    field_keys = {field["field_key"] for field in result["fields"]}

    assert result["anchor"] == "agg_parking_daily"
    assert "agg_parking_daily.utilization_rate" in field_keys
    assert "dim_parking_lot.parking_lot_name" in field_keys


def test_average_parking_duration_links_order_times_and_minutes():
    result = _simulate_schema_link("平均停车时长是多少？", ["fact_parking_order"])
    field_keys = {field["field_key"] for field in result["fields"]}

    assert result["anchor"] == "fact_parking_order"
    assert {
        "fact_parking_order.entry_time",
        "fact_parking_order.exit_time",
        "fact_parking_order.parking_minutes",
        "fact_parking_order.order_status",
    } <= field_keys
