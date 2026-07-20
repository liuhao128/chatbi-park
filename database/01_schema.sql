-- 智慧停车 ChatBI 核心表结构
-- 数据库：MySQL 8.0+
-- 说明：本脚本不创建任何外键约束，表间关系由应用层和数据治理规则维护。

SET NAMES utf8mb4;
USE chatbi_park;

DROP TABLE IF EXISTS agg_parking_hourly;
DROP TABLE IF EXISTS agg_parking_daily;
DROP TABLE IF EXISTS fact_operation_event;
DROP TABLE IF EXISTS fact_space_snapshot;
DROP TABLE IF EXISTS fact_parking_order;
DROP TABLE IF EXISTS dim_parking_lot;

CREATE TABLE dim_parking_lot (
    parking_lot_id BIGINT NOT NULL COMMENT '停车场ID',
    parking_lot_name VARCHAR(100) NOT NULL COMMENT '停车场名称',
    operator_id BIGINT NOT NULL COMMENT '运营商ID',
    city_name VARCHAR(50) DEFAULT NULL COMMENT '所属城市',
    parking_lot_type VARCHAR(30) DEFAULT NULL COMMENT '停车场类型，如商业、园区、医院',
    total_spaces INT NOT NULL COMMENT '总车位数',
    operation_status VARCHAR(20) NOT NULL COMMENT '运营状态，如operating、closed、maintenance',
    updated_at DATETIME NOT NULL COMMENT '更新时间',
    PRIMARY KEY (parking_lot_id),
    KEY idx_parking_lot_operator (operator_id),
    KEY idx_parking_lot_city (city_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='停车场维度表';

CREATE TABLE fact_parking_order (
    order_id BIGINT NOT NULL COMMENT '停车订单ID',
    parking_lot_id BIGINT NOT NULL COMMENT '停车场ID',
    order_type VARCHAR(20) NOT NULL COMMENT '订单类型，如temporary、monthly、visitor',
    entry_time DATETIME NOT NULL COMMENT '入场时间',
    exit_time DATETIME DEFAULT NULL COMMENT '出场时间',
    parking_minutes INT DEFAULT NULL COMMENT '停车时长，单位分钟',
    order_status VARCHAR(20) NOT NULL COMMENT '订单状态，如active、completed、cancelled、exception',
    receivable_amount DECIMAL(12,2) NOT NULL DEFAULT 0 COMMENT '应收金额',
    discount_amount DECIMAL(12,2) NOT NULL DEFAULT 0 COMMENT '优惠减免金额',
    paid_amount DECIMAL(12,2) NOT NULL DEFAULT 0 COMMENT '实收金额',
    refund_amount DECIMAL(12,2) NOT NULL DEFAULT 0 COMMENT '退款金额',
    payment_status VARCHAR(20) DEFAULT NULL COMMENT '支付状态，如unpaid、paid、refunded',
    payment_method VARCHAR(20) DEFAULT NULL COMMENT '支付方式，如wechat、alipay、cash',
    manual_open_flag TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否人工抬杆，0否1是',
    free_release_flag TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否免费放行，0否1是',
    updated_at DATETIME NOT NULL COMMENT '更新时间',
    PRIMARY KEY (order_id),
    KEY idx_order_lot_exit (parking_lot_id, exit_time, order_status),
    KEY idx_order_lot_entry (parking_lot_id, entry_time),
    KEY idx_order_payment_status (payment_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='停车订单事实表';

CREATE TABLE fact_space_snapshot (
    snapshot_id BIGINT NOT NULL COMMENT '快照ID',
    parking_lot_id BIGINT NOT NULL COMMENT '停车场ID',
    snapshot_time DATETIME NOT NULL COMMENT '快照时间',
    total_spaces INT NOT NULL COMMENT '可运营车位数',
    occupied_spaces INT NOT NULL COMMENT '已占用车位数',
    free_spaces INT NOT NULL COMMENT '空闲车位数',
    PRIMARY KEY (snapshot_id),
    UNIQUE KEY uk_lot_snapshot (parking_lot_id, snapshot_time),
    KEY idx_snapshot_time (snapshot_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='车位状态快照事实表';

CREATE TABLE fact_operation_event (
    event_id BIGINT NOT NULL COMMENT '事件ID',
    parking_lot_id BIGINT NOT NULL COMMENT '停车场ID',
    order_id BIGINT DEFAULT NULL COMMENT '关联停车订单ID',
    event_time DATETIME NOT NULL COMMENT '事件发生时间',
    event_type VARCHAR(50) NOT NULL COMMENT '事件类型，如支付失败、设备离线、人工抬杆',
    severity VARCHAR(20) DEFAULT NULL COMMENT '严重程度，如low、medium、high',
    event_status VARCHAR(20) NOT NULL COMMENT '处理状态，如pending、processing、resolved',
    estimated_loss DECIMAL(12,2) NOT NULL DEFAULT 0 COMMENT '预估收入损失',
    description VARCHAR(500) DEFAULT NULL COMMENT '事件说明',
    PRIMARY KEY (event_id),
    KEY idx_event_lot_time (parking_lot_id, event_time),
    KEY idx_event_type_time (event_type, event_time),
    KEY idx_event_order (order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='运营异常事件事实表';

CREATE TABLE agg_parking_daily (
    stat_date DATE NOT NULL COMMENT '统计日期',
    parking_lot_id BIGINT NOT NULL COMMENT '停车场ID',
    order_count INT NOT NULL DEFAULT 0 COMMENT '已完成订单量',
    net_revenue DECIMAL(14,2) NOT NULL DEFAULT 0 COMMENT '停车净收入，实收金额减退款金额',
    average_parking_minutes DECIMAL(10,2) NOT NULL DEFAULT 0 COMMENT '平均停车时长，单位分钟',
    average_occupied_spaces DECIMAL(10,2) NOT NULL DEFAULT 0 COMMENT '平均占用车位数',
    utilization_rate DECIMAL(8,4) NOT NULL DEFAULT 0 COMMENT '车位利用率，0至1之间的小数',
    manual_open_count INT NOT NULL DEFAULT 0 COMMENT '人工抬杆次数',
    free_release_count INT NOT NULL DEFAULT 0 COMMENT '免费放行次数',
    exception_count INT NOT NULL DEFAULT 0 COMMENT '异常事件数量',
    updated_at DATETIME NOT NULL COMMENT '更新时间',
    PRIMARY KEY (stat_date, parking_lot_id),
    KEY idx_daily_lot_date (parking_lot_id, stat_date),
    KEY idx_daily_revenue (stat_date, net_revenue)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='停车场日经营汇总表';

CREATE TABLE agg_parking_hourly (
    stat_date DATE NOT NULL COMMENT '统计日期',
    stat_hour TINYINT UNSIGNED NOT NULL COMMENT '统计小时，取值范围0至23',
    parking_lot_id BIGINT NOT NULL COMMENT '停车场ID',
    order_count INT NOT NULL DEFAULT 0 COMMENT '已完成订单量',
    net_revenue DECIMAL(14,2) NOT NULL DEFAULT 0 COMMENT '停车净收入，实收金额减退款金额',
    occupied_spaces DECIMAL(10,2) NOT NULL DEFAULT 0 COMMENT '平均占用车位数',
    utilization_rate DECIMAL(8,4) NOT NULL DEFAULT 0 COMMENT '车位利用率，0至1之间的小数',
    exception_count INT NOT NULL DEFAULT 0 COMMENT '异常事件数量',
    updated_at DATETIME NOT NULL COMMENT '更新时间',
    PRIMARY KEY (stat_date, stat_hour, parking_lot_id),
    KEY idx_hourly_lot_date (parking_lot_id, stat_date, stat_hour)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='停车场小时经营汇总表';
