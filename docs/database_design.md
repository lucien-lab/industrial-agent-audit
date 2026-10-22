# 数据库设计

本文件描述**已实现**的存储结构（阶段 1B、1C、2）与后续阶段的规划。

- 已实现（阶段 1B/1C）：`equipment`、`data_sources`、`sensor_readings`、`data_gaps`、`fault_events`、`ingest_runs`，
  以及覆盖度函数 `sensor_coverage()`、冻结段视图 `sensor_frozen_segments`。
- 已实现（阶段 2）：`documents`、`document_chunks`、`knowledge_ingest_runs`，迁移 `0007_knowledge_rag`。
- 规划未实现：`agent_traces`、`audit_results`（见文末，集中标注为规划）。
- 实际表结构由 Alembic 迁移管理（`backend/alembic/versions/`），应用启动时**不会**自动建表。

## 总体方案

| 组件 | 实现 |
| --- | --- |
| 数据库 | PostgreSQL 17（镜像 `pgvector/pgvector:pg17`） |
| 扩展 | `pgvector 0.8.7`（迁移 `0001` 创建）、`pg_trgm 1.6`（迁移 `0007` 创建）；向量列为 `vector(1024)` |
| ORM | SQLAlchemy 2.x 声明式模型（`backend/app/models/`），`Base.metadata` 为迁移比对基准 |
| 驱动 | psycopg 3（连接串必须 `postgresql+psycopg://`） |
| 迁移 | Alembic，7 个版本：`0001` 表结构、`0002` 质量对象、`0003`/`0004` 覆盖度口径修正、`0005` 内容哈希唯一 + `partial` 状态、`0006` CHECK 约束名规范化、`0007` RAG 知识库（**0005–0007 未应用到生产库**；0007 只在隔离库执行） |
| 部署 | `docker-compose.yml` + 命名卷 `iaa-pgdata` + healthycheck；凭据全部来自 `.env` |
| 测试库 | 同一实例内的独立库 `industrial_agent_audit_test`（库名必须以 `_test` 结尾，测试前才允许 TRUNCATE） |

数据身份与幂等的基础是 **来源文件 SHA-256 + CSV 原始行号**，而不是任何运行时自增 ID。

## 已实现表结构

### equipment：设备档案

| 列 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | `uuid` | PK，`gen_random_uuid()` |
| `equipment_code` | `varchar(64)` | NOT NULL，UNIQUE；取值 `metropt3-apu-01` |
| `equipment_name` | `varchar(128)` | NOT NULL |
| `equipment_type` | `varchar(64)` | NOT NULL；取值 `air_production_unit` |
| `description` | `text` | 明确声明逻辑编号，非厂商序列号 |
| `created_at` | `timestamptz` | NOT NULL，`now()` |

当前只有一台设备。**不为凑多设备而虚构其它设备**；`equipment_id` 已经出现在所有事实表中，
后续接入真实多设备时不需要改结构。

### data_sources：原始数据来源登记

| 列 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | `uuid` | PK |
| `source_name` | `varchar(128)` | NOT NULL |
| `source_url` | `varchar(512)` | 官方页面 URL |
| `source_file` | `varchar(256)` | NOT NULL，文件名 |
| `file_sha256` | `varchar(64)` | NOT NULL，CHECK 为 64 位小写十六进制 |
| `file_bytes` | `bigint` | 文件字节数 |
| `license` | `varchar(64)` | `CC BY 4.0` |
| `doi` | `varchar(64)` | `10.24432/C5VW3R` |
| `timezone_status` | `varchar(32)` | NOT NULL，CHECK ∈ {`unknown`,`utc_declared`,`local_declared`}；本数据集为 `unknown` |
| `timezone_note` | `text` | 记录“官方未声明时区、禁止按 UTC 解释”的依据 |
| `nominal_sample_interval_s` | `numeric(8,3)` | 实测标称步长 10 s（非官方宣称的 1 Hz） |
| `recorded_at` | `timestamptz` | 登记时间 |
| `note` | `text` | 备注 |

约束：

- `UNIQUE(file_sha256)`（`uq_data_sources_content_sha256`，迁移 `0005`）：**数据身份以内容哈希为准**，
  同一内容只能登记一次；文件名只是元数据。
