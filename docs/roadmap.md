# 开发阶段规划

所有验收指标在对应阶段记录实测结果；本文件没有未经验证的性能或准确率声明。
当前状态：**阶段 0、1A、1B（含缺陷修复）、1C、2、3、4 已完成开发、测试与评估**。
生产库迁移 `0005`/`0006` 与 23 行派生字段修订**仍待批准**（生产库仍在 `0004`）；
阶段 2 的 `0007` 只在隔离库执行，阶段 4 的 `0008` 只在 `iaa_audit_dev` / `*_test` 执行。
**阶段 2 未做独立验收**（按阶段 3 任务要求跳过），因此不得视为已正式验收通过。

## 阶段 0：项目初始化与架构规划 — 已完成

- 目标：建立轻量、职责清晰的项目骨架。
- 主要任务：目录与占位文件、中文说明、架构与数据流、来源核验、逻辑数据库模型、决策和忽略规则。
- 预期交付物：README、五份 docs 文档、`.env.example`、`.gitignore`、空模块目录。
- 验收标准：文件齐全、无覆盖既有内容；无业务代码、真实密钥、依赖安装、数据库变更、容器或数据集下载；计划与完成状态明确分开。
- 实测：交付物齐全；未提交密钥；后续阶段在此基础上扩展。

## 阶段 1：工业数据导入与数据库建设 — 已完成（1A + 1B）

- 目标：形成可复现、可查询的工业数据底座。
- 主要任务：确认环境及版本，核验许可、字段、采样间隔和时区；获取小样本再决定全量导入；实现迁移、字段映射、质量标记、幂等导入和设备/时序查询。
- 预期交付物：数据字典、来源清单、小样本、质量报告、数据库迁移与导入工具、数据查询测试。
- 验收标准：干净环境可完成迁移；抽查来源行与库内观测一致；重复导入不增重复记录；错误输入有报告；可按设备和时间窗口查询；待核验故障信息不转换为确定标签。

### 阶段 1A：真实数据获取与探索分析 — 已完成

- 实测：官方 CSV 1,516,948 行、17 列；采样间隔 10 s（非官方 1 Hz）；332 处 ≥30 s 缺口；24 段数值冻结（50,870 行）；
  4 次官方 Air leak 区间全部落在 CSV 时间范围内。
- 产物：`scripts/fetch_metropt.py`、`scripts/inspect_metropt.py`、`scripts/analyze_metropt.py`，
  `data/processed/metropt3/*`，`docs/figures/*.png`，[数据探索分析](data_analysis.md)。
- 遗留问题（阶段 1B 已修复）：`docs/data_analysis.md` 中“没有任何一天达到 99.5% 覆盖”的统计描述错误；
  文档状态未同步。

### 阶段 1B：PostgreSQL 数据建模、部署与批量导入 — 已完成

- 实测：Docker Compose 启动 PostgreSQL 17 + pgvector 0.8.7；Alembic 迁移 4 个版本全部通过且
  `alembic check` 无漂移；导入真实记录 1,516,948 行（10,000 行先行验证 → 全量）；
  官方故障事件 4 条；冻结候选 50,870 行 / 24 段；缺口 332 处；重复导入新增 0 行。
- 产物：`docker-compose.yml`、`deploy/initdb/`、`backend/alembic/`、`backend/app/models/`、
  `backend/app/services/`、`scripts/import_metropt.py`、`scripts/verify_phase1b.py`、
  `scripts/db.sh`、`scripts/migrate.sh`、`backend/tests/`（63 项通过）、[阶段 1B 报告](phase1b_report.md)。
- 未解决：源时区仍未知（按 `timestamp without time zone` + `timezone_status='unknown'` 存储）；
  `Oil_level` / `DV_pressure` 极性待外部资料核实；冻结成因为记录端卡死，无法从数据判定。

### 阶段 1B 缺陷修复（独立验收后）

