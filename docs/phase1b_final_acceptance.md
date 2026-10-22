# Phase 1B 最终收口验收报告：迁移部署与回归验收

- 执行日期：2026-10-22
- 执行范围：Phase 1B 的**数据库迁移准备、受控部署与最终验证**
- 明确**未**包含：Phase 1C（FastAPI 查询服务）、RAG、Agent、前端；无新功能开发
- 生产库状态：**仍为 `0004_coverage_missing_time`，本次未对其进行任何写入或 DDL**
- 最终结论：**CONDITIONAL PASS**（隔离环境全部通过，生产迁移等待用户批准后执行）

---

## 0. 结论速览

| 项目 | 结果 |
| --- | --- |
| 生产库迁移前基线 | `0004_coverage_missing_time`，1,516,948 行，指纹 `f40c3ace…` |
| 备份 | 已完成（自定义格式，39.7 MB，sha256 校验通过，独立集群恢复比对一致） |
| 隔离预演（0005+0006） | **通过**：版本 0006，行数/指纹/外键/来源关联全部不变 |
| 23 行 `missing_samples` 修订 | **通过**：在副本上实测 23 行 → 合计 327,099 → 327,122，独立复算 0 差异 |
| 自动化测试 | **110 passed，0 failed，总覆盖率 92%**（预演库上运行） |
| 回滚可逆性 | 副本上 0006→0004 再升级回 0006 成功，数据全程不变 |
| 生产写入 | **0 次**（本次全部操作均为只读 + 独立副本） |
| 是否可关闭 Phase 1B | **是，条件为执行第 4.3 节的获批部署命令** |

---

## 1. 迁移结果

### 1.1 版本

| 阶段 | 版本 | 说明 |
| --- | --- | --- |
| 迁移前（生产库，实测） | `0004_coverage_missing_time` | `scripts/migrate.sh current` 实测 |
| 预演库迁移后（实测） | `0006_check_constraint_names` | `alembic upgrade head` 顺序执行 0005 → 0006 |
| 生产库迁移后 | **待执行**（获批后为 `0006_check_constraint_names`） | 需批准 |

### 1.2 迁移 0005 `0005_source_hash_identity`（预演实测）

| 项目 | 结果 |
| --- | --- |
| 重复 SHA-256 预检 | 生产/副本均为 **0 组**（`data_sources` 仅 2 行：CSV + PDF，哈希不同） |
| 新增约束 | `uq_data_sources_content_sha256`（UNIQUE(file_sha256)），物理索引同名 |
| 状态取值域 | `('running','succeeded','failed','partial','dry_run')`，约束名 `ck_ingest_runs_status_valid` |
| 旧约束清理 | 按定义动态查找并删除历史双前缀 `ck_ingest_runs_ck_ingest_runs_status_valid` |
| 是否修改 `sensor_readings` | **否**（迁移脚本不引用该表） |
| 是否影响外键 | **否**（外键仍 7 个，前后一致） |
| 是否有历史数据违反新约束 | **无**（重复哈希 0 组；`ingest_runs` 现有 10 行状态均在允许值内） |
| 耗时实测 | **0.83 s**（全量 1,516,948 行副本，含 Alembic 启动约 0.6 s） |
| 重复执行安全性 | Alembic 版本表保证只执行一次；脚本内的检查与 `DO $$` 块本身幂等 |

### 1.3 迁移 0006 `0006_check_constraint_names`（预演实测）

| 项目 | 结果 |
| --- | --- |
| 变更内容 | 9 条 CHECK 约束 `RENAME CONSTRAINT`（含 2 条因 63 字符被截断的 `…_match_2833`、`…_non_f743`） |
| 是否改变约束定义 | **否**：归一化比对允许值集合/判定表达式，仅 0005 的 `partial` 属预期放宽 |
| 是否删除/重建业务表 | **否** |
| 是否重写数据 | **否**（`RENAME` 为元数据操作；无 `UPDATE`/`INSERT`/表重建） |
| 与 ORM 元数据一致性 | 数据库 10 条 CHECK = ORM 元数据 10 条，**完全一致**；无任何 `ck_表_ck_表_` 双前缀 |
| 耗时实测 | **1.77 s**（全量副本，含启动；实际锁窗口为毫秒级） |

