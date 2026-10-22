# 阶段 1B 报告：PostgreSQL 数据建模、部署与 MetroPT-3 批量导入

- 执行日期：2026-10-22
- 执行环境：macOS 宿主机 + Lima Linux 虚拟机内的 Docker 引擎；Python 3.11.15（Conda `ms_pointcloud_midterm`）
- 数据：官方 MetroPT-3 CSV（1,516,948 行）与官方 PDF；未使用任何伪造或替代数据
- 本阶段未实现：RAG、Embedding、LangGraph Agent、Agent-Probe、FastAPI 业务接口、React 前端

本文件所有数字均来自实际执行（命令与脚本见文内）。复现方式见文末“复现命令”。

---

## 1. 环境部署实况

### 1.1 运行位置与如实说明

本机 **没有安装 Docker Desktop / colima**（`docker` 不在 PATH）。可用的是已运行的 Lima 虚拟机
`dev`（aarch64，Ubuntu），其中装有 docker 引擎：

| 项 | 实测值 |
| --- | --- |
| Docker Engine | 29.1.3（客户端与服务端同版本） |
| Docker Compose | v5.6.1（本次为虚拟机补装了 `docker-compose` CLI 插件） |
| 镜像 | `pgvector/pgvector:pg17`，digest `sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d` |
| 服务端版本 | PostgreSQL 17.11 (Debian 17.11-1.pgdg12+2, aarch64) |
| 扩展 | `vector 0.8.7`（`CREATE EXTENSION vector` 成功） |
| 宿主机访问 | Lima 自动端口转发，宿主机 `127.0.0.1:5432` 可直连（已实测 TCP 连接成功） |

因此 `docker-compose.yml` 是**在虚拟机内**执行的，但文件、卷、环境变量与端口映射与在宿主机
直接执行完全一致。`scripts/db.sh` 会自动选择执行引擎：优先本机 docker，否则回退到
`limactl shell <实例> -- sg docker -c 'docker compose …'`。

未运行、也无法在本阶段声称验证过的部分：宿主机原生 Docker Desktop 路径未测（本机无该引擎）。
这不影响 PostgreSQL 本身的部署与数据结果，但属于环境限制，记录在此。

### 1.2 Compose 配置要点

`docker-compose.yml`（仓库根目录）：

- 单服务 `db`，镜像 `pgvector/pgvector:pg17`；不引入 Redis / Kafka / MongoDB / ClickHouse。
- 命名卷 `iaa-pgdata` 持久化 `PGDATA`（不 bind mount 宿主机目录）。
- 端口仅绑定 `127.0.0.1`，默认 5432，可通过 `.env` 的 `POSTGRES_PORT` 调整。
- 凭据全部来自 `.env`（已忽略，未提交），compose 使用 `${POSTGRES_PASSWORD:?…}` 强制校验。
- 健康检查：`pg_isready -U $POSTGRES_USER -d $POSTGRES_DB`，5 s 间隔 / 20 次重试 / 10 s 宽限。
- `shm_size: 256mb`（导入临时表与排序需要共享内存）。
- 首次初始化执行 `deploy/initdb/01-extensions.sh`：在主库与测试库启用 pgvector，
  并创建名以 `_test` 结尾的独立测试库。

启动命令：

```bash
scripts/db.sh up      # docker compose up -d + 轮询健康状态到 healthy
```

实测输出：`Container iaa-postgres Started` → `[db] healthy`。

### 1.3 环境问题与处理（如实记录）

1. 虚拟机内 apt 源经代理返回 `502`，`docker-compose-v2` 安装失败；
   改为在宿主机下载官方 `docker-compose-linux-aarch64`（v5.6.1）放入
   `~/.docker/cli-plugins/docker-compose`，通过共享目录拷入虚拟机。
2. 虚拟机内 docker 拉取镜像时直连 `registry-1.docker.io` 超时；
   为 docker daemon 配置 HTTP(S) 代理（`/etc/systemd/system/docker.service.d/http-proxy.conf`）
   后拉取成功。该配置属于本机环境，不进入仓库。
3. 虚拟机内 docker 需要 `docker` 组权限；已把当前用户加入组，脚本用 `sg docker -c` 保证
   非交互执行也能生效。

