# Phase 1B 缺陷修复与回归验证报告

- 修复日期：2026-10-22
- 依据：Phase 1B 独立验收（CONDITIONAL PASS）所列 2 项 P1 + 9 项 P2
- 范围：**只修复 Phase 1B 缺陷并验证**，未进入 Phase 1C，未开发 FastAPI/RAG/Agent/前端
- 结论：**代码层修复完成并在隔离库验证通过；生产库 Schema 变更（迁移 0005/0006）与
  23 行派生字段修订尚未执行，等待批准**

---

## 0. 状态总览

| 编号 | 问题 | 状态 | 证据 |
| --- | --- | --- | --- |
| P1-A | 数据源幂等身份包含文件名 | **FIXED（代码+隔离库）** / 生产待迁移 0005 | 场景 A–F 全部通过；内容 md5 与生产一致 |
| P1-B | 迁移脚本覆盖调用方环境变量 | **FIXED** | 14 项脚本测试；显式目标/默认拒绝/脱敏 |
| P2-4 | 导入失败后的部分成功状态 | **FIXED（代码+隔离库）** / 生产待迁移 0005 | `status=partial` + 结构化 notes + 恢复测试 |
| P2-5 | 验证脚本可能写生产库 | **FIXED** | 默认只读事务；写入型需 `--write-tests` + `_test` 库 |
| P2-6 | 查询缺少结果上限 | **FIXED** | 默认 1,000 / 上限 5,000 + offset；17 项测试 |
| P2-1 | `missing_samples` 取整口径不一致 | **PARTIAL** | 代码与新数据 FIXED；生产 23 行历史值待修订 |
| P2-2 | CHECK 约束名双前缀 | **FIXED（迁移 0006，隔离库）** / 生产待迁移 | 约束名与 ORM 元数据完全一致 |
| P2-3 | 运行账号为超级用户 | **DEFERRED** | 三账号方案 + SQL + 隔离库权限验证通过；生产未改 |
| P2-7 | E1 冻结行数 90 vs 91 | **FIXED（文档）** | 边界语义写入数据库设计；未改数据 |
| P2-8 | 无覆盖率工具 | **FIXED** | pytest-cov；总覆盖率 92% |
| P2-9 | README 密码说明 | **FIXED** | 新增「修改数据库密码」章节 |

**生产库在本次修复期间未被写入、未被迁移，版本仍为 `0004_coverage_missing_time`。**

---

## 1. 生产库只读基线（修复前 / 修复后一致）

| 指标 | 修复前 | 修复后 |
| --- | --- | --- |
| 数据库 | `industrial_agent_audit` | 同 |
| Alembic 版本 | `0004_coverage_missing_time` | **`0004_coverage_missing_time`**（未升级） |
| `sensor_readings` | 1,516,948 | 1,516,948 |
| `data_sources` | 2 | 2 |
| `fault_events` | 4 | 4 |
| `data_gaps` | 332 | 332 |
| `equipment` | 1 | 1 |
| `ingest_runs` | 10 | 10 |
| 冻结候选行 | 50,870 | 50,870 |
| 时间范围 | 2020-02-01 00:00:00 ~ 2020-09-01 03:59:50 | 同 |
| 内容指纹（17 字段 md5） | `ed60f05f41270865196ae5bb8727f785` | **同** |

结论：本次修复只新增了隔离库验证与代码/文档变更，生产数据未发生任何变更。

---

## 2. P1-A 数据源幂等性

### 2.1 缺陷与根因

唯一键是 `(source_id, source_row_number)`，而 `source_id` 由 `data_sources`
的 `UNIQUE(source_file, file_sha256)` 决定 —— **文件名进入了身份**。
字节完全相同的文件改名后会被登记为第二个来源，导致同一批观测重复入库：
独立验收实测 10,000 行 → 20,000 行，而 distinct 时间戳仍为 10,000。

### 2.2 修复方案