约束命名迁移前后对照（预演库实测）：

| 表 | 迁移前（生产现状） | 迁移后 |
| --- | --- | --- |
| `data_gaps` | `ck_data_gaps_ck_data_gaps_gap_end_after_start` | `ck_data_gaps_gap_end_after_start` |
| `data_gaps` | `ck_data_gaps_ck_data_gaps_missing_samples_positive` | `ck_data_gaps_missing_samples_positive` |
| `data_sources` | `ck_data_sources_ck_data_sources_file_sha256_hex` | `ck_data_sources_file_sha256_hex` |
| `data_sources` | `ck_data_sources_ck_data_sources_timezone_status_valid` | `ck_data_sources_timezone_status_valid` |
| `fault_events` | `ck_fault_events_ck_fault_events_end_after_start` | `ck_fault_events_end_after_start` |
| `fault_events` | `ck_fault_events_ck_fault_events_verification_status_valid` | `ck_fault_events_verification_status_valid` |
| `sensor_readings` | `ck_sensor_readings_ck_sensor_readings_digital_signals_binary` | `ck_sensor_readings_digital_signals_binary` |
| `sensor_readings` | `ck_sensor_readings_ck_sensor_readings_observed_at_match_2833` | `ck_sensor_readings_observed_at_matches_source_text` |
| `sensor_readings` | `ck_sensor_readings_ck_sensor_readings_quality_flags_non_f743` | `ck_sensor_readings_quality_flags_non_negative` |
| `ingest_runs` | `ck_ingest_runs_ck_ingest_runs_status_valid` | `ck_ingest_runs_status_valid` |

### 1.4 `missing_samples` 修订（预演实测）

口径：`missing_samples = max(round_half_up(gap_seconds / 10) - 1, 1)`（half-up，与 PostgreSQL `round(numeric)` 一致）。

| 项目 | 结果 |
| --- | --- |
| 修订前合计 | 327,099（332 条缺口） |
| 修订后合计 | **327,122** |
| 修订行数 | **23**（每行 +1，全部为 Δ 恰为 x.5 个步长的历史行） |
| 未触碰字段 | `gap_start` / `gap_end` / `gap_seconds` / `threshold_seconds` / 行数；未删除任何行 |
| 原始观测 | `sensor_readings` 指纹前后一致（`f40c3ace…`） |
| 独立复算 | 用 `app.models.quality.estimate_missing_samples` 逐行复算 332 行 → **0 差异** |
| 执行耗时 | 0.83 s（含脚本启动） |

修订差异清单（生产库只读预检，23 行，全部 +1）：