---

## 2. 实际数据库结构

共 6 张业务表 + 1 张迁移版本表，字段级说明见 [数据库设计](database_design.md)。

| 表 | 行数（实测） | 主键 | 关键约束 |
| --- | --- | --- | --- |
| `equipment` | 1 | `uuid` | `equipment_code` UNIQUE |
| `data_sources` | 2（CSV + PDF） | `uuid` | `(source_file, file_sha256)` UNIQUE |
| `sensor_readings` | **1,516,948** | `bigint identity` | `(source_id, source_row_number)` UNIQUE |
| `data_gaps` | **332** | `bigint identity` | `(source_id, gap_start, gap_end, rule_version)` UNIQUE |
| `fault_events` | **4** | `uuid` | `event_code` UNIQUE；`end_time > start_time` |
| `ingest_runs` | 8 | `bigint identity` | `status` CHECK |
| `alembic_version` | 1 | — | 当前版本 `0004_coverage_missing_time` |

派生只读对象：

- 函数 `sensor_coverage(uuid, timestamp, timestamp, double precision)`：行口径 + 时间口径覆盖率。
- 视图 `sensor_frozen_segments`：由行级冻结标记聚合出的连续区间（实测 24 段，与 Phase 1A 一致）。

存储占用（实测）：

| 对象 | 大小 |
| --- | --- |
| `sensor_readings`（含索引） | 428 MB |
| `data_gaps` | 152 kB |
| 数据库整体 | 436 MB |

迁移版本链：

| 版本 | 内容 |
| --- | --- |
| `0001_initial_schema` | 6 张表、全部索引与 CHECK、`CREATE EXTENSION vector` |
| `0002_quality_objects` | 覆盖度函数、冻结段视图 |
| `0003_coverage_time_ratio` | 覆盖度函数增加时间口径列 |
| `0004_coverage_missing_time` | 时间口径改用 `missing_samples×步长`，与 Phase 1A 定义对齐 |

`alembic check`（模型 ↔ 迁移漂移检测）实测输出：`No new upgrade operations detected.`

---

## 3. 字段类型与设计理由（要点）

| 议题 | 决策 | 依据 |
| --- | --- | --- |
| 源时间 | `timestamp without time zone` + `source_timestamp_raw` 原文 + CHECK 两者相等 | 官方未声明时区；Portugal 为 UTC+0/+1 不能假定；CHECK 杜绝隐性时区换算 |
| 时区声明 | `data_sources.timezone_status='unknown'` + 说明文本 | 让“未知”成为可查询的事实，而不是沉默 |
| 模拟量 | `numeric(8,4)` | 实测原始文本是 4 位小数值的 float64 序列化噪声：全量 1,516,948 行中 `|raw−round(raw,4)|` 最大仅 `2.84e-14`，无一行超过 `1e-9`；定点存储无损且保留逐位相等语义 |
| 精度保护 | 导入时偏差 > `1e-6` 直接中止 | 不用四舍五入静默改写原始测量值 |
| 数字量 | `smallint` + `CHECK IN (0,1)` | 实测原文只有 `0.0/1.0`；但极性（`Oil_level`、`DV_pressure`）未核验，不用 `boolean` 伪装语义 |
| 源行号 | `integer`，保留 0,10,…,15,169,470 | 是数据身份而非自增序号；实测步长恒为 10 |
| 事实表主键 | `bigint identity`（`equipment`/`data_sources`/`fault_events` 仍为 UUID） | 千万级事实表 + COPY 批写，顺序键索引局部性更好；业务身份由唯一约束保证 |
| 索引 | `(equipment_id, observed_at)`；部分索引 `WHERE quality_flags <> 0` | 时间窗查询与冻结查询两条主路径 |

---

## 4. 数据来源与幂等策略

### 4.1 来源登记

| 用途 | `source_file` | SHA-256 | `timezone_status` |
| --- | --- | --- | --- |
| 传感器时序 | `MetroPT3(AirCompressor).csv` | `db30ccb4…93e24` | `unknown` |
| 故障区间 | `Data Description_Metro.pdf` | `b00fac0e…24127` | `unknown`（不涉及时间序列） |

