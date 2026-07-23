import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.executor.report_generator import ReportGenerator


def _summary(completed: int, failed: int = 0, skipped: int = 0) -> dict:
    return {
        "completed_steps": completed,
        "failed_steps": failed,
        "skipped_steps": skipped,
        "summary_text": (
            f"已完成 {completed} 个步骤，失败 {failed} 个步骤，跳过 {skipped} 个步骤。"
        ),
    }


def _step(
    step_id: str,
    step_name: str,
    rows: list[dict],
    *,
    success: bool = True,
    error: str | None = None,
) -> dict:
    return {
        "step_id": step_id,
        "step_name": step_name,
        "question": f"请执行子任务：{step_name}",
        "success": success,
        "status": "completed" if success else "failed",
        "sql": "SELECT ...",
        "columns": list(rows[0].keys()) if rows else [],
        "formatted": "",
        "rows": rows,
        "result_reference": f"memory://{step_id}" if success else None,
        "error": error,
        "metadata": {
            "detected_indicators": ["停车净收入"],
        },
    }


def _rich_report_payload() -> dict:
    return {
        "title": "最近一个季度智慧停车运营分析报告",
        "question_overview": "分析最近一个季度停车收入、订单、利用率和异常运营情况。",
        "executive_summary": "季度停车净收入逐月下降，A停车场下降贡献最大。",
        "data_scope": "2026年第二季度，覆盖A、B两个停车场，收入采用停车净收入口径。",
        "key_metrics": [
            {
                "metric_name": "停车净收入",
                "value": "960,000.00 元",
                "comparison": "季度内月末较月初下降22.22%",
                "interpretation": "收入呈持续下降趋势。",
                "evidence_step": "step_1",
            },
            {
                "metric_name": "车位利用率",
                "value": "61.00%",
                "comparison": "",
                "interpretation": "A停车场利用率低于B停车场。",
                "evidence_step": "step_3",
            },
        ],
        "key_findings": [
            "月度停车净收入从36万元下降至28万元。",
            "A停车场收入下降幅度最大。",
        ],
        "trend_analysis": ["停车净收入连续三个月下降。"],
        "ranking_analysis": ["B停车场收入高于A停车场。"],
        "anomaly_analysis": ["A停车场支付失败事件增加。"],
        "root_causes": [
            "A停车场完成订单量与利用率同步下降，是收入下降的重要关联因素。"
        ],
        "trend_judgment": "若订单量和利用率未恢复，短期收入仍有压力。",
        "action_suggestions": [
            "优先排查A停车场支付设备和入口设备。",
            "针对A停车场晚高峰前的低利用时段制定引流方案。",
        ],
        "data_limitations": ["缺少去年同期数据，不能给出同比结论。"],
        "visualization_suggestions": [
            {
                "chart_type": "line",
                "title": "月度停车净收入趋势",
                "purpose": "观察季度收入变化和拐点。",
                "source_step": "step_1",
            }
        ],
    }


def _quarter_steps() -> list[dict]:
    return [
        _step(
            "step_1",
            "分析季度停车收入趋势",
            [
                {"month": "2026-04", "net_revenue": 360000},
                {"month": "2026-05", "net_revenue": 320000},
                {"month": "2026-06", "net_revenue": 280000},
            ],
        ),
        _step(
            "step_2",
            "比较停车场收入排名",
            [
                {"parking_lot_name": "B停车场", "net_revenue": 520000},
                {"parking_lot_name": "A停车场", "net_revenue": 440000},
            ],
        ),
        _step(
            "step_3",
            "分析停车场利用率",
            [
                {"parking_lot_name": "A停车场", "utilization_rate": 0.61},
                {"parking_lot_name": "B停车场", "utilization_rate": 0.76},
            ],
        ),
        _step(
            "step_4",
            "分析异常事件",
            [
                {"event_type": "payment_failed", "exception_count": 18},
                {"event_type": "device_offline", "exception_count": 6},
            ],
        ),
    ]


def test_report_generator_parses_parking_operational_report():
    def fake_text_generator(system_msg: str, prompt: str) -> str:
        assert "智慧停车运营分析 Report Agent" in system_msg
        assert "原因必须说明证据" in system_msg
        assert "data_limitations" in prompt
        assert "visualization_suggestions" in prompt
        return json.dumps(_rich_report_payload(), ensure_ascii=False)

    report = ReportGenerator(text_generator=fake_text_generator).generate(
        original_question="分析最近一个季度停车运营情况。",
        analysis_goal="评估季度停车经营、车位效率和异常运营情况",
        step_results=_quarter_steps(),
        summary=_summary(completed=4),
    )

    assert report.title == "最近一个季度智慧停车运营分析报告"
    assert report.key_metrics[0].metric_name == "停车净收入"
    assert report.trend_analysis == ["停车净收入连续三个月下降。"]
    assert report.ranking_analysis == ["B停车场收入高于A停车场。"]
    assert report.anomaly_analysis == ["A停车场支付失败事件增加。"]
    assert report.data_limitations == ["缺少去年同期数据，不能给出同比结论。"]
    assert "## 三、关键指标" in report.markdown
    assert "## 七、原因分析" in report.markdown
    assert "## 十、可视化建议" in report.markdown


