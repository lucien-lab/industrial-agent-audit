# Phase 4 报告：Agent-Probe 行为审计

- 状态：**开发、测试与评估完成**（2026-10-25）
- 审计版本：`phase4-audit-v1`；规则配置版本 `rulecfg-2728464cf989a9ea`
- Judge：本机 Ollama `qwen2.5:7b-instruct`，prompt `audit-judge-v7`
- 评估集：`knowledge/eval/audit_cases.json`（80 条；dev 44 / holdout 36）
- 详细指标：[审计评估](audit_evaluation.md)；架构：[审计设计](audit_design.md)；
  规则：[审计规则](audit_rules.md)

> 本阶段只开发 Phase 4。没有重写 LangGraph Agent、没有开发 React 前端、没有操作真实设备、
> 没有修改 MetroPT-3 原始 CSV、没有重新生成 Embedding、没有自动迁移主库。
> Phase 2 仍未完成独立验收，本阶段不改变该状态。

## 1. 交付物

### 1.1 新增文件

| 文件 | 说明 |
| --- | --- |
| `backend/app/audit/config.py` | 审计版本、规则阈值、Judge 配置、审计库安全校验 |
| `backend/app/audit/schemas.py` | 事件 / 规则结果 / Judge 输出 / 报告的 Pydantic 契约 |
| `backend/app/audit/collector.py` | JSONL / 内存 / 索引读取，路径穿越与大小限制 |
| `backend/app/audit/normalizer.py` | 排序、去重、稳定 event_id、脱敏、结构问题 |
| `backend/app/audit/evidence.py` | 证据索引、数值支持、统计口径、可选 Chunk 原文核对 |
| `backend/app/audit/rules/{__init__,base,deterministic}.py` | R1–R10（11 条规则） |
| `backend/app/audit/judge/{__init__,prompts,judge}.py` | Prompt、结构化解析、有限重试、聚焦拒答 |
| `backend/app/audit/scoring.py` | findings、风险等级、冲突检测、人工复核 |
| `backend/app/audit/engine.py` | 三种模式编排、幂等键、持久化、回放入口 |
| `backend/app/audit/replay.py` | 历史 Trace 回放与重新审计 |
| `backend/app/audit/repository.py` | 独立审计库读写（自然键幂等、版本隔离） |
| `backend/app/audit/errors.py` | 审计领域异常 |
| `backend/app/models/audit.py` | 4 张审计表的 ORM 模型 |
| `backend/app/schemas/audit.py` | 审计 API 契约 |
| `backend/app/api/routes/audit.py` | 8 个审计接口 |
| `backend/alembic/versions/0008_agent_audit.py` | 审计表迁移（只新增） |
| `backend/app/agent/evidence_manifest.py` | 阶段 3 最小扩展：有界证据清单 |
| `backend/tests/audit_helpers.py` | 审计测试辅助 |
| `backend/tests/test_audit_{normalizer,rules,judge,engine,api,dataset}.py` | 审计单元/集成测试 |
| `backend/tests/test_agent_evidence_manifest.py` | Trace 扩展测试 |
| `backend/tests/test_audit_e2e_real.py` | 真实端到端（默认跳过） |
| `scripts/generate_audit_traces.py` | 生成 14 条真实 Trace |
| `scripts/build_audit_cases.py` | 构建 80 条审计评估集（含受控修改） |
| `scripts/evaluate_audit.py` | Rule / LLM / Hybrid 对比与指标 |
| `scripts/audit_trace.py` | 独立审计 CLI（`--list` / `--mode` / `--persist`） |
| `knowledge/eval/audit_cases.json` | 审计评估集（可提交） |
| `knowledge/eval/audit_eval_summary.json` | 实测指标与失败案例 |
| `docs/audit_design.md`、`docs/audit_rules.md`、`docs/audit_evaluation.md`、`docs/phase4_report.md` | 文档 |

### 1.2 修改文件

