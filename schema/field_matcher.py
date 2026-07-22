"""
智慧停车字段语义匹配模块。

在表级召回基础上，为停车 MVP 六张表的字段建立向量索引，
并通过业务规则修正收入、时间、利用率、停车时长等高风险字段的召回结果。
"""

import os

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings

from schema.table_retriever import retrieve_tables
from tools.config import LLM_CONFIG


def _field(table: str, field: str, description: str, domain: str) -> dict:
    """构造统一的字段元数据，避免不同表的描述结构漂移。"""
    return {
        "table": table,
        "field": field,
        "description": description,
        "domain": domain,
    }


# 字段描述必须与 database/01_schema.sql 保持一致。
# 描述同时写出业务同义词、时间归属、过滤条件和聚合注意事项，供 Embedding 召回。
FIELD_METADATA = {
    # ---------- 停车场维度 ----------
    "dim_parking_lot.parking_lot_id": _field(
        "dim_parking_lot", "parking_lot_id",
        "停车场ID，BIGINT 主键。连接停车订单、车位快照、异常事件、日汇总和小时汇总的统一停车场键。",
        "维度表",
    ),
    "dim_parking_lot.parking_lot_name": _field(
        "dim_parking_lot", "parking_lot_name",
        "停车场名称，VARCHAR(100)。用于查询某个停车场、各停车场排名、收入最高或利用率最低的停车场。",
        "维度表",
    ),
    "dim_parking_lot.operator_id": _field(
        "dim_parking_lot", "operator_id",
        "运营商ID，BIGINT。用于按停车运营商过滤和汇总，MVP 暂无独立运营商维表。",
        "维度表",
    ),
    "dim_parking_lot.city_name": _field(
        "dim_parking_lot", "city_name",
        "停车场所属城市，例如上海市、杭州市。用于按城市、地区比较停车收入、订单和利用率。",
        "维度表",
    ),
    "dim_parking_lot.parking_lot_type": _field(
        "dim_parking_lot", "parking_lot_type",
        "停车场类型，例如商业、园区、医院。用于比较不同业态停车场的经营表现。",
        "维度表",
    ),
    "dim_parking_lot.total_spaces": _field(
        "dim_parking_lot", "total_spaces",
        "停车场基础总车位数。用于展示规划容量；计算历史利用率优先使用车位快照同一时点的可运营 total_spaces。",
        "维度表",
    ),
    "dim_parking_lot.operation_status": _field(
        "dim_parking_lot", "operation_status",
        "停车场运营状态，例如 operating、closed、maintenance。经营分析通常应识别或过滤停运和维护停车场。",
        "维度表",
    ),
    "dim_parking_lot.updated_at": _field(
        "dim_parking_lot", "updated_at",
        "停车场维度更新时间，DATETIME。用于判断维度数据新鲜度，不是经营指标统计时间。",
        "维度表",
    ),

    # ---------- 停车订单明细事实 ----------
    "fact_parking_order.order_id": _field(
        "fact_parking_order", "order_id",
        "停车订单ID，BIGINT 主键。一行代表一次停车过程，用于停车次数、订单量和订单明细。",
        "明细事实表",
    ),
    "fact_parking_order.parking_lot_id": _field(
        "fact_parking_order", "parking_lot_id",
        "订单所属停车场ID。关联 dim_parking_lot.parking_lot_id 后可按停车场名称、城市和类型分析。",
        "明细事实表",
    ),
    "fact_parking_order.order_type": _field(
        "fact_parking_order", "order_type",
        "停车订单类型，例如 temporary 临停、monthly 月租、visitor 访客。用于订单结构和收入结构分析。",
        "明细事实表",
    ),
    "fact_parking_order.entry_time": _field(
        "fact_parking_order", "entry_time",
        "车辆入场时间，DATETIME。入场车流、进场高峰按此字段归属；不是默认收入确认时间。",
        "明细事实表",
    ),
    "fact_parking_order.exit_time": _field(
        "fact_parking_order", "exit_time",
        "车辆出场时间，DATETIME，可为空。完成订单量、停车收入和出场车流默认按此时间归属。",
        "明细事实表",
    ),
    "fact_parking_order.parking_minutes": _field(
        "fact_parking_order", "parking_minutes",
        "停车时长，单位分钟。平均停车时长优先对 completed 完成订单的该字段求平均，也可用出场时间减入场时间核验。",
        "明细事实表",
    ),
    "fact_parking_order.order_status": _field(
        "fact_parking_order", "order_status",
        "停车订单状态：active 在场、completed 完成、cancelled 取消、exception 异常。收入、完成订单量和平均停车时长默认过滤 completed。",
        "明细事实表",
    ),
    "fact_parking_order.receivable_amount": _field(
        "fact_parking_order", "receivable_amount",
        "停车应收金额，DECIMAL。表示优惠和实收前按收费结果形成的应收，用于应收、优惠和收入达成分析。",
        "明细事实表",
    ),
    "fact_parking_order.discount_amount": _field(
        "fact_parking_order", "discount_amount",
        "停车优惠减免金额，DECIMAL。用于优惠金额、应收与实收差异及收入下降驱动分析。",
        "明细事实表",
    ),
    "fact_parking_order.paid_amount": _field(
        "fact_parking_order", "paid_amount",
        "停车实收金额，DECIMAL。明细净收入基础字段；停车净收入等于 paid_amount 减 refund_amount。",
        "明细事实表",
    ),
    "fact_parking_order.refund_amount": _field(
        "fact_parking_order", "refund_amount",
        "停车退款金额，DECIMAL。计算停车净收入时从实收中扣除，也用于退款和收入下降分析。当前表没有独立退款时间。",
        "明细事实表",
    ),
    "fact_parking_order.payment_status": _field(
        "fact_parking_order", "payment_status",
        "支付状态，例如 unpaid、paid、refunded。支付成功率和实收分析使用该字段区分支付结果。",
        "明细事实表",
    ),
    "fact_parking_order.payment_method": _field(
        "fact_parking_order", "payment_method",
        "停车支付方式，例如 wechat、alipay、cash。用于支付渠道订单量和收入贡献分析。",
        "明细事实表",
    ),
    "fact_parking_order.manual_open_flag": _field(
        "fact_parking_order", "manual_open_flag",
        "是否人工抬杆，0 否 1 是。用于人工放行次数和非标准运营行为分析。",
        "明细事实表",
    ),
    "fact_parking_order.free_release_flag": _field(
        "fact_parking_order", "free_release_flag",
        "是否免费放行，0 否 1 是。用于免费放行次数和潜在收入影响分析。",
        "明细事实表",
    ),
    "fact_parking_order.updated_at": _field(
        "fact_parking_order", "updated_at",
        "停车订单更新时间。用于增量同步和数据新鲜度，不作为默认收入统计时间。",
        "明细事实表",
    ),

    # ---------- 车位状态快照事实 ----------
    "fact_space_snapshot.snapshot_id": _field(
        "fact_space_snapshot", "snapshot_id",
        "车位状态快照ID，BIGINT 主键。一行代表一个停车场一个采样时点。",
        "明细事实表",
    ),
    "fact_space_snapshot.parking_lot_id": _field(
        "fact_space_snapshot", "parking_lot_id",
        "快照所属停车场ID。关联停车场维度后可输出停车场名称。",
        "明细事实表",
    ),
    "fact_space_snapshot.snapshot_time": _field(
        "fact_space_snapshot", "snapshot_time",
        "车位快照时间，DATETIME。当前空闲车位取每个停车场最新快照；历史利用率按该时间过滤和分组。",
        "明细事实表",
    ),
    "fact_space_snapshot.total_spaces": _field(
        "fact_space_snapshot", "total_spaces",
        "快照时点可运营总车位数，是历史车位利用率的分母；可能与停车场基础总车位数不同。",
        "明细事实表",
    ),
    "fact_space_snapshot.occupied_spaces": _field(
        "fact_space_snapshot", "occupied_spaces",
        "快照时点已占用车位数，是车位利用率分子，也表示停车位占用量。",
        "明细事实表",
    ),
    "fact_space_snapshot.free_spaces": _field(
        "fact_space_snapshot", "free_spaces",
        "快照时点空闲车位数。用于当前剩余车位、空闲车位和空闲率分析。",
        "明细事实表",
    ),

    # ---------- 运营异常事件事实 ----------
    "fact_operation_event.event_id": _field(
        "fact_operation_event", "event_id",
        "停车运营事件ID，BIGINT 主键。一行代表一次异常或人工操作事件。",
        "明细事实表",
    ),
    "fact_operation_event.parking_lot_id": _field(
        "fact_operation_event", "parking_lot_id",
        "事件所属停车场ID。关联停车场维度后用于异常停车场排行。",
        "明细事实表",
    ),
    "fact_operation_event.order_id": _field(
        "fact_operation_event", "order_id",
        "可选关联停车订单ID，可为空。设备离线等停车场级事件不一定对应具体订单。",
        "明细事实表",
    ),
    "fact_operation_event.event_time": _field(
        "fact_operation_event", "event_time",
        "运营异常发生时间。用于异常趋势、异常小时和与收入变化的时间对比。",
        "明细事实表",
    ),
    "fact_operation_event.event_type": _field(
        "fact_operation_event", "event_type",
        "异常事件类型，例如 payment_failed、device_offline、plate_recognition_failed、manual_gate_open、space_count_mismatch。",
        "明细事实表",
    ),
    "fact_operation_event.severity": _field(
        "fact_operation_event", "severity",
        "异常严重程度，例如 low、medium、high。用于筛选高风险停车运营异常。",
        "明细事实表",
    ),
    "fact_operation_event.event_status": _field(
        "fact_operation_event", "event_status",
        "异常处理状态，例如 pending、processing、resolved。用于未解决异常和闭环情况分析。",
        "明细事实表",
    ),
    "fact_operation_event.estimated_loss": _field(
        "fact_operation_event", "estimated_loss",
        "异常预估收入损失，DECIMAL。只表示影响估算，不能等同实际损失，也不要从净收入再次扣减。",
        "明细事实表",
    ),
    "fact_operation_event.description": _field(
        "fact_operation_event", "description",
        "异常事件文字说明。用于运营报告展示和人工核查，不适合作为主要聚合维度。",
        "明细事实表",
    ),

    # ---------- 停车场日聚合事实 ----------
    "agg_parking_daily.stat_date": _field(
        "agg_parking_daily", "stat_date",
        "日经营统计日期，DATE。一行代表一个停车场一个自然日；今天、最近七天、最近三个月和月度趋势使用该字段。",
        "日聚合事实表",
    ),
    "agg_parking_daily.parking_lot_id": _field(
        "agg_parking_daily", "parking_lot_id",
        "日经营数据所属停车场ID。关联停车场维度用于名称、城市、类型和停车场排名。",
        "日聚合事实表",
    ),
    "agg_parking_daily.order_count": _field(
        "agg_parking_daily", "order_count",
        "停车场当日已完成订单量，可跨日期和停车场求和。用于订单趋势、车流代理和收入下降驱动分析。",
        "日聚合事实表",
    ),
    "agg_parking_daily.net_revenue": _field(
        "agg_parking_daily", "net_revenue",
        "停车场当日停车净收入，口径为实收金额减退款金额，可跨日期和停车场求和。收入、停车费、营收默认使用该字段。",
        "日聚合事实表",
    ),
    "agg_parking_daily.average_parking_minutes": _field(
        "agg_parking_daily", "average_parking_minutes",
        "停车场当日完成订单平均停车时长，单位分钟。跨日或跨停车场汇总不能直接简单平均，应按订单量加权或回到订单明细重算。",
        "日聚合事实表",
    ),
    "agg_parking_daily.average_occupied_spaces": _field(
        "agg_parking_daily", "average_occupied_spaces",
        "停车场当日平均占用车位数。用于日均占用分析，跨日汇总需要考虑快照频率或时间权重。",
        "日聚合事实表",
    ),
    "agg_parking_daily.utilization_rate": _field(
        "agg_parking_daily", "utilization_rate",
        "停车场当日车位利用率，0至1之间。用于利用率排行和趋势；跨日或跨停车场不能直接简单平均。",
        "日聚合事实表",
    ),
    "agg_parking_daily.manual_open_count": _field(
        "agg_parking_daily", "manual_open_count",
        "停车场当日人工抬杆次数，可求和。用于非标准放行和收入下降辅助分析。",
        "日聚合事实表",
    ),
    "agg_parking_daily.free_release_count": _field(
        "agg_parking_daily", "free_release_count",
        "停车场当日免费放行次数，可求和。用于免费策略、异常放行和收入影响分析。",
        "日聚合事实表",
    ),
    "agg_parking_daily.exception_count": _field(
        "agg_parking_daily", "exception_count",
        "停车场当日运营异常数量，可求和。用于经营总览和收入下降的异常变化分析。",
        "日聚合事实表",
    ),
    "agg_parking_daily.updated_at": _field(
        "agg_parking_daily", "updated_at",
        "日汇总更新时间，用于判断统计任务数据新鲜度，不是业务统计日期。",
        "日聚合事实表",
    ),

    # ---------- 停车场小时聚合事实 ----------
    "agg_parking_hourly.stat_date": _field(
        "agg_parking_hourly", "stat_date",
        "小时经营数据所属日期，DATE，与 stat_hour 一起表示小时统计时间。",
        "小时聚合事实表",
    ),
    "agg_parking_hourly.stat_hour": _field(
        "agg_parking_hourly", "stat_hour",
        "统计小时，0至23。用于几点最忙、停车高峰、小时趋势和时段对比。",
        "小时聚合事实表",
    ),
    "agg_parking_hourly.parking_lot_id": _field(
        "agg_parking_hourly", "parking_lot_id",
        "小时经营数据所属停车场ID。关联停车场维度后输出停车场名称。",
        "小时聚合事实表",
    ),
    "agg_parking_hourly.order_count": _field(
        "agg_parking_hourly", "order_count",
        "停车场该小时完成订单量。用于小时订单、完成车流和高峰辅助分析。",
        "小时聚合事实表",
    ),
    "agg_parking_hourly.net_revenue": _field(
        "agg_parking_hourly", "net_revenue",
        "停车场该小时停车净收入。用于小时收入贡献和收入高峰分析。",
        "小时聚合事实表",
    ),
    "agg_parking_hourly.occupied_spaces": _field(
        "agg_parking_hourly", "occupied_spaces",
        "停车场该小时平均占用车位数。用于小时占用和停车高峰分析。",
        "小时聚合事实表",
    ),
    "agg_parking_hourly.utilization_rate": _field(
        "agg_parking_hourly", "utilization_rate",
        "停车场该小时车位利用率，0至1之间。默认停车高峰可定义为利用率最高小时。",
        "小时聚合事实表",
    ),
    "agg_parking_hourly.exception_count": _field(
        "agg_parking_hourly", "exception_count",
        "停车场该小时运营异常数量。用于定位异常集中时段。",
        "小时聚合事实表",
    ),
    "agg_parking_hourly.updated_at": _field(
        "agg_parking_hourly", "updated_at",
        "小时汇总更新时间，用于数据新鲜度，不是业务时间字段。",
        "小时聚合事实表",
    ),
}