| id | gap_start | gap_end | Δ(s) | 旧 | 新 |
| ---: | --- | --- | ---: | ---: | ---: |
| 14 | 2020-02-05 01:07:37 | 2020-02-05 04:28:42 | 12,065 | 1,205 | 1,206 |
| 43 | 2020-02-27 21:51:14 | 2020-02-27 22:32:59 | 2,505 | 249 | 250 |
| 49 | 2020-03-04 06:55:57 | 2020-03-04 07:00:02 | 245 | 23 | 24 |
| 70 | 2020-03-14 01:33:56 | 2020-03-14 02:56:01 | 4,925 | 491 | 492 |
| 71 | 2020-03-14 03:05:45 | 2020-03-14 06:29:10 | 12,205 | 1,219 | 1,220 |
| 72 | 2020-03-15 01:09:00 | 2020-03-15 08:28:45 | 26,385 | 2,637 | 2,638 |
| 81 | 2020-03-24 01:07:26 | 2020-03-24 01:12:31 | 305 | 29 | 30 |
| 99 | 2020-04-07 12:16:13 | 2020-04-07 12:26:58 | 645 | 63 | 64 |
| 127 | 2020-04-21 19:41:35 | 2020-04-22 00:15:40 | 16,445 | 1,643 | 1,644 |
| 132 | 2020-04-27 01:17:36 | 2020-04-27 01:42:01 | 1,465 | 145 | 146 |
| 164 | 2020-05-14 23:45:59 | 2020-05-15 00:49:04 | 3,785 | 377 | 378 |
| 169 | 2020-05-17 11:03:36 | 2020-05-17 23:10:01 | 43,585 | 4,357 | 4,358 |
| 170 | 2020-05-17 23:34:19 | 2020-05-18 00:40:44 | 3,985 | 397 | 398 |
| 173 | 2020-05-19 00:53:49 | 2020-05-19 01:06:34 | 765 | 75 | 76 |
| 199 | 2020-06-03 19:20:14 | 2020-06-03 21:22:39 | 7,345 | 733 | 734 |
| 202 | 2020-06-07 14:19:39 | 2020-06-08 11:48:04 | 77,305 | 7,729 | 7,730 |
| 236 | 2020-07-03 02:53:14 | 2020-07-03 02:57:59 | 285 | 27 | 28 |
| 263 | 2020-07-15 23:41:10 | 2020-07-15 23:48:15 | 425 | 41 | 42 |
| 275 | 2020-07-19 08:53:01 | 2020-07-19 08:56:26 | 205 | 19 | 20 |
| 289 | 2020-07-28 20:22:03 | 2020-07-28 22:55:48 | 9,225 | 921 | 922 |
| 316 | 2020-08-18 00:31:43 | 2020-08-18 00:35:08 | 205 | 19 | 20 |
| 319 | 2020-08-18 13:44:31 | 2020-08-18 14:43:56 | 3,565 | 355 | 356 |
| 332 | 2020-08-27 23:21:14 | 2020-08-28 01:11:39 | 6,625 | 661 | 662 |

### 1.5 锁与并发风险分析

| 迁移 | 涉及对象 | 锁类型 | 实测窗口 | 风险与依据 |
| --- | --- | --- | --- | --- |
| 0005 | `data_sources`（2 行） | ACCESS EXCLUSIVE（ADD CONSTRAINT） | 毫秒级 | 仅对 2 行建唯一索引，无表重写；不碰 `sensor_readings` |
| 0005 | `ingest_runs`（10 行） | ACCESS EXCLUSIVE（DROP + ADD CHECK） | 毫秒级 | CHECK 需校验现有 10 行，实测全部满足 |
| 0005 | `data_sources` 重复哈希预检 | ACCESS SHARE | 毫秒级 | 只读 `GROUP BY file_sha256`，2 行 |
| 0006 | 4 张表的 9 条 CHECK | ACCESS EXCLUSIVE（RENAME CONSTRAINT） | 毫秒级 | `RENAME` 为元数据操作，不重写 1,516,948 行；无并发会话时立即获锁 |

- Alembic 以**单事务**执行（`Will assume transactional DDL`）：0005+0006 要么全部生效、要么全部回滚。
- 当前项目无对外服务与并发导入，锁窗口内不存在竞争；实测全流程（含解释器启动）< 3 s。
- 唯一可能长时间持有锁的是 `pg_dump` 备份：普通 dump 仅持 ACCESS SHARE，不阻塞读写。

### 1.6 本次修复的两个新缺陷（预演阶段发现）

| 编号 | 问题 | 影响 | 处理 |
| --- | --- | --- | --- |
| D-1 | `scripts/recompute_gap_samples.py --apply` 的 UPDATE 把步长以浮点绑定，PostgreSQL 走 `round(double precision)`（银行家舍入），与 dry-run 的 `round(numeric)`（half-up）不一致 | **`--apply` 报告“需修订 23 行”却实际更新 0 行**（静默失败，生产若照此执行会误以为已完成修订） | 已改为 `CAST(:step AS numeric)`；并新增自校验：更新行数必须等于预检差异数、修订后剩余差异必须为 0、合计必须等于预检值，否则抛错回滚 |
| D-2 | `backend/tests/test_scripts.py` 两处把测试库名硬编码为 `industrial_agent_audit_test` | 任何非默认命名的隔离库（如预演库）都会误报失败，掩盖真实回归信号 | 改为从 `TEST_DATABASE_URL` 推导库名 |