- 已修复并在隔离库回归：P1-A 内容哈希身份与跨来源重叠保护、P1-B 配置优先级与迁移目标保护、
  P2-4 `partial` 状态、P2-5 验证脚本只读化、P2-6 查询分页上限、P2-1 取整口径统一、
  P2-2 约束命名、P2-8 覆盖率工具、P2-9 改密说明。
- 109 项测试通过，覆盖率 92%；全量隔离库导入的内容指纹与生产库一致
  （`ed60f05f41270865196ae5bb8727f785`）。
- **待批准后在生库执行**：迁移 `0005`（`UNIQUE(file_sha256)`、`status` 支持 `partial`）、
  迁移 `0006`（CHECK 约束改名）、23 行 `missing_samples` 修订、最小权限账号切换。
  在此之前生产库仍为 `0004_coverage_missing_time`。
- 详见 [Phase 1B 修复报告](phase1b_remediation.md)。

## 阶段 1C：工业数据查询服务与接口契约 — 已完成（开发与自测）

- 目标：把已有数据库查询能力转换为稳定、可复用、可测试的后端服务。
- 实测：FastAPI 应用可启动（`uvicorn app.main:app`），Swagger `/docs` 与 `/openapi.json` 正常；
  8 个接口（健康/就绪、设备列表与详情、历史数据、统计、故障事件、数据质量）全部基于真实数据返回；
  193 项测试通过（其中阶段 1B 110 项无回归），覆盖率 96%。
- 契约要点：只读；时间无时区且**拒绝**带偏移输入；区间左闭右开 `[start, end)`；
  `limit` 默认 100 / 硬上限 1000；时间窗上限 366 天；错误统一 `{"error": {...}}` 结构；
  未知错误不回显 SQL/连接串。
- 性能（全量 1,516,418 行真实数据副本，实测）：24 h 窗口 stats 19 ms、1 天 quality 6 ms、
  列表 5 ms；全量 213 天窗口 stats 1.62 s（唯一较慢场景，已用 366 天上限约束）。
- 产物：`backend/app/api/`、`backend/app/schemas/`、`backend/app/services/*`（5 个 Agent-ready 业务函数）、
  `backend/tests/test_api_*.py`、`scripts/api_query_bench.py`、[阶段 1C 报告](phase1c_report.md)。
- 待办（不阻塞本阶段）：生产库迁移仍待批准；Agent Tool 封装、LangGraph 编排未开始。

## 阶段 2：工业知识文档 RAG 检索 — 已完成（开发、测试、评估）

- 目标：检索适用且可追溯的知识证据。
- 实测：
  - 6 份来源明确的文档 / 131 页入库（2 份 `metropt3_direct`，4 份 DOE `generic_compressor`）；
  - PyMuPDF 按页解析，全部 `parsed`；结构感知与固定切分并存（750 + 564 = 1,314 Chunk）；
  - 本地 Ollama `bge-m3`，实测 **1024 维**，1,314 个真实向量；HNSW + GIN + trgm 索引；
  - 52 题评估集（41 可回答 / 11 必拒答），检索对比：结构感知 + 混合 **Recall@5 0.939 / MRR@10 0.784**，
    纯向量 0.878 / 0.685，纯关键词 0.561 / 0.486；固定切分混合 0.878 / 0.769；
  - 生成：37 题有证据回答、15 题证据不足、0 次模型失败；11/11 必拒答题正确拒答，
    0 条编造参数；人工复核有证据支持 42/52（80.8%）、引用完全正确 36/52（69.2%）；
  - 端到端平均 2.25 s/题（检索 123 ms + LLM 2.12 s）。
- 产物：`backend/app/rag/`、`backend/app/models/knowledge.py`、`backend/alembic/versions/0007_knowledge_rag.py`、
  `backend/app/api/routes/knowledge.py`、`scripts/{fetch_knowledge_docs,ingest_knowledge,evaluate_rag}.py`、
  `knowledge/{sources.json,eval/}`、7 个新测试文件（81 项）、
  [RAG 设计](rag_design.md)、[知识库来源](knowledge_sources.md)、[RAG 评估](rag_evaluation.md)、
  [Phase 2 报告](phase2_report.md)。