导入前会与 `data/raw/metropt3/manifest.json` 的哈希比对；不一致直接拒绝导入。

### 4.2 幂等实现（数据库层，不靠 Python 预查）

| 对象 | 唯一键 | 写入策略 |
| --- | --- | --- |
| `sensor_readings` | `(source_id, source_row_number)` | `COPY` → UNLOGGED 临时表 → `INSERT … ON CONFLICT DO NOTHING` |
| `data_gaps` | `(source_id, gap_start, gap_end, rule_version)` | `ON CONFLICT ON CONSTRAINT … DO NOTHING` |
| `fault_events` | `event_code` | `ON CONFLICT (event_code) DO NOTHING` |
| `data_sources` / `equipment` | 文件+哈希 / 设备编号 | `ON CONFLICT DO NOTHING` + 回查 |

实测幂等结果（同一份 CSV 重复导入）：

| 次数 | attempted | inserted | conflict | 表内行数 |
| --- | --- | --- | --- | --- |
| 第 1 次（10,000 行） | 10,000 | 10,000 | 0 | 10,000 |
| 第 2 次（10,000 行） | 10,000 | **0** | 10,000 | 10,000 |
| 全量（1,516,948 行） | 1,516,948 | 1,506,948 | 10,000 | 1,516,948 |
| 全量后再导入 1 万行 | 10,000 | **0** | 10,000 | 1,516,948 |

官方故障事件重复导入：`inserted=0, skipped_existing=4`，表内仍为 4 条。

---

## 5. 数据导入方法与实测结果

### 5.1 方法

- **分块读取**：`pandas.read_csv(chunksize=100_000, dtype=str)`，流式处理，内存与文件大小无关。
- **逐字保真**：所有字段先按字符串读入，数字用 `Decimal` 定点解析，时间戳原文单独入库。
- **批量写入**：psycopg3 `COPY` 写入会话临时表 `stg_sensor_readings`（`ON COMMIT DELETE ROWS`），
  每条 chunk 一个事务，随后 `INSERT … SELECT … ON CONFLICT DO NOTHING` 入正式表。
  - 选择理由：`COPY` 是 PostgreSQL 最快的装载通道；`ON CONFLICT` 让幂等性由唯一约束保证，
    同时可以直接用 `rowcount` 得到“新增 / 冲突”两类的准确计数。
  - 未采用“暂存表 + 差集比对”的两段式方案：本阶段冲突语义简单（唯一键相同即同一条记录），
    `ON CONFLICT` 已足够且少一次全表比对。
- **跨块状态**：冻结判定与缺口判定需要上一条记录，状态在 chunk 之间显式传递，不因分块漏判。
- **试运行**：`--dry-run` 完全不连接数据库，只做解析、校验与统计。

### 5.2 实测导入记录（`ingest_runs` 表原始数据）

| run | 模式 | attempted | inserted | conflict | invalid | frozen | gaps | 耗时（s） |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 10,000 行 | 10,000 | 0 | 0 | 0 | 0 | 0 | 0.268（**failed**） |
| 2 | 10,000 行 | 10,000 | 10,000 | 0 | 0 | 0 | 4 | 0.330 |
| 3 | 10,000 行（重复） | 10,000 | 0 | 10,000 | 0 | 0 | 0 | 0.285 |
| 4 | 全量 | 1,516,948 | 1,506,948 | 10,000 | 0 | 50,870 | 328 | **24.167** |
| 5–8 | 重复导入 1 万行 ×4（验证脚本） | 10,000 | 0 | 10,000 | 0 | 0 | 0 | 0.28–0.30 |

- run 1 是导入器早期版本的失败记录（COPY 列数与目标表不匹配）。它按设计**没有写入任何数据**
  （事务回滚，`inserted_rows=0`），并以 `status=failed` 留在日志里。修复后重跑得到 run 2。
- run 4 中 `frozen=50,870` 与 `gaps=328`：328 是本轮新写入的缺口数，加上 run 2 已写入的 4 处，
  全库共 **332** 处（与 Phase 1A 完全一致），重复的缺口被唯一约束挡掉。
