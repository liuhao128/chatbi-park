"""
智慧停车 ChatBI Prompt 构造模块。

负责将动态/静态 Schema、停车业务规则、Few-shot 示例、错误防护、
指标知识和用户问题组装为 Text2SQL Prompt。

设计原则：
1. 动态 Schema Linking 结果优先，静态六表 Schema 只作为兜底；
2. Prompt 中的表名、字段名和业务口径必须与 Day8 Schema 一致；
3. 业务规则负责约束语义，SQL 生成与执行逻辑仍由原有模块负责。
"""


# ==================== System Prompt ====================
SYSTEM_MESSAGE = (
    "你是一名智慧停车运营分析与 Text2SQL 专家。"
    "你能够理解停车收入、停车订单、停车场经营、车位利用率、停车时长、"
    "车流高峰和运营异常等问题，并依据提供的数据库 Schema 生成标准 MySQL 查询。"
    "只使用上下文明确提供的表、字段、关联关系和业务口径，不得编造数据库对象。"
)

STRICT_SYSTEM_MESSAGE = (
    f"{SYSTEM_MESSAGE}"
    "请严格遵守关键业务规则和错误防护；当指标知识与当前 Schema 冲突时，"
    "以当前 Schema 和关键业务规则为准。"
)


# ==================== 静态兜底 Schema ====================
# 动态 Schema Linking 开启且召回成功时会替换该文本；这里必须与
# database/01_schema.sql 的六张停车 MVP 表保持一致。
SCHEMA = """
表：dim_parking_lot（停车场维度表）
- parking_lot_id BIGINT 主键，停车场ID
- parking_lot_name VARCHAR(100)，停车场名称
- operator_id BIGINT，运营商ID
- city_name VARCHAR(50)，所属城市
- parking_lot_type VARCHAR(30)，停车场类型，如商业、园区、医院
- total_spaces INT，停车场基础总车位数
- operation_status VARCHAR(20)，运营状态：operating / closed / maintenance
- updated_at DATETIME，数据更新时间，不是经营统计时间

表：fact_parking_order（停车订单事实表）
- order_id BIGINT 主键，停车订单ID
- parking_lot_id BIGINT，关联 dim_parking_lot.parking_lot_id
- order_type VARCHAR(20)，订单类型：temporary / monthly / visitor
- entry_time DATETIME，车辆入场时间
- exit_time DATETIME，车辆出场时间，未出场时为空
- parking_minutes INT，停车时长，单位分钟
- order_status VARCHAR(20)，订单状态：active / completed / cancelled / exception
- receivable_amount DECIMAL(12,2)，应收金额
- discount_amount DECIMAL(12,2)，优惠减免金额
- paid_amount DECIMAL(12,2)，实收金额
- refund_amount DECIMAL(12,2)，退款金额
- payment_status VARCHAR(20)，支付状态：unpaid / paid / refunded
- payment_method VARCHAR(20)，支付方式：wechat / alipay / cash
- manual_open_flag TINYINT，是否人工抬杆，0否1是
- free_release_flag TINYINT，是否免费放行，0否1是
- updated_at DATETIME，数据更新时间，不是默认收入统计时间

表：fact_space_snapshot（车位状态快照事实表）
- snapshot_id BIGINT 主键，快照ID
- parking_lot_id BIGINT，关联 dim_parking_lot.parking_lot_id
- snapshot_time DATETIME，快照时间
- total_spaces INT，快照时点可运营车位数
- occupied_spaces INT，快照时点已占用车位数
- free_spaces INT，快照时点空闲车位数

表：fact_operation_event（运营异常事件事实表）
- event_id BIGINT 主键，事件ID
- parking_lot_id BIGINT，关联 dim_parking_lot.parking_lot_id
- order_id BIGINT，可选关联 fact_parking_order.order_id
- event_time DATETIME，事件发生时间
- event_type VARCHAR(50)，事件类型，如支付失败、设备离线、人工抬杆
- severity VARCHAR(20)，严重程度：low / medium / high
- event_status VARCHAR(20)，处理状态：pending / processing / resolved
- estimated_loss DECIMAL(12,2)，预估收入损失，不等于实际停车收入
- description VARCHAR(500)，事件说明

表：agg_parking_daily（停车场日经营汇总表）
- stat_date DATE，统计日期，与 parking_lot_id 组成主键
- parking_lot_id BIGINT，关联 dim_parking_lot.parking_lot_id
- order_count INT，当日已完成订单量
- net_revenue DECIMAL(14,2)，当日停车净收入，等于实收减退款
- average_parking_minutes DECIMAL(10,2)，当日平均停车时长，单位分钟
- average_occupied_spaces DECIMAL(10,2)，当日平均占用车位数
- utilization_rate DECIMAL(8,4)，当日车位利用率，取值0至1
- manual_open_count INT，人工抬杆次数
- free_release_count INT，免费放行次数
- exception_count INT，异常事件数量
- updated_at DATETIME，数据更新时间，不是业务统计日期

表：agg_parking_hourly（停车场小时经营汇总表）
- stat_date DATE，统计日期
- stat_hour TINYINT，统计小时，取值0至23
- parking_lot_id BIGINT，关联 dim_parking_lot.parking_lot_id
- order_count INT，该小时已完成订单量
- net_revenue DECIMAL(14,2)，该小时停车净收入
- occupied_spaces DECIMAL(10,2)，该小时平均占用车位数
- utilization_rate DECIMAL(8,4)，该小时车位利用率，取值0至1
- exception_count INT，该小时异常事件数量
- updated_at DATETIME，数据更新时间，不是业务统计时间

表间关系：
- fact_parking_order.parking_lot_id = dim_parking_lot.parking_lot_id
- fact_space_snapshot.parking_lot_id = dim_parking_lot.parking_lot_id
- fact_operation_event.parking_lot_id = dim_parking_lot.parking_lot_id
- fact_operation_event.order_id = fact_parking_order.order_id
- agg_parking_daily.parking_lot_id = dim_parking_lot.parking_lot_id
- agg_parking_hourly.parking_lot_id = dim_parking_lot.parking_lot_id
"""