同时新增一条针对性回归测试 `test_recompute_script_apply_handles_half_step_gap`（Δ=1,405 s 恰为 x.5 步长），锁死 D-1。

---

## 2. 数据完整性验证

### 2.1 迁移前后（副本：生产库备份的完整还原）

| 指标 | 迁移前（生产库实测） | 迁移后（预演库实测） | 判定 |
| --- | --- | --- | --- |
| Alembic 版本 | `0004_coverage_missing_time` | `0006_check_constraint_names` | 预期变更 |
| `sensor_readings` | 1,516,948 | **1,516,948** | 不变 |
| `equipment` | 1 | 1 | 不变 |
| `data_sources` | 2 | 2 | 不变 |
| `fault_events` | 4 | 4 | 不变 |
| `data_gaps` | 332 | 332 | 不变 |
| `ingest_runs` | 10 | 10 | 不变（迁移不写运行记录） |
| 内容指纹（17 字段 md5） | `f40c3ace26caedd77d9931fac8d2bfcc` | **`f40c3ace26caedd77d9931fac8d2bfcc`** | **不变** |
| 时间范围 | 2020-02-01 00:00:00 ~ 2020-09-01 03:59:50 | 同 | 不变 |
| 冻结候选行 | 50,870 | 50,870 | 不变 |
| 冻结段数（视图） | 24 | 24 | 不变 |
| `missing_samples` 合计 | 327,099 | 327,122（修订后） | **预期变更 +23** |
| 外键数量 | 7 | 7 | 不变 |
| 孤儿观测（设备 / 来源） | 0 / 0 | 0 / 0 | 不变 |
| `data_sources` 明细（id / 文件名 / sha256） | 2 行 | 逐字段相同 | 不变 |
| `fault_events` 明细（E1–E4 编号与区间） | 4 行 | 逐字段相同 | 不变 |
| 覆盖度函数输出 | 214,850 / 250,561 / 0.8575 / 0.8500 / 36 / 39,026 / 100,981 | 同 | 不变 |
| 重复 SHA-256 来源 | 0 组 | 0 组 | 满足新约束 |

指纹定义（本项目唯一定义，见 `scripts/migration_audit.py`）：

```
md5(string_agg(concat_ws('|',
    observed_at, tp2, tp3, h1, dv_pressure, reservoirs, oil_temperature, motor_current,
    comp, dv_eletric, towers, mpg, lps, pressure_switch, oil_level, caudal_impulses,
    quality_flags), '||' order by id))
```

> 说明：`docs/phase1b_remediation.md` 中记录的 `ed60f05f…` 来自当时未落盘的临时口径，未在仓库中定义；
> 本次以脚本内的显式 17 字段口径作为**唯一可复现基线**，并在“生产库 / 备份恢复副本 / 预演库”
> 三处逐一对齐。两者不可直接比较，也不影响任何结论（迁移前后比对全部使用新口径）。

### 2.2 约束行为验证（副本，写入探针全部 ROLLBACK）

| 探针 | 期望 | 实测 |
| --- | --- | --- |
| 插入与既有来源相同 `file_sha256`、不同文件名 | 被唯一约束拒绝 | `UniqueViolation: uq_data_sources_content_sha256` ✅ |
| 插入 `ingest_runs.status='bogus'` | 被 CHECK 拒绝 | `CheckViolation: ck_ingest_runs_status_valid` ✅ |
| 插入 `ingest_runs.status='partial'` | 允许 | 成功 ✅ |
| 探针后数据 | 无残留 | `data_sources=2`、`ingest_runs=10` ✅ |

### 2.3 备份可恢复性（独立环境）