- 全量导入吞吐约 **62,800 行/秒**（含解析、冻结/缺口判定、COPY、索引维护），
  数据流向为“宿主机读 CSV → 转发端口 → 虚拟机内 PostgreSQL”。

### 5.3 导入顺序（按任务要求）

1. 先用 `--dry-run` 全量校验：1,516,948 行、0 无效行、50,870 冻结候选、332 缺口。
2. 导入前 10,000 条真实记录 → 校验通过（行数、边界、幂等）。
3. 导入全量 → 1,516,948 条。
4. 导入 4 条官方故障事件。

---

## 6. 数据一致性验证结果

验证脚本：`scripts/verify_phase1b.py`（**独立实现**：CSV 一侧用 pandas 全量重算，
不复用导入模块的解析逻辑；数据库一侧只跑 SQL / 查询服务）。
结果文件：`data/processed/metropt3/phase1b_verification.json`。

**实测：59 项检查全部通过。**

| 检查项 | 期望（CSV 独立重算） | 实际（数据库） |
| --- | --- | --- |
| 总行数 | 1,516,948 | 1,516,948 |
| 最早 / 最晚时间 | 2020-02-01 00:00:00 / 2020-09-01 03:59:50 | 一致 |
| 源行号 | 0 … 15,169,470，唯一且连续 | 一致（1,516,948 个不同值） |
| 源时间文本 ↔ 入库时间 | 0 处不一致 | 0 处不一致 |
| E1 区间行数 | 8,657 | 8,657 |
| E1 7 路模拟量 min / max / avg（21 项） | pandas 全量重算 | 逐项一致（容差 1e-4） |
| 8 路数字量取 1 的行数（8 项） | pandas 全量重算 | 逐项完全一致 |
| 冻结候选行数 | 50,870 | 50,870 |
| 冻结候选连续段数 | 24（Phase 1A） | 24 |
| 采样缺口处数（≥30 s） | 332 | 332 |
| 官方故障事件 | 4（E1–E4） | 4 |
| E2 核验状态 | `typo_suspected` | `typo_suspected` |
| E1 覆盖度 | 8,657 行 / 缺失 34 样本 / 时间覆盖率 0.996 | 一致 |
| E3 行覆盖率 | 17,315/18,901 = 0.9161 | 0.9161 |
| E3 时间覆盖率（含跨窗口尾部缺口） | — | 0.9081 |
| E3 仅统计窗口内完整缺口 | 0.9114（Phase 1A） | 0.9114 |
| 重复导入新增 | 0 | 0（表行数不变） |

说明：`sensor_coverage()` 的时间口径比 Phase 1A 更严格——E3 窗口末端有一个从 06-07 14:19:39
延续到次日的大缺口，函数计入其落在窗口内的 620 s，因此得到 0.9081；Phase 1A 只统计完全落在
窗口内的缺口，得到 0.9114。两项都在验证中单独断言，差异是口径而非数据不一致。

### 6.1 EXPLAIN ANALYZE（索引是否被使用）

| 查询 | 计划 | 实际耗时 |
| --- | --- | --- |
| E1 时间窗计数 | `Index Only Scan using ix_sensor_readings_equipment_observed_at`，Heap Fetches 0，8657 行 | 0.65 ms |
| 时间窗取 1000 行 | `Index Scan`（同一索引） | 0.15 ms |
| 冻结候选计数 | `Index Only Scan using ix_sensor_readings_quality_flags`，50,870 行 | 4.48 ms |
| `sensor_coverage()` | 时间索引 + `data_gaps` 顺序扫描（332 行） | 0.69 ms |

未出现对 `sensor_readings` 的全表顺序扫描。未做进一步调优（无实测瓶颈）。

---

## 7. 数据质量标记说明

| 类别 | 载体 | 规则 | 版本 |
| --- | --- | --- | --- |
| 行级：冻结候选 | `sensor_readings.quality_flags` bit0 | 7 路模拟量与**源文件中紧邻上一条**记录逐位相同（四舍五入到 4 位小数后比较），时间戳继续推进 | `frozen-v1`（写在每行 `quality_rule_version`） |
| 窗口级：采样缺口 | `data_gaps` | 相邻两条记录时间差 ≥ 30 s；`missing_samples = round(Δ/10) − 1` | `gap-v1` |
| 窗口级：覆盖度 | `sensor_coverage()` | 行口径（实际/期望，封顶 1.0）与时间口径（1 − 缺失时长/窗口时长） | 随函数版本 |

