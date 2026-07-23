"""智慧停车 Query 拆解器。

将停车运营问题拆成可由 Text2SQL 独立执行的数据证据任务，最终总结仍由
Workflow 的 Summarizer/Report 阶段完成。
"""

import json
from pathlib import Path
import re
from typing import Callable

from pydantic import BaseModel, Field, ValidationError

from prompts.builder import SCHEMA
from text2sql.llm_client import LLMClient


class DecomposedTask(BaseModel):
    """单个子任务定义。"""

    task_id: str
    task_name: str
    task_type: str
    description: str
    depends_on: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)


class DecompositionPlan(BaseModel):
    """复杂查询拆解结果。"""

    question_type: str
    analysis_goal: str
    subtasks: list[DecomposedTask]


AVAILABLE_DIMENSIONS = [
    "时间",
    "日期",
    "小时",
    "月份",
    "季度",
    "停车场",
    "城市",
    "停车场类型",
    "订单类型",
    "支付方式",
    "异常类型",
    "严重程度",
    "处理状态",
]


def _load_indicator_definitions() -> list[dict]:
    config_path = Path(__file__).parents[2] / "rag" / "indicators_full.json"
    with config_path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    return data.get("indicators", [])


def _load_indicator_catalog() -> list[str]:
    """加载标准指标名和别名，供 Planner 使用。"""
    names: list[str] = []
    for indicator in _load_indicator_definitions():
        names.append(indicator["name"])
        names.extend(indicator.get("aliases", []))
    return names


def _indicator_alias_map() -> dict[str, str]:
    """建立指标名称/别名到标准名称的映射。"""
    aliases: dict[str, str] = {}
    for indicator in _load_indicator_definitions():
        standard_name = indicator["name"]
        aliases[standard_name.lower()] = standard_name
        for alias in indicator.get("aliases", []):
            aliases[alias.lower()] = standard_name
    return aliases


def _detect_metrics(question: str) -> list[str]:
    """为确定性 fallback 识别问题中明确出现的停车指标。"""
    normalized_question = question.lower()
    detected: list[str] = []
    for alias, standard_name in _indicator_alias_map().items():
        if alias in normalized_question and standard_name not in detected:
            detected.append(standard_name)
    return detected


def _max_tasks_for_question_type(question_type: str) -> int:
    normalized = question_type.lower()
    if "trend" in normalized or "趋势" in question_type:
        return 6
    if "diagnosis" in normalized or "原因" in question_type or "下降" in question_type:
        return 6
    if "dimension" in normalized or "维度" in question_type:
        return 5
    if "overview" in normalized or "总览" in question_type or "运营" in question_type:
        return 6
    return 8


def _format_planning_context(planning_context: dict | None) -> str:
    """把规划前的指标和 Schema 召回压缩成 Planner 可读上下文。"""
    if not planning_context:
        return "（未提供规划前召回上下文，请依据完整目录规划）"

    metrics = planning_context.get("metrics", [])
    tables = planning_context.get("tables", [])
    fields = planning_context.get("fields", [])
    return (
        f"已识别指标：{'、'.join(metrics) if metrics else '无明确命中'}\n"
        f"候选表：{'、'.join(tables) if tables else '无明确召回'}\n"
        f"候选字段：{'、'.join(fields[:20]) if fields else '无明确召回'}"
    )


