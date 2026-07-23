from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.planner.query_decomposer import (
    QueryDecomposer,
    build_decomposition_prompt,
    build_parking_fallback_plan,
)


def test_build_decomposition_prompt_includes_schema_and_indicator_catalog():
    system_msg, prompt = build_decomposition_prompt(
        "分析近半年的停车收入变化情况",
        planning_context={
            "metrics": ["停车净收入"],
            "tables": ["agg_parking_daily"],
            "fields": ["agg_parking_daily.net_revenue"],
        },
    )

    assert "【数据库 Schema】" in prompt
    assert "【可用分析维度】" in prompt
    assert "【可用指标】" in prompt
    assert "【智慧停车分析拆解策略】" in prompt
    assert "【规划前召回上下文】" in prompt
    assert "停车场" in prompt
    assert "停车净收入" in prompt
    assert "agg_parking_daily" in prompt
    assert "智慧停车" in system_msg


def test_query_decomposer_rejects_unsupported_dimensions():
    raw_response = json.dumps(
        {
            "question_type": "停车收入下降原因分析",
            "analysis_goal": "定位停车收入下降原因",
            "subtasks": [
                {
                    "task_id": "task_1",
                    "task_name": "确认最近三个月停车收入趋势及下降幅度",
                    "task_type": "时间趋势分析",
                    "description": "查看停车净收入趋势",
                    "depends_on": [],
                    "dimensions": ["月份"],
                    "metrics": ["停车净收入"],
                },
                {
                    "task_id": "task_2",
                    "task_name": "拆解收入驱动指标变化",
                    "task_type": "指标拆解分析",
                    "description": "分析订单量和退款金额",
                    "depends_on": ["task_1"],
                    "dimensions": ["月份"],
                    "metrics": ["完成订单量", "退款金额"],
                },
                {
                    "task_id": "task_3",
                    "task_name": "分析停车场对收入下降的贡献",
                    "task_type": "维度对比分析",
                    "description": "分析停车场收入贡献",
                    "depends_on": ["task_1", "task_2"],
                    "dimensions": ["月份", "停车场", "渠道"],
                    "metrics": ["停车净收入", "完成订单量"],
                },
            ],
        },
        ensure_ascii=False,
    )

    decomposer = QueryDecomposer(
        response_generator=lambda _system_msg, _prompt: raw_response
    )

    try:
        decomposer.decompose("最近三个月停车收入为什么下降？")
    except ValueError as exc:
        assert "不支持的分析维度" in str(exc)
        assert "渠道" in str(exc)
    else:
        raise AssertionError("应当拒绝未注册维度")


def test_query_decomposer_retries_when_trend_plan_has_too_many_tasks():
    oversized_plan = json.dumps(
        {
            "question_type": "trend_analysis",
            "analysis_goal": "分析近半年停车收入变化情况",
            "subtasks": [
                {
                    "task_id": f"task_{i}",
                    "task_name": f"任务{i}",
                    "task_type": "aggregation",
                    "description": "测试任务",
                    "depends_on": [],
                    "dimensions": ["月份"],
                    "metrics": ["停车净收入"],
                }
                for i in range(1, 8)
            ],
        },
        ensure_ascii=False,
    )
    valid_plan = json.dumps(
        {
            "question_type": "trend_analysis",
            "analysis_goal": "分析近半年停车收入变化情况",
            "subtasks": [
                {
                    "task_id": "task_1",
                    "task_name": "统计近半年月度停车收入趋势",
                    "task_type": "aggregation",
                    "description": "查看停车净收入趋势",
                    "depends_on": [],
                    "dimensions": ["月份"],
                    "metrics": ["停车净收入"],
                },
                {
                    "task_id": "task_2",
                    "task_name": "统计近半年月度订单趋势",
                    "task_type": "aggregation",
                    "description": "查看完成订单量趋势",
                    "depends_on": [],
                    "dimensions": ["月份"],
                    "metrics": ["完成订单量"],
                },
            ],
        },
        ensure_ascii=False,
    )
    responses = iter([oversized_plan, valid_plan])

    decomposer = QueryDecomposer(
        response_generator=lambda _system_msg, _prompt: next(responses)
    )

    result = decomposer.decompose("分析近半年的停车收入变化情况")

    assert len(result["subtasks"]) == 2


def test_query_decomposer_uses_parking_fallback_when_llm_is_unavailable():
    decomposer = QueryDecomposer(
        response_generator=lambda _system_msg, _prompt: (_ for _ in ()).throw(
            ConnectionError("模型服务不可用")
        )
    )

    result = decomposer.decompose("哪个停车场收入下降，原因是什么？")

    assert result["question_type"] == "parking_revenue_diagnosis"
    assert len(result["subtasks"]) == 4
    assert result["subtasks"][0]["metrics"] == ["停车净收入"]
    assert "异常事件数" in result["subtasks"][3]["metrics"]


def test_parking_fallback_keeps_simple_query_as_one_task():
    result = build_parking_fallback_plan("今天停车收入是多少？")

    assert len(result["subtasks"]) == 1
    assert result["subtasks"][0]["metrics"] == ["停车净收入"]
