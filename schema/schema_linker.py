"""
Schema Linking Pipeline 编排模块

第 18 课：将表召回（table_retriever）、字段匹配（field_matcher）、
Join 推理（join_resolver）三个模块串联为完整 Pipeline。

输入：用户的自然语言问题
输出：动态组装的精简 Schema（表+字段+Join 条件），可直接注入 Prompt

替代原 prompt_builder.py 中的硬编码全量 Schema 注入方式。
"""

from schema.table_retriever import retrieve_tables, build_index
from schema.field_matcher import match_fields, build_field_index
from schema.join_resolver import (
    select_anchor,
    resolve_joins,
    STRONG_METRIC_WORDS,
    TABLE_TYPES,
    TABLE_KEYWORDS,
)


# ==================== Schema Linking Pipeline ====================
def _score_table_by_keywords(query: str, table_name: str) -> int:
    """基于17课 TABLE_KEYWORDS，计算查询与某张表的关键词匹配分数。"""
    keywords = TABLE_KEYWORDS.get(table_name, [])
    return sum(1 for kw in keywords if kw in query)


def _ensure_fact_table_for_metric(query: str, tables: list[dict]) -> list[dict]:
    """
    兜底策略：对于指标型问题，确保召回结果中包含语义最相关的事实表。

    背景：table_retriever 基于 Embedding 相似度召回，可能把停车收入趋势同时
    关联到订单明细、日汇总和小时汇总。只有符合问题粒度的事实表才适合作为
    SQL 锚表，因此需要结合停车业务关键词做二次校验。

    逻辑：
    1. 无强指标词时跳过；
    2. 用 TABLE_KEYWORDS 给所有事实表打分；
    3. 若当前召回的事实表不是关键词得分最高的事实表，则把最高分事实表补充进来；
    4. 若当前未召回任何事实表，则放宽阈值检索并补充最相关事实表。
    """
    if not any(w in query for w in STRONG_METRIC_WORDS):
        return tables

    existing_names = {t["table_name"] for t in tables}

    # 当前已召回的事实表
    recalled_fact_tables = [
        t for t in tables if TABLE_TYPES.get(t["table_name"]) == "fact"
    ]

    # 放宽阈值，获取更大范围的候选表，用于关键词评分
    all_tables = retrieve_tables(query, top_k=10, score_threshold=0.0)
    all_fact_tables = [
        t for t in all_tables if TABLE_TYPES.get(t["table_name"]) == "fact"
    ]

    if not all_fact_tables:
        return tables

    # 按关键词匹配度选出最相关的事实表
    best_fact = max(
        all_fact_tables,
        key=lambda t: _score_table_by_keywords(query, t["table_name"]),
    )
    best_fact_name = best_fact["table_name"]

    # 如果最高分事实表不在召回结果中，则补充
    if best_fact_name not in existing_names:
        tables = tables + [best_fact]

    # 兜底：确保至少召回了一张事实表（兼容旧逻辑）
    if not recalled_fact_tables and not any(
        TABLE_TYPES.get(t["table_name"]) == "fact" for t in tables
    ):
        tables = tables + [all_fact_tables[0]]

    return tables


def _ensure_parking_lot_dimension(query: str, tables: list[dict]) -> list[dict]:
    """停车场比较、排名和点名查询必须包含停车场维度表。"""
    dimension_signals = ("停车场", "车场", "场库", "城市", "停车场类型")
    if not any(signal in query for signal in dimension_signals):
        return tables

    existing_names = {table["table_name"] for table in tables}
    if "dim_parking_lot" in existing_names:
        return tables

    broad_candidates = retrieve_tables(query, top_k=10, score_threshold=0.0)
    parking_lot_dimension = next(
        (
            table for table in broad_candidates
            if table["table_name"] == "dim_parking_lot"
        ),
        None,
    )
    if parking_lot_dimension is not None:
        return tables + [parking_lot_dimension]
    return tables


