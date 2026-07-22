# Day8：Schema 与 Schema Linking 改造记录

> 改造范围：只修改 Schema 与 Schema Linking。
>
> 未修改：`prompts/`、`agent/`、Planner、Executor、`text2sql/`、报告生成、指标 RAG JSON、数据库 DDL 和模拟数据。
>
> 业务基线：Day7 确定的智慧停车 MVP 六表，不新增车辆、支付流水、单泊位或收费规则表。

## 1. 改造背景

### 1.1 为什么需要修改 Schema？

项目的物理数据库已经是 `chatbi_park`，`database/01_schema.sql` 也已经定义了六张停车表，但改造前 AI 侧仍然理解旧新能源销售业务：

```text
物理数据库
  → dim_parking_lot、fact_parking_order 等停车六表

改造前 AI Schema
  → dim_customers、dim_products、sales_orders、exchange_rates、finance_expenses
```

这会导致：

- 停车问题召回销售表；
- 字段匹配仍把收入关联到 `net_amount`；
- Join 图仍围绕客户、产品和汇率；
- 动态 Schema Context 与真实数据库不一致；
- LLM 最终可能生成不存在的销售 SQL。

数据库连接改成 `chatbi_park` 只解决“连接哪里”，Schema 改造才解决“AI 认为库里有什么”。

### 1.2 为什么修改 Schema 后必须同步 Schema Linking？

Schema Linking 的表向量索引、字段向量索引、业务规则、表类型、关键词和 Join 图都引用物理表字段。如果只修改其中一个层次：

- 只换表描述：字段仍会召回旧字段；
- 只换字段描述：候选表仍可能是旧表；
- 不换 Join 图：新表无法正确连接；
- 不重建索引：Chroma 仍保存旧销售向量；
- 不改粒度路由：日表、小时表和订单表会被同时召回并错误 Join。

因此 Day8 把六个部分作为一个整体迁移。

## 2. 原架构分析

### 2.1 Schema 定义在哪里？

改造前项目不存在单一 Schema Registry，而是多处 Python 对象共同描述数据库。

| 位置 | 数据结构 | 职责 | 本次是否修改 |
|---|---|---|---|
| `schema/table_retriever.py::TABLE_METADATA` | Python 字典 | 表级自然语言描述、领域、关键字段 | 是 |
| `schema/field_matcher.py::FIELD_METADATA` | Python 字典 | 字段名、所属表、业务语义和枚举说明 | 是 |
| `schema/field_matcher.py::BUSINESS_RULES` | Python 列表 | 关键词触发的字段包含/排除规则 | 是 |
| `schema/join_resolver.py::TABLE_RELATIONSHIPS` | Python 图配置 | 表关系、Join 类型和连接键 | 是 |
| `schema/join_resolver.py::TABLE_TYPES` | Python 字典 | 事实表/维度表分类 | 是 |
| `schema/join_resolver.py::TABLE_KEYWORDS` | Python 字典 | 锚表和事实表业务关键词 | 是 |
| `prompts/builder.py::SCHEMA` | Prompt 字符串 | 静态全量 Schema fallback | 否，Day9 范围 |
| `database/01_schema.sql` | MySQL DDL | 真实物理表和字段 | 否，Day7 已确定 |
| `rag/indicators*.json` | JSON | 指标定义和 SQL 参考 | 否，Day9 处理 |

严格来说，当前 Schema 元数据不是从数据库 `information_schema` 自动加载，也不是 YAML/JSON 配置，而是代码中的 Python 对象。

### 2.2 Schema 加载和初始化流程

#### 表索引

```text
TABLE_METADATA
  → table_retriever.build_index()
  → 每张表构造 LangChain Document
  → OpenAI-compatible Embedding
  → Chroma collection: table_descriptions
  → schema/chroma_db/tables
```

#### 字段索引

```text
FIELD_METADATA
  → field_matcher.build_field_index()
  → 每个字段构造 LangChain Document
  → OpenAI-compatible Embedding
  → Chroma collection: field_descriptions
  → schema/chroma_db/fields
```

统一初始化入口是 `schema/schema_linker.py::ensure_indexes()`。

本次新增了索引 ID 一致性检查：只有 Chroma 中 ID 集合与当前 Python 元数据完全一致时才跳过；否则清理旧 ID 并重建。这解决了“代码已切换停车表，但本地仍复用旧销售向量”的问题。