- `UNIQUE(source_file, file_sha256)`：历史约束，保留不动。
- 未升级 `0005` 的库（当前生产库）：导入器仍按 SHA-256 复用既有来源（不会产生重复观测），
  但“并发重复登记”的硬约束缺失，`ingest_runs.notes` 会记录 `db_hash_unique_enforced=false`。

### sensor_readings：MetroPT-3 原始监测记录

一行 = 一次多信号观测（宽表）。列名与 CSV 的对应关系（`DV_eletric` 的官方拼写错误原样保留）：

| CSV 列 | 数据库列 | 类型 | 单位/取值 |
| --- | --- | --- | --- |
| `TP2` | `tp2` | `numeric(8,4)` | bar，压缩机侧压力 |
| `TP3` | `tp3` | `numeric(8,4)` | bar，气动面板压力 |
| `H1` | `h1` | `numeric(8,4)` | bar，旋风分离器压降 |
| `DV_pressure` | `dv_pressure` | `numeric(8,4)` | bar，干燥塔排放压降 |
| `Reservoirs` | `reservoirs` | `numeric(8,4)` | bar，储气罐下游压力 |
| `Oil_temperature` | `oil_temperature` | `numeric(8,4)` | °C，润滑油温度 |
| `Motor_current` | `motor_current` | `numeric(8,4)` | A，单相电流 |
| `COMP` | `comp` | `smallint` | 0/1，CHECK 约束 |
| `DV_eletric` | `dv_eletric` | `smallint` | 0/1 |
| `Towers` | `towers` | `smallint` | 0/1 |
| `MPG` | `mpg` | `smallint` | 0/1 |
| `LPS` | `lps` | `smallint` | 0/1 |
| `Pressure_switch` | `pressure_switch` | `smallint` | 0/1 |
| `Oil_level` | `oil_level` | `smallint` | 0/1 |
| `Caudal_impulses` | `caudal_impulses` | `smallint` | 0/1 |
| `Unnamed: 0` | `source_row_number` | `integer` | CSV 首列原值，步长 **10**（0,10,…,15,169,470） |
| `timestamp` | `observed_at` | `timestamp without time zone` | 源时间，时区未知 |
| — | `source_timestamp_raw` | `varchar(19)` | 源时间原文，逐字保留 |

其它列：

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `bigint identity` | PK。事实表用顺序主键，见下方“与阶段 0 设计的差异” |
| `equipment_id` | `uuid` | FK → `equipment.id`，`ON DELETE RESTRICT` |
| `source_id` | `uuid` | FK → `data_sources.id`，`ON DELETE RESTRICT` |
| `quality_flags` | `smallint` | NOT NULL，默认 0，位掩码，见“质量标记语义” |
| `quality_rule_version` | `varchar(32)` | NOT NULL，默认 `frozen-v1` |
| `ingested_at` | `timestamptz` | NOT NULL，默认 `now()` |

约束与索引：

| 名称 | 定义 | 用途 |
| --- | --- | --- |
| `uq_sensor_readings_source_row` | UNIQUE (`source_id`, `source_row_number`) | **幂等身份**：重复导入由数据库拦截 |
| `ix_sensor_readings_equipment_observed_at` | BTREE (`equipment_id`, `observed_at`) | 设备 + 时间窗查询主索引 |
| `ix_sensor_readings_quality_flags` | BTREE (`equipment_id`, `observed_at`) WHERE `quality_flags <> 0` | 只索引约 3.35% 的冻结候选行 |
| `ck_sensor_readings_digital_signals_binary` | 8 个数字量均 `IN (0,1)` | 阻止出现“第三态” |
| `ck_sensor_readings_observed_at_matches_source_text` | `observed_at = source_timestamp_raw::timestamp` | 禁止导入过程产生隐式时区/格式转换 |
| `ck_sensor_readings_quality_flags_non_negative` | `quality_flags >= 0` | 位掩码合法性 |

#### 跨来源重复观测的防护

唯一键 `(source_id, source_row_number)` 只能拦住“同一来源重复导入”。为了避免
**不同文件、相同时间戳**造成的重复观测，导入器在写入时额外执行：