| 层 | 措施 | 位置 |
| --- | --- | --- |
| 身份定义 | 数据源解析改为**按 `file_sha256` 查找**，命中即复用；文件名只作元数据 | `register_data_source()` |
| 数据库硬保证 | `UNIQUE(file_sha256)` 约束（`uq_data_sources_content_sha256`） | 迁移 `0005_source_hash_identity` |
| 并发兜底 | `INSERT ... ON CONFLICT DO NOTHING` + 回查（唯一来源 id） | 同上 |
| 跨来源重复观测 | 插入时 `WHERE NOT EXISTS (同设备同 observed_at)`，任何来源都不得重复同一时刻的观测 | `_INSERT_GUARDED_SQL` |
| 取值冲突 | 同一时刻取值不同 → 默认 `ImportConflictError` 中止，**不覆盖**；`skip` 策略下跳过并标记 `partial` | `_OVERLAP_COUNT_SQL` / `overlap_policy` |
| 可追溯 | `source_reused` / `source_file_mismatch` / `overlap_skipped` / `timestamp_conflicts` / `db_hash_unique_enforced` 写入 `ingest_runs.notes` 与 CLI 输出 | `_stats_notes()` |

设计取舍：

- **为什么不用 `UNIQUE(equipment_id, observed_at)`**：Phase 0/1A 明确决定时间戳不做唯一键
  （时钟抖动可能导致同一秒两条采样）。本修复用“插入时 NOT EXISTS + 显式冲突报告”实现跨来源
  去重，既达到目的，又不改变已确认的时间语义；并发窗口内的极端竞态由文档标注限制。
- **为什么保留 `UNIQUE(source_file, file_sha256)`**：历史约束，无害，避免额外 ALTER 风险。
- **未升级 0005 的库**：`SELECT ... WHERE file_sha256` 仍会复用原来源（不会重复观测），
  只是缺少“并发重复登记”的硬约束，并在 notes 中记录 `db_hash_unique_enforced=false`。

### 2.3 验证结果（隔离库，迁移 0001→0006）

| 场景 | 操作 | 实测结果 |
| --- | --- | --- |
| A | 首次导入 10,000 行 | `inserted=10,000`，`data_sources`=1 |
| B | 再次导入同一文件 | `inserted=0`，`conflict=10,000`，行数仍 10,000 |
| C | 字节相同、文件名不同 | `inserted=0`，`source_reused=true`，`source_file_mismatch=true`，**`data_sources` 仍为 1** |
| D | 先 5,000 再 10,000 | 两次共 10,000 行，`overlap_skipped=5,000`，无重复时间戳 |
| E1 | 哈希不同、时间重叠、取值相同 | `inserted=0`，`overlap_skipped=10,000`，无冲突、行数不变 |
| E2 | 哈希不同、时间重叠、取值不同 | 抛出 `ImportConflictError`，**既有 10,000 行逐行未被修改**，运行记录 `failed` + `timestamp_conflicts=100` |
| E3 | 同 E2 但 `overlap_policy=skip` | `inserted=0`、`overlap_skipped=9,900`、`timestamp_conflicts=100`、`status=partial`、行数不变 |
| F | 两线程并发导入同一文件 | 0 异常；`inserted` 分别为 10,000 / 0；`data_sources`=1；行数 10,000 |
| G | 不同时间段的新文件 | 正常导入 1,000 行，无重叠 |

**全量等价性验证（关键）**：在全新隔离库用修复后的导入器全量导入 1,516,948 行后，
`md5(string_agg(<17 字段>))` = `ed60f05f41270865196ae5bb8727f785`，与生产库**完全一致**，
说明修复没有改变任何观测值、时间戳或质量标记。

---

## 3. P1-B 配置优先级与迁移目标保护

### 3.1 缺陷与根因

`db.sh` / `migrate.sh` 使用 `set -a; source .env`，会无条件覆盖调用方显式设置的
`DATABASE_URL` / `TEST_DATABASE_URL`。独立验收实测：把 `DATABASE_URL` 指向不存在的库，
`migrate.sh current` 仍然操作 `.env` 里的生产库。

### 3.2 修复方案

| 措施 | 实现 |
| --- | --- |
| 优先级 | 命令行 `--url` > 调用方环境变量 > `.env`（只填空值）> 脚本内置默认（无数据库默认值） |
| 只填不覆盖 | 新增 `scripts/lib/env.sh::load_env_defaults()`，仅当变量为空时 `export` |
| 目标身份校验 | 新增 `scripts/db_target.py`：真实连接后返回 `current_database()`，与连接串声明不一致即失败 |
| 连接失败即中止 | 退出码 3，明确不尝试其它库 |
| 危险操作默认拒绝 | 回滚类命令需 `ALLOW_TEST_ROLLBACK=1` **且实际连接的库名以 `_test` 结尾**（退出码 5） |
| 不泄露密码 | 所有输出经 `mask_url()`；`db_target.py` 只输出脱敏 URL |
| 容器配置一致 | `db.sh` 生成 0600 临时 compose env 文件（退出即删，已加入 `.gitignore`），使调用方变量真正生效 |