当前 Chroma 文件被 `.gitignore` 忽略，因此本地已成功构建，不会作为源码提交。新环境仍需使用 `ensure_indexes()` 初始化。

### 2.3 Schema 在哪里被使用？

```text
用户问题
  → schema_linker.schema_link()
  → table_retriever.retrieve_tables()
  → 事实表兜底 + 停车事实粒度路由 + 停车场维度兜底
  → join_resolver.select_anchor()
  → field_matcher.match_fields()
  → join_resolver.resolve_joins()
  → _assemble_dynamic_schema()
  → build_dynamic_prompt_schema()
  → prompts.builder.build_prompt()
  → Text2SQL LLM
```

Agent 的 `StepExecutor` 默认给 `ChatBISystem.run()` 传入 `use_schema_linking=True`，所以每个 Agent 子任务也会使用该链路。

但普通 API 是否启用由请求参数和 `FEATURE_SCHEMA_LINKING` 决定；系统默认配置仍是关闭。

### 2.4 原销售 Schema

| 原表 | 主要字段 | 原业务指标/描述 |
|---|---|---|
| `dim_customers` | 客户、类型、行业、国家、区域 | 客户维度收入和订单分析 |
| `dim_products` | 产品、产品线、品类、成本 | 产品收入、成本和毛利 |
| `sales_orders` | 订单日期、状态、数量、含税/不含税金额、币种 | 销售收入、订单量、毛利 |
| `exchange_rates` | 日期、币种、人民币汇率 | 多币种收入换算 |
| `finance_expenses` | 费用日期、部门、各类费用 | 费用和利润分析 |

表级索引还包含 HR、IoT、法务、仓储四张干扰表。这些内容在停车 MVP 中都已移出 Schema Linking 候选空间。

## 3. 原 Schema Linking 实现

### 3.1 是否存在？

存在。总入口是 `schema/schema_linker.py::schema_link()`。

它不是纯 LLM 判断，而是混合方式：

```text
表级 Embedding + Chroma Top-K
  → 关键词/表类型事实表兜底
  → 规则式锚表选择
  → 字段级 Embedding + 业务规则混合评分
  → 手工关系图 + BFS Join
  → 动态 Schema Context
```

### 3.2 输入

输入是原始用户问题字符串，例如“最近三个月停车收入趋势”。

当前没有独立 Query Rewrite 结果传入，也没有结构化的指标、维度和时间对象。指标 RAG 的结果也不会传给 `schema_link()`。

### 3.3 输出

`schema_link()` 返回：

- `tables`：候选表及相似度、描述、领域；
- `fields`：字段、混合得分、Embedding 分和规则命中；
- `anchor`：SQL 主表；
- `join_path`：Join 列表、ON 条件、SQL 片段和不可达表；
- `dynamic_schema`：可注入 Prompt 的精简文本；
- `metadata`：表、字段、Join 数量等。

### 3.4 如何传给 Text2SQL？

`prompts/builder.py::build_prompt()` 在 `use_schema_linking=True` 时调用 `build_dynamic_prompt_schema(user_question)`。动态文本非空就替换静态 `SCHEMA`，否则回退静态全量 Schema。

本次没有修改这段 Prompt 集成代码。

## 4. 改造方案与修改映射

### 4.1 MVP 六表

| 新表 | 类型 | 一行粒度 | 核心用途 |
|---|---|---|---|
| `dim_parking_lot` | 维度 | 一个停车场 | 名称、城市、类型、容量、状态 |
| `fact_parking_order` | 明细事实 | 一笔停车订单 | 收入、订单、支付、时长、优惠、退款 |
| `fact_space_snapshot` | 明细事实 | 一场一时点快照 | 占用、空闲、实时/历史利用率 |
| `fact_operation_event` | 明细事实 | 一次运营事件 | 异常、处理状态、预估损失 |
| `agg_parking_daily` | 聚合事实 | 一场一自然日 | 日周月趋势、排行、经营归因 |
| `agg_parking_hourly` | 聚合事实 | 一场一日期一小时 | 高峰和小时经营分析 |

### 4.2 原业务到停车业务的映射