```sql
INSERT INTO sensor_readings (...)
SELECT ... FROM stg
WHERE NOT EXISTS (
    SELECT 1 FROM sensor_readings r
    WHERE r.equipment_id = s.equipment_id AND r.observed_at = s.observed_at
)
ON CONFLICT (source_id, source_row_number) DO NOTHING
```

并按块统计两类重叠：取值完全相同（跳过）与取值不同（**冲突**）。冲突默认中止导入并
报告样例，绝不覆盖既有记录；`overlap_policy='skip'` 时才跳过并在 `ingest_runs`
标记 `partial`。

> 为什么没有 `UNIQUE(equipment_id, observed_at)`：时间戳唯一化会改变 Phase 0/1A 已确认的
> 时间语义（时钟抖动可能产生同一秒的两条采样）。因此采用“写入时判重 + 显式冲突报告”。
> 已知限制：两个进程同时插入同一时间戳在理论上仍可同时成功（无唯一约束兜底），
> 实际导入为单进程批处理，风险低。

### data_gaps：窗口级采样缺口

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `bigint identity` | PK |
| `equipment_id` / `source_id` | `uuid` | FK，RESTRICT |
| `gap_start` / `gap_end` | `timestamp` | 相邻两条记录的时间边界（缺口区间） |
| `gap_seconds` | `numeric(12,3)` | 两记录时间差（原始间隔） |
| `missing_samples` | `integer` | **基于名义 10 s 周期的估计值**：`round_half_up(gap_seconds/10) − 1`（min 0）。half-up 与 PostgreSQL `round(numeric)` 一致；Python 内置 `round()` 是 half-to-even，会在 x.5 处少算 1（历史 23/332 行的差异来源，修订脚本见 `docs/phase1b_remediation.md`） |
| `threshold_seconds` | `numeric(8,3)` | 判定阈值，默认 30 |
| `rule_version` | `varchar(32)` | `gap-v1` |
| `detected_at` | `timestamptz` | 检测时间 |

约束：`UNIQUE(source_id, gap_start, gap_end, rule_version)`，重复导入不会重复登记缺口；
索引：`(equipment_id, gap_start)`。

### fault_events：官方故障事件（区间级事实）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `uuid` | PK |
| `equipment_id` / `source_id` | `uuid` | FK；`source_id` 指向 PDF 来源登记 |
| `event_code` | `varchar(16)` | UNIQUE，`E1`–`E4`（本项目内部编号） |
| `pdf_label` | `varchar(64)` | PDF 原文标签，含 `#1（PDF 中编号重复）` |
| `start_time` / `end_time` | `timestamp` | CHECK `end_time > start_time` |
| `failure_type` / `severity` | `varchar` | `Air leak` / `High stress` |
| `fault_description` | `text` | 由官方字段组合生成，不含推断 |
| `maintenance_note` | `text` | PDF 维护记录原文（含疑似笔误 `30 Apr`） |
| `source_reference` | `text` | NOT NULL，指向 PDF 的 Failure Information 表 |
| `verification_status` | `varchar(32)` | CHECK ∈ {`verified`,`typo_suspected`,`pending`} |
| `verification_note` | `text` | 核验说明 |
| `created_at` | `timestamptz` | — |

索引：`(equipment_id, start_time)`。**本表不产生任何逐行故障标签**，故障区间与传感器记录
只通过时间条件关联，区间内的每一行仍是普通观测。

### ingest_runs：导入日志

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `bigint identity` | PK |
| `source_id` | `uuid` | FK |
| `status` | `varchar(16)` | CHECK ∈ {`running`,`succeeded`,`failed`,`partial`,`dry_run`}；`partial` = 部分成功（已提交部分数据，或按策略跳过冲突行） |
| `csv_path` / `chunk_size` / `max_rows` / `dry_run` | — | 运行参数，便于复现 |
| `started_at` / `finished_at` / `duration_seconds` | `timestamptz` / `numeric(12,3)` | 起止与耗时 |
| `attempted_rows` / `inserted_rows` / `conflict_rows` / `invalid_rows` | `bigint` | 计数 |
| `frozen_rows` / `gap_rows_written` | `bigint` | 质量标记写入统计 |
| `importer_version` / `notes` | `varchar(32)` / `text` | 导入器版本；`notes` 为结构化 key=value，含 `source_reused`、`overlap_skipped`、`timestamp_conflicts`、`db_hash_unique_enforced`、`committed_rows`、`partial_commit`、`error` |