def build_decomposition_prompt(
    user_question: str,
    planning_context: dict | None = None,
) -> tuple[str, str]:
    """构造智慧停车 Query 拆解 Prompt。"""
    indicator_catalog = "、".join(_load_indicator_catalog())
    dimension_catalog = "、".join(AVAILABLE_DIMENSIONS)
    resolved_context = _format_planning_context(planning_context)
    system_msg = (
        "你是智慧停车 ChatBI 系统中的任务规划器。"
        "请把停车运营分析问题拆成可由 Text2SQL 独立执行的数据查询子任务，"
        "输出必须是 JSON，不要输出额外解释。"
    )
    prompt = f"""
【数据库 Schema】
{SCHEMA}

【可用分析维度】
{dimension_catalog}

【可用指标】
{indicator_catalog}

【规划前召回上下文】
{resolved_context}

请将下面的智慧停车运营问题拆解为结构化子任务，并严格输出 JSON：

用户问题：{user_question}

输出要求：
1. 顶层字段包含 question_type、analysis_goal、subtasks
2. subtasks 是有序数组，每个任务必须包含 task_id、task_name、task_type、description、depends_on、dimensions、metrics
3. task_id 使用 task_1、task_2 这类格式
4. depends_on 只能引用前面已经出现的 task_id
5. 每个子任务必须对应一条独立、可执行的数据查询，不要生成需要再次交给 Text2SQL 的“总结结论”任务
6. 仅返回 JSON 对象，不要使用 Markdown
7. 维度只能从【可用分析维度】中选择；未建模维度应回退到最接近的已建模维度
8. 指标优先复用【可用指标】中的标准名称或别名
9. 如果一个任务同时要求多个维度或多个驱动因素，请拆成多个更简单的子任务
10. 简单查询、单指标趋势和单一排名通常只需要 1 个子任务，不要过度拆解
11. 最终原因归纳和运营总结由 Workflow 后置阶段完成，Planner 只负责查询证据

【智慧停车分析拆解策略】
1. 简单指标：例如“今天停车收入”，只查询停车净收入，不扩展无关指标
2. 单指标趋势：例如“最近三个月收入趋势”，只查询按月停车净收入趋势
3. 停车场排名：查询目标指标并按停车场维度比较，通常 1 个子任务
4. 收入下降诊断：拆成“收入趋势 + 完成订单量变化 + 停车场收入贡献 + 退款/利用率/异常等驱动证据”，通常 4~5 个查询任务
5. 停车运营总览：拆成“收入与订单 + 利用率与停车时长 + 停车场排名 + 异常运营”，通常不超过 5 个查询任务
6. 不要把利润、成本、客户、产品等旧销售概念加入停车任务计划
""".strip()
    return system_msg, prompt