| 原业务概念 | 新业务概念 | 实际停车字段/表 |
|---|---|---|
| 客户维度 | 停车场维度 | `dim_parking_lot` |
| 销售订单 | 停车订单 | `fact_parking_order` |
| 销售收入 `net_amount` | 停车净收入 | 日表 `net_revenue`；明细 `paid_amount - refund_amount` |
| 订单日期 | 收入确认/完成时间 | `exit_time`；汇总使用 `stat_date` |
| 销售区域 | 城市/停车场 | `city_name`、`parking_lot_name` |
| 产品/产品线 | 订单类型/停车场类型 | `order_type`、`parking_lot_type` |
| 销售数量 | 完成订单量/车流量 | `order_count` 或订单计数 |
| 产品成本、毛利 | 不进入停车 MVP | 无对应字段，不制造概念 |
| 汇率 | 不进入停车 MVP | 停车金额默认同一币种，无汇率表 |
| 费用表 | 异常影响证据 | `fact_operation_event.estimated_loss`，但不等于财务费用 |

### 4.3 用户示例概念与真实六表字段

用户示例使用了 `parking_order`、`parking_fee`、`pay_time`、`parking_space` 等概念名。Day7 的真实模型没有这些同名对象，Day8 按真实 DDL映射：

| 概念表达 | 实际 Schema |
|---|---|
| `parking_order` | `fact_parking_order` |
| `parking_fee` | `agg_parking_daily.net_revenue`，或 `paid_amount - refund_amount` |
| `pay_time` | 当前没有独立支付时间；收入默认按 `exit_time` / `stat_date` 归属 |
| `parking_lot` | `dim_parking_lot` |
| `parking_space` | 当前没有单泊位表；使用 `fact_space_snapshot` 的总量、占用量和空闲量 |
| `entry_count` | 当前没有同名字段；从订单 `entry_time` 计数 |

不创建不存在的别名字段，是为了让动态 Schema 与 MySQL 保持一致，避免 Text2SQL 幻觉。

### 4.4 为什么不新增支付、车辆、泊位、收费规则表？

- 支付状态、方式、实收和退款已在订单表中；
- 当前不做车辆画像，避免车牌和车主隐私；
- 利用率只需要停车场级快照，不做单泊位运营；
- 当前分析收费结果，不重新运行计费引擎；
- 六表已经覆盖 Day7 的核心运营问题。

增加这些表会扩大向量召回空间、Join 图、Prompt Token 和 SQL 幻觉风险，不符合 MVP。

## 5. 修改内容

### 5.1 `schema/table_retriever.py`

**修改前**：5 张销售业务表加 4 张无关干扰表。

**修改后**：只保留 Day7 六张停车表，描述中明确表粒度、用途、核心指标和适用问题。

**修改原因**：表级 Embedding 必须从停车业务候选空间召回，不能继续出现客户、产品、汇率和费用表。

**额外变化**：

- 演示问题替换为五个停车问题；
- Chroma 索引 ID 与元数据不一致时自动清理重建；
- 领域区分维度、明细事实、日聚合事实和小时聚合事实。

### 5.2 `schema/field_matcher.py`

**修改前**：销售客户、产品、订单、汇率和费用字段；规则围绕 `net_amount/gross_amount`、产品成本、汇率和费用层级。

**修改后**：完整登记六张停车表的 59 个真实字段，业务规则覆盖：

- 停车净收入；
- 统计日期和不同事实时间；
- 停车场 ID 与名称；
- 利用率、占用、总车位和空闲车位；
- 平均停车时长与出入场时间；
- 订单、入场/出场车流；
- 高峰小时；
- 支付、退款和优惠；
- 异常、人工抬杆和免费放行。

**修改原因**：Embedding 解决同义表达，规则保证关键指标字段和时间字段不被语义召回遗漏。

**索引变化**：字段索引从旧销售字段切换为 59 个停车字段，并增加 ID 一致性检测。

### 5.3 `schema/join_resolver.py`

**修改前**：`sales_orders` 连接客户、产品、汇率，费用表独立。

**修改后**：

- 停车场维度通过 `parking_lot_id` 连接五张事实表；
- 订单与事件可通过可空 `order_id` 关联；
- 停车场维度出发使用 LEFT JOIN；
- 事实表连接维度使用 JOIN；
- 更新事实/维度分类、锚表关键词、指标信号和强指标词。

**修改原因**：BFS 必须在停车表关系图上生成真实 ON 条件。

### 5.4 `schema/schema_linker.py`