- 备份：`pg_dump -Fc -Z6`（容器内），TOC 60 项，结构 + 数据完整。
- 在**另一个独立容器（独立 PostgreSQL 集群，独立数据目录）**中恢复成功；
- 用同一组确定性查询分别在生产库与恢复集群执行，`diff` 结果 **完全一致**：

```
alembic|0004_coverage_missing_time
count|sensor_readings|1516948
fingerprint|f40c3ace26caedd77d9931fac8d2bfcc
gap_sum|332|327099|2020-02-01 12:48:40|2020-09-01 02:37:24
source|0b2cfc5f-…|MetroPT3(AirCompressor).csv|db30ccb4…
fk_count|7
coverage|214850|250561|0.8574758242503822|0.8499949834002636|36|39026|100981
frozen_segments|24
```

---

## 3. 自动化测试

```
cd backend
TEST_DATABASE_URL=postgresql+psycopg://app_user:***@127.0.0.1:5432/iaa_rehearsal_test \
  python -m pytest --cov=app --cov-report=term
→ 110 passed, 0 failed，总覆盖率 92%
```

| 模块 | 覆盖率 |
| --- | --- |
| `app/services/metropt_import.py` | 94% |
| `app/services/queries.py` | 88% |
| `app/models/*` | 100%（`quality.py` 91%） |
| `app/core/config.py` | 93% |
| `app/db/session.py` | 43%（引擎缓存/释放分支，非关键路径） |

写入型测试全部在 `iaa_rehearsal_test`（`_test` 结尾，由 `conftest.py` 强制校验）中运行；
生产库未被任何测试连接写入（仅被两处“拒绝写入”用例以只读方式读取版本号）。

补充验证（同一副本，全量 1,516,948 行）：

| 验证 | 结果 |
| --- | --- |
| `scripts/verify_phase1b.py`（只读） | **56/56 通过**，`ingest_runs` 无变化 |
| `scripts/verify_phase1b.py --write-tests` | **59/59 通过**，行数不变，`ingest_runs` +1（副本内） |
| `scripts/verify_migration_0005_0006.py`（含写探针） | **22/22 通过** |
| `alembic check`（预演库） | `No new upgrade operations detected.` |
| `alembic check`（生产库） | `Target database is not up to date.`（0004 vs head，符合“未迁移”现状） |

---

## 4. 生产部署安全方案（等待批准）

### 4.1 备份（已完成，可重复执行）

```bash
TS=$(date +%Y%m%d_%H%M%S)
# 1) 容器内生成自定义格式备份（只读操作，不锁业务写入超过元数据级别）
limactl shell dev -- sg docker -c \
  "docker exec iaa-postgres pg_dump -U app_user -d industrial_agent_audit -Fc -Z6 -f /tmp/iaa_prod_0004_$TS.dump"

# 2) 校验 TOC 可读（不恢复）
limactl shell dev -- sg docker -c \
  "docker exec iaa-postgres pg_restore -l /tmp/iaa_prod_0004_$TS.dump | wc -l"

# 3) 取回宿主机（Lima 的 $HOME 为只读挂载，需经 limactl copy）
limactl shell dev -- sg docker -c \
  "docker cp iaa-postgres:/tmp/iaa_prod_0004_$TS.dump /home/lucien.guest/"
limactl copy "dev:/home/lucien.guest/iaa_prod_0004_$TS.dump" ~/iaa-backups/

# 4) 宿主机校验 sha256（与容器内 sha256sum 输出比对）
shasum -a 256 ~/iaa-backups/iaa_prod_0004_$TS.dump
```

本次已产出备份：

| 项目 | 值 |
| --- | --- |
| 路径 | `~/iaa-backups/iaa_prod_0004_20261022_104059.dump` |
| 大小 | 39,688,496 字节（39.7 MB，gzip 压缩的自定义格式） |
| sha256 | `1d38c68e2dd4147d7ca102608ff9d35e611535b8460bbecabcc419518d09b223` |
| 容器内/宿主机校验 | 一致 |
| 恢复验证 | 已在**独立容器集群**完整恢复，比对输出与生产库逐行一致 |