# 规则仅修正 Schema 字段召回，不负责生成 SQL 指标公式。
# whitelist/conditional 表示提高并保证候选表内字段进入结果；blacklist 表示硬排除。
# 同一字段同时命中时由 evaluate_rules 保证白名单优先，便于更具体的业务词覆盖通用黑名单。
BUSINESS_RULES = [
    {
        "type": "whitelist",
        "trigger_keywords": ["收入", "营收", "停车费", "停车收入", "净收入"],
        "force_include": [
            "agg_parking_daily.net_revenue",
            "agg_parking_daily.stat_date",
            "fact_parking_order.paid_amount",
            "fact_parking_order.refund_amount",
            "fact_parking_order.exit_time",
            "fact_parking_order.order_status",
            "fact_parking_order.payment_status",
        ],
        "reason": "收入趋势优先日净收入；明细净收入使用实收减退款并按出场时间归属",
    },
    {
        "type": "blacklist",
        "trigger_keywords": ["收入", "营收", "停车费", "停车收入", "净收入"],
        "force_exclude": [
            "fact_parking_order.receivable_amount",
            "fact_parking_order.discount_amount",
            "fact_operation_event.estimated_loss",
        ],
        "reason": "默认收入采用实收减退款口径，排除应收、优惠金额和异常预估损失",
    },
    {
        "type": "whitelist",
        "trigger_keywords": ["应收", "应收金额"],
        "force_include": [
            "fact_parking_order.receivable_amount",
            "fact_parking_order.exit_time",
        ],
        "reason": "用户明确查询应收口径时，允许覆盖默认收入黑名单",
    },
    {
        "type": "whitelist",
        "trigger_keywords": ["优惠", "减免", "优惠金额"],
        "force_include": [
            "fact_parking_order.discount_amount",
            "fact_parking_order.exit_time",
        ],
        "reason": "用户明确查询优惠口径时，允许覆盖默认收入黑名单",
    },
    {
        "type": "whitelist",
        "trigger_keywords": ["今天", "昨日", "昨天", "最近", "近三个月", "趋势", "同比", "环比", "本月", "上月"],
        "force_include": [
            "agg_parking_daily.stat_date",
            "agg_parking_hourly.stat_date",
            "fact_parking_order.exit_time",
            "fact_space_snapshot.snapshot_time",
            "fact_operation_event.event_time",
        ],
        "reason": "为不同事实粒度补充正确的业务时间字段",
    },
    {
        "type": "blacklist",
        "trigger_keywords": ["今天", "昨日", "昨天", "最近", "近三个月", "趋势", "同比", "环比", "本月", "上月"],
        "force_exclude": [
            "dim_parking_lot.updated_at",
            "fact_parking_order.updated_at",
            "agg_parking_daily.updated_at",
            "agg_parking_hourly.updated_at",
        ],
        "reason": "updated_at 是数据维护时间，不能代替订单、快照、事件或统计业务时间",
    },
    {
        "type": "whitelist",
        "trigger_keywords": ["停车场", "车场", "场库", "哪个停车场", "各停车场", "某停车场"],
        "force_include": [
            "dim_parking_lot.parking_lot_id",
            "dim_parking_lot.parking_lot_name",
            "agg_parking_daily.parking_lot_id",
            "agg_parking_hourly.parking_lot_id",
            "fact_parking_order.parking_lot_id",
            "fact_space_snapshot.parking_lot_id",
            "fact_operation_event.parking_lot_id",
        ],
        "reason": "停车场问题需要统一停车场键和名称维度",
    },
    {
        "type": "whitelist",
        "trigger_keywords": ["利用率", "车位利用率", "泊位利用率", "占用率", "空闲率", "空闲车位", "剩余车位"],
        "force_include": [
            "agg_parking_daily.utilization_rate",
            "agg_parking_daily.stat_date",
            "fact_space_snapshot.occupied_spaces",
            "fact_space_snapshot.total_spaces",
            "fact_space_snapshot.free_spaces",
            "fact_space_snapshot.snapshot_time",
        ],
        "reason": "利用率使用占用车位与同一快照的可运营总车位，趋势可使用日汇总",
    },
    {
        "type": "blacklist",
        "trigger_keywords": ["利用率", "车位利用率", "泊位利用率", "占用率", "空闲率"],
        "force_exclude": [
            "dim_parking_lot.total_spaces",
        ],
        "reason": "历史利用率不能使用停车场当前静态总车位数作为历史分母",
    },
    {
        "type": "whitelist",
        "trigger_keywords": ["总车位", "车位总数", "停车位总数", "泊位总数"],
        "force_include": [
            "dim_parking_lot.total_spaces",
            "fact_space_snapshot.total_spaces",
            "fact_space_snapshot.snapshot_time",
        ],
        "reason": "明确查询总车位时，允许覆盖利用率场景中的静态容量黑名单",
    },
    {
        "type": "whitelist",
        "trigger_keywords": ["平均停车时长", "停车时长", "停留时长", "平均时长", "停了多久"],
        "force_include": [
            "fact_parking_order.entry_time",
            "fact_parking_order.exit_time",
            "fact_parking_order.parking_minutes",
            "fact_parking_order.order_status",
            "agg_parking_daily.average_parking_minutes",
        ],
        "reason": "明细平均时长使用完成订单 parking_minutes，并可用出入场时间核验",
    },
    {
        "type": "blacklist",
        "trigger_keywords": ["平均停车时长", "停车时长", "停留时长", "平均时长", "停了多久"],
        "force_exclude": [
            "fact_parking_order.receivable_amount",
            "fact_parking_order.discount_amount",
            "fact_parking_order.paid_amount",
            "fact_parking_order.refund_amount",
            "fact_parking_order.payment_status",
            "fact_parking_order.payment_method",
            "fact_parking_order.updated_at",
        ],
        "reason": "时长问题排除金额、支付属性和数据维护时间等易混淆字段",
    },
    {
        "type": "whitelist",
        "trigger_keywords": ["订单", "订单量", "订单数", "停车次数", "停车量"],
        "force_include": [
            "fact_parking_order.order_id",
            "fact_parking_order.order_status",
            "fact_parking_order.exit_time",
            "agg_parking_daily.order_count",
        ],
        "reason": "订单趋势优先完成订单汇总，明细计数需过滤 completed",
    },
    {
        "type": "whitelist",
        "trigger_keywords": ["车流", "车流量", "入场", "进场"],
        "force_include": [
            "fact_parking_order.order_id",
            "fact_parking_order.entry_time",
            "fact_parking_order.parking_lot_id",
        ],
        "reason": "入场车流按 entry_time 统计停车订单",
    },
    {
        "type": "whitelist",
        "trigger_keywords": ["出场", "离场"],
        "force_include": [
            "fact_parking_order.order_id",
            "fact_parking_order.exit_time",
            "fact_parking_order.order_status",
        ],
        "reason": "出场车流按 exit_time 统计完成订单",
    },
    {
        "type": "whitelist",
        "trigger_keywords": ["高峰", "几点最忙", "小时", "时段"],
        "force_include": [
            "agg_parking_hourly.stat_date",
            "agg_parking_hourly.stat_hour",
            "agg_parking_hourly.utilization_rate",
            "agg_parking_hourly.occupied_spaces",
            "agg_parking_hourly.order_count",
        ],
        "reason": "停车高峰默认使用小时利用率和平均占用车位分析",
    },
    {
        "type": "blacklist",
        "trigger_keywords": ["高峰", "几点最忙", "小时", "时段"],
        "force_exclude": [
            "agg_parking_hourly.net_revenue",
            "agg_parking_hourly.exception_count",
        ],
        "reason": "未指定高峰类型时默认分析繁忙程度，不混入收入或异常口径",
    },
    {
        "type": "whitelist",
        "trigger_keywords": ["收入高峰", "营收高峰", "停车费高峰", "小时收入"],
        "force_include": [
            "agg_parking_hourly.stat_date",
            "agg_parking_hourly.stat_hour",
            "agg_parking_hourly.net_revenue",
        ],
        "reason": "用户明确查询收入高峰时，允许覆盖通用高峰黑名单",
    },
    {
        "type": "whitelist",
        "trigger_keywords": ["异常高峰", "异常集中时段"],
        "force_include": [
            "agg_parking_hourly.stat_date",
            "agg_parking_hourly.stat_hour",
            "agg_parking_hourly.exception_count",
        ],
        "reason": "用户明确查询异常高峰时，允许覆盖通用高峰黑名单",
    },
    {
        "type": "whitelist",
        "trigger_keywords": ["支付", "支付成功率", "未支付", "支付方式", "微信", "支付宝", "现金"],
        "force_include": [
            "fact_parking_order.payment_status",
            "fact_parking_order.payment_method",
            "fact_parking_order.paid_amount",
            "fact_parking_order.order_id",
        ],
        "reason": "支付分析在停车订单表内按支付状态和方式完成",
    },
    {
        "type": "whitelist",
        "trigger_keywords": ["退款", "退费"],
        "force_include": [
            "fact_parking_order.refund_amount",
            "fact_parking_order.paid_amount",
            "fact_parking_order.payment_status",
            "fact_parking_order.exit_time",
        ],
        "reason": "MVP 无独立退款时间，退款只能结合订单完成时间分析",
    },
    {
        "type": "whitelist",
        "trigger_keywords": ["异常", "原因", "下降", "设备离线", "支付失败", "车牌识别", "预估损失"],
        "force_include": [
            "fact_operation_event.event_time",
            "fact_operation_event.event_type",
            "fact_operation_event.severity",
            "fact_operation_event.event_status",
            "fact_operation_event.estimated_loss",
            "agg_parking_daily.exception_count",
        ],
        "reason": "异常事件提供原因诊断证据，但不能单独证明因果",
    },
    {
        "type": "whitelist",
        "trigger_keywords": ["人工抬杆", "人工放行"],
        "force_include": [
            "fact_parking_order.manual_open_flag",
            "agg_parking_daily.manual_open_count",
        ],
        "reason": "人工抬杆可从订单明细或日汇总分析",
    },
    {
        "type": "whitelist",
        "trigger_keywords": ["免费放行", "免费车辆"],
        "force_include": [
            "fact_parking_order.free_release_flag",
            "agg_parking_daily.free_release_count",
        ],
        "reason": "免费放行可从订单明细或日汇总分析",
    },
]