**新增停车场维度兜底**：问题出现“停车场、车场、城市、停车场类型”时，确保包含 `dim_parking_lot`，避免排行只返回 ID。

**新增事实粒度路由**：

| 问题语义 | 优先事实表 |
|---|---|
| 收入总览、趋势、排行 | `agg_parking_daily` |
| 高峰、几点、小时 | `agg_parking_hourly` |
| 当前空闲、实时利用率 | `fact_space_snapshot` |
| 支付、退款、入出场、普通平均时长 | `fact_parking_order` |
| 异常、设备离线、预估损失 | `fact_operation_event` |
| 原因诊断 | 日汇总与异常事件候选，建议由 Agent 拆成独立步骤 |

**修改原因**：真实 Embedding 测试首次将日表、小时表和订单表同时召回，BFS 经停车场维度把多张事实表连接起来，存在聚合放大风险。Embedding 保证召回率，确定性业务路由保证事实粒度。

### 5.5 `schema/vector_search_demo.py`

**修改前**：独立维护一套旧销售表描述。

**修改后**：直接从 `TABLE_METADATA` 派生六张停车表描述，演示问题改为停车问题。

**修改原因**：避免教学 Demo 成为第二套、过期的 Schema 事实来源。

### 5.6 `tests/test_parking_schema_linking.py`

新增 8 个测试，覆盖：

- 只有六张停车表；
- 59 个字段且不含旧销售字段；
- 收入、利用率、时长规则；
- 日汇总锚表和停车场 Join；
- 五个指定问题的 Pipeline 组装。

## 6. 新智慧停车 Schema 设计

### 6.1 `dim_parking_lot`

| 字段 | 业务含义 |
|---|---|
| `parking_lot_id` | 停车场统一 ID |
| `parking_lot_name` | 停车场名称 |
| `operator_id` | 运营商 ID |
| `city_name` | 城市 |
| `parking_lot_type` | 商业、园区、医院等类型 |
| `total_spaces` | 基础总车位数 |
| `operation_status` | operating、closed、maintenance |
| `updated_at` | 维度更新时间 |

### 6.2 `fact_parking_order`

| 字段组 | 字段 | 业务含义 |
|---|---|---|
| 标识 | `order_id`、`parking_lot_id` | 订单和停车场 |
| 类型 | `order_type` | 临停、月租、访客 |
| 时间 | `entry_time`、`exit_time`、`parking_minutes` | 入场、出场、停车分钟数 |
| 状态 | `order_status` | 在场、完成、取消、异常 |
| 金额 | `receivable_amount`、`discount_amount`、`paid_amount`、`refund_amount` | 应收、优惠、实收、退款 |
| 支付 | `payment_status`、`payment_method` | 支付结果和方式 |
| 行为 | `manual_open_flag`、`free_release_flag` | 人工抬杆、免费放行 |
| 审计 | `updated_at` | 更新时间 |

### 6.3 `fact_space_snapshot`

| 字段 | 业务含义 |
|---|---|
| `snapshot_id` | 快照 ID |
| `parking_lot_id` | 停车场 ID |
| `snapshot_time` | 快照时间 |
| `total_spaces` | 同时点可运营总车位，利用率分母 |
| `occupied_spaces` | 已占用车位，利用率分子 |
| `free_spaces` | 空闲车位 |

### 6.4 `fact_operation_event`

| 字段 | 业务含义 |
|---|---|
| `event_id` | 事件 ID |
| `parking_lot_id` | 停车场 ID |
| `order_id` | 可选关联订单 |
| `event_time` | 事件发生时间 |
| `event_type` | 支付失败、设备离线、识别失败等 |
| `severity` | 严重程度 |
| `event_status` | 处理状态 |
| `estimated_loss` | 预估损失，不能等同实际损失 |
| `description` | 事件说明 |

### 6.5 `agg_parking_daily`

| 字段 | 业务含义 |
|---|---|
| `stat_date`、`parking_lot_id` | 一场一日粒度 |
| `order_count` | 完成订单量，可加 |
| `net_revenue` | 停车净收入，可加 |
| `average_parking_minutes` | 平均停车时长，不可直接跨日简单平均 |
| `average_occupied_spaces` | 平均占用车位 |
| `utilization_rate` | 车位利用率，不可直接跨场简单平均 |
| `manual_open_count` | 人工抬杆次数 |
| `free_release_count` | 免费放行次数 |
| `exception_count` | 异常数量 |
| `updated_at` | 汇总更新时间 |