### 4.2 备份完整性/可恢复性验证（不触碰生产）

```bash
# 独立集群（独立容器、独立数据目录，不挂载任何生产卷）
export PW='<取自 .env 的 POSTGRES_PASSWORD>'
limactl shell dev -- sg docker -c \
  "docker run -d --name iaa-restore-verify -e POSTGRES_PASSWORD='$PW' -e POSTGRES_DB=postgres pgvector/pgvector:pg17"
limactl shell dev -- sg docker -c "
  docker cp /home/lucien.guest/iaa_prod_0004_$TS.dump iaa-restore-verify:/tmp/d.dump &&
  docker exec iaa-restore-verify psql -U postgres -c 'CREATE DATABASE iaa_restore_verify' &&
  docker exec iaa-restore-verify pg_restore -U postgres -d iaa_restore_verify --no-owner --exit-on-error /tmp/d.dump"
# 用 /tmp/iaa_audit/verify.sql 在两侧执行 → diff 必须为空
limactl shell dev -- sg docker -c "docker rm -f iaa-restore-verify"   # 验证后清理
```

判定标准：`pg_restore --exit-on-error` 返回 0，且两侧查询输出 `diff` 为空。

### 4.3 实际部署命令（获批后按序执行）

```bash
cd /Users/lucien/workspace/self-study/projects/industrial-agent-audit

# 0) 迁移前快照（只读）
conda run -n ms_pointcloud_midterm python scripts/migration_audit.py \
  --out /tmp/iaa_audit/prod_pre_deploy.json

# 1) 目标确认（只读，打印真实库名/账号，失败即中止）
scripts/migrate.sh target          # 期望 database=industrial_agent_audit

# 2) 迁移 0005 + 0006（Alembic 单事务，任一步失败整体回滚）
scripts/migrate.sh up

# 3) 版本确认
scripts/migrate.sh current         # 期望 0006_check_constraint_names

# 4) 派生字段修订：先只读预检，再写入（必须显式 --allow-production）
conda run -n ms_pointcloud_midterm python scripts/recompute_gap_samples.py
conda run -n ms_pointcloud_midterm python scripts/recompute_gap_samples.py \
  --apply --allow-production     # 期望：已更新 23 行 / 剩余差异 0 / 合计 327122

# 5) 部署后核验（只读）
conda run -n ms_pointcloud_midterm python scripts/migration_audit.py \
  --compare /tmp/iaa_audit/prod_pre_deploy.json --allow-missing-samples-change
conda run -n ms_pointcloud_midterm python scripts/verify_migration_0005_0006.py \
  --baseline /tmp/iaa_audit/prod_pre_deploy.json --expect-gap-diff 0
conda run -n ms_pointcloud_midterm python scripts/verify_phase1b.py --skip-explain
cd backend && conda run -n ms_pointcloud_midterm python -m alembic check   # 无漂移
```

> `scripts/migrate.sh up` 自带目标身份校验（声明库名 ≠ 实际库名即中止）、密码脱敏，
> 且回滚类子命令默认拒绝——不要用 `down-test`/`downgrade` 操作生产库。

### 4.4 预期结果

| 项目 | 预期 |
| --- | --- |
| 迁移后版本 | `0006_check_constraint_names` |
| Schema 变更 | +1 唯一约束（含索引）、+1 CHECK 约束重建（status 增加 partial）、9 条 CHECK 改名 |
| 数据变更 | **仅** `data_gaps.missing_samples` 的 23 行（每行 +1，合计 327,099 → 327,122） |
| `sensor_readings` | 行数与内容指纹**完全不变** |
| 总停机窗口 | 无需停机（当前无对外服务）；DDL 锁窗口为毫秒级，实测全流程 < 3 s |

### 4.5 失败恢复方案

1. **迁移失败**：Alembic 以单事务执行，失败自动回滚，库仍为 `0004`；
   用 `scripts/migrate.sh current` 确认版本未变，再读 Alembic 错误栈定位（预演已覆盖全部数据前置条件，
   失败只可能来自并发/权限/磁盘等环境因素）。
