# 基于 RAG 与 Agent 行为审计的工业设备智能运维系统

英文名称：Industrial Equipment Intelligent Maintenance & Agent Audit System

## 项目背景与目标

工业设备运维需要结合传感器记录、技术资料和维修经验。项目计划以工业数据查询与知识检索为基础，构建具有工具调用能力的运维助手，并对其可观察行为进行追踪与审计。

项目面向 AI 应用开发工程师岗位，展示从数据治理、知识检索、Agent 编排到审计评估、接口集成和测试的完整开发流程。初期定位为离线历史数据分析与辅助建议系统，不直接控制真实设备。

## 核心功能

| 能力 | 状态 |
| --- | --- |
| 工业设备运行数据管理：按设备、时间窗口查询传感器与故障记录 | ✅ 阶段 1B/1C |
| 工业知识 RAG：解析 PDF、混合检索、带页码与 Chunk ID 的证据回答 | ✅ 阶段 2 |
| 检索与问答评估：52 题评估集、Recall@5 / MRR@10、人工复核 | ✅ 阶段 2 |
| Agent 评估：51 题开发集 + 11 题留出集、工具选择/数值/引用/耗时指标 | ✅ 阶段 3 |
| 智能运维 Agent（LangGraph 单 Agent + 6 个只读工具、证据整合、执行 Trace） | ✅ 阶段 3 |
| Agent-Probe 行为审计（轨迹、规则、LLM 评审） | ⬜ 阶段 4（未开始） |
| 可视化（设备趋势、引用、轨迹、审计面板） | ⬜ 阶段 5（未开始） |

## 技术栈规划

| 领域 | 计划选型 |
| --- | --- |
| 后端 | Python 3.11、FastAPI、SQLAlchemy 2.x、Pydantic |
| 存储 | PostgreSQL、pgvector |
| AI 应用 | Embedding（`bge-m3`，1024 维）、RAG、混合检索；LangGraph 1.2.2（单 Agent + 多工具） |
| 审计 | Agent Trace、确定性规则、LLM-as-a-Judge、审计评估 |
| 前端 | React、TypeScript、Tailwind CSS |
| 工程 | Docker Compose、pytest、Git、Alembic |

已实测版本：PostgreSQL 17.11 + pgvector 0.8.7 + pg_trgm 1.6、SQLAlchemy 2.1.6、Alembic 1.20.0、
psycopg 3.3.4、pytest 9.1.1、pandas 3.0.3（Python 3.11.15，Conda 环境 `ms_pointcloud_midterm`）。
阶段 2 新增：PyMuPDF 1.28.0、pgvector（Python）0.4.2；Embedding `bge-m3`（1024 维）与
`qwen2.5:7b-instruct` 通过本机 Ollama 调用，无需外部 API Key。
阶段 3 新增：`langgraph 1.2.2`（conda-forge 安装，未升级既有依赖）。
依赖清单见 `backend/requirements.txt`。

## 项目目录说明

| 路径 | 职责 |
| --- | --- |
| `backend/app/api/` | HTTP 接口适配 |
| `backend/app/core/` | 配置、日志与公共基础约定 |
| `backend/app/db/` | 数据库引擎、会话管理与连接自检 |
| `backend/alembic/` | Alembic 迁移环境与版本脚本（表结构唯一来源） |
| `backend/app/models/` | ORM 持久化模型与数据质量标记语义 |
| `backend/app/schemas/` | 输入输出契约 |
| `backend/app/services/` | 数据导入、来源登记、只读查询服务 |
| `backend/app/rag/` | 知识库：PDF 解析、切分、Embedding、混合检索、RAG 回答 |
| `backend/app/agent/` | LangGraph 工作流、意图/规划、只读工具、证据整合与执行 Trace |
| `backend/app/audit/` | 轨迹采集契约、规则审计与模型评审 |
| `backend/tests/` | 后续单元、集成与评估测试 |
| `frontend/` | 运行与审计可视化 |
| `docker-compose.yml` | PostgreSQL + pgvector 单服务编排（命名卷、健康检查） |
| `data/raw/`、`data/processed/`、`data/samples/` | 原始数据、派生数据与可分享小样本 |
| `scripts/` | 数据获取/体检/分析（1A）、导入与验证（1B）、`db.sh` 与 `migrate.sh` |
| `deploy/initdb/` | 容器首次启动时启用 pgvector 扩展并创建独立测试库 |
| `knowledge/` | `sources.json` 来源清单、`raw/` 文档（不进 Git）、`eval/` 评估集与结果 |
| `configs/` | 非敏感配置 |
| `docs/` | 架构、数据来源、数据探索、数据库、阶段规划和决策记录 |