### 6.6 `agg_parking_hourly`

| 字段 | 业务含义 |
|---|---|
| `stat_date`、`stat_hour`、`parking_lot_id` | 一场一日一小时粒度 |
| `order_count` | 小时完成订单量 |
| `net_revenue` | 小时停车净收入 |
| `occupied_spaces` | 小时平均占用车位 |
| `utilization_rate` | 小时利用率 |
| `exception_count` | 小时异常数量 |
| `updated_at` | 汇总更新时间 |

## 7. Schema Linking 变化

### 7.1 改造后的召回方式

```text
用户停车问题
  → 表级 Embedding Top-K
  → 强指标事实表关键词兜底
  → 停车事实粒度路由
  → 停车场维度兜底
  → 锚表选择
  → 候选表内字段 Embedding
  → 停车业务字段规则混合评分
  → BFS Join
  → 动态停车 Schema Context
```

它仍是混合方式，不是纯关键词，也不是 LLM 判断。

### 7.2 表召回

表级 Chroma 只包含六张停车表。事实粒度路由在向量召回后收敛候选，避免一个简单问题同时携带订单、日表和小时表。

### 7.3 字段匹配

字段匹配仍使用源码原有公式：

```text
final_score = (1 - rule_weight) × embedding_score
              + rule_weight × rule_score
```

默认 `rule_weight=0.3`。规则字段没有进入向量 Top-K 时，会从 `FIELD_METADATA` 补入，但只在候选表范围内补入。

### 7.4 业务语义匹配

| 用户表达 | 匹配字段 |
|---|---|
| 收入、停车费、营收 | `net_revenue`；明细为 `paid_amount` 与 `refund_amount` |
| 时间趋势 | `stat_date`；明细收入时间为 `exit_time` |
| 停车场 | `parking_lot_name`、统一 `parking_lot_id` |
| 利用率、占用率 | `utilization_rate`；快照为 `occupied_spaces/total_spaces` |
| 空闲车位 | `free_spaces`、`snapshot_time` |
| 平均停车时长 | `parking_minutes`、`entry_time`、`exit_time`、完成状态 |
| 车流量 | 按 `entry_time` 或 `exit_time` 统计订单 |
| 支付成功率 | `payment_status`、`order_id` |
| 高峰 | `stat_hour`、小时 `utilization_rate` 和 `occupied_spaces` |
| 异常原因 | `event_type`、`severity`、`event_status`、`estimated_loss` |

### 7.5 Join 逻辑

停车场排行示例：

```text
agg_parking_daily.parking_lot_id
  = dim_parking_lot.parking_lot_id
```

订单异常下钻可通过 `order_id` 关联。其他事实表之间不应在未聚合时直接连接；原因分析优先由 Agent 拆成多个查询步骤。

## 8. 测试结果

### 8.1 索引构建

使用项目现有 Embedding 配置成功构建：

- 表索引：6 张停车表；
- 字段索引：59 个停车字段；
- 持久化位置：`schema/chroma_db/tables`、`schema/chroma_db/fields`；
- 目录被 Git 忽略，不属于源码变更。

### 8.2 五个问题的真实 Embedding 结果

#### 问题 1：今天停车收入是多少？

```text
召回 Schema
  → agg_parking_daily

关键字段
  → net_revenue
  → stat_date

锚表
  → agg_parking_daily

Join
  → 无需 Join

结果
  → 正确
```

用户示例期望 `parking_order/parking_fee/pay_time` 是概念表达。根据 Day7 真实模型，经营总览优先日汇总，实际字段为 `net_revenue/stat_date`。若查询退款或支付明细，才路由到订单表。

#### 问题 2：最近三个月收入趋势？

```text
召回 Schema
  → agg_parking_daily

关键字段
  → net_revenue
  → stat_date

锚表
  → agg_parking_daily

Join
  → 无需 Join

结果
  → 正确
```

#### 问题 3：哪个停车场收入最高？

```text
召回 Schema
  → agg_parking_daily
  → dim_parking_lot

关键字段
  → agg_parking_daily.net_revenue
  → agg_parking_daily.parking_lot_id
  → dim_parking_lot.parking_lot_name
  → dim_parking_lot.parking_lot_id

锚表
  → agg_parking_daily

Join
  → agg_parking_daily.parking_lot_id = dim_parking_lot.parking_lot_id

结果
  → 正确
```