CHROMA_PERSIST_DIR = os.path.join(os.path.dirname(__file__), "chroma_db", "fields")


def _cosine_relevance_score_fn(distance: float) -> float:
    """将 Chroma cosine 距离转换为余弦相似度。"""
    return 1 - distance


def get_embeddings() -> OpenAIEmbeddings:
    """构建项目统一的 OpenAI-compatible Embedding 客户端。"""
    return OpenAIEmbeddings(
        model=LLM_CONFIG["embedding_model"],
        base_url=LLM_CONFIG["base_url"],
        api_key=LLM_CONFIG["api_key"],
        check_embedding_ctx_length=False,
        chunk_size=10,
    )


def get_vectorstore() -> Chroma:
    """获取或创建字段描述 Chroma 向量存储。"""
    return Chroma(
        collection_name="field_descriptions",
        embedding_function=get_embeddings(),
        persist_directory=CHROMA_PERSIST_DIR,
        collection_metadata={"hnsw:space": "cosine"},
        relevance_score_fn=_cosine_relevance_score_fn,
    )


def build_field_index(force_rebuild: bool = False) -> Chroma:
    """将全部停车字段描述向量化并写入 Chroma。"""
    vectorstore = get_vectorstore()
    existing = vectorstore._collection.count()
    existing_ids = set(vectorstore._collection.get()["ids"]) if existing > 0 else set()
    expected_ids = set(FIELD_METADATA)
    index_matches_metadata = existing_ids == expected_ids

    if existing > 0 and not force_rebuild and index_matches_metadata:
        print(f"字段索引已存在（{existing} 条），跳过重建。如需重建请传入 force_rebuild=True")
        return vectorstore

    if existing > 0 and (force_rebuild or not index_matches_metadata):
        if existing_ids:
            vectorstore._collection.delete(ids=list(existing_ids))
        print("已清空与当前停车字段元数据不一致的旧索引数据")

    documents = []
    ids = []
    for field_key, meta in FIELD_METADATA.items():
        documents.append(
            Document(
                page_content=meta["description"],
                metadata={
                    "table_name": meta["table"],
                    "field_name": meta["field"],
                    "field_key": field_key,
                    "domain": meta["domain"],
                },
            )
        )
        ids.append(field_key)

    vectorstore.add_documents(documents, ids=ids)
    print(f"字段索引构建完成：{len(documents)} 个字段已写入 ChromaDB")
    print(f"持久化路径：{CHROMA_PERSIST_DIR}")
    return vectorstore