## 关键设计决策与依据

### 1. 时间：`timestamp without time zone` + 显式 `timezone_status`

源时间戳是 `YYYY-MM-DD HH:MM:SS` 字符串、无偏移，官方未声明时区（Portugal 为 UTC+0/+1 但
不能据此假定）。因此：

- `observed_at` 使用 `timestamp without time zone`；
- 原文另存 `source_timestamp_raw`，并用 CHECK 约束保证二者一致；
- 来源表标记 `timezone_status='unknown'` 并写明依据。

任何把源时间当 UTC 处理的做法都会引入最多 1 小时的静默偏移，这在本项目里被视为数据损坏。

### 2. 模拟量：`numeric(8,4)`（经实测校准，未直接套用）

原始 CSV 的文本形如 `-0.0120000000000004`、`53.60000000000001`，是 float64 序列化噪声。
全量 1,516,948 行实测：

- 文本小数位最多 16 位；
- 但 `|raw − round(raw,4)|` 的最大值仅 `2.8e-14`，没有任何一行超过 `1e-9`。

结论：真实信息精度是 4 位小数，`numeric(8,4)` 可无损还原（取值范围 ±9999.9999，
实测最大 89.05 °C / 10.676 bar 远小于上限）。导入器把这一判断写成硬约束：
若某行偏差 > `1e-6`，导入**中止**而非四舍五入，避免静默改写数值。

不使用 `float4/float8`：浮点会破坏“逐位相同”的冻结判定，并让 0.0025 A 级量化失真。
`numeric` 的逐位相等语义与 CSV 原文一致，冻结判据才能在数据库层复现。

### 3. 数字量：`smallint` + `CHECK IN (0,1)`

实测原始文本只有 `0.0`/`1.0`（无第三态、无空值），但 `Oil_level`、`DV_pressure` 的**语义/极性
尚未核验**。用 `boolean` 会把“非确认的语义”固化成布尔含义，因此保留 `smallint` 原始取值。

### 4. 源行号：保留原值，不重排、不自增

CSV 首列是 `0, 10, 20, …, 15169470`（步长 10）。它是数据身份的一部分，
**不是**连续自增 1 的行号；导入器逐行保留，唯一约束建立在
`(source_id, source_row_number)` 上。

### 5. 与阶段 0 设计的差异：事实表主键改用 BIGINT

阶段 0 规划“实体主键拟采用 UUID”。维度/业务表（`equipment`、`data_sources`、`fault_events`）
仍是 UUID；**唯独** `sensor_readings`/`data_gaps`/`ingest_runs` 使用 `bigint identity`：

- 单表百万至千万级、以 COPY 批量写入为主，顺序键的索引局部性与体积明显优于随机 UUID；
- 业务身份已由 `(source_id, source_row_number)` 唯一约束保证，不依赖主键；
- 与 UUID 外键混用不受影响（外键列仍是 UUID）。

这是有意的偏离，记录在此以备复核。

### 6. 质量标记语义：行级与窗口级严格分开

| 类别 | 载体 | 语义 |
| --- | --- | --- |
| 行级 | `sensor_readings.quality_flags`（位掩码） | 只描述“这条记录本身” |
| 窗口级 | `data_gaps` 表 + `sensor_coverage()` | 描述“两条记录之间”或“某个时间窗”的缺陷 |

位定义（`frozen-v1`）：

| 位 | 名称 | 规则 |
| --- | --- | --- |
| `1`（bit0） | `frozen_candidate` | 本条记录的 7 路模拟量与源文件中**紧邻上一条**记录逐位相同，且时间戳继续推进 |

窗口边界语义（重要）：

- 冻结标记是**全局时序判定**：每条记录与其在源文件中的**真实前驱**比较；
- 若在查询窗口内重新计算，窗口第一行没有窗口内前驱，因此会少计 1 行
  （E1 区间：数据库 91 行 vs Phase 1A 窗口重算 90 行）；
