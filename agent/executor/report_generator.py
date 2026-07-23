"""
分析报告生成模块

把多步 SQL 执行后的结构化结果，整理为业务可读的分析报告。
默认优先调用 LLM 输出结构化 JSON；如果模型不可用或返回格式异常，
则回退到确定性的模板化报告，保证 Agent 输出层始终可用。
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Callable

from pydantic import BaseModel, Field

from tools.config import LLM_CONFIG
from text2sql.llm_client import LLMClient


ReportTextGenerator = Callable[[str, str], str]


class MetricInsight(BaseModel):
    """报告中的单个核心指标卡。"""

    metric_name: str
    value: str
    comparison: str = ""
    interpretation: str = ""
    evidence_step: str = ""


class VisualizationSuggestion(BaseModel):
    """根据实际查询结果生成的可视化建议。"""

    chart_type: str
    title: str
    purpose: str
    source_step: str = ""


class AnalysisReport(BaseModel):
    """面向智慧停车运营人员的结构化分析报告。"""

    title: str
    question_overview: str = ""
    executive_summary: str
    data_scope: str = ""
    key_metrics: list[MetricInsight] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list)
    trend_analysis: list[str] = Field(default_factory=list)
    ranking_analysis: list[str] = Field(default_factory=list)
    anomaly_analysis: list[str] = Field(default_factory=list)
    root_causes: list[str] = Field(default_factory=list)
    trend_judgment: str
    action_suggestions: list[str] = Field(default_factory=list)
    data_limitations: list[str] = Field(default_factory=list)
    visualization_suggestions: list[VisualizationSuggestion] = Field(default_factory=list)
    markdown: str = ""


class ReportGenerator:
    """将执行结果收敛为结构化报告。"""

    def __init__(self, text_generator: ReportTextGenerator | None = None):
        self.text_generator = text_generator or self._build_default_text_generator()

    def generate(
        self,
        original_question: str,
        analysis_goal: str,
        step_results: list[dict[str, Any]],
        summary: dict[str, Any],
    ) -> AnalysisReport:
        if self.text_generator is None:
            return self._build_fallback_report(
                original_question=original_question,
                analysis_goal=analysis_goal,
                step_results=step_results,
                summary=summary,
            )

        system_msg = self._build_system_message()
        prompt = self._build_prompt(
            original_question=original_question,
            analysis_goal=analysis_goal,
            step_results=step_results,
            summary=summary,
        )

        try:
            raw_text = self.text_generator(system_msg, prompt)
            parsed = self._parse_report_json(raw_text)
            if not parsed.question_overview:
                parsed.question_overview = original_question
            if not parsed.data_scope:
                parsed.data_scope = self._infer_data_scope(original_question)
            parsed.markdown = self._render_markdown(parsed)
            return parsed
        except Exception:
            return self._build_fallback_report(
                original_question=original_question,
                analysis_goal=analysis_goal,
                step_results=step_results,
                summary=summary,
            )

    @staticmethod
    def _build_system_message() -> str:
        return (
            "你是智慧停车运营分析 Report Agent，读者是停车运营负责人和区域经理。"
            "你的职责不是复述 SQL，而是把查询数据转化为可核验的经营结论、异常线索和行动建议。"
            "事实必须来自成功步骤的返回数据；原因必须说明证据，不能把相关性写成确定因果。"
            "数据不足时必须明确限制，禁止编造数值、同比、环比、停车场名称或原因。"
            "输出必须是合法 JSON。"
        )

    def _build_prompt(
        self,
        original_question: str,
        analysis_goal: str,
        step_results: list[dict[str, Any]],
        summary: dict[str, Any],
    ) -> str:
        report_context = {
            "original_question": original_question,
            "analysis_goal": analysis_goal,
            "summary": summary,
            "step_results": [self._compress_step_result(step_result) for step_result in step_results],
        }
        context_json = json.dumps(
            report_context,
            ensure_ascii=False,
            indent=2,
            default=self._json_default,
        )
        return (
            "请根据下面的 Agent 执行结果，生成智慧停车运营分析报告。\n\n"
            "【分析规则】\n"
            "1. 只把 status=completed 且 success=true 的步骤作为事实证据。\n"
            "2. 核心结论必须直接回答用户问题，并尽量包含数据中的具体数值、时间和停车场。\n"
            "3. 趋势分析应说明方向、起止值、变化量或变化率；上下文没有这些数据时不得补算或编造。\n"
            "4. 排名分析必须说明排序指标和对象；只返回一条数据时不能声称完成了全量排名。\n"
            "5. 原因分析必须形成“现象 → 驱动指标/异常证据 → 谨慎结论”的证据链。\n"
            "6. 同比、环比只有在上下文存在对应比较期数据时才能输出。\n"
            "7. 运营建议必须对应已有发现，并写成可执行动作；不得把建议写成已经发生的事实。\n"
            "8. 失败步骤、缺失维度、样本不足和时间范围不明确，必须写入 data_limitations。\n"
            "9. key_metrics 只放能从结果直接读取或严谨计算的指标；value 使用带单位的字符串。\n"
            "10. visualization_suggestions 只能基于本次实际返回的数据字段推荐图表。\n"
            "11. 如果某类分析没有证据，对应数组返回 []，不要用常识补齐。\n"
            "12. 只返回 JSON，不要输出 Markdown 或额外解释。\n\n"
            "【输出 JSON 结构】\n"
            "{\n"
            '  "title": "报告标题",\n'
            '  "question_overview": "用户问题与分析目标",\n'
            '  "executive_summary": "一句话核心结论",\n'
            '  "data_scope": "统计时间、停车场范围和数据口径",\n'
            '  "key_metrics": [\n'
            "    {\n"
            '      "metric_name": "指标名称",\n'
            '      "value": "指标值及单位",\n'
            '      "comparison": "同比/环比/排名信息，没有则为空字符串",\n'
            '      "interpretation": "指标说明",\n'
            '      "evidence_step": "证据步骤ID"\n'
            "    }\n"
            "  ],\n"
            '  "key_findings": ["关键数据发现"],\n'
            '  "trend_analysis": ["趋势分析"],\n'
            '  "ranking_analysis": ["停车场或时段排名分析"],\n'
            '  "anomaly_analysis": ["异常停车场、异常时段或异常事件"],\n'
            '  "root_causes": ["有数据证据支持的原因判断"],\n'
            '  "trend_judgment": "整体趋势判断；无趋势数据时明确说明",\n'
            '  "action_suggestions": ["与发现对应的可执行运营建议"],\n'
            '  "data_limitations": ["数据缺失、失败步骤或口径限制"],\n'
            '  "visualization_suggestions": [\n'
            "    {\n"
            '      "chart_type": "line/bar/pie/ranking/kpi_card/table",\n'
            '      "title": "图表标题",\n'
            '      "purpose": "图表希望帮助读者判断什么",\n'
            '      "source_step": "数据来源步骤ID"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            f"上下文如下：\n{context_json}"
        )

    @staticmethod
    def _compress_step_result(step_result: dict[str, Any]) -> dict[str, Any]:
        rows = step_result.get("rows") or []
        columns = step_result.get("columns") or (
            list(rows[0].keys()) if rows and isinstance(rows[0], dict) else []
        )
        rows_preview = rows[:12]
        rows_tail = rows[-3:] if len(rows) > 12 else []
        formatted = (step_result.get("formatted") or "").strip()
        if ReportGenerator._looks_like_table(formatted):
            formatted = ""

        metadata = step_result.get("metadata") or {}
        return {
            "step_id": step_result.get("step_id"),
            "step_name": step_result.get("step_name"),
            "question": step_result.get("question"),
            "status": step_result.get("status"),
            "success": step_result.get("success"),
            "sql": (step_result.get("sql") or "")[:1500],
            "columns": columns,
            "row_count": len(rows),
            "rows_preview": rows_preview,
            "rows_tail": rows_tail,
            "formatted_summary": formatted[:600],
            "result_reference": step_result.get("result_reference"),
            "error": step_result.get("error"),
            "detected_indicators": metadata.get("detected_indicators", []),
        }

    @staticmethod
    def _parse_report_json(raw_text: str) -> AnalysisReport:
        cleaned = raw_text.strip()
        cleaned = re.sub(r"^```json\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        payload = json.loads(cleaned)
        return AnalysisReport.model_validate(payload)

    def _build_fallback_report(
        self,
        original_question: str,
        analysis_goal: str,
        step_results: list[dict[str, Any]],
        summary: dict[str, Any],
    ) -> AnalysisReport:
        completed_results = [
            step_result for step_result in step_results
            if step_result.get("status") == "completed" and step_result.get("success")
        ]
        key_findings = [
            f"{step_result.get('step_name')}：{self._pick_step_brief(step_result)}"
            for step_result in completed_results
        ] or ["当前执行结果不足，暂时无法提炼关键发现。"]

        report_type = self._classify_report_type(original_question)
        root_causes = self._build_fallback_causes(
            report_type=report_type,
            completed_results=completed_results,
        )
        data_limitations = self._build_data_limitations(
            report_type=report_type,
            completed_results=completed_results,
            summary=summary,
        )
        action_suggestions = self._build_fallback_actions(
            original_question=original_question,
            report_type=report_type,
        )
        trend_analysis = self._collect_analysis_briefs(
            completed_results,
            keywords=("趋势", "变化", "同比", "环比", "日期", "月份", "季度"),
        )
        ranking_analysis = self._collect_analysis_briefs(
            completed_results,
            keywords=("排名", "最高", "最低", "停车场", "贡献"),
        )
        anomaly_analysis = self._collect_analysis_briefs(
            completed_results,
            keywords=("异常", "下降", "下滑", "退款", "人工抬杆", "免费放行"),
        )
        first_finding = key_findings[0] if completed_results else ""
        executive_summary = (
            f"已围绕“{original_question}”完成 {len(completed_results)} 项有效查询。"
            f"{first_finding}"
            if completed_results
            else f"“{original_question}”当前没有足够的成功查询结果，无法形成可靠运营结论。"
        )
        report = AnalysisReport(
            title=f"{original_question}｜智慧停车运营分析报告",
            question_overview=f"用户问题：{original_question}；分析目标：{analysis_goal}",
            executive_summary=executive_summary,
            data_scope=self._infer_data_scope(original_question),
            key_metrics=self._extract_key_metrics(completed_results),
            key_findings=key_findings,
            trend_analysis=trend_analysis,
            ranking_analysis=ranking_analysis,
            anomaly_analysis=anomaly_analysis,
            root_causes=root_causes,
            trend_judgment=self._build_fallback_trend_judgment(
                report_type=report_type,
                trend_analysis=trend_analysis,
            ),
            action_suggestions=action_suggestions,
            data_limitations=data_limitations,
            visualization_suggestions=self._build_visualization_suggestions(
                completed_results
            ),
        )
        report.markdown = self._render_markdown(report)
        return report

    @staticmethod
    def _classify_report_type(question: str) -> str:
        if any(keyword in question for keyword in ("原因", "为什么", "下降", "下滑")):
            return "diagnosis"
        if any(keyword in question for keyword in ("运营情况", "运营分析", "综合分析")):
            return "overview"
        if any(keyword in question for keyword in ("趋势", "变化", "同比", "环比")):
            return "trend"
        if any(keyword in question for keyword in ("哪个停车场", "最高", "最低", "排名")):
            return "ranking"
        return "summary"

    @staticmethod
    def _infer_data_scope(question: str) -> str:
        if "今天" in question or "今日" in question:
            return "统计时间为今天；停车场范围和指标口径以成功 SQL 步骤为准。"
        if "最近三个月" in question or "近三个月" in question:
            return "统计时间为最近三个月；停车场范围和指标口径以成功 SQL 步骤为准。"
        if "最近一个季度" in question or "近一个季度" in question:
            return "统计时间为最近一个季度；停车场范围和指标口径以成功 SQL 步骤为准。"
        return "统计时间、停车场范围和指标口径以成功 SQL 步骤为准。"

    @staticmethod
    def _build_fallback_causes(
        report_type: str,
        completed_results: list[dict[str, Any]],
    ) -> list[str]:
        if report_type != "diagnosis":
            return []
        if len(completed_results) < 2:
            return []
        return [
            "已获得多项经营指标证据，但模板化降级报告不会把指标相关性直接认定为因果；"
            "需要结合各步骤的同期变化和停车场贡献进一步确认。"
        ]

    @staticmethod
    def _build_data_limitations(
        report_type: str,
        completed_results: list[dict[str, Any]],
        summary: dict[str, Any],
    ) -> list[str]:
        limitations: list[str] = []
        failed_steps = summary.get("failed_steps", 0)
        skipped_steps = summary.get("skipped_steps", 0)
        if failed_steps or skipped_steps:
            limitations.append(
                f"本次有 {failed_steps} 个步骤失败、{skipped_steps} 个步骤跳过，"
                "结论只覆盖成功返回的数据。"
            )
        if not completed_results:
            limitations.append("没有成功查询结果，当前报告不能形成数据结论。")
        if report_type == "diagnosis":
            limitations.append(
                "当前为模型不可用或输出异常时的模板化降级报告，"
                "仅整理证据，不自动给出确定性根因。"
            )
        return limitations

    @staticmethod
    def _build_fallback_actions(
        original_question: str,
        report_type: str,
    ) -> list[str]:
        if report_type == "diagnosis":
            return [
                "按停车场核对停车净收入、完成订单量和平均订单金额的同期变化。",
                "复查退款、优惠、车位利用率和异常事件是否与收入下降时段一致。",
                "优先处理下降贡献较大且存在高严重度未解决异常的停车场。",
            ]
        if "利用率" in original_question:
            return [
                "对低利用率停车场按小时进一步拆分，识别持续低谷时段。",
                "结合周边需求和收费规则评估分时定价或引流活动。",
            ]
        if report_type == "trend":
            return [
                "持续按相同口径监控停车收入与完成订单量，避免统计范围变化造成误判。",
                "对波动最大的月份下钻到停车场和小时维度。",
            ]
        if report_type == "ranking":
            return [
                "复核排名首尾停车场的数据完整性和统计口径。",
                "对低表现停车场进一步比较订单量、利用率、退款和异常事件。",
            ]
        if report_type == "overview":
            return [
                "建立停车净收入、订单量、利用率、停车时长和异常事件的固定运营看板。",
                "优先跟进收入下降、利用率偏低或异常事件集中的停车场。",
            ]
        return ["持续按相同指标口径监控，并在出现显著波动时下钻到停车场和时段。"]

    def _collect_analysis_briefs(
        self,
        completed_results: list[dict[str, Any]],
        keywords: tuple[str, ...],
    ) -> list[str]:
        briefs: list[str] = []
        for step_result in completed_results:
            searchable_text = " ".join(
                str(step_result.get(field) or "")
                for field in ("step_name", "question", "formatted")
            )
            if any(keyword in searchable_text for keyword in keywords):
                briefs.append(
                    f"{step_result.get('step_name')}：{self._pick_step_brief(step_result)}"
                )
        return briefs

    @staticmethod
    def _build_fallback_trend_judgment(
        report_type: str,
        trend_analysis: list[str],
    ) -> str:
        if trend_analysis:
            return "已返回趋势相关数据，具体方向和幅度见趋势分析；模板报告不补算缺失比较值。"
        if report_type in {"trend", "diagnosis", "overview"}:
            return "当前成功结果不足以形成可靠趋势判断。"
        return "当前问题不要求趋势判断，或查询结果未包含时间序列。"

    @classmethod
    def _extract_key_metrics(
        cls,
        completed_results: list[dict[str, Any]],
    ) -> list[MetricInsight]:
        """只从单行汇总结果中提取可直接核验的停车核心指标。"""
        metric_names = {
            "parking_revenue": "停车净收入",
            "parking_net_revenue": "停车净收入",
            "net_revenue": "停车净收入",
            "completed_order_count": "完成订单量",
            "order_count": "完成订单量",
            "average_order_amount": "平均订单金额",
            "average_parking_minutes": "平均停车时长",
            "average_parking_duration": "平均停车时长",
            "average_utilization_rate": "车位利用率",
            "utilization_rate": "车位利用率",
            "refund_amount": "退款金额",
            "exception_count": "异常事件数",
            "manual_open_count": "人工抬杆次数",
            "free_release_count": "免费放行次数",
            "estimated_loss": "预估收入损失",
        }
        insights: list[MetricInsight] = []
        seen_metrics: set[str] = set()

        for step_result in completed_results:
            rows = step_result.get("rows") or []
            if len(rows) != 1 or not isinstance(rows[0], dict):
                continue
            for field_name, value in rows[0].items():
                metric_name = metric_names.get(field_name.lower())
                if not metric_name or metric_name in seen_metrics or value is None:
                    continue
                insights.append(MetricInsight(
                    metric_name=metric_name,
                    value=cls._format_metric_value(field_name, value),
                    interpretation="该数值直接来自成功查询步骤。",
                    evidence_step=str(step_result.get("step_id") or ""),
                ))
                seen_metrics.add(metric_name)
                if len(insights) >= 8:
                    return insights
        return insights

    @staticmethod
    def _format_metric_value(field_name: str, value: Any) -> str:
        normalized_name = field_name.lower()
        if "utilization_rate" in normalized_name:
            try:
                numeric_value = float(value)
                percentage = numeric_value * 100 if abs(numeric_value) <= 1 else numeric_value
                return f"{percentage:.2f}%"
            except (TypeError, ValueError):
                return str(value)
        if any(
            keyword in normalized_name
            for keyword in ("revenue", "amount", "loss")
        ):
            try:
                return f"{float(value):,.2f} 元"
            except (TypeError, ValueError):
                return str(value)
        if "minute" in normalized_name or "duration" in normalized_name:
            try:
                return f"{float(value):,.2f} 分钟"
            except (TypeError, ValueError):
                return str(value)
        if any(
            keyword in normalized_name
            for keyword in ("count", "order")
        ):
            try:
                return f"{int(value):,}"
            except (TypeError, ValueError):
                return str(value)
        return str(value)

    @staticmethod
    def _build_visualization_suggestions(
        completed_results: list[dict[str, Any]],
    ) -> list[VisualizationSuggestion]:
        suggestions: list[VisualizationSuggestion] = []
        seen_types: set[str] = set()

        for step_result in completed_results:
            rows = step_result.get("rows") or []
            if not rows or not isinstance(rows[0], dict):
                continue
            columns = {
                str(column).lower()
                for row in rows
                if isinstance(row, dict)
                for column in row
            }
            step_id = str(step_result.get("step_id") or "")
            step_name = str(step_result.get("step_name") or "查询结果")

            has_time = bool(columns & {
                "stat_date", "stat_hour", "date", "month", "quarter", "时间", "月份"
            })
            has_parking_lot = bool(columns & {
                "parking_lot_id", "parking_lot_name", "停车场", "停车场名称"
            })
            has_category = bool(columns & {
                "payment_method", "order_type", "event_type", "支付方式", "订单类型", "异常类型"
            })
            numeric_columns = [
                column
                for column, value in rows[0].items()
                if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
            ]

            if has_time and numeric_columns and "line" not in seen_types:
                suggestions.append(VisualizationSuggestion(
                    chart_type="line",
                    title=f"{step_name}趋势图",
                    purpose="观察指标随时间的变化方向、拐点和异常波动。",
                    source_step=step_id,
                ))
                seen_types.add("line")
            if has_parking_lot and numeric_columns and "ranking" not in seen_types:
                suggestions.append(VisualizationSuggestion(
                    chart_type="ranking",
                    title=f"{step_name}停车场排行",
                    purpose="比较不同停车场的经营表现并识别首尾对象。",
                    source_step=step_id,
                ))
                seen_types.add("ranking")
            if has_category and numeric_columns and "pie" not in seen_types:
                suggestions.append(VisualizationSuggestion(
                    chart_type="pie",
                    title=f"{step_name}构成图",
                    purpose="观察支付方式、订单类型或异常类型的构成占比。",
                    source_step=step_id,
                ))
                seen_types.add("pie")
            if len(rows) == 1 and numeric_columns and "kpi_card" not in seen_types:
                suggestions.append(VisualizationSuggestion(
                    chart_type="kpi_card",
                    title=f"{step_name}指标卡",
                    purpose="突出展示单值核心运营指标。",
                    source_step=step_id,
                ))
                seen_types.add("kpi_card")

        return suggestions[:4]

    @staticmethod
    def _pick_step_brief(step_result: dict[str, Any]) -> str:
        formatted = (step_result.get("formatted") or "").strip()
        if formatted and not ReportGenerator._looks_like_table(formatted):
            return formatted.splitlines()[0][:120]
        rows = step_result.get("rows") or []
        if rows:
            row = rows[0]
            parts = [
                f"{key}={value}"
                for key, value in row.items()
                if value is not None
            ]
            return "，".join(parts[:4])[:120]
        if formatted:
            for line in formatted.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if set(stripped) <= {"-", "+"}:
                    continue
                return stripped[:120]
        error = step_result.get("error")
        if error:
            return f"执行失败：{error}"
        return "步骤已完成，但当前没有可展示的结果摘要。"

    @staticmethod
    def _render_markdown(report: AnalysisReport) -> str:
        metric_lines = [
            "| 指标 | 当前值 | 对比 | 业务解释 | 证据步骤 |",
            "|---|---:|---|---|---|",
            *[
                f"| {metric.metric_name} | {metric.value} | "
                f"{metric.comparison or '-'} | {metric.interpretation or '-'} | "
                f"{metric.evidence_step or '-'} |"
                for metric in report.key_metrics
            ],
        ] if report.key_metrics else ["- 当前结果未形成可直接展示的单值指标卡。"]

        trend_lines = (
            [*[f"- {item}" for item in report.trend_analysis], "", report.trend_judgment]
            if report.trend_analysis
            else [report.trend_judgment]
        )
        ranking_lines = (
            [f"- {item}" for item in report.ranking_analysis]
            or ["- 当前结果没有足够的排名数据。"]
        )
        anomaly_lines = (
            [f"- {item}" for item in report.anomaly_analysis]
            or ["- 当前结果没有识别出可核验的异常信息。"]
        )
        cause_lines = (
            [f"- {cause}" for cause in report.root_causes]
            or ["- 当前问题无需归因，或现有证据不足以判断原因。"]
        )
        action_lines = (
            [f"- {suggestion}" for suggestion in report.action_suggestions]
            or ["- 当前证据不足，建议先补充数据再制定运营动作。"]
        )

        sections = [
            f"# {report.title}",
            "",
            "## 一、问题概述",
            report.question_overview,
            "",
            f"数据范围：{report.data_scope}",
            "",
            "## 二、核心结论",
            report.executive_summary,
            "",
            "## 三、关键指标",
            *metric_lines,
            "",
            "## 关键发现",
            *[f"- {finding}" for finding in report.key_findings],
            "",
            "## 五、趋势分析",
            *trend_lines,
            "",
            "## 六、排名与异常分析",
            "### 排名分析",
            *ranking_lines,
            "",
            "### 异常分析",
            *anomaly_lines,
            "",
            "## 七、原因分析",
            *cause_lines,
            "",
            "## 八、运营建议",
            *action_lines,
        ]

        if report.data_limitations:
            sections.extend([
                "",
                "## 九、数据限制",
                *[f"- {limitation}" for limitation in report.data_limitations],
            ])

        if report.visualization_suggestions:
            sections.extend([
                "",
                "## 十、可视化建议",
                *[
                    f"- **{item.chart_type}｜{item.title}**：{item.purpose}"
                    f"{f'（来源：{item.source_step}）' if item.source_step else ''}"
                    for item in report.visualization_suggestions
                ],
            ])

        return "\n".join(sections)

    @staticmethod
    def _json_default(value: Any) -> str:
        if isinstance(value, Decimal):
            return str(value)
        return str(value)

    @staticmethod
    def _looks_like_table(formatted: str) -> bool:
        text = formatted.strip()
        return "+" in text and "|" in text

    @staticmethod
    def _build_default_text_generator() -> ReportTextGenerator | None:
        if not LLM_CONFIG.get("api_key"):
            return None

        client = LLMClient()

        def _generate(system_msg: str, prompt: str) -> str:
            return client.generate_text(system_msg=system_msg, prompt=prompt)

        return _generate