### 3.3 验证结果

| 测试 | 结果 |
| --- | --- |
| `.env` 只填未设置变量 | PASS（`AUDIT_FOO` 保留进程值，`AUDIT_BAR` 取文件值） |
| 仅 `.env` 时可用 | PASS（`target` 报告 `.env` 配置的库） |
| 显式 `DATABASE_URL` 生效 | PASS（报告 `database=industrial_agent_audit_test`） |
| 不存在的库 | PASS（退出码 3，提示库不存在，不回退） |
| 回滚未显式允许 | PASS（退出码 5，默认拒绝） |
| 回滚指向生产库 | PASS（退出码 5，按**实际库名**拒绝；生产库版本前后均为 0004） |
| 输出不含密码 | PASS（密码字符串不出现在 stdout/stderr，仅出现 `***`） |
| 正常迁移工作流 | PASS（`up-test` / `down-test` / `current` / `check` / `heads` 均正常） |

这些行为由 `backend/tests/test_scripts.py`（14 项）自动回归。

---

## 4. P2-4 部分成功状态与失败恢复

- `ingest_runs.status` 取值扩展为 `running / succeeded / failed / partial / dry_run`（迁移 0005）。
- 语义：
  - `succeeded`：无异常、无冲突、无跳过冲突行；
  - `partial`：已提交部分数据后失败，或按策略跳过了冲突行（数据不完整）；
  - `failed`：未提交任何行即失败（例如首个 chunk 精度校验失败）。
- `ingest_runs.notes` 写入结构化信息：
  `source_reused=…; overlap_skipped=…; timestamp_conflicts=…; db_hash_unique_enforced=…; committed_rows=…; partial_commit=true; error=…`
- 未升级 0005 的库：`_supported_status()` 检测 CHECK 定义，自动回退为 `failed` 并在 notes 前置
  `status_requested=partial`，不会因等待迁移而报错。
- **不把部分提交描述为完全回滚**：导入报告、CLI 输出（`[部分成功]`）与本文档均如实说明。

验证：

| 场景 | 结果 |
| --- | --- |
| 第 2 个 chunk 精度违规（第 15,000 行） | `status=partial`，`attempted=10,000`、`inserted=10,000`、`partial_commit=true` |
| 第 1 个 chunk 即失败 | `status=failed`，`inserted=0`，表内 0 行 |
| 失败后重跑同一文件 | 0 新增、1,516,948 冲突（幂等收敛） |
| 失败后改用“修正版新文件” | 不再产生重复：相同时间戳被跳过，取值变化的行报冲突 |

---

## 5. P2-5 验证脚本只读化

- 默认模式：`SET TRANSACTION READ ONLY` 开启只读事务，**数据库层面**禁止写入
  `sensor_readings` / `ingest_runs` 等业务表；任何写入都会直接报错。
- 写入型验证（重复导入检查）移到 `--write-tests`，并强制：
  - 目标库名以 `_test` 结尾（以真实连接的库名为准）；
  - 否则以退出码 1 拒绝。
- 只读模式下额外验证“`ingest_runs` 数量前后不变”。

实测：

| 场景 | 结果 |
| --- | --- |
| 生产库默认只读运行 | 56 项检查全部通过；`ingest_runs` 10 → 10（无写入） |
| 生产库 `--write-tests` | 拒绝执行（提示目标非 `_test`）；生产库无任何变化 |
| 全量隔离库只读 | 56 项通过，`ingest_runs` 无变化 |
| 全量隔离库 `--write-tests` | 59 项通过，`ingest_runs` +1，观测行数不变 |

同时修复了脚本在“非完整数据集库”上抛 `IndexError` 的健壮性问题：现在会明确提示
“本脚本只对完整导入的库有效”并以退出码 1 结束。

---

## 6. P2-6 查询结果上限与分页

`backend/app/services/queries.py`：

| 接口 | 默认 | 上限 | 分页 |
| --- | --- | --- | --- |
| `readings_between` | 1,000 | 5,000 | `offset`，按 `(observed_at, id)` 稳定排序 |
| `frozen_candidates` | 1,000 | 5,000 | 同上 |
| `list_fault_events` / `list_gaps` / `list_equipment` / `frozen_segments` | 100 | 1,000 | `offset` |
| `analog_statistics` / `coverage` / `count_readings` | 不受限（聚合/计数） | — | — |