# ==================== Few-shot 示例 ====================
FEW_SHOT_EXAMPLES = """
示例1：
问题：今天停车收入是多少？
SQL：SELECT COALESCE(SUM(net_revenue), 0) AS parking_revenue FROM agg_parking_daily WHERE stat_date = CURDATE();

示例2：
问题：最近三个月收入趋势？
SQL：SELECT DATE_FORMAT(stat_date, '%Y-%m-01') AS revenue_month, SUM(net_revenue) AS parking_revenue FROM agg_parking_daily WHERE stat_date >= DATE_SUB(CURDATE(), INTERVAL 3 MONTH) AND stat_date < CURDATE() + INTERVAL 1 DAY GROUP BY DATE_FORMAT(stat_date, '%Y-%m-01') ORDER BY revenue_month;

示例3：
问题：最近三个月哪个停车场收入最高？
SQL：SELECT p.parking_lot_name, SUM(d.net_revenue) AS parking_revenue FROM agg_parking_daily d JOIN dim_parking_lot p ON d.parking_lot_id = p.parking_lot_id WHERE d.stat_date >= DATE_SUB(CURDATE(), INTERVAL 3 MONTH) AND d.stat_date < CURDATE() + INTERVAL 1 DAY GROUP BY p.parking_lot_id, p.parking_lot_name ORDER BY parking_revenue DESC LIMIT 1;

示例4：
问题：哪个停车场利用率最低？
SQL：SELECT p.parking_lot_name, AVG(d.utilization_rate) AS average_utilization_rate FROM agg_parking_daily d JOIN dim_parking_lot p ON d.parking_lot_id = p.parking_lot_id WHERE p.operation_status = 'operating' GROUP BY p.parking_lot_id, p.parking_lot_name ORDER BY average_utilization_rate ASC LIMIT 1;

示例5：
问题：平均停车时长是多少？
SQL：SELECT AVG(parking_minutes) AS average_parking_minutes FROM fact_parking_order WHERE order_status = 'completed' AND parking_minutes IS NOT NULL;
"""