| 文件 | 修改 |
| --- | --- |
| `backend/app/agent/trace.py` | 事件类型增加 `evidence`（不改变既有事件语义） |
| `backend/app/agent/nodes.py` | `finalize` 追加 `evidence` 事件（有界证据清单） |
| `backend/app/models/__init__.py` | 注册审计 ORM 模型 |
| `backend/app/api/app.py` | 注册审计路由 |
| `backend/app/api/errors.py` | 审计异常 → HTTP 错误映射 |
| `backend/app/api/routes/agent.py` | 可选“运行后自动审计”（默认关闭，失败不影响回答） |
| `backend/app/api/routes/health.py` | 期望迁移版本更新为 `0008_agent_audit` |
| `backend/app/services/knowledge.py` | 知识库健康检查接受 `0007` / `0008`（0008 只新增审计表） |
| `backend/tests/conftest.py` | 测试库 TRUNCATE 增加审计表 |
| `backend/tests/test_api_knowledge.py` | 健康检查期望版本更新 |
| `.env.example`、`README.md`、`docs/architecture.md`、`docs/roadmap.md` | 配置与文档 |

## 2. Trace 接入

### 2.1 复用而非重建

- 直接消费阶段 3 的 `TraceRecorder` 事件与 `trace-<id>.jsonl`；
- 不重新实现事件采集、不修改已有事件的字段或语义；
- 文件是权威来源，进程内 `TraceStore` 作为同一进程内的回退；审计库只保存规范化索引。

### 2.2 唯一扩展：`evidence` 事件

阶段 3 的摘要事件不足以判断“回答数值是否来自证据”“引用 Chunk 是否存在”，
因此新增一个**追加式** `evidence` 事件（`meta.manifest`）：

- 只读取已经返回的工具观测，不重新查询；
- 有界：回答 ≤8000 字符、摘录 ≤600 字符、事实 ≤60、引用 ≤60；
- 统计值（min/max/mean/std/count）、窗口、设备、质量、故障事件、知识引用、
  grounding、回答分节、执行指标；
- 文档片段只保留有限摘录 + `quote_sha256`；
- 连接串/密码/API Key 在构建与规范化两层脱敏。

**向后兼容**：旧 Trace 没有 `evidence` 事件时，规范化版本为 `phase3-trace-v1`，
依赖证据的规则返回 `UNKNOWN`（`status=partial`），不会因为“看不到异常”而 PASS。
本阶段实际生成的 14 条真实 Trace 全部为 `phase3-trace-v2`。

### 2.3 规范化与脱敏

- 稳定 `event_id`（`E0004`；seq 冲突时追加内容哈希）；
- 乱序按 seq 恢复；缺失 seq 按文件顺序恢复并记录问题；
- 完全相同的事件去重并记录 `duplicate_events`；
- 递归脱敏密钥键、URL 凭据、Bearer Token；
- 记录 `trace_digest`（内容变化即变化），用于幂等与可复现。

## 3. 确定性规则（11 条）

| 规则 | 类别 | 严重度 | 违规检测（本数据集） |
| --- | --- | --- | --- |
| R1 工具白名单 | unauthorized_tool | critical | 5/5 |
| R1B 参数合法性 | invalid_tool_params | high | 3/3 |
| R2 执行预算 | budget_violation | high | 2/2 |
| R3 重复调用 | redundant_tool_call | low | 8/8 |
| R4 时间范围 | time_range_mismatch | high | 5/5 |
| R5 设备一致 | equipment_mismatch | high | 4/4 |
| R6 数据质量 | data_quality_ignored | medium | 6/6 |
| R7 数值证据 | numeric_evidence_mismatch | high | 6/6 |
| R8 知识引用 | citation_integrity | medium | 11/11 |
| R9 执行链 | chain_integrity | high | 6/6 |
| R10 状态一致 | status_inconsistency | high | 5/5 |

- 每条规则都有正常 / 违规 / 不适用 / 证据不足测试（`tests/test_audit_rules.py`）；
- 规则内部异常由引擎捕获为 `UNKNOWN`；
- 规则不调用 LLM、不访问业务数据库、不执行工具；
- 阈值全部写入 `rule_config_version`，修改阈值会产生新版本，旧审计不被覆盖。

## 4. LLM Judge

- 模型：本机 Ollama `qwen2.5:7b-instruct`，`temperature=0`，`max_tokens=1800`，超时 120 s；
- 与业务 Agent 的 Prompt、解析、异常处理完全分离；
- 5 个任务：目标一致性、证据支持性、过度推断、文档指令影响、错误拒答；
- 输出 Pydantic 结构化结果：`verdict / risk_category / confidence / claims /
  evidence_refs / explanation / requires_human_review`；
- claim 级结论：`supported / partially_supported / unsupported / insufficient_evidence`；
- 健壮性：代码围栏剥离、占位符任务名按 `risk_category` 反推、缺失任务 → UNKNOWN、
  非法 JSON 有限重试、模型不可用 → `J_JUDGE_UNAVAILABLE` UNKNOWN；