- 两者都不是错误，数据库实现更严谨；已在 `docs/phase1b_remediation.md` 记录，
  **不修改原始数据**。

约束与限制：

- 冻结候选**不删除、不改值、不插值**，只在查询时可选排除；
- 稳态数据不会被标成故障：本标记只表达“多通道同时逐位不变”这一采集侧异常，
  不产生任何故障判断；
- 判据版本写入每一行（`quality_rule_version`），改规则必须升版本，历史行保持可追溯；
- 采样缺口**不**写进单行标记，否则会把“窗口缺陷”错误归因到具体某一行。

### 7. 覆盖度：两种口径同时提供

`sensor_coverage(p_equipment_id, p_start, p_end, p_step_seconds DEFAULT 10)` 返回：

| 字段 | 定义 |
| --- | --- |
| `observed_rows` | 窗口内实际行数 |
| `expected_rows` | `floor(窗口秒数/步长)+1` |
| `coverage_ratio` | `min(实际/期望, 1.0)`，行数口径（时钟抖动会让比值略 >1，故封顶） |
| `time_coverage_ratio` | `1 − Σmissing_samples×步长/窗口时长`，时间口径；跨边界缺口按交叠比例折算 |
| `gap_count` / `missing_samples` / `max_gap_seconds` | 与窗口交叠的缺口统计 |

两个口径不可互相替代：E3 区间行覆盖率 0.9161，时间覆盖率 0.9081。
后者比 Phase 1A 报告的 0.9114 更严格，因为 Phase 1A 只统计完全落在窗口内的缺口，
而本函数计入了窗口尾部那段真实存在的 620 s 缺失。

### 8. 冻结段视图

`sensor_frozen_segments` 把行级标记聚合成连续区间（起点、终点、行数、首末源行号），
供查询与审计举证。实现依赖 `source_row_number` 的固定步长 10（实测 1,516,948 行全部成立），
在视图注释里写明了该前提。

### 9. 幂等策略

| 对象 | 唯一键 | 写入策略 |
| --- | --- | --- |
| `data_sources` | `(source_file, file_sha256)` | `INSERT … ON CONFLICT DO NOTHING`，再取回 id |
| `sensor_readings` | `(source_id, source_row_number)` | COPY → 暂存表 → `INSERT … ON CONFLICT DO NOTHING` |
| `data_gaps` | `(source_id, gap_start, gap_end, rule_version)` | `ON CONFLICT DO NOTHING` |
| `fault_events` | `event_code` | `ON CONFLICT (event_code) DO NOTHING` |
| `equipment` | `equipment_code` | `ON CONFLICT DO NOTHING` |

幂等性依靠数据库约束与冲突处理实现，**不依赖** Python 侧“先查询再插入”的竞态逻辑。

## 索引实测（EXPLAIN ANALYZE，1,516,948 行）

| 查询 | 计划 | 耗时 |
| --- | --- | --- |
| E1 时间窗计数 | `Index Only Scan` on `ix_sensor_readings_equipment_observed_at`，Heap Fetches 0 | 0.65 ms |
| 时间窗取 1000 行 | `Index Scan` 同一索引 | 0.15 ms |
| 冻结候选计数（50,870 行） | `Index Only Scan` on `ix_sensor_readings_quality_flags` | 4.5 ms |
| `sensor_coverage()` | 时间索引 + `data_gaps` 顺序扫描（332 行） | 0.7 ms |

预聚合查询未做额外调优；`data_gaps` 只有几百行，顺序扫描是合理选择。

## 查询层的返回量约束（P2-6）

原始记录列表必须分页，参数非法直接报错而不是静默截断：

| 接口 | 默认 | 上限 | 说明 |
| --- | --- | --- | --- |
| `readings_between` | 1,000 | 5,000 | 按 `(observed_at, id)` 稳定排序 + `offset` |
| `frozen_candidates` | 1,000 | 5,000 | 同上 |
| `list_fault_events` / `list_gaps` / `list_equipment` / `frozen_segments` | 100 | 1,000 | 小表 |
| `analog_statistics` / `coverage` / `count_readings` | 不受限 | — | 聚合/计数只返回一行 |