def build_parking_fallback_plan(user_question: str) -> dict:
    """Planner LLM 不可用时生成最小、可执行的停车任务计划。"""
    question = user_question.strip()
    detected_metrics = _detect_metrics(question)
    is_diagnosis = any(keyword in question for keyword in ("原因", "为什么", "下降", "下滑"))
    is_overview = any(keyword in question for keyword in ("运营情况", "运营分析", "综合分析", "经营情况"))
    is_ranking = any(keyword in question for keyword in ("哪个停车场", "最高", "最低", "排名"))
    is_trend = any(keyword in question for keyword in ("趋势", "变化", "同比", "环比"))

    if is_diagnosis:
        return {
            "question_type": "parking_revenue_diagnosis",
            "analysis_goal": "定位停车收入下降的时间、停车场和运营驱动因素",
            "subtasks": [
                {
                    "task_id": "task_1",
                    "task_name": "查询停车净收入趋势",
                    "task_type": "trend_analysis",
                    "description": "按时间观察停车净收入变化并定位下降区间",
                    "depends_on": [],
                    "dimensions": ["时间"],
                    "metrics": ["停车净收入"],
                },
                {
                    "task_id": "task_2",
                    "task_name": "查询完成订单量变化",
                    "task_type": "driver_analysis",
                    "description": "判断收入下降是否伴随完成订单量减少",
                    "depends_on": [],
                    "dimensions": ["时间"],
                    "metrics": ["完成订单量"],
                },
                {
                    "task_id": "task_3",
                    "task_name": "比较各停车场收入贡献",
                    "task_type": "dimension_drilldown",
                    "description": "定位收入下降贡献较大的停车场",
                    "depends_on": ["task_1"],
                    "dimensions": ["停车场"],
                    "metrics": ["停车净收入"],
                },
                {
                    "task_id": "task_4",
                    "task_name": "查询运营驱动与异常证据",
                    "task_type": "driver_analysis",
                    "description": "比较退款、利用率和异常事件变化，为原因总结提供证据",
                    "depends_on": ["task_3"],
                    "dimensions": ["时间", "停车场", "异常类型"],
                    "metrics": ["退款金额", "车位利用率", "异常事件数"],
                },
            ],
        }

    if is_overview:
        return {
            "question_type": "parking_operation_overview",
            "analysis_goal": "从经营、车位效率和异常运营三个方面评估停车业务",
            "subtasks": [
                {
                    "task_id": "task_1",
                    "task_name": "分析收入和订单趋势",
                    "task_type": "trend_analysis",
                    "description": "查询停车净收入与完成订单量的时间变化",
                    "depends_on": [],
                    "dimensions": ["时间"],
                    "metrics": ["停车净收入", "完成订单量"],
                },
                {
                    "task_id": "task_2",
                    "task_name": "分析车位效率和停车时长",
                    "task_type": "operation_analysis",
                    "description": "查询车位利用率与平均停车时长",
                    "depends_on": [],
                    "dimensions": ["时间", "停车场"],
                    "metrics": ["车位利用率", "平均停车时长"],
                },
                {
                    "task_id": "task_3",
                    "task_name": "比较停车场经营表现",
                    "task_type": "dimension_drilldown",
                    "description": "按停车场比较收入、订单和利用率",
                    "depends_on": [],
                    "dimensions": ["停车场"],
                    "metrics": ["停车净收入", "完成订单量", "车位利用率"],
                },
                {
                    "task_id": "task_4",
                    "task_name": "分析异常运营情况",
                    "task_type": "exception_analysis",
                    "description": "查询异常事件、人工抬杆和免费放行情况",
                    "depends_on": [],
                    "dimensions": ["时间", "停车场", "异常类型"],
                    "metrics": ["异常事件数", "人工抬杆次数", "免费放行次数"],
                },
            ],
        }

    metric_names = detected_metrics or ["停车净收入"]
    dimensions = ["停车场"] if is_ranking else ["时间"] if is_trend else []
    task_type = "ranking_analysis" if is_ranking else "trend_analysis" if is_trend else "metric_query"
    return {
        "question_type": task_type,
        "analysis_goal": question,
        "subtasks": [
            {
                "task_id": "task_1",
                "task_name": question,
                "task_type": task_type,
                "description": "按用户问题查询对应停车运营指标",
                "depends_on": [],
                "dimensions": dimensions,
                "metrics": metric_names,
            }
        ],
    }