# ==================== 停车业务规则 ====================
RULES = """
【关键业务规则】
1. 停车收入：默认指净收入。日/小时趋势和停车场排名优先使用 agg_parking_daily.net_revenue 或 agg_parking_hourly.net_revenue；明细查询使用 SUM(fact_parking_order.paid_amount - fact_parking_order.refund_amount)。不要把 receivable_amount、discount_amount 或 estimated_loss 当作实际收入。
2. 收入时间：聚合表按 stat_date 统计；订单明细收入按 exit_time 归属，并过滤 order_status = 'completed'。updated_at 只表示数据更新时间，不能作为收入、订单、利用率或异常的业务时间。
3. 停车订单量：聚合查询优先使用 agg_parking_daily.order_count 或 agg_parking_hourly.order_count；明细查询使用 COUNT(fact_parking_order.order_id)，统计完成订单时过滤 order_status = 'completed'。
4. 平均停车时长：明细使用 AVG(fact_parking_order.parking_minutes)，过滤 completed 且 parking_minutes IS NOT NULL；跨日汇总不能直接对 average_parking_minutes 再做简单平均，应按 order_count 加权，或回到订单明细重算。
5. 车位利用率：历史日/小时分析使用聚合表 utilization_rate；快照明细使用 occupied_spaces / NULLIF(total_spaces, 0)。利用率字段取值0至1，不得 SUM；需要百分比展示时再乘100。历史利用率分母不得使用 dim_parking_lot.total_spaces。
6. 当前车位：查询当前空闲/占用车位时使用 fact_space_snapshot，并为每个停车场选择 snapshot_time 最新的一条快照。
7. 停车场维度：输出停车场名称、城市或类型时，通过 parking_lot_id 关联 dim_parking_lot。比较经营表现时，除非用户另有要求，排除 operation_status 非 operating 的停车场。
8. 高峰时段：按小时分析使用 agg_parking_hourly.stat_date 和 stat_hour；“最忙”默认按 order_count 或 utilization_rate 判断，只有用户明确问收入高峰时才使用 net_revenue。
9. 异常诊断：收入下降原因只能根据订单量、退款、免费放行、人工抬杆、利用率、异常数量和 fact_operation_event 等证据分析。estimated_loss 是预估损失，不能当作已确认收入损失；数据不足时不得虚构因果。
10. 时间范围：“最近N个月”使用 DATE_SUB(CURDATE(), INTERVAL N MONTH) 作为起始边界；“今天”使用 CURDATE()；范围查询优先使用 >= 起点且 < 终点的闭开区间。
"""


# ==================== Text2SQL 错误防护 ====================
ERROR_GUARDS = """
【常见错误防护】
- Schema 优先级：只使用【数据库Schema】实际出现的表和字段。不要生成 parking_fee、pay_time、occupied_space、parking_space 等当前 Schema 不存在的字段。
- 知识冲突：如果注入的指标知识仍引用销售、客户、产品、汇率或费用等旧业务对象，视为与当前停车 Schema 冲突并忽略。
- 收入口径：默认净收入不是应收金额，也不是预估损失；订单明细必须使用 paid_amount - refund_amount。
- 时间字段：入场车流使用 entry_time，完成订单和明细收入使用 exit_time，车位状态使用 snapshot_time，异常使用 event_time，聚合趋势使用 stat_date；禁止使用 updated_at 代替业务时间。
- 聚合粒度：不要在同一指标中直接混合订单明细、车位快照、日聚合和小时聚合，避免重复统计；能由一张聚合表回答的问题不要无必要回到明细表。
- Join 防重复：事实表之间不要仅凭 parking_lot_id 直接 Join，否则可能产生多对多行膨胀；需要多个事实口径时应先分别聚合到相同粒度，再进行关联。
- 比率与平均值：utilization_rate 不得 SUM；跨日平均停车时长应按 order_count 加权或使用订单明细 AVG，不能直接平均各日平均值。
- 分组规则：SELECT 中的非聚合字段必须出现在 GROUP BY 中；停车场排名建议同时按 parking_lot_id 和 parking_lot_name 分组。
- 空值与除零：exit_time、parking_minutes 可能为空；除法使用 NULLIF(分母, 0)，金额汇总可使用 COALESCE。
- 只读约束：只生成一条 SELECT 或 WITH ... SELECT 查询，禁止 INSERT、UPDATE、DELETE、DROP、ALTER、TRUNCATE、CREATE。
"""