- 非法 `limit`（0、负数、超上限、非整数）与非法 `offset`（负数、非整数）直接 `ValueError`，
  不静默截断。
- 索引与查询方式未改变（仍走 `ix_sensor_readings_equipment_observed_at`）。
- 验证脚本已改为按上限分页取回，顺带验证分页本身。

---

## 7. P2-1 缺失样本取整口径

- 统一公式（写入 `app/models/quality.py`）：

  ```
  missing_samples = round_half_up(gap_seconds / 10) - 1   （最小 0）
  ```

- 明确：这是**基于名义采样周期 10 s 的估计值**，不是真实缺失次数的观测；
  时钟抖动（±1–3 s）不计缺口，只有 Δ ≥ 30 s 才记为缺口。
- 与 PostgreSQL `round(numeric)`（half-up）完全一致；旧实现使用 Python 内置 `round()`
  （half-to-even），在 23 个 Δ 恰为 x.5 步长的缺口上少算 1。
- 新导入数据自动使用新口径：全量隔离库实测 `sum(missing_samples)=327,122`。
- 生产库现有 332 行为旧口径（`327,099`），**未修改**。已提供
  `scripts/recompute_gap_samples.py`：
  - 默认只读，报告差异（生产：23 行，327,099 → 327,122）；
  - `--apply` 才写，且默认只允许 `*_test` 库；
  - 只更新 `missing_samples` 派生字段，不触碰原始观测、不删除行。

---

## 8. P2-2 CHECK 约束名

- 新增迁移 `0006_check_constraint_names`：把 9 条双前缀（其中 2 条因 63 字符被截断）
  的 CHECK 约束重命名为 ORM 元数据中的名称；`0005` 新建的状态约束也统一为
  `ck_ingest_runs_status_valid`。
- 只做 `ALTER TABLE ... RENAME CONSTRAINT`，不改变约束定义、不触碰数据。
- 隔离库验证：ORM 元数据约束集合 ⊂ 数据库约束集合（唯一额外项是 `alembic_version_pkc`），
  且不存在任何双前缀 `ck_` 名称。新增回归测试 `test_constraint_names_match_orm_metadata`。
- 该迁移**尚未应用到生产库**。

---

## 9. P2-3 数据库账号权限（DEFERRED）

- 设计：迁移账号（现状 `app_user`）/ `iaa_app`（只读）/ `iaa_importer`（可写无 DDL），
  脚本见 `deploy/roles/01-roles.sql`，说明见 `deploy/roles/README.md`。
- 隔离库（`iaa_fix_iso`）实测 13 项权限边界：

| 探测 | iaa_app | iaa_importer |
| --- | --- | --- |
| SELECT / 覆盖度函数 / 冻结段视图 | ✅ | ✅ |
| INSERT / UPDATE / TRUNCATE | ❌ 拒绝 | ✅（DML 允许） |
| CREATE TABLE / CREATE EXTENSION | ❌ 拒绝 | ❌ 拒绝 |
| 超级用户 | ❌ `rolsuper=false` | ❌ |

- 验证后已清理测试角色，集群仅剩 `app_user`；**生产权限未做任何变更**。
- 切换生产账号需要单独变更窗口（修改 `.env` 与连接串、重启服务），故标记 DEFERRED。

---

## 10. P2-7 E1 冻结行数边界（文档化）

- Phase 1A 的 90 行是在窗口内重算 `frozen`：窗口第一行没有窗口内前驱，因此不计数。
- 数据库标记是**全局时序判定**：E1 首行（2020-04-18 00:00:01）与窗口外的真实前驱
  （2020-04-17 23:59:49）7 路模拟量逐位相同，因此被正确标记，合计 91 行。
- 结论：数据库实现更严谨，差异属口径而非数据错误；**未修改任何原始数据**。
  该说明已写入 [数据库设计](database_design.md) 的质量标记章节。

---

## 11. P2-8 测试覆盖率

- 安装 `pytest-cov`，命令：`cd backend && python -m pytest --cov=app --cov-report=term-missing`。
- 实测：**109 项测试全部通过，总覆盖率 92%**。

| 模块 | 覆盖率 |
| --- | --- |
| `app/services/metropt_import.py` | 94% |
| `app/services/queries.py` | 88% |
| `app/models/*`（含 quality） | 100%（quality 91%） |
| `app/core/config.py` | 93% |
| `app/db/session.py` | 43%（引擎缓存/释放等非关键分支） |