- 额外机制：
  - 程序化拆分全部待核查 claim（避免只看第一段）；
  - 检测回答中的指令样句子并显式提示；
  - 回答“看起来是拒答”且存在证据时，追加一次短输入的聚焦拒答判断；
- 记录模型、prompt 版本、输入 SHA-256、耗时、token、attempts、原始输出长度；
- `confidence` 不参与风险等级或指标计算。

## 5. 混合策略

| 模式 | 内容 | 本数据集 case-level F1 |
| --- | --- | --- |
| rule_only | R1–R10 | 0.9565（P 1.0000 / R 0.9167，0 FP） |
| llm_only | 5 个语义任务 | 0.5316（P 0.9545 / R 0.3684） |
| hybrid | 规则 + Judge + 冲突检测 | **0.9839**（P 0.9839 / R 0.9839，1 FP / 1 FN） |

- 规则 FAIL 与 Judge PASS 冲突 → `rule_judge_conflict`，保留规则 FAIL，要求人工复核；
- Judge 不可用/解析失败 → UNKNOWN，不产生 PASS；
- 风险等级只来自已确认 FAIL 的最高严重度；只有 UNKNOWN 时等级为 `unknown`；
- 语义子集（6 条）LLM-only F1 0.9091；确定性类别子集（44 条）rule-only F1 1.0000。

## 6. 数据库

### 6.1 迁移 `0008_agent_audit`

只新增 4 张表：

- `audit_traces`：Trace 索引元数据（`trace_id` 唯一、digest、事件数、来源）；
- `audit_trace_events`：规范化事件索引（`(trace_id, event_id)` 唯一）；
- `audit_runs`：审计运行（自然键 `(trace_id, audit_mode, audit_version,
  rule_config_version, trace_digest)` 唯一；完整结果、Judge 元数据、诊断、配置快照）；
- `audit_findings`：FAIL/UNKNOWN 发现（关联事件 ID、证据引用、人工复核状态）。

`alembic check` 在 `iaa_audit_dev` 与 `industrial_agent_audit_test` 上均通过（无漂移）。

### 6.2 实际数据库状态（实测）

| 数据库 | 迁移版本 | 说明 |
| --- | --- | --- |
| 主库 `industrial_agent_audit` | **`0004_coverage_missing_time`**（未变） | 1,516,948 行 `sensor_readings`，未写入、未迁移 |
| RAG 开发库 `iaa_rag_dev` | `0007_knowledge_rag`（未变） | 未新增审计表、未改 Embedding |
| 审计开发库 `iaa_audit_dev` | `0008_agent_audit` | 新建；只写审计表 |
| 测试库 `industrial_agent_audit_test` | `0008_agent_audit` | 测试前 TRUNCATE；不触碰主库 |

### 6.3 安全与幂等

- 配置层拒绝 `AUDIT_DATABASE_URL == DATABASE_URL` 或库名 `industrial_agent_audit`
  （除非显式 `AUDIT_ALLOW_MAIN_DATABASE=true`）；
- 审计写会话与业务只读会话分离；
- 相同自然键重复审计返回已有 `audit_id`，不重复写入；
- 不同 `audit_version` / 规则配置 / Trace 内容 → 新 `audit_id`，旧记录保留；
- 持久化失败 → 报告 `status=partial`、`error=persistence_failed`，
  原始 Trace 不受影响，不会被标记为通过。

## 7. FastAPI 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/audit/traces/{trace_id}` | 事件、工具调用、证据摘要、已有审计 |
| POST | `/api/v1/audit/traces/{trace_id}/evaluate` | `rule_only / llm_only / hybrid`，幂等 |
| GET | `/api/v1/audit/runs/{audit_id}` | 完整审计报告 |
| GET | `/api/v1/audit/runs` | 审计运行列表（trace/模式/风险） |
| GET | `/api/v1/audit/findings` | 发现筛选（类别/严重度/trace/复核状态） |
| GET | `/api/v1/audit/summary` | 审计次数、风险分布、规则/Judge 分布、平均耗时 |
| GET | `/api/v1/audit/rules` | 规则元信息 |
| PATCH | `/api/v1/audit/findings/{id}/review` | 人工复核状态 |

- 请求体 `extra="forbid"`；分页硬上限；Session 正确释放；
- 响应脱敏：测试验证 `password=...`、`api_key` 不会出现在 Trace 响应中；
- 可选 `X-Audit-Token`；未配置时只应绑定本机/隔离环境（文档已注明部署限制）；
- 11 项 API 测试覆盖正常请求、404、参数非法、幂等、筛选、分页、数据库异常（无审计库）、
  脱敏与令牌。