## 开发阶段

0. 项目初始化与架构规划。
1. 工业数据导入与数据库建设。
2. 工业知识文档 RAG 检索。
3. 工业设备智能运维 Agent。
4. Agent-Probe 行为审计模块。
5. 系统集成、测试与可视化。

具体交付物与验收标准见 [开发路线图](docs/roadmap.md)。

## 当前开发状态

已完成阶段 0（项目骨架与文档）、阶段 1A（MetroPT-3 真实数据获取与探索分析）、
阶段 1B（PostgreSQL 数据建模、部署与全量导入）。

阶段 1B 收口：缺陷修复与迁移方案已在隔离副本验证通过（110 项测试 / 覆盖率 92%），
生产库迁移 0005、0006 与 23 行派生字段修订**等待批准后执行**，
详见 [Phase 1B 最终收口验收报告](docs/phase1b_final_acceptance.md)。

- 数据：官方 CSV 1,516,948 行已全部入库，`source_row_number` 保留原始步长 10；
  与 CSV 独立重算的结果逐项一致（行数、时间边界、7 路模拟量 min/max/avg、8 路数字量取 1 行数）。
- 质量：冻结候选 50,870 行 / 24 段（只标记不删除），采样缺口 332 处写入 `data_gaps`；
  覆盖度函数同时返回行口径与时间口径。
- 故障：4 条官方 Air leak 区间入库，PDF 编号重复与 `30 Apr` 疑似笔误按原文保留并标注核验状态。
- 工程：Docker Compose 一键起库（PostgreSQL 17 + pgvector 0.8.7），Alembic 迁移 4 个版本，
  `pytest` 63 项通过，重复导入新增 0 行。
- 缺陷修复（2026-10-22）：独立验收提出的 2 项 P1 + 9 项 P2 已修复并在隔离库回归通过
  （109 项测试、覆盖率 92%）。**迁移 0005/0006 与 23 行派生字段修订尚未应用到生产库**，
  详见 [Phase 1B 修复报告](docs/phase1b_remediation.md)。

阶段 1C 收口后的实际状态：FastAPI 只读查询服务已完成（8 个接口、193 项测试、覆盖率 96%）。

当前整体状态：阶段 0、1A、1B（含缺陷修复）、1C、2 已完成开发与自测，**阶段 3 已完成开发、测试与评估**；
阶段 4（Agent-Probe）与阶段 5（前端集成）未开始。**阶段 2 未做独立验收**，不得视为已正式验收通过。

## 阶段 2 已交付（2026-10-22）

- **知识库**：6 份来源明确的文档（131 页）成功入库，全部登记 URL、SHA-256、许可、适用范围；
  其中 2 份直接关联 MetroPT-3，4 份为 DOE 通用压缩空气资料（只能作原理参考）。
- **解析与切分**：PyMuPDF 按页提取并保留页码/标题/段落；重复页眉页脚剔除；无 OCR、不补写内容。
  结构感知与固定长度两套切分并存（`structural-v1` 750 块、`fixed-v1` 564 块），用于实测对比。
- **向量**：本地 Ollama `bge-m3`，实测 1024 维，1,314 个真实向量；HNSW + GIN 索引。
- **检索**：向量 + 关键词（ASCII 标识符 + 中文 bigram + tsvector）+ RRF 混合；
  41 道可回答题实测 **Recall@5 0.939 / MRR@10 0.784**（混合，结构感知切分），
  优于纯向量（0.878 / 0.685）与纯关键词（0.561 / 0.486）。