#### 问题 4：哪个停车场利用率最低？

```text
召回 Schema
  → agg_parking_daily
  → dim_parking_lot

关键字段
  → agg_parking_daily.utilization_rate
  → agg_parking_daily.stat_date
  → dim_parking_lot.parking_lot_name

锚表
  → agg_parking_daily

Join
  → agg_parking_daily.parking_lot_id = dim_parking_lot.parking_lot_id

结果
  → 正确
```

如果用户问“当前还有多少空闲车位”或“实时利用率”，规则会改为 `fact_space_snapshot`，使用 `snapshot_time/free_spaces/occupied_spaces/total_spaces`。

#### 问题 5：平均停车时长是多少？

```text
召回 Schema
  → fact_parking_order

关键字段
  → parking_minutes
  → entry_time
  → exit_time
  → order_status

锚表
  → fact_parking_order

Join
  → 无需 Join

结果
  → 正确
```

当前表已有 `parking_minutes`，所以不必强制每次用时间差重算；出入场字段同时召回，用于核验和满足明确要求。

### 8.3 单元测试

```text
tests/test_parking_schema_linking.py
8 passed
```

### 8.4 全量测试

```text
39 passed, 1 failed
```

唯一失败：`tests/test_prompt_and_config.py::test_llm_client_allows_longer_sql_output` 要求 `max_tokens >= 4000`，但 `tools/config.py` 的现有默认值是 1000。

该失败与本次 Schema 改造无关，且修改配置会越过 Day8 的严格范围，因此本次没有处理。

## 9. 代码 Review

### 9.1 优点

#### Schema 清晰

- 六张表与物理 DDL 一致；
- 表名前缀直接表达维度、明细事实和聚合事实；
- 字段描述说明了业务时间、状态、计算口径和聚合注意事项；
- 没有制造 `parking_fee/pay_time` 等不存在字段。

#### 方便 Text2SQL

- 高频趋势优先日表，高峰优先小时表；
- 支付和时长明细优先订单表；
- 停车场排行的 Join 路径简单；
- 动态 Context 不再同时注入多张无关事实表；
- 业务规则能补充关键字段。

#### 方便 Agent 扩展

- 收入趋势、订单、利用率和异常分别有清晰事实来源；
- 复杂原因分析可以拆成多个简单子任务；
- 日表可作为定位入口，明细事实用于下钻；
- 表粒度在描述中明确，便于 Planner 后续选择工具和任务。

#### 索引治理有所增强

- 表/字段 ID 不一致时不再静默复用旧索引；
- 教学 Demo 复用主表元数据，减少重复维护。

### 9.2 当前问题

#### Prompt 尚未迁移

今天严格没有修改 `prompts/builder.py::SCHEMA`、Few-shot、规则和错误防护。Schema Linking 开启且正常时会提供停车动态 Schema；但功能默认关闭或动态链路失败后，系统仍会回退旧销售静态 Schema。

因此不能宣称整个 Text2SQL 已完成停车迁移，Day9 必须解决。

#### 指标 RAG 尚未迁移

`rag/indicators.json` 和 `indicators_full.json` 仍是销售指标。它们可能与新停车 Schema Context 冲突，留待 Day9 Prompt/指标知识改造。

#### Planner 尚未迁移

Planner 的维度白名单和拆解 Prompt 仍是客户、产品等旧业务。留待 Day10。

#### Schema 仍有多处来源

虽然 Schema Linking 已对齐，但静态 Prompt、DDL、RAG 和 Planner 各自维护业务信息。企业项目应建立统一元数据源并生成不同消费格式。

#### 规则仍是字符串匹配

停车事实路由基于关键词，具有可解释性和确定性，但同义表达没有穷尽。应通过真实问句集持续补充，而不是无限堆规则。

#### 原因分析仍有多事实风险

“为什么收入下降”可能需要日汇总和异常事件。当前 Schema Context可返回两类事实，但直接一次 SQL Join 仍可能放大数据。正确用法是 Agent 分步查询后汇总；Day10 应强化这一拆解。

#### 比率和平均值缺少结构化聚合属性

字段描述已说明利用率和平均时长不能简单平均，但系统没有机器可执行的 `aggregation_type`、分子、分母和加权字段。后续指标中心应补足。