def build_prompt(
    user_question: str,
    use_few_shot: bool = True,
    use_rules: bool = False,
    use_guards: bool = False,
    indicator_knowledge: str = "",
    use_schema_linking: bool = False,
) -> tuple[str, str]:
    """构造发送给 LLM 的智慧停车 Text2SQL Prompt。

    Args:
        user_question: 用户自然语言问题。
        use_few_shot: 是否注入智慧停车 Few-shot 示例。
        use_rules: 是否注入停车业务规则。
        use_guards: 是否注入 Text2SQL 错误防护。
        indicator_knowledge: 可选的指标知识文本块。
        use_schema_linking: 是否使用动态 Schema Linking；失败时回退到静态六表 Schema。

    Returns:
        (system_message, user_message)
    """
    system_msg = (
        STRICT_SYSTEM_MESSAGE
        if use_rules or use_guards
        else SYSTEM_MESSAGE
    )

    # 动态 Schema 只注入与问题相关的表、字段和 Join，降低 Token 消耗；
    # 召回失败或功能未开启时，回退到与 Day8 DDL 对齐的静态六表 Schema。
    schema_text = SCHEMA
    if use_schema_linking:
        try:
            from schema.schema_linker import build_dynamic_prompt_schema

            dynamic_schema = build_dynamic_prompt_schema(user_question)
            if dynamic_schema:
                schema_text = dynamic_schema
        except Exception:
            pass

    prompt_parts = [f"【数据库Schema】\n{schema_text}"]

    if use_rules:
        prompt_parts.append(RULES)

    if use_few_shot:
        prompt_parts.append(f"【智慧停车示例】\n{FEW_SHOT_EXAMPLES}")

    if use_guards:
        prompt_parts.append(ERROR_GUARDS)

    if indicator_knowledge:
        prompt_parts.append(
            "【检索到的指标知识】\n"
            "以下知识只作补充；如果与当前数据库Schema或关键业务规则冲突，必须忽略冲突内容。\n"
            f"{indicator_knowledge}"
        )

    requirements = [
        "只输出一条完整 SQL，不要解释，不要使用 Markdown 代码块",
        "使用标准 MySQL 8.0 语法，只生成 SELECT 或 WITH ... SELECT 只读查询",
        "只能使用【数据库Schema】中出现的表名、字段名和表间关联",
        "先识别指标、维度、时间范围和业务粒度，再选择最匹配的事实表",
        "涉及多表时使用 Schema 给出的 Join 条件，避免事实表直接 Join 导致重复统计",
        "SQL 必须完整闭合，SELECT、CTE、JOIN、WHERE、GROUP BY、ORDER BY、LIMIT 不得输出半句",
    ]
    if use_rules or use_guards:
        requirements.append("优先遵循【关键业务规则】和【常见错误防护】")

    numbered_requirements = "\n".join(
        f"{index}. {requirement}"
        for index, requirement in enumerate(requirements, start=1)
    )
    prompt_parts.append(
        f"【用户问题】\n{user_question}\n\n"
        f"【输出要求】\n{numbered_requirements}\n\n"
        "请直接输出 SQL："
    )

    return system_msg, "\n\n".join(prompt_parts)