- 测试：267 passed / 1 skipped（条件跳过），整体覆盖率 91%；原有 186 项无回归。
- 未解决：多文档联合检索（最高 Recall@5 0.625）、Chunk 级引用精度、7B 模型错误拒答、
  中文问题对英文文档的纯关键词检索无效；未引入 Reranker（按要求本阶段先验证混合检索）。
- 阈值说明：本阶段**没有预注册目标阈值**，属探索性评估，已在评估报告中列为方法学限制。

## 阶段 3：工业设备智能运维 Agent — 已完成（开发、测试、评估）

- 目标：通过数据和检索工具完成可解释的运维问答。
- 实测：
  - LangGraph `StateGraph` 单 Agent（6 节点 / 3 处条件分支），6 个只读工具
    （5 个数据工具包装阶段 1C Service + 1 个知识检索工具包装阶段 2 混合检索）；
  - 工具选择默认确定性规则：开发集 51 题工具完全匹配 **1.000**、状态匹配 1.000；
    留出集 11 题（开发后新写、未调参）工具完全匹配 0.818、状态匹配 0.818，3 个失败案例已记录；
  - 数值回答正确率：开发集 23/23、留出集 5/5；引用逐字校验 1.000；无依据结论 0；
  - 工具调用成功率 0.956（91 次调用，4 次失败：设备不存在 ×2、时间参数非法 ×2），
    失败场景（知识库不可达 / 统计超时 / 预算不足）3/3 状态符合预期且无编造；
  - 平均 1.78 次工具调用、3.87 s/题（本机 Ollama，单请求串行，随负载波动）；
  - 测试 **420 passed / 4 skipped**，整体覆盖率 91%；真实模型 + 真实 RAG + 真实数据端到端 3 项通过。
- 预算与边界：最大工具调用 8、决策轮次 5、节点步数 24、整轮 180 s；工具只读，会话
  `default_transaction_read_only=on`；未新增迁移（主库仍 `0004`、知识库仍 `0007`）。
- 产物：`backend/app/agent/`、`backend/app/api/routes/agent.py`、`backend/app/schemas/agent.py`、
  `backend/tests/test_agent_*.py`（8 个文件）、`scripts/evaluate_agent.py`、
  `knowledge/eval/agent_questions.json`（51 题）、`knowledge/eval/agent_holdout.json`（11 题）、
  [Agent 设计](agent_design.md)、[工具契约](agent_tools.md)、[Agent 评估](agent_evaluation.md)、
  [Phase 3 报告](phase3_report.md)。
- 未解决：引用页码级精度不足（延续阶段 2 检索排序问题，未引入 Reranker）；
  规则意图覆盖有限（罕见措辞退化为“要求补充信息”）；混合分析平均 5.4 次工具调用；
  7B 模型的知识要点质量有限（引用不合格会被丢弃）。
- 阶段 2 状态：**未做独立验收**，本阶段只消费其检索能力，不改变“未正式验收”的状态。

## 阶段 4：Agent-Probe 行为审计模块 — 已完成（开发、测试、评估）