#### Value Linking 不存在

“科技园停车场”能语义关联到停车场名称字段，但当前 Schema Linking 不检索数据库真实字段值，不能保证名称实体解析和纠错。

#### 索引初始化仍需治理

本地已构建索引，但服务启动没有显式调用 `ensure_indexes()`。新环境若索引为空，动态召回可能为空并回退静态 Schema。企业版应在部署或启动阶段执行版本化索引初始化。

### 9.3 Token 影响

六表共 59 个字段，全量注入仍然较长，但正常动态路由通常只保留：

- 单事实表；或
- 一张事实表加停车场维度。

这比全量六表更节省 Token。字段 `top_k=12` 控制 Context 上限，但规则补入后仍可能带入同表的次要字段；后续可通过召回评估调整阈值和 Top-K，不能只凭主观缩小。

### 9.4 后续优化建议

1. 建立统一 Schema/指标元数据源，生成 Prompt、向量文档和 Join 图；
2. 为字段增加 `aggregation_type`、`time_semantics`、`allowed_dimensions`；
3. 增加停车场名称 Value Linking；
4. 建立至少 100 条停车问句的表/字段/Join 标注集；
5. 记录 Recall@K、字段准确率、锚表准确率和 Join 准确率；
6. 在服务启动或部署任务中验证索引版本；
7. 原因诊断由 Planner 强制拆步，避免多事实明细 Join；
8. Day9 后进行 Text2SQL Execution Accuracy 回归。

## 10. 后续 Day9 Prompt 改造计划

Day9 会依赖今天的新 Schema，需要改造但今天未修改的内容：

### `prompts/builder.py::SCHEMA`

把客户、产品、销售订单、汇率和费用替换成停车六表，确保 Schema Linking关闭或失败时 fallback 仍然正确。

### `FEW_SHOT_EXAMPLES`

替换为：

- 今日停车净收入；
- 最近三个月收入趋势；
- 停车场收入排行；
- 利用率最低停车场；
- 平均停车时长；
- 小时高峰。

### `RULES`

明确：

- 收入默认净收入；
- 完成订单和支付状态；
- 收入按 `exit_time/stat_date` 归属；
- 利用率与平均值的加权聚合；
- 实时车位使用最新快照；
- 多事实表避免明细 Join。

### `ERROR_GUARDS`

增加：

- 禁止使用不存在的 `parking_fee/pay_time`；
- 禁止直接平均日利用率；
- 禁止订单、快照和事件明细互相按停车场直接 Join；
- 零分母处理；
- 排行必须连接停车场名称。

### 指标知识

迁移 `rag/indicators.json` 和 `indicators_full.json`，让停车收入、订单量、平均时长、利用率、空闲率、支付成功率和异常指标与今天的 59 个字段一致。

### 测试

验证 Schema Linking 开启和关闭两条路径，确保二者都只生成停车表 SQL。

## 11. 面试总结

如果面试官问“你的 ChatBI 是如何实现 Schema Linking 的”，可以这样回答：