def _route_parking_fact_tables(query: str, tables: list[dict]) -> list[dict]:
    """
    用停车业务粒度规则收敛向量召回，避免多张事实表在明细层直接 Join。

    Embedding 负责高召回，路由规则负责为一个简单分析问题保留最合适的事实粒度。
    原因诊断类问题允许保留日汇总与异常事件，供 Agent 后续拆成独立任务；
    Schema Context 只提供逻辑连接，不表示应做明细级多对多聚合。
    """
    preferred_tables: list[str] = []
    is_cause_analysis = any(word in query for word in ("为什么", "原因", "归因"))
    is_hourly = any(word in query for word in ("高峰", "几点", "小时", "时段", "几点最忙"))
    is_realtime_space = any(
        word in query
        for word in ("当前", "实时", "空闲车位", "剩余车位", "还有多少车位", "快照")
    )
    is_utilization = any(word in query for word in ("利用率", "占用率", "空闲率"))
    is_duration = any(word in query for word in ("停车时长", "停留时长", "平均时长", "停了多久"))
    is_order_detail = any(
        word in query
        for word in (
            "支付", "退款", "退费", "优惠", "应收", "实收", "订单明细",
            "订单类型", "入场", "出场", "进场", "离场", "车流量",
            "人工抬杆", "免费放行",
        )
    )
    is_exception = any(
        word in query
        for word in ("异常", "设备离线", "支付失败", "车牌识别", "预估损失", "未解决")
    )
    is_revenue = any(word in query for word in ("收入", "营收", "停车费", "净收入"))
    is_daily_analysis = any(
        word in query
        for word in ("今天", "昨日", "昨天", "最近", "趋势", "同比", "环比", "本月", "上月", "最高", "最低", "排名", "下降")
    )

    if is_hourly:
        preferred_tables.append("agg_parking_hourly")
    elif is_realtime_space:
        preferred_tables.append("fact_space_snapshot")
    elif is_utilization:
        preferred_tables.append("agg_parking_daily")
    elif is_duration and not is_daily_analysis:
        preferred_tables.append("fact_parking_order")
    elif is_order_detail:
        preferred_tables.append("fact_parking_order")
    elif is_exception and not is_revenue:
        preferred_tables.append("fact_operation_event")
    elif is_revenue or is_daily_analysis:
        preferred_tables.append("agg_parking_daily")

    if is_cause_analysis or (is_exception and is_revenue):
        if "agg_parking_daily" not in preferred_tables:
            preferred_tables.append("agg_parking_daily")
        if "fact_operation_event" not in preferred_tables:
            preferred_tables.append("fact_operation_event")

    if not preferred_tables:
        return tables

    candidate_by_name = {table["table_name"]: table for table in tables}
    missing_names = [name for name in preferred_tables if name not in candidate_by_name]
    if missing_names:
        broad_candidates = retrieve_tables(query, top_k=10, score_threshold=0.0)
        candidate_by_name.update({table["table_name"]: table for table in broad_candidates})

    routed = [
        candidate_by_name[table_name]
        for table_name in preferred_tables
        if table_name in candidate_by_name
    ]
    return routed or tables


def schema_link(
    query: str,
    table_top_k: int = 3,
    field_top_k: int = 12,
    table_threshold: float = 0.3,
    field_threshold: float = 0.25,
    include_join: bool = True,
) -> dict:
    """
    Schema Linking 完整 Pipeline：
    用户问题 → 表召回 → 锚表选择 → 字段匹配 → Join 推理 → 动态 Schema 组装

    Args:
        query: 用户的自然语言问题
        table_top_k: 表召回数量
        field_top_k: 字段匹配数量
        table_threshold: 表召回相似度阈值
        field_threshold: 字段匹配最终得分阈值
        include_join: 是否生成 Join 路径

    Returns:
        {
            "tables": [{"table_name": str, "score": float, ...}],
            "fields": [{"field_key": str, "table": str, "field": str, "score": float, ...}],
            "anchor": str,                        # 选定的锚表
            "join_path": {"anchor": str, "joins": [...], "sql_fragment": str, "unreachable": [...]},
            "dynamic_schema": str,                # 可直接注入 Prompt 的精简 Schema 文本
            "metadata": {
                "table_count": int,
                "field_count": int,
                "join_count": int,
                "has_unreachable": bool,
            }
        }
    """
    # Step 1: 表召回
    tables = retrieve_tables(query, top_k=table_top_k, score_threshold=table_threshold)

    # 兜底：指标型问题必须召回事实表，否则 SQL 无法生成
    tables = _ensure_fact_table_for_metric(query, tables)

    # 用业务粒度收敛事实表，避免将日、小时和明细事实同时注入简单查询。
    tables = _route_parking_fact_tables(query, tables)

    # 停车场排名或指定停车场查询需要名称维度，避免只返回 parking_lot_id。
    tables = _ensure_parking_lot_dimension(query, tables)

    candidate_table_names = [t["table_name"] for t in tables]

    # 无召回结果时直接返回空，触发 prompt_builder 的 fallback
    if not candidate_table_names:
        return {
            "tables": [],
            "fields": [],
            "anchor": "",
            "join_path": {"anchor": "", "joins": [], "sql_fragment": "", "unreachable": []},
            "dynamic_schema": "",
            "metadata": {
                "table_count": 0,
                "field_count": 0,
                "join_count": 0,
                "has_unreachable": False,
            },
        }

    # Step 2: 锚表选择（基于查询意图）
    anchor, anchor_reason = select_anchor(query, candidate_table_names)

    # Step 3: 字段匹配（限定在召回的表范围内）
    fields = match_fields(
        query,
        candidate_tables=candidate_table_names,
        top_k=field_top_k,
        score_threshold=field_threshold,
    )

    # Step 4: Join 路径推理
    join_path = {"anchor": anchor, "joins": [], "sql_fragment": "", "unreachable": []}
    if include_join:
        join_path = resolve_joins(anchor, candidate_table_names)

    # Step 5: 动态 Schema 组装
    dynamic_schema = _assemble_dynamic_schema(query, tables, fields, join_path, anchor_reason)

    return {
        "tables": tables,
        "fields": fields,
        "anchor": anchor,
        "join_path": join_path,
        "dynamic_schema": dynamic_schema,
        "metadata": {
            "table_count": len(tables),
            "field_count": len(fields),
            "join_count": len(join_path.get("joins", [])),
            "has_unreachable": len(join_path.get("unreachable", [])) > 0,
        },
    }