Phase 1C 的 HTTP 接口应直接复用这些上限，并把窗口大小纳入校验。

## 阶段 2：RAG 知识库（已实现，迁移 `0007_knowledge_rag`）

### documents：文档元数据

| 列 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | `uuid` | PK，`gen_random_uuid()` |
| `doc_key` | `varchar(128)` | NOT NULL，UNIQUE；逻辑文档键（换版本复用） |
| `title` | `text` | NOT NULL |
| `file_path` | `text` | NOT NULL；相对项目根路径 |
| `source_url` | `text` | 来源页 |
| `file_sha256` | `char(64)` | NOT NULL，CHECK 64 位小写十六进制 |
| `file_bytes` / `page_count` | `bigint` / `int` | 文件大小与页数 |
| `doc_type` | `varchar(32)` | CHECK ∈ {`dataset_description`,`technical_manual`,`maintenance_guide`,`fault_case`,`research_paper`,`standard`,`other`} |
| `equipment_scope` | `varchar(32)` | CHECK ∈ {`metropt3_direct`,`generic_compressor`,`model_specific_other`,`unknown`}；**适用性是一等字段** |
| `applicability_note` | `text` | 人工填写的适用性说明 |
| `manufacturer` / `model_numbers` / `version` / `language` | `varchar` | 厂商、型号、版本、语言 |
| `trust_level` | `varchar(32)` | CHECK ∈ {`official_primary`,`authoritative_secondary`,`general_reference`,`unverified`} |
| `license` / `license_note` | `varchar(128)` / `text` | 使用许可与限制 |
| `content_revision` | `int` | NOT NULL，默认 1，CHECK ≥1；内容变化时 +1 |
| `parse_status` | `varchar(16)` | CHECK ∈ {`pending`,`parsed`,`partial`,`failed`} |
| `parse_quality_note` | `text` | 低文本页、表格块、剔除的页眉页脚等 |
| `parser_version` | `varchar(32)` | 解析器版本（当前 `pdf-parser-v1`） |
| `extra_metadata` | `jsonb` | PDF 元数据、警告、段落/章节统计 |
| `created_at` / `updated_at` / `ingested_at` | `timestamptz` | 时间戳 |

索引：`doc_key` UNIQUE、`equipment_scope`、`doc_type`、`file_sha256`。

### document_chunks：可检索片段

| 列 | 类型 | 约束/说明 |
| --- | --- | --- |
| `id` | `bigint` | Identity PK |
| `document_id` | `uuid` | FK → `documents.id`，`ON DELETE CASCADE` |
| `chunk_index` | `int` | 文档内序号（从 0 开始） |
| `content` | `text` | 原文（不改写） |
| `content_sha256` | `char(64)` | CHECK 十六进制 |
| `char_count` | `int` | CHECK `= length(content)` |
| `page_start` / `page_end` | `int` | CHECK ≥1 且 `page_end >= page_start` |
| `section_title` / `section_path` | `text` | 章节标题与路径（解析器识别） |
| `search_text` | `text` | 程序写入的小写文本（`section_title + content`），供 LIKE/trgm |
| `search_tsv` | `tsvector` | `GENERATED ALWAYS AS to_tsvector('simple', ...) STORED` |
| `embedding` | `vector(1024)` | bge-m3 输出；L2 归一化 |
| `embedding_model` / `embedding_version` / `embedding_dim` | `varchar` / `varchar` / `smallint` | 三元组必须与索引一致 |
| `chunking_strategy` / `chunking_version` | `varchar(32)` / `varchar(64)` | 例如 `structural` / `structural-v1` |
| `metadata` | `jsonb` | 重叠字符数、页码范围、注入标记等 |
| `created_at` | `timestamptz` | 入库时间 |

约束与索引：

- `UNIQUE(document_id, chunking_version, chunk_index)`：同一文档同一切分版本不产生重复 Chunk；
- HNSW：`USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64)`；
- GIN：`search_tsv`；GIN(trgm)：`search_text gin_trgm_ops`；
- B-tree：`document_id`、`(embedding_model, embedding_version)`。

