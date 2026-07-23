"""智慧停车 Agent 的离线 Workflow 回归测试。"""

from agent.executor.report_generator import ReportGenerator
from agent.planner.query_decomposer import build_parking_fallback_plan
from agent.workflow.agent_planner import (
    ParkingContextResolver,
    ParkingPlanningContext,
    PlanAndExecuteAgent,
    PlanGenerator,
    ResultSummarizer,
    StepExecutor,
)


def _fake_metric_context(_question: str) -> dict:
    return {
        "detected_indicators": ["停车净收入"],
        "retrieved_indicators": [
            {
                "name": "停车净收入",
                "formula": "SUM(net_revenue)",
                "tables": ["agg_parking_daily"],
                "fields": ["net_revenue", "stat_date"],
                "dimensions": ["时间", "停车场"],
                "is_dependency": False,
                "is_related": False,
            }
        ],
    }


def _fake_schema_context(_question: str) -> dict:
    return {
        "tables": [
            {"table_name": "agg_parking_daily"},
            {"table_name": "dim_parking_lot"},
        ],
        "fields": [
            {"field_key": "agg_parking_daily.net_revenue"},
            {"field_key": "agg_parking_daily.stat_date"},
            {"field_key": "dim_parking_lot.parking_lot_name"},
        ],
        "anchor": "agg_parking_daily",
        "join_path": {
            "anchor": "agg_parking_daily",
            "joins": ["agg_parking_daily.parking_lot_id = dim_parking_lot.parking_lot_id"],
        },
    }


def _fake_step_runner(question: str) -> dict:
    return {
        "success": True,
        "sql": "SELECT 1 AS metric_value",
        "columns": ["metric_value"],
        "rows": [{"metric_value": 1}],
        "formatted": f"已执行：{question.splitlines()[0]}",
        "metadata": {
            "detected_indicators": ["停车净收入"],
            "used_schema_linking": True,
            "used_indicator_rag": True,
        },
    }


def _parking_report_generator() -> ReportGenerator:
    return ReportGenerator(
        text_generator=lambda _system_msg, _prompt: """
        {
          "title": "智慧停车运营分析报告",
          "executive_summary": "已完成停车运营指标查询。",
          "key_findings": ["已获得各步骤结构化数据。"],
          "root_causes": ["最终原因需要根据步骤数据综合判断。"],
          "trend_judgment": "当前测试链路执行正常。",
          "action_suggestions": ["继续观察停车收入和利用率。"]
        }
        """
    )


def _build_agent() -> PlanAndExecuteAgent:
    resolver = ParkingContextResolver(
        metric_retriever=_fake_metric_context,
        schema_retriever=_fake_schema_context,
    )
    return PlanAndExecuteAgent(
        planner=PlanGenerator(),
        executor=StepExecutor(step_runner=_fake_step_runner),
        summarizer=ResultSummarizer(),
        report_generator=_parking_report_generator(),
        context_resolver=resolver,
    )


def test_parking_context_resolver_records_metric_and_schema_tools():
    context = ParkingContextResolver(
        metric_retriever=_fake_metric_context,
        schema_retriever=_fake_schema_context,
    ).resolve("哪个停车场收入最高？")

    assert context.intent == "ranking"
    assert context.metrics == ["停车净收入"]
    assert context.tables == ["agg_parking_daily", "dim_parking_lot"]
    assert context.anchor == "agg_parking_daily"
    assert [trace.tool_name for trace in context.tool_traces] == [
        "metric_retriever",
        "schema_retriever",
    ]


def test_plan_steps_declare_complete_chatbi_tool_chain():
    plan = PlanGenerator().build_plan(
        "今天停车收入是多少？",
        build_parking_fallback_plan("今天停车收入是多少？"),
    )

    assert plan.steps[0].tools == [
        "metric_retriever",
        "schema_retriever",
        "text2sql",
        "sql_executor",
    ]


def test_agent_state_records_context_execution_metadata_and_completion():
    question = "今天停车收入是多少？"
    decomposition = build_parking_fallback_plan(question)
    result = _build_agent().run(
        question,
        decomposition_override=decomposition,
        resolve_context=True,
    )

    state = result["agent_state"]
    assert state["status"] == "completed"
    assert state["intent"] == "query"
    assert state["metrics"] == ["停车净收入"]
    assert state["tables"] == ["agg_parking_daily", "dim_parking_lot"]
    assert state["step_results"][0]["metadata"]["used_indicator_rag"] is True
    assert state["step_results"][0]["metadata"]["used_schema_linking"] is True
    assert [trace["tool_name"] for trace in state["tool_traces"]] == [
        "metric_retriever",
        "schema_retriever",
        "planner",
        "text2sql_sql_executor",
    ]


def test_four_parking_questions_have_expected_workflow_sizes():
    cases = [
        ("今天停车收入是多少？", 1),
        ("最近三个月收入趋势。", 1),
        ("哪个停车场收入下降，原因是什么？", 4),
        ("分析最近一个季度停车运营情况。", 4),
    ]

    for question, expected_steps in cases:
        decomposition = build_parking_fallback_plan(question)
        result = _build_agent().run(
            question,
            decomposition_override=decomposition,
            resolve_context=True,
        )

        assert len(result["plan"]["steps"]) == expected_steps
        assert len(result["step_results"]) == expected_steps
        assert result["summary"]["completed_steps"] == expected_steps
        assert result["summary"]["failed_steps"] == 0
        assert result["report"]["title"] == "智慧停车运营分析报告"


def test_context_retrieval_failure_is_recorded_but_not_raised():
    resolver = ParkingContextResolver(
        metric_retriever=lambda _question: (_ for _ in ()).throw(
            ConnectionError("指标服务不可用")
        ),
        schema_retriever=lambda _question: (_ for _ in ()).throw(
            ConnectionError("Schema服务不可用")
        ),
    )

    context: ParkingPlanningContext = resolver.resolve("今天停车收入是多少？")

    assert context.metrics == []
    assert context.tables == []
    assert len(context.errors) == 2
    assert all(trace.status == "failed" for trace in context.tool_traces)


def test_agent_state_marks_completed_workflow_with_failed_steps():
    agent = PlanAndExecuteAgent(
        planner=PlanGenerator(),
        executor=StepExecutor(
            step_runner=lambda _question: {
                "success": False,
                "error": "模拟 SQL 执行失败",
            },
            max_retries=0,
        ),
        summarizer=ResultSummarizer(),
        report_generator=_parking_report_generator(),
    )
    question = "今天停车收入是多少？"

    result = agent.run(
        question,
        decomposition_override=build_parking_fallback_plan(question),
    )

    assert result["summary"]["failed_steps"] == 1
    assert result["agent_state"]["status"] == "completed_with_errors"
    assert result["agent_state"]["errors"] == ["step_1: 模拟 SQL 执行失败"]