def evaluate_rules(query: str) -> dict:
    """根据停车业务关键词返回强制包含和排除字段。"""
    force_include = set()
    force_exclude = set()
    query_lower = query.lower()

    for rule in BUSINESS_RULES:
        if not any(keyword in query_lower for keyword in rule["trigger_keywords"]):
            continue
        if rule["type"] in ("whitelist", "conditional"):
            force_include.update(rule.get("force_include", []))
        elif rule["type"] == "blacklist":
            force_exclude.update(rule.get("force_exclude", []))

    force_exclude -= force_include
    return {
        "force_include": list(force_include),
        "force_exclude": list(force_exclude),
    }


def match_fields(
    query: str,
    candidate_tables: list[str] | None = None,
    top_k: int = 10,
    score_threshold: float = 0.15,
    rule_weight: float = 0.3,
) -> list[dict]:
    """在候选停车表内融合向量相似度和业务规则召回字段。"""
    vectorstore = get_vectorstore()
    search_kwargs = {"k": min(top_k * 3, 30)}
    if candidate_tables and len(candidate_tables) == 1:
        search_kwargs["filter"] = {"table_name": candidate_tables[0]}
    elif candidate_tables and len(candidate_tables) > 1:
        search_kwargs["filter"] = {"table_name": {"$in": candidate_tables}}

    results_with_scores = vectorstore.similarity_search_with_relevance_scores(
        query, **search_kwargs
    )
    rule_result = evaluate_rules(query)
    force_include = set(rule_result["force_include"])
    force_exclude = set(rule_result["force_exclude"])

    scored_fields = []
    seen_keys = set()
    for doc, embedding_score in results_with_scores:
        field_key = doc.metadata["field_key"]
        if field_key in seen_keys:
            continue
        seen_keys.add(field_key)
        if candidate_tables and doc.metadata["table_name"] not in candidate_tables:
            continue

        # evaluate_rules 已执行 force_exclude -= force_include，因此这里硬排除时
        # 仍然保持“更具体白名单优先”的覆盖语义。
        if field_key in force_exclude:
            continue

        rule_score = 0.0
        rule_applied = None
        if field_key in force_include:
            rule_score = 1.0
            rule_applied = "强制包含"

        final_score = (1 - rule_weight) * embedding_score + rule_weight * rule_score
        scored_fields.append({
            "field_key": field_key,
            "table": doc.metadata["table_name"],
            "field": doc.metadata["field_name"],
            "score": round(final_score, 4),
            "embedding_score": round(embedding_score, 4),
            "rule_applied": rule_applied,
            "description": doc.page_content,
        })

    for field_key in force_include:
        if field_key in seen_keys:
            continue
        meta = FIELD_METADATA.get(field_key)
        if meta and (not candidate_tables or meta["table"] in candidate_tables):
            scored_fields.append({
                "field_key": field_key,
                "table": meta["table"],
                "field": meta["field"],
                "score": round(rule_weight, 4),
                "embedding_score": 0.0,
                "rule_applied": "强制包含（补充）",
                "description": meta["description"],
            })

    scored_fields.sort(key=lambda item: item["score"], reverse=True)
    scored_fields = [
        field for field in scored_fields
        if field["score"] >= score_threshold
    ]
    return scored_fields[:top_k]