---

## 12. P2-9 README 密码说明

新增「修改数据库密码」章节：说明 `.env` 只是连接凭据副本，已有持久化 Volume 时
必须先用 `ALTER ROLE ... PASSWORD` 修改数据库内密码，再同步 `.env`；明确禁止以
“删除 Volume”作为常规改密手段。

---

## 13. 变更文件清单

**新增**

| 文件 | 用途 |
| --- | --- |
| `scripts/lib/env.sh` | `.env` 只填不覆盖 + URL 脱敏 |
| `scripts/db_target.py` | 只读校验真实数据库目标（库名/账号） |
| `scripts/recompute_gap_samples.py` | `missing_samples` 安全修订（默认只读） |
| `backend/alembic/versions/0005_source_hash_identity.py` | UNIQUE(file_sha256) + status 支持 partial |
| `backend/alembic/versions/0006_check_constraint_names.py` | CHECK 约束名规范化 |
| `backend/tests/test_identity.py` | P1-A 场景 A–F + P2-4 回归（13 项） |
| `backend/tests/test_scripts.py` | P1-B / P2-5 脚本行为（14 项） |
| `backend/tests/test_limits_and_rounding.py` | P2-6 / P2-1（17 项） |
| `deploy/roles/01-roles.sql`、`deploy/roles/README.md` | 最小权限账号设计（DEFERRED） |
| `docs/phase1b_remediation.md` | 本报告 |

**修改**

| 文件 | 变更 |
| --- | --- |
| `backend/app/services/metropt_import.py` | 哈希身份、重叠保护、冲突策略、partial 状态、统一取整 |
| `backend/app/services/queries.py` | 结果上限与分页 |
| `backend/app/models/data_source.py` | `UNIQUE(file_sha256)` |
| `backend/app/models/ingest.py` | status 增加 `partial` |
| `backend/app/models/quality.py` | `estimate_missing_samples()`（half-up） |
| `scripts/migrate.sh` | 配置优先级、目标校验、默认拒绝、脱敏 |
| `scripts/db.sh` | 配置优先级、显式传递 compose 变量、`config` 子命令 |
| `scripts/verify_phase1b.py` | 只读事务、`--write-tests` 保护、分页、健壮性 |
| `scripts/import_metropt.py` | `--overlap-policy`、新统计字段、来源复用提示 |
| `backend/tests/test_db_connection.py` | 约束命名 / partial 状态回归（+2） |
| `backend/requirements.txt`、`backend/pytest.ini` | pytest-cov |
| `docs/database_design.md`、`README.md`、`docs/roadmap.md`、`docs/data_sources.md` | 语义与运维说明 |
| `.gitignore` | `.env.compose.*` |

---

## 14. 测试结果

```
cd backend && python -m pytest --cov=app --cov-report=term-missing
109 passed, 0 failed, 0 skipped  (10.0s)  total coverage 92%
```

| 测试文件 | 数量 | 覆盖点 |
| --- | --- | --- |
| `test_config.py` | 4 | 配置缺失/脱敏/优先级 |
| `test_db_connection.py` | 9 | 连接、扩展、索引、约束命名、partial 状态、迁移无漂移 |
| `test_csv_parsing.py` | 14 | 表头、时间戳、精度、数字量、行号、冻结/缺口规则 |
| `test_import.py` | 8 | 导入、日志、幂等、试运行、`max_rows`、逐值精度 |
| `test_quality.py` | 7 | 冻结标记、缺口、视图、覆盖度双口径 |
| `test_queries.py` | 7 | 设备、时间窗、统计、冻结、缺口 |
| `test_fault_events.py` | 7 | 4 条官方事件、编号重复、疑笔误、幂等、无逐行标签 |
| `test_consistency.py` | 9 | 2 万行真实样本独立交叉核对 |
| `test_scripts.py` | 14 | 配置优先级、目标保护、脱敏、语法 |
| `test_identity.py` | 13 | 场景 A–F、部分成功、来源复用 notes |
| `test_limits_and_rounding.py` | 17 | 分页/上限、half-up 与 PG 一致性、修订脚本 |

**全量隔离库端到端**（`iaa_full_e2e_test`，真实 1,516,948 行，验证后已删除）：