- **问答**：有证据才回答；引用包含文档、页码、Chunk ID 与逐字引文；
  11 道必拒答题 11/11 正确拒答；型号不匹配（GA30/GA37/ASD 40）确定性拒答；
  文档内容按不可信数据处理，模型输出代码/SQL 会被拒绝返回。
- **接口**：`/api/v1/knowledge/{search,ask,documents,health}`，全部只读。
- **测试**：267 项通过（阶段 2 新增 81 项），1 项因未配置全量数据库副本而跳过；
  原有 186 项无回归；整体覆盖率 91%。
- **数据库**：迁移 `0007_knowledge_rag` 只在隔离库（`iaa_rag_dev` / `*_test`）执行；
  **生产库仍为 `0004`，1,516,948 行工业数据未改动**。

阶段 2 详情：[RAG 设计](docs/rag_design.md) · [知识库来源](docs/knowledge_sources.md) ·
[RAG 评估](docs/rag_evaluation.md) · [Phase 2 报告](docs/phase2_report.md)

## 阶段 3 已交付（2026-10-24）

- **Agent 形态**：LangGraph `StateGraph`，**单 Agent + 6 个只读工具**；节点
  `analyze → plan → execute → check → compose → finalize`，三处条件分支（意图分流 / 是否继续查询 / 是否结束）。
- **工具**：`get_equipment_info`、`get_sensor_history`、`get_sensor_statistics`、`get_fault_events`、
  `get_data_quality`（复用阶段 1C Service）、`search_technical_knowledge`（复用阶段 2 混合检索，
  只取原始片段，不调用 RAG 生成）。
- **工具选择**：默认确定性规则（`AGENT_PLANNER=rule`）；9 题对比中规则计划完全匹配 1.000、
  LLM 计划 0.778，因此默认不用模型规划，`llm` 仅保留为对比实验接口。
- **证据与回答**：数据事实编号 `D1…`（含工具/Service/表/时间窗来源），知识片段编号 `K1…`
  （含文档、`doc_key`、Chunk ID、页码、适用范围、逐字摘录）；数据小节由事实清单确定性渲染，
  模型只写结论句/知识要点/推断；引用必须逐字命中该 Chunk，否则丢弃并记录原因；
  数值必须命中事实清单（含知识原文与用户问题中的数字），否则降级为确定性渲染。
- **预算与恢复**：最大工具调用 8 次、最大决策轮次 5 次、节点步数 24、整轮超时 180 s，
  每个工具有独立超时；错误按 `invalid_input / not_found / empty / timeout / transient /
  config_error / budget / security / internal` 分类，只有 `timeout`/`transient` 才重试。
- **只读保证**：Agent 的数据库会话在连接层 `default_transaction_read_only=on`；
  未新增迁移，主库仍为 `0004`，知识库仍为 `0007`。
- **Trace**：`run_start → intent → plan → tool_call/tool_result → decision → answer → run_end`，
  进程内有界存储 + `data/processed/agent/traces/*.jsonl`；不含连接串/密码/API Key。
- **接口**：`POST /api/v1/agent/chat`、`GET /api/v1/agent/traces/{trace_id}`（可选 `include_events=true`）。
- **测试**：`420 passed / 4 skipped`，整体覆盖率 91%；`app/agent` 各模块 72%–100%（最低为可选 LLM 规划器）；
  原有 267 项无回归；真实模型 + 真实 RAG + 真实数据的端到端测试 3 项通过（约 18 s，
  需 `IAA_AGENT_E2E=1`）。
- **评估**：开发集 51 题 —— 工具完全匹配 1.000、状态匹配 1.000、数值 23/23、引用逐字 1.000、
  无依据结论 0、平均 1.78 次工具调用 / 3.87 s；留出集 11 题（未调参）—— 工具完全匹配 0.818、
  数值 5/5、无依据结论 0，3 个失败案例已如实记录；故障注入 3/3 状态符合预期且无编造。