明确不做的事：

- 冻结候选**不删除、不改值、不插值**，仅在查询时可选用 `exclude_frozen=True` 过滤；
- 不把稳态数据标成故障；本阶段**没有任何故障判别规则**，冻结标记只表达采集侧异常；
- 缺口不写进单行标记，避免把窗口缺陷错误归因到某一行；
- 故障事件只有区间级事实，不生成逐行标签。

实测：冻结候选 50,870 行（3.35%）、连续 24 段；缺口 332 处，累计缺失 37.86 天（与 Phase 1A 一致）。

---

## 8. 测试结果

```bash
cd backend && python -m pytest
# 63 passed in 2.50s
```

| 文件 | 用例数 | 覆盖点 |
| --- | --- | --- |
| `tests/test_config.py` | 4 | 配置缺失报错、密码脱敏、环境变量优先级 |
| `tests/test_db_connection.py` | 7 | 连接、pgvector 扩展、表/索引、唯一约束、Alembic head、模型↔迁移无漂移 |
| `tests/test_csv_parsing.py` | 14 | 表头校验、时间戳保真、4 位小数还原、精度越界中止、数字量二值、源行号步长、冻结与缺口规则、manifest 哈希 |
| `tests/test_import.py` | 8 | 真实样本导入、日志字段、重复导入、缺口去重、试运行不写库、`max_rows`、来源只登记一次、逐值精度比对 |
| `tests/test_quality.py` | 7 | 冻结行标记且保留原值、缺口落表不进行标记、冻结段视图聚合、两种覆盖度口径 |
| `tests/test_queries.py` | 7 | 设备、时间窗计数/取数、模拟量统计对齐 SQL、排除冻结、缺口查询、覆盖度 |
| `tests/test_fault_events.py` | 7 | 4 条事件、PDF 编号重复保留、E2 疑笔误、幂等、无逐行标签、关联 PDF 来源 |
| `tests/test_consistency.py` | 9 | 2 万行真实样本：行数、时间边界、统计、源行号、精度、冻结标记与缺口独立重算一致 |

测试隔离：全部使用 `industrial_agent_audit_test` 库；`conftest.py` 强制校验库名以 `_test` 结尾，
否则终止会话；每个用例前 TRUNCATE **测试库**。生产库（`industrial_agent_audit`）在测试过程中
行数保持 1,516,948 不变。

未在测试中做的事：没有对含真实数据的库执行 `downgrade`；回滚路径只在测试库验证过函数级迁移。

---

## 9. Phase 1A 遗留问题修复

| 问题 | 处理 |
| --- | --- |
| `docs/data_analysis.md` 第 5.3 节错误结论“没有任何一天达到 99.5% 的完整覆盖” | 改为“**没有连续 7 天均达到 99.5% 覆盖**；单日达到或超过 99.5% 的有 51 天，其中 49 天的覆盖率裁剪后为 1.0，最长连续达标 4 天”。该数字由 `daily_summary.csv` 独立重算确认（≥0.995 的 51 天，其中 49 天裁剪后为 1.0，最长连续 4 天） |
| 文档状态过期（“尚未下载数据”“下一阶段获取”） | `docs/data_sources.md` 改为阶段 1A 已完成获取与登记；`docs/roadmap.md` 标注阶段 0/1A/1B 完成、1C 未启动 |
| 数据质量信息保留 | 缺口、冻结候选、共线信号、时间抖动、故障区间与原始数据的关系全部保留并写入数据库/文档；未删除任何原始行 |

---

## 10. 尚未解决的问题