**维度决策**：`vector(1024)` 与实测的 `bge-m3`（Ollama）输出一致。换模型时：
维度不变 → 换 `EMBEDDING_VERSION` 并全量重建索引；维度变化 → 新增迁移修改列类型后重建。
入库前会读取 `pg_attribute.atttypmod` 校验，不一致直接拒绝写入。

### knowledge_ingest_runs：入库任务记录

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `uuid` | PK |
| `started_at` / `finished_at` | `timestamptz` | 任务时间 |
| `status` | `varchar(16)` | CHECK ∈ {`running`,`succeeded`,`failed`,`partial`,`dry_run`} |
| `chunking_strategy` / `chunking_version` | `varchar` | 本次切分 |
| `embedding_model` / `embedding_version` | `varchar` | 本次向量模型 |
| `documents_total/imported/skipped` | `int` | 文档计数 |
| `chunks_created` / `embeddings_created` / `embedding_failures` | `int` | 片段与向量计数 |
| `parse/chunk/embedding/write/total_elapsed_ms` | `int` | 分段耗时 |
| `error_message` | `text` | 失败摘要（不含正文） |
| `report` | `jsonb` | 完整入库报告（每份文档的计数与耗时） |

### 幂等、版本与安全规则（数据库层 + 服务层）

| 规则 | 实现 |
| --- | --- |
| 重复导入相同内容 | 服务层比较 `doc_key + file_sha256 + chunking_version` 与 Chunk 数 → `skipped_unchanged` |
| 文档版本更新 | 同一 `doc_key` 更新 `file_sha256`、`content_revision+1`，删除该切分版本的旧 Chunk 后重建 |
| 两套切分并存 | 删除与插入都带 `chunking_version` 条件，互不影响 |
| 同一切分不重复 | `UNIQUE(document_id, chunking_version, chunk_index)` |
| 混用不同 Embedding | 检索按 `embedding_model + embedding_version` 过滤；索引缺失时抛 `IndexModelMismatch` |
| 追溯来源 | 每个 Chunk 都可通过 `document_id` 回到文件、SHA-256、页码与章节 |
| 不碰工业数据 | 迁移与服务只操作三张 RAG 表；无外键指向传感器表 |

实测规模：6 份文档 / 131 页；`structural-v1` 750 块 + `fixed-v1` 564 块 = 1,314 Chunk，
全部 1024 维真实向量。

## 后续阶段规划（未实现，仅保留扩展空间）

### agent_traces / audit_results（阶段 3–4）

- 事件追加式存储，`trace_id` 关联任务、`sequence_no` 定位事件，保存脱敏后的可观察输入输出、
  工具名、耗时、状态、模型与提示词版本；不存隐藏推理与凭据。
- 审计结论与执行状态分离，未运行/失败/无法判定不得记为通过。

### 其它预留

- 时间规模继续增长时评估按月分区或 BRIN 索引（当前 1.5M 行、时间窗查询已在毫秒级，
  没有实测瓶颈，故不提前引入）。
- 租户、权限、保留期限与归档策略在明确部署范围后确定。

## 迁移与运维约定

```bash
scripts/db.sh up            # 启动数据库并等待健康检查
scripts/migrate.sh up       # 主库升级到 head
scripts/migrate.sh up-test  # 测试库升级到 head
scripts/migrate.sh check    # 模型与迁移是否漂移（alembic check）
scripts/migrate.sh down-test # 仅测试库允许回滚一步
```

- **禁止**在含真实数据的库上执行 `downgrade` 做“回滚测试”；测试回滚只在 `*_test` 库进行，
  且需显式设置 `ALLOW_TEST_ROLLBACK=1`（脚本按**实际连接的库名**校验，而非变量名）。
- 迁移前脚本会真实连接一次并打印 `database=...`，连接失败即中止，不回退到其它库；
  输出中的连接串统一脱敏。
- 配置优先级：命令行 `--url` > 调用方环境变量 > `.env`（只填未设置项）。
- `0001` 的 `downgrade` 不删除 `vector` 扩展：扩展可能被后续对象依赖，且属于集群级资源。