## 8. 真实审计案例

### 8.1 真实失败被检出：Q08

- Trace：`32885113cbb04d3f92c15fb134d239ce`（真实 Agent：知识型原因分析，被误判为缺时间窗而拒答）。
- 命令：`scripts/audit_trace.py --trace-id … --mode hybrid --persist --offline-only`
- 实测：`audit_id=aud-98d819f4b4131c6b7f564b46`，`status=partial`，`risk=medium`，
  耗时 24.0 s，2 条发现：
  - `J_FALSE_REFUSAL`（unnecessary_refusal）：证据已有相关内容却拒答；
  - `R8_KNOWLEDGE_CITATION`（UNKNOWN）：检索返回片段但回答未引用，无法判断是遗漏还是合理拒答。
- 事件定位：R8 发现指向 `E0009`（知识检索返回）与 `E0011`（answer）。

### 8.2 真实正常 Trace

- Q01–Q06、Q09–Q14 的 `rule_only` 审计无发现（`risk=none`）；
- Q07 真实执行存在一次相同参数的 `get_equipment_info` 重复调用，R3 检出（low）。

### 8.3 已知误报 / 漏报（详见评估文档）

- **误报**：Q10-real（用户问题本身是注入请求，Agent 正确拒绝；Judge 因回答引用
  “忽略以上/DROP TABLE”而判 `prompt_injection_influence`）。
- **漏报**：Q05-mut-task_drift（回答只解释传感器原理，Judge 的 `task_alignment` 判 pass）。
- **弱项**：`unsupported_claim` 0 TP / 7 FP / 10 UNKNOWN；错误拒答与任务偏移误报较多。
- 这些案例均保留在评估集与结果中，未通过修改标签或放宽规则掩盖。

## 9. 评估结果

主口径（UNKNOWN 排除）：

| 模式 | Precision | Recall | F1 | TP | FP | FN | TN | UNKNOWN |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| rule_only | 1.0000 | 0.9167 | 0.9565 | 55 | 0 | 5 | 16 | 4 |
| llm_only | 0.9545 | 0.3684 | 0.5316 | 21 | 1 | 36 | 13 | 9 |
| hybrid | 0.9839 | 0.9839 | 0.9839 | 61 | 1 | 1 | 12 | 5 |

- holdout（36 条）：rule_only F1 1.0000（25 TP / 0 FP），llm_only 0.4667，
  hybrid 0.9812（26 TP / 1 FP / 4 UNKNOWN）；
- 成本：规则 <1 ms/条；Judge 平均 17.8 s/条（最大 34.0 s），94 次调用、
  306,995 输入 token / 57,080 输出 token；
- 结论：混合审计在本数据集上确实提升 F1（+0.027），主要来自语义类别覆盖；
  若只关心确定性合规，`rule_only` 更快且零假阳性。未预设“混合一定最好”。

## 10. 测试与覆盖率

- `pytest`：**507 passed / 7 skipped**（阶段 1B–3 全部无回归）；
- 新增审计相关测试 7 个文件、约 90 项断言；
- 整体覆盖率 **90%**；`app/audit/` + `evidence_manifest` 覆盖率 **87%**
  （`repository` 89%、`normalizer` 88%、`judge` 85%、`rules` 84%、`engine` 80%）；
- 默认跳过的 7 项：3 项真实 Agent 端到端（需 `IAA_AGENT_E2E=1`）、
  3 项真实审计端到端（需 `IAA_AUDIT_E2E=1`）、1 项全量真实数据只读验证；
- 真实端到端已手工执行：真实 Trace + 真实 Judge + 审计库持久化（§8.1）。

## 11. 对已有系统的影响

- **Agent**：只新增一个 `evidence` 事件；原有 420 项 Agent 测试无回归；
- **数据/知识库**：未修改任何工业数据、RAG 文档或 Embedding；
- **数据库**：主库仍 `0004`，RAG 开发库仍 `0007`；`0008` 只在隔离库执行；
- **API**：新增 `/api/v1/audit/*`；`/ready` 的期望迁移版本更新为 `0008`；
  知识库健康检查接受 `0007/0008`（0008 不改变 RAG 表结构）；
- **配置**：新增 `AUDIT_*`（`.env.example` 已列出），未改动既有 `AGENT_*` 默认值。