阶段 3 详情：[Agent 设计](docs/agent_design.md) · [工具契约](docs/agent_tools.md) ·
[Agent 评估](docs/agent_evaluation.md) · [Phase 3 报告](docs/phase3_report.md)

仍未实现：Agent-Probe 审计（阶段 4）、React 前端（阶段 5）。

## 快速开始（数据、查询服务与知识库）

```bash
cp .env.example .env        # 填写本地密码（.env 已被忽略）
scripts/db.sh up            # 启动 PostgreSQL + pgvector，等待 healthy
scripts/migrate.sh up       # 建表（迁移，不在应用启动时自动建表）

# 以下 Python 命令按项目约定在 Conda 环境中执行
RUN="conda run -n ms_pointcloud_midterm python"

# 迁移目标自检（只读，打印“将要连接的库/账号”，连接失败即中止）
scripts/migrate.sh target

# 导入：先试运行，再 1 万条验证，最后全量
$RUN scripts/import_metropt.py --dry-run
$RUN scripts/import_metropt.py --max-rows 10000
$RUN scripts/import_metropt.py

# 校验（默认**只读事务**，不会写任何业务表）
$RUN scripts/verify_phase1b.py --json data/processed/metropt3/phase1b_verification.json
# 写入型验证（重复导入）只在 *_test 库允许
$RUN scripts/verify_phase1b.py --write-tests

# 迁移审计：只读快照 + 迁移/修订前后比对（行数、内容指纹、约束、索引）
$RUN scripts/migration_audit.py --out /tmp/iaa_before.json
$RUN scripts/migration_audit.py --compare /tmp/iaa_before.json --allow-missing-samples-change
# 0005/0006 与 missing_samples 修订核验（默认只读；写探针强制 ROLLBACK）
$RUN scripts/verify_migration_0005_0006.py --baseline /tmp/iaa_before.json --expect-gap-diff 0

# 测试（独立测试库，含覆盖率）
cd backend && conda run -n ms_pointcloud_midterm python -m pytest --cov=app
```

### 知识库（阶段 2）

```bash
# 0) 准备本地模型（只需一次；本机已安装 Ollama）
ollama pull bge-m3              # Embedding，实测 1024 维
ollama pull qwen2.5:7b-instruct # 生成模型（也可用其它兼容服务）

# 1) 下载并核验文档（已存在且 SHA-256 一致则跳过）
$RUN scripts/fetch_knowledge_docs.py
$RUN scripts/fetch_knowledge_docs.py --verify-only

# 2) 迁移隔离知识库（示例库名 iaa_rag_dev；脚本拒绝写入非 *_dev/*_test 库）
export RAG_DATABASE_URL='postgresql+psycopg://app_user:***@127.0.0.1:5432/iaa_rag_dev'
cd backend && ALEMBIC_DATABASE_URL="$RAG_DATABASE_URL" \
  conda run -n ms_pointcloud_midterm python -m alembic upgrade head && cd ..

# 3) 入库（两套切分并存，便于对比）
$RUN scripts/ingest_knowledge.py --strategy structural --chunking-version structural-v1
$RUN scripts/ingest_knowledge.py --strategy fixed --chunking-version fixed-v1 --chunk-size 900
# 只解析不写库：--dry-run --no-embed

# 4) 评估（检索对比约 30 s；含真实 LLM 问答约 3 分钟）
$RUN scripts/evaluate_rag.py --skip-generation
$RUN scripts/evaluate_rag.py --out data/processed/knowledge/rag_eval.json
```

入库与评估都记录解析/切分/Embedding/写库/检索/LLM 耗时；知识库写入只允许隔离库，
`conftest` 会把测试用 `RAG_DATABASE_URL` 强制指向测试库。

### 启动查询服务（阶段 1C）

```bash
cd backend
conda run -n ms_pointcloud_midterm python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# Swagger：http://127.0.0.1:8000/docs    OpenAPI：http://127.0.0.1:8000/openapi.json
```