def test_report_prompt_keeps_representative_rows_without_unbounded_context():
    rows = [
        {"stat_date": f"2026-06-{day:02d}", "net_revenue": day * 1000}
        for day in range(1, 21)
    ]
    captured_prompt = ""

    def fake_text_generator(_system_msg: str, prompt: str) -> str:
        nonlocal captured_prompt
        captured_prompt = prompt
        return json.dumps(_rich_report_payload(), ensure_ascii=False)

    ReportGenerator(text_generator=fake_text_generator).generate(
        original_question="最近一个月停车收入趋势。",
        analysis_goal="分析最近一个月停车净收入趋势",
        step_results=[_step("step_1", "查询每日收入趋势", rows)],
        summary=_summary(completed=1),
    )

    assert '"row_count": 20' in captured_prompt
    assert "2026-06-01" in captured_prompt
    assert "2026-06-12" in captured_prompt
    assert "2026-06-18" in captured_prompt
    assert "2026-06-20" in captured_prompt
    assert "2026-06-13" not in captured_prompt


def test_fallback_extracts_single_value_parking_metric_and_kpi_card():
    report = ReportGenerator(
        text_generator=lambda _system_msg, _prompt: "not-json"
    ).generate(
        original_question="今天停车收入是多少？",
        analysis_goal="查询今日停车净收入",
        step_results=[
            _step(
                "step_1",
                "查询今日停车净收入",
                [{"parking_revenue": 125800.5}],
            )
        ],
        summary=_summary(completed=1),
    )

    assert report.key_metrics[0].metric_name == "停车净收入"
    assert report.key_metrics[0].value == "125,800.50 元"
    assert report.visualization_suggestions[0].chart_type == "kpi_card"
    assert "产品线" not in report.markdown
    assert "费用项" not in report.markdown


def test_fallback_diagnosis_is_evidence_conservative_and_records_failures():
    steps = [
        _step(
            "step_1",
            "查询最近三个月停车收入下降趋势",
            [
                {"month": "2026-04", "net_revenue": 360000},
                {"month": "2026-05", "net_revenue": 320000},
                {"month": "2026-06", "net_revenue": 280000},
            ],
        ),
        _step(
            "step_2",
            "比较停车场收入下降贡献",
            [
                {"parking_lot_name": "A停车场", "revenue_change": -80000},
                {"parking_lot_name": "B停车场", "revenue_change": -10000},
            ],
        ),
        _step(
            "step_3",
            "查询退款与异常事件",
            [],
            success=False,
            error="模拟数据库错误",
        ),
    ]

    report = ReportGenerator(
        text_generator=lambda _system_msg, _prompt: "not-json"
    ).generate(
        original_question="为什么某停车场收入下降？",
        analysis_goal="定位停车场收入下降的主要驱动因素",
        step_results=steps,
        summary=_summary(completed=2, failed=1),
    )

    assert report.root_causes
    assert "不会把指标相关性直接认定为因果" in report.root_causes[0]
    assert any("1 个步骤失败" in item for item in report.data_limitations)
    assert any("模板化降级报告" in item for item in report.data_limitations)
    assert any("退款" in item for item in report.action_suggestions)


@pytest.mark.parametrize(
    ("question", "steps", "expected_attribute"),
    [
        (
            "今天停车收入是多少？",
            [_step("step_1", "查询今日停车收入", [{"parking_revenue": 125800.5}])],
            "key_metrics",
        ),
        (
            "最近三个月收入趋势。",
            [_quarter_steps()[0]],
            "trend_analysis",
        ),
        (
            "哪个停车场收入下降？",
            [
                _step(
                    "step_1",
                    "比较停车场收入下降幅度",
                    [
                        {"parking_lot_name": "A停车场", "revenue_change": -80000},
                        {"parking_lot_name": "B停车场", "revenue_change": -10000},
                    ],
                )
            ],
            "anomaly_analysis",
        ),
        (
            "分析最近一个季度停车运营情况。",
            _quarter_steps(),
            "action_suggestions",
        ),
        (
            "为什么某停车场收入下降？",
            _quarter_steps(),
            "root_causes",
        ),
    ],
)
def test_five_parking_questions_generate_required_report_section(
    question: str,
    steps: list[dict],
    expected_attribute: str,
):
    report = ReportGenerator(
        text_generator=lambda _system_msg, _prompt: "not-json"
    ).generate(
        original_question=question,
        analysis_goal=question,
        step_results=steps,
        summary=_summary(completed=len(steps)),
    )

    assert getattr(report, expected_attribute)
    assert report.executive_summary
    assert report.action_suggestions
    assert "智慧停车运营分析报告" in report.title