> 我的智慧停车 ChatBI 使用的是混合式 Schema Linking，不是直接把整个数据库 Schema 交给大模型。核心入口在 `schema/schema_linker.py` 的 `schema_link()`，链路包括表级召回、事实表兜底、业务粒度路由、锚表选择、字段匹配、Join 推理和动态 Schema Context 组装。
>
> 表级召回方面，我为 Day7 确定的六张停车 MVP 表维护自然语言元数据，包括一张停车场维度、停车订单、车位快照、运营事件三张明细事实，以及日、小时两张聚合事实。元数据通过 OpenAI-compatible Embedding 写入 Chroma，查询时做余弦相似度 Top-K 召回。比如“最近三个月收入趋势”语义上会命中日经营汇总，“几点最忙”会命中小时汇总，“当前空闲车位”会命中车位快照。
>
> 我们没有完全依赖 Embedding，因为语义相关不代表事实粒度正确。真实测试中，“今天停车收入”曾同时召回日表、小时表和订单表，BFS 会通过停车场维度把多张事实表连接起来，容易把收入重复聚合。因此我增加了确定性停车业务路由：收入总览和趋势优先日表，高峰优先小时表，实时车位优先快照，支付、退款和普通平均时长优先订单，异常问题优先事件。Embedding 负责召回率，规则负责业务确定性。
>
> 字段层也采用混合匹配。六张表一共登记了 59 个真实字段，每个字段描述包含业务同义词、时间语义、状态过滤和聚合注意事项。系统先在候选表范围内做字段向量检索，再把 Embedding 分数和规则分数加权。规则字段如果没有出现在向量 Top-K 中，会从元数据补入。例如“收入”在日表映射 `net_revenue`，在订单明细映射 `paid_amount`、`refund_amount`、`exit_time` 和完成状态；“利用率”映射日表 `utilization_rate`，实时问题映射快照 `occupied_spaces/total_spaces`；“平均停车时长”映射 `parking_minutes`、出入场时间和订单状态。
>
> Join 方面，我人工维护逻辑关系图，因为企业库不一定有物理外键，而且还需要控制 INNER JOIN 和 LEFT JOIN。五张事实表统一通过 `parking_lot_id` 连接 `dim_parking_lot`，订单和异常事件还可以通过可空 `order_id` 下钻。系统根据指标型或实体型意图选择锚表，再用 BFS 生成最短 Join 路径。停车场收入排行会以 `agg_parking_daily` 为锚表，只连接 `dim_parking_lot` 获取名称。
>
> Schema Linking 最终返回候选表、字段、锚表、Join 条件、不可达表和精简动态 Schema。Text2SQL Prompt 开启 Schema Linking 时使用这段 Context，失败则回退静态 Schema。本次 Day8 还增加了索引 ID 一致性检测，防止代码切换停车业务后仍复用旧销售向量。
>
> 我也会说明当前边界：它还没有 Value Linking，指标 RAG、静态 Prompt 和 Planner 尚未完成停车迁移，复杂原因问题仍应由 Agent 拆成多个事实查询，不能把多张明细事实直接 Join。后续会补统一元数据、指标聚合属性、索引版本治理和 Schema Linking 标注评估集。

## 12. 今日学习总结

### 12.1 Schema 在 ChatBI 中的作用

Schema 不只是表字段清单，它还需要表达：

- 事实粒度；
- 业务语义；
- 指标时间；
- 状态过滤；
- Join 关系；
- 字段可加性；
- 适用问题。

### 12.2 Schema Linking 为什么重要？

它把用户业务语言收敛为模型可使用的有限数据库上下文，减少无关表、字段幻觉、错误 Join 和 Prompt Token。

### 12.3 为什么修改 Schema 后必须同步修改 Schema Linking？

因为表向量、字段向量、规则、锚表、Join 图和持久化索引共同决定召回结果。只换 DDL 或只换 Prompt，都不能让 AI 真正理解新业务。

### 12.4 今天真正掌握的内容

1. 如何从物理 DDL构建表级和字段级语义元数据；
2. Embedding 与业务规则如何分工；
3. 为什么事实粒度路由比盲目 Top-K 更重要；
4. 如何用锚表和 BFS 输出可解释 Join；
5. 如何防止持久化向量索引与代码元数据不一致；
6. 如何用单元测试和真实 Embedding 测试分别验证确定性逻辑和语义召回；
7. 为什么 Day8 完成不等于整个停车 Text2SQL 已完成。

# 我的思考题

1. 为什么“今天停车收入”真实 Embedding 同时召回日表、小时表和订单表并不代表召回效果优秀？如果直接把三张表交给 Text2SQL，可能产生什么数据粒度问题？

2. 当前字段匹配为什么既要有 Embedding 分数，又要有业务规则和强制补入？请结合 `net_revenue` 与 `paid_amount/refund_amount` 说明二者职责。

3. “哪个停车场利用率最低”和“当前哪个停车场空闲车位最多”为什么应该路由到不同事实表？请说明各自的时间语义和粒度。

4. 当前 Chroma collection 已经有数据时，为什么不能只根据 `count > 0` 判断索引可复用？本次 ID 集合比较解决了什么问题，还没有解决什么版本问题？

5. Schema Linking 已经迁移为停车六表，为什么现在仍不能宣称 Text2SQL 已完成停车改造？请沿动态 Schema、静态 fallback、指标 RAG、Prompt 和 Planner 五个层次回答。

> 请先独立回答。后续点评会依据本次实际代码、真实 Embedding 结果和六表粒度约束判断，不以通用概念堆砌作为正确答案。