接口一览（全部只读）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 进程存活（不访问数据库） |
| GET | `/ready` | 数据库连接、pgvector、Alembic 版本 |
| GET | `/api/v1/equipment` | 设备列表（含观测时间跨度） |
| GET | `/api/v1/equipment/{equipment_id}` | 设备详情 |
| GET | `/api/v1/equipment/{equipment_id}/readings` | 历史观测（`start_time`/`end_time` 必填，`limit`≤1000） |
| GET | `/api/v1/equipment/{equipment_id}/stats` | 模拟量 min/max/mean/std + 数字量 0/1 占比与切换次数 |
| GET | `/api/v1/equipment/{equipment_id}/faults` | 官方故障事件（按窗口重叠） |
| GET | `/api/v1/equipment/{equipment_id}/quality` | 采样覆盖率、缺口、冻结候选与质量提示 |
| POST | `/api/v1/knowledge/search` | 知识检索（vector / keyword / hybrid，Top-K ≤20） |
| POST | `/api/v1/knowledge/ask` | RAG 回答 + 引用（文档/页码/Chunk ID）+ 证据不足/模型失败状态 |
| GET | `/api/v1/knowledge/documents` | 已入库文档、解析状态与各切分版本 Chunk 数 |
| GET | `/api/v1/knowledge/health` | 知识库迁移版本、索引模型/维度、可选模型服务探测 |
| POST | `/api/v1/agent/chat` | 运维 Agent 问答：意图识别 → 工具编排 → 证据整合 → 有引用回答 |
| GET | `/api/v1/agent/traces/{trace_id}` | 一次执行的结构化 Trace（可选完整事件流） |

约定：时间**无时区**、区间**左闭右开** `[start_time, end_time)`；带 `Z`/`+08:00` 的输入被拒绝（422）；
所有错误统一为 `{"error": {"code", "message", "details", "request_id"}}`。

```bash
# 示例：官方故障 E1 当天的质量概览
curl "http://127.0.0.1:8000/api/v1/equipment/metropt3-apu-01/quality?\
start_time=2020-04-18T00:00:00&end_time=2020-04-19T00:00:00"
```

### Agent（阶段 3）

```bash
# 需要：主库/知识库可连、Ollama 已就绪（bge-m3 + qwen2.5:7b-instruct）
cd backend && conda run -n ms_pointcloud_midterm python -m uvicorn app.main:app --port 8000

# 纯数据问题
curl -s -X POST http://127.0.0.1:8000/api/v1/agent/chat \
  -H 'Content-Type: application/json' \
  -d '{"question":"2020 年 4 月 17 日电机电流的最大值是多少？"}'

# 混合分析 + 返回 Trace
curl -s -X POST http://127.0.0.1:8000/api/v1/agent/chat \
  -H 'Content-Type: application/json' \
  -d '{"question":"这台空气压缩机在 2020 年 4 月 17 日的电流和压力是否异常？结合公开技术资料，可能需要关注哪些问题？","include_trace":true}'

# 查看一次执行的 Trace
curl -s "http://127.0.0.1:8000/api/v1/agent/traces/<trace_id>?include_events=true"

# 真实模型端到端测试（默认跳过）
cd backend && IAA_AGENT_E2E=1 conda run -n ms_pointcloud_midterm \
  python -m pytest tests/test_agent_e2e_real.py -m slow -q

# Agent 评估（51 题，真实模型；约 5 分钟）
conda run -n ms_pointcloud_midterm python scripts/evaluate_agent.py

# 留出集 / 规划器对比
conda run -n ms_pointcloud_midterm python scripts/evaluate_agent.py \
  --questions knowledge/eval/agent_holdout.json --skip-fault-scenarios
conda run -n ms_pointcloud_midterm python scripts/evaluate_agent.py --planner llm \
  --ids A01,A04,B01,B10,C01,C04,G02,H01,I04 --skip-fault-scenarios
```

Agent 配置项见 `.env.example` 的 `AGENT_*`（预算、超时、返回量、回答模式、Trace 目录）。
若本机开启了系统 HTTP 代理，代码会把 loopback 主机（`127.0.0.1`/`localhost`）写入 `NO_PROXY`，
避免发往本机 Ollama 的请求被代理接管。