def _assemble_dynamic_schema(
    query: str,
    tables: list[dict],
    fields: list[dict],
    join_path: dict,
    anchor_reason: str,
) -> str:
    """
    将召回结果组装为可注入 Prompt 的动态 Schema 文本。

    格式设计原则：
    - 清晰列出涉及的表和字段
    - 包含字段的关键业务说明
    - 明确标注锚表和 Join 条件
    - 保持紧凑，尽量减少 Token 消耗
    """
    lines = []

    # 1. 按表分组字段
    table_fields_map: dict[str, list[dict]] = {}
    for t in tables:
        table_fields_map[t["table_name"]] = []
    for f in fields:
        if f["table"] in table_fields_map:
            table_fields_map[f["table"]].append(f)

    # 2. 生成各表的精简 Schema
    for table_name, field_list in table_fields_map.items():
        table_info = next((t for t in tables if t["table_name"] == table_name), None)
        domain = table_info.get("domain", "") if table_info else ""
        anchor_tag = " [锚表]" if table_name == join_path.get("anchor") else ""

        lines.append(f"表：{table_name}（{domain}）{anchor_tag}")
        if field_list:
            for f in field_list:
                short_desc = f.get("description", "").split("。")[0]
                rule_tag = ""
                if f.get("rule_applied"):
                    rule_tag = f" ★{f['rule_applied']}"
                lines.append(f"  - {f['field']} {short_desc}{rule_tag}")
        else:
            lines.append("  （未匹配到特定字段，参考全部字段）")
        lines.append("")

    # 3. 生成 Join 关系说明
    if join_path.get("joins"):
        lines.append("表间关联：")
        for j in join_path["joins"]:
            lines.append(f"  {j['join_type']} {j['to_table']} ON {j['on_clause']}")
        lines.append("")

    # 4. 标注不可连通的表
    if join_path.get("unreachable"):
        lines.append(
            f"注意：以下表与其他表无直接关联，需独立查询：{join_path['unreachable']}"
        )
        lines.append("")

    return "\n".join(lines).strip()


# ==================== Prompt 集成接口 ====================
def build_dynamic_prompt_schema(query: str) -> str:
    """
    供 prompt_builder.py 调用的简化接口。
    输入用户问题，输出可直接替换 SCHEMA 常量的动态文本。

    如果 Schema Linking 失败（如无召回结果），返回空字符串，
    调用方应回退到全量 Schema。
    """
    try:
        result = schema_link(query)
        if result["metadata"]["table_count"] == 0:
            return ""  # 无召回，触发 fallback
        return result["dynamic_schema"]
    except Exception as e:
        print(f"[Schema Linking 异常] {e}，将回退到全量 Schema")
        return ""


# ==================== 索引初始化 ====================
def ensure_indexes(force_rebuild: bool = False):
    """确保表索引和字段索引都已构建"""
    print("检查向量索引状态...")
    build_index(force_rebuild=force_rebuild)
    print("索引就绪。")
    build_field_index(force_rebuild=force_rebuild)


# ==================== 主程序：演示 ====================
if __name__ == "__main__":
    print("=" * 60)
    print("Schema Linking 完整链路演示")
    print("=" * 60)

    # 初始化索引（如果已存在则跳过，避免重复调用 Embedding API）
    print("\n--- 初始化向量索引 ---")
    ensure_indexes(force_rebuild=False)

    # 测试问题集
    test_questions = [
        "今天停车收入是多少？",
        "最近三个月收入趋势？",
        "哪个停车场收入最高？",
        "哪个停车场利用率最低？",
        "平均停车时长是多少？",
    ]

    for question in test_questions:
        print(f"\n{'='*60}")
        print(f"问题：{question}")
        print("-" * 60)

        result = schema_link(question)

        # 打印元数据
        meta = result["metadata"]
        print(f"召回：{meta['table_count']} 张表, "
              f"{meta['field_count']} 个字段, "
              f"{meta['join_count']} 个 Join")
        print(f"锚表：{result['anchor']}")

        # 打印动态 Schema
        print(f"\n动态 Schema（注入 Prompt）：")
        print("-" * 40)
        print(result["dynamic_schema"])
        print("-" * 40)

        # 打印 Join SQL
        if result["join_path"].get("sql_fragment"):
            print(f"\nJoin SQL 片段：")
            print(f"  {result['join_path']['sql_fragment'].replace(chr(10), chr(10) + '  ')}")

    # 对比 Token 估算
    print(f"\n\n{'='*60}")
    print("Token 消耗对比（粗略估算）")
    print("=" * 60)
    full_schema_tokens = 1800  # 停车六表全量 Schema 的粗略估算
    for question in test_questions[:3]:
        result = schema_link(question)
        dynamic_tokens = len(result["dynamic_schema"]) // 2  # 粗略估算：2字符≈1token
        savings = (1 - dynamic_tokens / full_schema_tokens) * 100
        print(f"  {question[:20]}... → ~{dynamic_tokens} tokens (节省 {savings:.0f}%)")