def retrieve_schema(
    query: str,
    table_top_k: int = 3,
    field_top_k: int = 10,
    table_threshold: float = 0.2,
    field_threshold: float = 0.15,
) -> dict:
    """先召回停车表，再在候选表范围内匹配字段。"""
    tables = retrieve_tables(query, top_k=table_top_k, score_threshold=table_threshold)
    candidate_table_names = [table["table_name"] for table in tables]
    fields = match_fields(
        query,
        candidate_tables=candidate_table_names,
        top_k=field_top_k,
        score_threshold=field_threshold,
    )
    return {
        "tables": tables,
        "fields": fields,
        "schema_snippet": _build_schema_snippet(tables, fields),
    }


def _build_schema_snippet(tables: list[dict], fields: list[dict]) -> str:
    """按停车表分组生成精简 Schema 片段。"""
    table_fields = {table["table_name"]: [] for table in tables}
    for field in fields:
        if field["table"] in table_fields:
            table_fields[field["table"]].append(field["field"])

    lines = []
    for table_name, field_list in table_fields.items():
        if field_list:
            lines.append(f"表：{table_name}（相关字段：{', '.join(field_list)}）")
        else:
            lines.append(f"表：{table_name}")
    return "\n".join(lines) if lines else "（未召回相关表）"


if __name__ == "__main__":
    print("=" * 70)
    print("智慧停车字段级语义匹配演示")
    print("=" * 70)
    build_field_index(force_rebuild=True)

    for question in [
        "今天停车收入是多少？",
        "最近三个月收入趋势？",
        "哪个停车场收入最高？",
        "哪个停车场利用率最低？",
        "平均停车时长是多少？",
    ]:
        print(f"\n问题：{question}")
        result = retrieve_schema(question, table_top_k=3, field_top_k=12)
        print(f"候选表：{[table['table_name'] for table in result['tables']]}")
        print(f"匹配字段：{[field['field_key'] for field in result['fields']]}")