- 目标：独立、可重放地审计 Agent 可观察行为。
- 实测：
  - **独立审计器**：`backend/app/audit/`，不调用业务 Agent、不执行工业数据工具、不修改原始 Trace/回答；
    明确区分 Observability / Audit / Protection（本阶段不宣称运行时阻断）。
  - **Trace 接入**：阶段 3 只新增一个 `evidence` 事件（有界证据清单），旧 Trace 自动按 v1 处理，
    依赖证据的规则返回 UNKNOWN 而非 PASS。
  - **规则**：11 条确定性规则（R1/R1B + R2–R10），覆盖权限、参数、预算、重复调用、时间窗、设备、
    数据质量、数值与统计口径、引用结构、执行链、状态一致性；全部有正常/违规/不适用/证据不足测试。
  - **LLM Judge**：本机 Ollama `qwen2.5:7b-instruct`，5 个语义任务、Pydantic 结构化输出、
    有限重试、聚焦拒答复核；失败/超时/解析错误 → UNKNOWN + 人工复核。
  - **混合审计**：`rule_only / llm_only / hybrid`；规则与 Judge 冲突会记录并保留确定性结论。
  - **持久化**：迁移 `0008_agent_audit`（4 张表），独立审计库 `iaa_audit_dev` / `*_test`；
    自然键幂等 + 审计版本隔离；主库仍 `0004`、RAG 开发库仍 `0007`。
  - **评估集**：80 条（14 真实 Trace / 54 受控修改 / 12 手工构造），dev 44 / holdout 36，
    同一原始 Trace 不跨 split；标签人工检查或由明确修改操作定义，不由审计器/LLM 生成。
  - **实测指标**（UNKNOWN 排除）：
    - rule_only：P **1.0000** / R 0.9167 / F1 0.9565，0 FP；
    - llm_only：P 0.9545 / R 0.3684 / F1 0.5316（语义子集 F1 0.9091）；
    - hybrid：P 0.9839 / R **0.9839** / F1 **0.9839**，1 FP / 1 FN / 5 UNKNOWN；
    - holdout（36 条）：rule_only F1 1.0000、hybrid 0.9812、llm_only 0.4667。
  - **成本**：规则 <1 ms/条；Judge 平均 17.8 s/条（最大 34.0 s），94 次调用、
    306,995 输入 token / 57,080 输出 token。本地模型无 API 费用，但 CPU 时间是主要成本。
  - **误报/漏报**：Q10-real 注入误报（拒绝注入被 Judge 当成采纳）；Q05 task drift 漏报；
    `unsupported_claim` 0 TP / 7 FP / 10 UNKNOWN；错误拒答与任务偏移误报较多。均已记录，未改标签掩盖。
  - **测试**：**507 passed / 7 skipped**，整体覆盖率 **90%**；`app/audit` + `evidence_manifest`
    覆盖率 87%；阶段 1B–3 无回归。真实 Trace + 真实 Judge + 持久化已手工验证。
- 产物：`backend/app/audit/`、`backend/app/models/audit.py`、`backend/app/api/routes/audit.py`、
  `backend/alembic/versions/0008_agent_audit.py`、`backend/tests/test_audit_*.py`、
  `scripts/{generate_audit_traces,build_audit_cases,evaluate_audit,audit_trace}.py`、
  `knowledge/eval/audit_cases.json`、`knowledge/eval/audit_eval_summary.json`、
  [审计设计](audit_design.md)、[审计规则](audit_rules.md)、[审计评估](audit_evaluation.md)、
  [Phase 4 报告](phase4_report.md)。
- 未解决：7B Judge 的语义证据支持、错误拒答/任务偏移判定不稳定；拒绝注入被误判；
  单条审计约 18 s；旧/不完整 Trace 只能 UNKNOWN；标注规模 80、无标注者一致性；
  API 仅有可选共享令牌，默认限本机/隔离环境。

## 阶段 5：系统集成、测试与可视化

- 目标：完成可演示的端到端系统与工程交付。
- 主要任务：设备趋势、知识引用、工具时间线和审计面板；接口联调；Docker Compose；集成与端到端测试；性能、错误状态及敏感信息检查。
- 预期交付物：前端页面、部署说明、测试报告、演示用例、已知限制说明。
- 验收标准：从干净环境按说明启动；完整演示数据查询→检索→Agent→追踪→审计；界面区分待处理、失败和已完成；展示引用、模型/策略版本及证据；性能与成本只报告实测结果；模拟案例与真实来源明确区分。

## 阶段依赖

阶段 1 提供数据和存储基础，阶段 2 提供可评估的知识证据与检索/问答接口，阶段 3 消费两类工具，
阶段 4 消费阶段 3 的轨迹，阶段 5 完成全链路交付。阶段 2、3、4 已完成开发与自测
（阶段 2 未做独立验收）；阶段 5 尚未启动。各阶段先确定契约，模块可用模拟输入独立测试；
不因并行开发省略来源核验与评估验收。