测试分三类：单元测试、API 集成测试（`backend/tests/test_api_*.py`）、
真实工业数据只读验证（默认跳过，需提供全量库）：

```bash
export IAA_FULLDATA_DATABASE_URL='postgresql+psycopg://app_user:***@127.0.0.1:5432/<full_data_test_db>'
cd backend && conda run -n ms_pointcloud_midterm python -m pytest tests/test_api_real_data.py -v
```

SQL 性能基准（只读，EXPLAIN ANALYZE）：

```bash
conda run -n ms_pointcloud_midterm python scripts/api_query_bench.py --window-hours 24
```

本机若没有原生 Docker，`scripts/db.sh` 会自动回退到已运行的 Lima 虚拟机内的 docker 引擎；
compose 文件、卷与环境变量完全一致，不影响可复现性（详见阶段 1B 报告）。

### 导入的幂等与冲突语义

| 情形 | 行为 |
| --- | --- |
| 同一文件再次导入 | 0 新增，全部记为 conflict（`ON CONFLICT` 拦在数据库层） |
| 字节相同、文件名不同 | **按 SHA-256 复用既有来源**，不新建来源；`notes` 记录 `source_file_mismatch` |
| 不同文件、时间戳重叠且取值相同 | 跳过（`overlap_skipped`），不重复入库 |
| 不同文件、同一时间戳取值不同 | 默认**中止并报告**（`ImportConflictError`），绝不覆盖；`--overlap-policy skip` 可跳过并标记 `partial` |
| 中途失败 | 已提交的 chunk 保留，`ingest_runs.status=partial`（未提交任何行则为 `failed`），重跑同一文件可安全续传 |

### 修改数据库密码（非破坏性）

PostgreSQL 的账号密码保存在**已初始化的数据卷**里；`.env` 只是连接凭据的副本。
只改 `.env` 不会改变数据库内密码，表现为认证失败。正确步骤：

```bash
# 1) 先在数据库内改密码（使用当前可用凭据）
scripts/db.sh psql   # 或：docker exec -it iaa-postgres psql -U app_user -d postgres
#    ALTER ROLE app_user WITH PASSWORD '<新密码>';

# 2) 再同步 .env 中的 POSTGRES_PASSWORD / DATABASE_URL / TEST_DATABASE_URL / ALEMBIC_DATABASE_URL

# 3) 验证
scripts/migrate.sh target
```

**禁止**把“删除卷再重建”当作常规改密方式：那会销毁全部已导入数据（需重新导入 151 万行）。

## 后续工作计划

阶段 2（知识库 RAG）已完成开发、测试与评估，详见 [Phase 2 报告](docs/phase2_report.md)。
下一阶段（阶段 3）将构建 LangGraph Agent 与受约束的查询/检索工具；**本次未启动**。

生产库结构仍停在 `0004`（迁移 `0005`/`0006` 与 23 行派生字段修订待批准），
本阶段新增的 `0007` 也只在隔离库执行。Phase 1C 的只读 API 在 `0004` 上仍可运行；
详见 [Phase 1B 最终收口验收报告](docs/phase1b_final_acceptance.md)。

- [系统架构](docs/architecture.md)
- [数据来源](docs/data_sources.md)
- [数据探索分析](docs/data_analysis.md)
- [数据库设计](docs/database_design.md)
- [开发路线图](docs/roadmap.md)
- [技术决策](docs/decisions.md)
- [阶段 1B 报告](docs/phase1b_report.md)
- [Phase 1B 缺陷修复报告](docs/phase1b_remediation.md)
- [阶段 1C 报告](docs/phase1c_report.md)
- [RAG 设计](docs/rag_design.md)
- [知识库来源](docs/knowledge_sources.md)
- [RAG 评估](docs/rag_evaluation.md)
- [Phase 2 报告](docs/phase2_report.md)

`.env.example` 仅用于说明未来配置项，不能直接连接生产服务。实际环境文件与原始数据不进入 Git。