class QueryDecomposer:
    """智慧停车复杂问题拆解器。"""

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        response_generator: Callable[[str, str], str] | None = None,
    ):
        self.llm = llm_client or LLMClient()
        self.response_generator = response_generator or self._generate_response

    def decompose(
        self,
        user_question: str,
        planning_context: dict | None = None,
    ) -> dict:
        """将停车问题拆成结构化查询子任务。"""
        question = user_question.strip()
        if not question:
            raise ValueError("输入问题不能为空")

        system_msg, prompt = build_decomposition_prompt(question, planning_context)
        last_error: ValueError | None = None

        for attempt in range(2):
            try:
                raw_response = self.response_generator(system_msg, prompt)
            except Exception as exc:
                print(f"Planner LLM 调用失败，使用停车规则计划兜底：{exc}")
                return build_parking_fallback_plan(question)

            print(f"LLM 原始输出: \n{raw_response}")
            plan = self._parse_plan(raw_response)
            print(f"解析后的子任务: \n{plan}")

            try:
                plan = self._normalize_plan(plan)
                self._validate_dependencies(plan)
                self._validate_dimensions(plan)
                self._validate_metrics(plan)
                self._validate_plan_complexity(plan)
                return plan.model_dump()
            except ValueError as exc:
                last_error = exc
                if attempt == 1:
                    raise
                prompt = self._build_retry_prompt(prompt, str(exc))

        assert last_error is not None
        raise last_error

    def _generate_response(self, system_msg: str, prompt: str) -> str:
        """默认通过 LLM 生成 JSON 任务计划。"""
        response = self.llm.client.chat.completions.create(
            model=self.llm.model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("LLM 没有返回可用的拆解结果")
        return content

    def _parse_plan(self, raw_response: str) -> DecompositionPlan:
        """解析 LLM 返回结果，兼容 Markdown 代码块。"""
        json_text = self._extract_json(raw_response)
        try:
            return DecompositionPlan.model_validate_json(json_text)
        except ValidationError as exc:
            raise ValueError(f"拆解结果结构不合法: {exc}") from exc

    @staticmethod
    def _extract_json(raw_response: str) -> str:
        """从模型输出中提取 JSON 文本。"""
        text = raw_response.strip()
        fence_match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if fence_match:
            return fence_match.group(1).strip()

        try:
            json.loads(text)
            return text
        except json.JSONDecodeError as exc:
            raise ValueError("LLM 返回的内容不是合法 JSON") from exc

    @staticmethod
    def _normalize_plan(plan: DecompositionPlan) -> DecompositionPlan:
        """把指标别名归一为知识库标准名称。"""
        alias_map = _indicator_alias_map()
        for task in plan.subtasks:
            task.metrics = [alias_map.get(metric.lower(), metric) for metric in task.metrics]
        return plan

    @staticmethod
    def _validate_dependencies(plan: DecompositionPlan) -> None:
        """校验依赖任务是否存在且顺序合法。"""
        seen_ids: set[str] = set()
        for task in plan.subtasks:
            for dependency_id in task.depends_on:
                if dependency_id not in seen_ids:
                    raise ValueError(
                        f"任务 {task.task_id} 依赖了不存在的任务: {dependency_id}"
                    )
            seen_ids.add(task.task_id)

    @staticmethod
    def _validate_dimensions(plan: DecompositionPlan) -> None:
        unsupported = sorted(
            {
                dimension
                for task in plan.subtasks
                for dimension in task.dimensions
                if dimension not in AVAILABLE_DIMENSIONS
            }
        )
        if unsupported:
            raise ValueError(f"不支持的分析维度: {', '.join(unsupported)}")

    @staticmethod
    def _validate_metrics(plan: DecompositionPlan) -> None:
        """拒绝 Planner 幻觉出的旧业务或未注册指标。"""
        allowed_metrics = set(_indicator_alias_map().values())
        unsupported = sorted(
            {
                metric
                for task in plan.subtasks
                for metric in task.metrics
                if metric not in allowed_metrics
            }
        )
        if unsupported:
            raise ValueError(f"不支持的分析指标: {', '.join(unsupported)}")

    @staticmethod
    def _validate_plan_complexity(plan: DecompositionPlan) -> None:
        max_tasks = _max_tasks_for_question_type(plan.question_type)
        if len(plan.subtasks) > max_tasks:
            raise ValueError(
                f"子任务过多：当前 question_type={plan.question_type} 最多允许 {max_tasks} 个子任务，"
                f"实际返回 {len(plan.subtasks)} 个。请合并重复任务并保持每步简单。"
            )

    @staticmethod
    def _build_retry_prompt(prompt: str, error_message: str) -> str:
        return (
            f"{prompt}\n\n"
            "上一次拆解结果不合法，存在以下问题：\n"
            f"{error_message}\n"
            "请重新拆解，并严格修正上述问题后只返回 JSON。"
        )


if __name__ == "__main__":
    import sys

    question = " ".join(sys.argv[1:]).strip() or "最近三个月停车收入为什么下降？"
    decomposer = QueryDecomposer()
    result = decomposer.decompose(question)
    print(json.dumps(result, ensure_ascii=False, indent=2))