| 编号 | 问题 | 现状 | 影响 |
| --- | --- | --- | --- |
| P1 | 源时区未知 | 以 `timezone_status='unknown'` + naive timestamp 保存 | 跨时区换算与“当地时间”推断均不可用；任何时间比较只在源时间语义内有效 |
| P2 | `Oil_level` 极性未核验 | 90.42% 时间为 1，与官方“油位低才激活”矛盾 | 不得用于油位异常判断；数据库保留原始 0/1 |
| P3 | `DV_pressure` 语义未确认 | ≤0 占 95.22%，E4 期间恒为 0 | 不得作为统一故障判据 |
| P4 | 冻结成因 | 只知记录端多通道同时卡死 | 无法区分采集网关与传感器保持；只能标记不能修复 |
| P5 | 覆盖度口径差异 | 函数含跨窗口尾部缺失（0.9081），Phase 1A 只算窗口内缺口（0.9114） | 已在文档与验证脚本中显式说明，需在接口层固定口径 |
| P6 | 宿主机无原生 Docker | 本次经 Lima 虚拟机执行 compose | 换机部署需重新验证引擎路径；`scripts/db.sh` 已内置回退 |
| P7 | 故障事件只有区间级、无逐样本标签 | 官方仅 4 行报告 | 后续评估只能用区间级标注 + 人工判读，不得编造标签 |
| P8 | 正常候选周工况与故障周不同 | 正常周 TP2 均值 1.09 bar，故障周 7.9–8.5 bar | 后续对比实验须补同工况正常区间，否则混杂工况差异 |
| P9 | 未做长期容量与并发验证 | 单机单用户导入 | 无并发写入、无分区方案；1.5M 行当前毫秒级查询，未测更大规模 |

---

## 11. 验收自查

- [x] Phase 1A 必要遗留问题已修复（统计描述、文档状态）
- [x] PostgreSQL 成功启动（容器 healthy，宿主机 127.0.0.1:5432 可连）
- [x] pgvector 扩展启用（`vector 0.8.7`）
- [x] SQLAlchemy 模型创建成功（`alembic check` 无漂移）
- [x] Alembic 数据迁移通过（4 个版本，主库与测试库均升级到 head）
- [x] 10,000 条真实记录导入成功
- [x] 完整数据导入成功（1,516,948 条，24.17 s）
- [x] 重复导入验证通过（新增 0，行数不变）
- [x] 原始数据与入库数据统计一致（59 项独立交叉核对）
- [x] 官方故障事件导入成功（4 条，幂等）
- [x] 数据质量标记可以查询（冻结候选、冻结段视图、缺口、覆盖度）
- [x] 基础 SQL 查询正确（设备、时间窗、计数、统计、故障、覆盖度）
- [x] pytest 测试通过（63 passed）
- [x] Phase 1B 报告完成（本文件）

### 是否具备进入 Phase 1C 的条件

**具备数据基础，但不自动进入。** 数据底座已可支撑后续查询接口与 Agent 工具：表结构由迁移管理、
幂等导入可重跑、质量语义明确、查询路径已验证走索引。进入 Phase 1C 前建议先确认：

1. 查询接口的边界（时间窗上限、返回行上限、空窗口与超大窗口的语义）；
2. 覆盖度口径在接口层固定为“行口径 + 时间口径”两者，还是只暴露其一；
3. P1–P3（时区、两个数字量的极性）是否需要在接口文档中以“不可用于判断”的形式显式约束。

---

## 12. 复现命令

```bash
# 0) 配置（.env 已被忽略，不提交真实密码）
cp .env.example .env

# 1) 启动数据库（自动等待 healthy）
scripts/db.sh up

# 2) 迁移（主库 / 测试库）
scripts/migrate.sh up
scripts/migrate.sh up-test
scripts/migrate.sh check          # 期望：No new upgrade operations detected.

# 3) 试运行 + 分阶段导入
python scripts/import_metropt.py --dry-run
python scripts/import_metropt.py --max-rows 10000
python scripts/import_metropt.py                     # 全量 + 官方故障事件

# 4) 一致性验证（含 EXPLAIN ANALYZE）
python scripts/verify_phase1b.py \
  --json data/processed/metropt3/phase1b_verification.json

# 5) 测试（独立测试库）
cd backend && python -m pytest
```

`scripts/db.sh down` 停止容器但保留命名卷 `iaa-pgdata`；如需彻底重建数据库，
需显式删除该卷（本阶段未执行，避免丢失已导入数据）。