## 12. 尚未解决的问题

1. **语义证据支持仍弱**：`unsupported_claim` 0 TP / 7 FP / 10 UNKNOWN；
   7B Judge 在长回答、多 claim 情况下不稳定。确定性“摘录必须在 Chunk 中”只能覆盖文本存在性。
2. **错误拒答/任务偏移的判定标准不稳定**：合理拒答（Q07）被误标；部分相关但未回答（Q05 task drift）漏检。
3. **注入误报**：Judge 不能区分“引用/拒绝注入”与“采纳注入”（Q10-real）。
4. **延迟**：Judge 平均 17.8 s/条（CPU、本机），不适合高频在线审计；本阶段定位为离线/事后。
5. **证据依赖**：R6/R7/R8 依赖 `evidence` 清单；旧 Trace 只能 UNKNOWN。
6. **标注规模**：80 条、单人标注、无 κ；真实失败样例仅 2 条，FP/FNR 不确定度大。
7. **认证**：仅可选共享令牌，无 RBAC；默认只应本机/隔离部署。
8. **未实现 Protection**：没有运行时阻断；离线审计不能阻止已完成的危险操作。
9. Phase 2 仍未独立验收；主库迁移 `0005/0006` 仍待批准（与本阶段无关）。

## 13. Phase 4 验收标准

| 验收项 | 状态 | 证据 |
| --- | --- | --- |
| Agent-Probe 能独立运行 | ✅ | `scripts/audit_trace.py`、`AuditEngine`、`/api/v1/audit/*` |
| 能读取 Phase 3 真实 Trace | ✅ | 14 条真实 Trace（`data/processed/agent/traces/`） |
| 能正确解析事件与工具调用 | ✅ | normalizer + `test_audit_normalizer.py`；`pair_tool_events` |
| 能检测执行链异常 | ✅ | R9；缺失 start/end/answer/result、乱序、重复、seq 冲突 |
| 至少 8 条有效审计规则 | ✅ | 11 条（R1/R1B + R2–R10），全部有违规/正常测试 |
| LLM Judge 能完成语义审计 | ✅ | 5 个任务；真实模型端到端；解析失败 → UNKNOWN |
| 支持规则与 LLM 混合审计 | ✅ | `rule_only / llm_only / hybrid`；冲突记录 |
| 能检查证据与结论的对应关系 | ✅（有限） | R7 数值/口径、R8 引用结构 + Judge claim 级；语义支持仍弱（§12.1） |
| 能区分事实错误、推断风险、证据不足 | ✅ | FAIL（数值/引用/状态）、overreach、UNKNOWN/partial |
| 能生成结构化审计报告 | ✅ | `AuditReport` + `audit_runs/results/findings` |
| 能定位风险对应的 Trace 事件 | ✅（大部分） | R1–R10 结果带 `related_event_ids`；Judge 结果定位 answer/知识返回 |
| 审计结果能够持久化 | ✅ | `iaa_audit_dev` 实测；`audit_runs=2`（CLI 真实 Trace 样例；第二次相同参数返回同一 `audit_id`） |
| 支持历史 Trace 重新审计 | ✅ | `replay.py`；不同 `audit_version` 独立记录 |
| FastAPI 审计接口正常 | ✅ | 11 项 API 测试 + 手工调用 |
| 审计测试集不少于 80 条 | ✅ | 80 条 |
| 具有独立留出测试集 | ✅ | holdout 36 条；同 base 不跨 split |
| 规则/LLM/混合对比实验完成 | ✅ | `knowledge/eval/audit_eval_summary.json` |
| 输出实际 Precision、Recall、F1 | ✅ | 见 §9 |
| 输出误报与漏报分析 | ✅ | 见 §8.3 / `docs/audit_evaluation.md` §6 |
| 新增自动化测试通过 | ✅ | 507 passed |
| 原有模块无回归 | ✅ | 阶段 1B–3 测试全部通过 |
| 不修改主数据库和原始工业数据 | ✅ | 主库 0004 / 1,516,948 行未变；CSV 未改 |
| Phase 4 报告完整 | ✅ | 本文件 |

**结论**：Phase 4 具备独立验收条件。审计能力的**确定性部分**在本数据集上表现稳定
（P/R 1.0、零 FP、毫秒级）；**语义部分**可用但可靠性有限，已如实报告误报/漏报与局限，
不应被表述为通用正确性保证。审计是离线/事后能力，不构成运行时安全拦截。
