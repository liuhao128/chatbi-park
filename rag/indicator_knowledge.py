"""智慧停车指标知识关键词兜底模块。

向量 RAG 不可用或未命中时，通过指标名称和别名完成确定性识别，
并从同一份停车指标知识源生成可注入 Text2SQL 的业务上下文。
"""

import json
from pathlib import Path


def _load_knowledge_file(config_path: Path) -> dict:
    """加载指标 JSON，并解析轻量 source 引用，避免维护两份指标口径。"""
    with config_path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    source = data.get("source")
    if source:
        source_path = config_path.parent / source
        with source_path.open("r", encoding="utf-8") as file:
            return json.load(file)
    return data


class IndicatorKnowledge:
    """加载停车指标、识别名称/别名并生成知识块。"""

    def __init__(self, config_path: str | Path | None = None):
        resolved_path = Path(config_path) if config_path else Path(__file__).with_name("indicators.json")
        data = _load_knowledge_file(resolved_path)
        self.knowledge_version = data.get("knowledge_version", "")
        self.indicators = {ind["name"]: ind for ind in data["indicators"]}
        self.alias_map = {}
        for ind in data["indicators"]:
            self.alias_map[ind["name"].lower()] = ind["name"]
            for alias in ind.get("aliases", []):
                self.alias_map[alias.lower()] = ind["name"]

    def detect_indicators(self, question: str) -> list[str]:
        """从用户问题中识别涉及的指标名称"""
        detected = []
        question_lower = question.lower()
        for alias, standard_name in self.alias_map.items():
            if alias in question_lower and standard_name not in detected:
                detected.append(standard_name)
        return detected

    def get_indicator_text(self, indicator_name: str) -> str:
        """将单个指标定义格式化为 Prompt 可用的文本"""
        ind = self.indicators.get(indicator_name)
        if not ind:
            return ""

        lines: list[str] = [
            f"指标：{ind['name']}",
            f"  定义：{ind['definition']}",
            f"  计算公式：{ind['formula']}",
            f"  数据来源：{ind['data_source']}",
        ]
        if ind.get("tables"):
            lines.append(f"  关联表：{', '.join(ind['tables'])}")
        if ind.get("fields"):
            lines.append(f"  关联字段：{', '.join(ind['fields'])}")
        if ind.get("time_field"):
            lines.append(f"  时间口径：{ind['time_field']}")
        if ind.get("dimensions"):
            lines.append(f"  支持维度：{', '.join(ind['dimensions'])}")
        if ind.get("depends_on"):
            lines.append(f"  依赖指标：{', '.join(ind['depends_on'])}")
        if ind.get("filters"):
            lines.append(f"  强制过滤：{' AND '.join(ind['filters'])}")
        if ind.get("business_rules"):
            lines.append(f"  业务规则：{'；'.join(ind['business_rules'])}")
        return "\n".join(lines)

    def build_knowledge_block(self, question: str) -> str:
        """根据用户问题构建指标知识文本块"""
        detected = self.detect_indicators(question)
        return self.build_knowledge_block_from_detected(detected)

    def build_knowledge_block_from_detected(self, detected: list[str]) -> str:
        """基于已识别出的指标列表构建知识块，避免重复执行关键词扫描。"""
        if not detected:
            return ""

        blocks = ["【指标知识】"]
        injected = set()
        for name in detected:
            if name not in injected:
                blocks.append(self.get_indicator_text(name))
                injected.add(name)
            # 注入依赖指标
            ind = self.indicators.get(name)
            if ind and ind.get("depends_on"):
                for dep in ind["depends_on"]:
                    if dep not in injected:
                        blocks.append(self.get_indicator_text(dep))
                        injected.add(dep)
        return "\n\n".join(blocks)

    def get_indicator_context(self, question: str) -> dict[str, list[str] | str]:
        """一次关键词扫描同时返回识别结果和知识块，供主链路复用。"""
        detected = self.detect_indicators(question)
        return {
            "detected_indicators": detected,
            "indicator_block": self.build_knowledge_block_from_detected(detected),
        }


if __name__ == "__main__":
    ik = IndicatorKnowledge()

    test_questions = [
        "最近一个月停车收入是多少",
        "哪个停车场利用率最低",
        "平均停车时间是多少",
        "收入下降原因",
    ]

    for q in test_questions:
        print(f"\n问题：{q}")
        detected = ik.detect_indicators(q)
        print(f"识别到的指标：{detected}")
        block = ik.build_knowledge_block(q)
        if block:
            print("生成的知识块：")
            print(block)
        else:
            print("未识别到指标")