2. **修订失败**：`recompute_gap_samples.py` 内置自校验（行数/剩余差异/合计三项），任一不符即抛错回滚，
   不会留下“改了一半”的缺口值；失败后库内数值仍为旧口径，可安全重试。
3. **最坏情况的恢复**：**以备份恢复为准**（不把 downgrade 作为默认恢复手段）：

```bash
# 停止一切写入后，另建库恢复并核对，再决定切换（不在原库上做破坏性操作时的做法）
limactl shell dev -- sg docker -c \
  "docker exec iaa-postgres psql -U app_user -d postgres -c 'CREATE DATABASE iaa_restore_251010'"
limactl shell dev -- sg docker -c \
  "docker exec iaa-postgres pg_restore -U app_user -d iaa_restore_251010 --no-owner --exit-on-error /tmp/iaa_prod_0004_$TS.dump"
# 核对通过后，用 psql 的 ALTER DATABASE ... RENAME 做库级切换（需单独评审，勿在未评估时执行）
```

4. **downgrade 仅作最后手段**：副本实测 `0006 → 0004 → 0006` 可逆、数据不变，
   但 `ingest_runs` 的状态约束名在 downgrade 后会停留在 `ck_ingest_runs_status_valid`
   而非迁移前的双前缀名（纯命名差异，语义等价）。因此**恢复默认走备份**，downgrade 不作为默认路径。

### 4.6 审批点（必须）

- [ ] 用户确认已阅读本报告第 1–3 节，批准在生产库执行 4.3 的步骤 2 与步骤 4。
- [ ] 确认备份文件 sha256 与 4.1 一致，并已独立恢复验证。
- [ ] 确认执行窗口内无其它写入方（无外部服务、无并行导入）。
- [ ] 明确执行人，并约定执行后 30 分钟内复核 4.3 步骤 5 的输出。

**未获得上述批准前，本次不执行任何生产库写入、迁移或数据修订。**

---

## 5. 遗留问题

### 5.1 已解决

1. P1-A 数据源幂等身份（代码层 + 数据库层硬约束均已具备，硬约束待获批后在生产生效）。
2. P1-B 迁移脚本配置优先级与目标保护。
3. P2-4 `partial` 状态（代码 + 约束，待获批上线）。
4. P2-5 验证脚本只读化与写入保护。
5. P2-6 查询结果上限与分页。
6. P2-2 CHECK 约束命名规范（迁移 0006，副本验证通过）。
7. P2-8 覆盖率工具（92%）。
8. P2-9 README 改密说明。
9. **本次新增**：`--apply` 取整口径缺陷（D-1）、测试库名硬编码（D-2）。

### 5.2 待部署（阻塞 Phase 1B 正式关闭）

1. 生产库迁移 0005、0006（副本已全绿，等批准）。
2. 生产库 23 行 `missing_samples` 修订（副本已验证 327,099 → 327,122）。
3. 生产库部署后只读核验（4.3 步骤 5）。

生产库迁移前状态核验（只读，`--expected-version 0004_coverage_missing_time`）：**12/18 通过**，
6 项失败全部是“尚未迁移”的预期项——`uq_data_sources_content_sha256` 不存在、
status 约束不含 `partial`、10 条 CHECK 仍为双前缀名、`missing_samples` 仍有 23 行旧值。
这同时证明本次所有操作都没有改动生产库。

### 5.3 可暂缓

1. **P2-3 最小权限账号切换**（`app_user` 仍为超级用户）：已设计与隔离验证，需单独变更窗口。
2. 跨来源并发去重的极端竞态（依赖 `NOT EXISTS`，无唯一约束兜底）；当前为单进程批处理，风险低。
3. `app/db/session.py` 覆盖率 43%（引擎缓存/释放分支）。
4. 仓库尚无任何 Git 提交（`git log` 为空）：建议在 Phase 1B 关闭时做一次基线提交，便于后续回滚与审计。
5. 备份文件目前只落在本机 `~/iaa-backups/`：建议增加异地/对象存储副本（含 sha256 清单）。
6. `docs/phase1b_remediation.md` 中的历史指纹 `ed60f05f…` 无对应查询定义，建议后续废弃该数字或补上定义
   （本次已改用 `scripts/migration_audit.py` 的可复现口径）。