| 步骤 | 结果 |
| --- | --- |
| 空库迁移到 head | 成功（0001→0006） |
| 全量导入 | 1,516,948 行，25.0 s，冻结 50,870，缺口 332 |
| 重复导入 | `inserted=0`、`conflict=1,516,948` |
| 内容指纹 | `ed60f05f…`，**与生产库一致** |
| 只读验证 | 56/56 通过，`ingest_runs` 无变化 |
| 写入型验证 | 59/59 通过，`ingest_runs` +1，行数不变 |
| 缺口修订（dry-run） | 0 行需修订（新口径已生效） |
| 清理 | 数据库已删除 |

---

## 15. 数据安全验证

| 检查 | 结果 |
| --- | --- |
| 生产库行数 | 1,516,948（未变） |
| 生产库内容指纹 | `ed60f05f41270865196ae5bb8727f785`（未变） |
| 生产库版本 | `0004_coverage_missing_time`（未升级） |
| 故障事件 / 缺口 / 设备 / 来源 | 4 / 332 / 1 / 2（未变） |
| 原始 CSV | 只读访问，哈希与 manifest 一致，未修改 |
| 生产库写入 | 无 INSERT/UPDATE/DELETE/TRUNCATE/DDL |
| 设备/来源关联完整性 | 孤儿观测 0（设备）、0（来源）；全部 1,516,948 行归属 `metropt3-apu-01`；缺口与故障事件无孤立外键 |
| `alembic check`（生产库） | `Target database is not up to date.`（0004 vs head 0006，属预期，说明迁移确未应用） |
| `alembic check`（测试库，已到 0006） | `No new upgrade operations detected.` |
| 测试隔离 | 所有写入测试使用 `*_test` 临时库；验证后删除 |
| 集群角色 | 验证用角色已清理，仅剩 `app_user` |

---

## 16. 尚待执行（需明确批准）

| 事项 | 影响 | 风险 | 建议 |
| --- | --- | --- | --- |
| **应用迁移 0005**（`UNIQUE(file_sha256)`、`status` 支持 `partial`） | 生产库 Schema | 低：先做重复哈希检查，重复即中止；仅新增约束/放宽取值域 | 在维护窗口执行 `scripts/migrate.sh up`，随后 `alembic check` |
| **应用迁移 0006**（CHECK 约束改名） | 生产库 Schema | 低：仅重命名，不改定义 | 与 0005 同批 |
| **修订 23 行 `missing_samples`** | 派生统计字段 | 低：只改估计值，不触碰观测 | `recompute_gap_samples.py --apply --allow-production`（先 dry-run） |
| **切换最小权限账号** | 连接配置与权限 | 中：需改 `.env`、重启服务、验证权限 | 单独变更窗口，先演练回滚 |
| 生产库上运行 `--write-tests` 验证 | 无（已被保护拒绝） | — | 保持现状：写入型验证只在隔离库执行 |

在上述迁移被批准并执行前，生产库处于 **0004**：P1-A 的**代码层**修复已生效
（按哈希复用来源、跨来源去重、冲突报告），但**数据库层硬约束**要等 0005。

---

## 17. 遗留问题

1. **并发极端竞态**：跨来源去重依赖 `NOT EXISTS` 判断，两个事务同时插入同一时间戳在理论上
   仍可能都成功（PostgreSQL READ COMMITTED 下无唯一约束兜底）。本阶段未引入
   `UNIQUE(equipment_id, observed_at)`（会与既有时间语义冲突），仅文档标注；
   实际导入为单进程批处理，风险低。
2. `app/db/session.py` 覆盖率 43%，引擎缓存/释放分支缺少专门测试。
3. 生产库历史缺口值（23 行）与新口径不一致，待批准后修订。
4. `verify_phase1b.py` 依赖完整数据集（1,516,948 行）；对部分导入库会明确中止，
   这是设计选择，不是缺陷。
5. 角色切换（P2-3）只完成设计与隔离验证，生产仍用超级用户。

---

## 18. Phase 1B 是否达到正式验收要求

- 代码层：**是**。两项 P1 与主要 P2 已修复，并有 109 项自动化测试（覆盖率 92%）
  与全量隔离库端到端证据（内容指纹与生产库一致）。
- 生产库层：**仍有 2 个迁移（0005/0006）与 23 行派生字段修订未执行**，因此
  P1-A 的数据库硬约束、P2-2 的命名规范、P2-1 的历史数据一致性在生产环境
  **尚未生效**。按要求标记为“隔离环境验证通过、生产待批准执行”。
- Phase 1C：**未启动**，本次未开发任何 FastAPI / RAG / Agent / 前端代码。