---

## 6. 最终结论

**CONDITIONAL PASS**

- 隔离环境（生产库备份的完整还原副本）已完成 0005 → 0006 迁移与 23 行修订，
  行数、内容指纹、外键关联、数据源、故障事件、缺口记录全部符合预期，
  110 项自动化测试与 56+59+22 项端到端核验全部通过，并已验证备份可独立恢复。
- **生产库本阶段未被写入**：版本仍为 `0004_coverage_missing_time`，数据未变（只读核验 PASS）。
- 因此 Phase 1B 的**代码与迁移方案已经收口**，但“生产已生效”这一条件尚未满足，
  按验收口径记为 **CONDITIONAL PASS**，而不是 PASS。

### 是否可正式关闭 Phase 1B 并进入 Phase 1C？

**可以在执行第 4.3 节获批部署并复核通过后立即关闭；在此之前不进入 Phase 1C。**

具体判定：

1. 若用户批准并执行 4.3（迁移 + 修订 + 核验），`current=0006`、行数 1,516,948、
   指纹 `f40c3ace…`、缺口合计 327,122、约束核验全绿 → 结论升级为 **PASS**，Phase 1B 正式关闭。
2. 若暂不批准，Phase 1B 保持 CONDITIONAL PASS 状态：**不建议**在此状态下开始 Phase 1C，
   因为 FastAPI 查询服务会直接依赖 `partial` 状态语义与规范化的约束名。
3. 本次**未**编写任何 FastAPI / RAG / Agent / 前端代码，未进入 Phase 1C。

---

## 附录 A：本次新增/修改文件

**新增**

| 文件 | 用途 |
| --- | --- |
| `scripts/migration_audit.py` | 只读快照与比对（行数/指纹/约束/索引/缺口差异），迁移前后与备份恢复均可复用 |
| `scripts/verify_migration_0005_0006.py` | 0005/0006 与缺口修订的核验（默认只读；写探针需显式开关且强制 ROLLBACK） |
| `docs/phase1b_final_acceptance.md` | 本报告 |

**修改**

| 文件 | 变更 |
| --- | --- |
| `scripts/recompute_gap_samples.py` | 修复 `--apply` 的 `round(double)` 口径缺陷（改 `CAST(:step AS numeric)`）+ 三项自校验 |
| `backend/tests/test_scripts.py` | 两处测试库名由硬编码改为从 `TEST_DATABASE_URL` 推导 |
| `backend/tests/test_limits_and_rounding.py` | 新增半值步长 `--apply` 回归测试（+1，共 110 项） |

**未修改**：`backend/app/**`（除已完成的 Phase 1B 修复外无改动）、`data/raw/**` 原始 CSV、
`docker-compose.yml`、任何 Alembic 迁移脚本（0005/0006 与副本验证过的版本一致）。

## 附录 B：本次产生的临时对象（均已清理或隔离）

| 对象 | 状态 |
| --- | --- |
| `~/iaa-backups/iaa_prod_0004_20261022_104059.dump` | **保留**（生产备份） |
| 容器 `iaa-restore-verify`（独立恢复验证） | 已删除 |
| 数据库 `iaa_rehearsal_test`（预演 + pytest，数据已被测试清空） | 保留至部署完成，随后可删除 |
| 数据库 `iaa_rehearsal2_test`（全量副本，0006 + 修订完成，18/18 核验通过） | 保留至部署完成，随后可删除 |
| 快照/日志 `/tmp/iaa_audit/*.json` | 保留（本次证据） |
| 生产库 `industrial_agent_audit` | **未被写入任何数据，版本仍为 0004** |
