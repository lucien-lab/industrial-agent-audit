# Agent-Probe 行为审计设计（阶段 4）

- 版本：`phase4-audit-v1`；规则配置版本由 `AuditRuleConfig` 的哈希生成（`rulecfg-<16 hex>`）；
  LLM Judge prompt 版本：`audit-judge-v7`。
- 代码位置：`backend/app/audit/`、`backend/app/models/audit.py`、`backend/app/api/routes/audit.py`。
- 形态：**独立于业务 Agent 的离线审计器**（Observability + Audit），不是运行时拦截器。

## 1. 边界与定位

| 概念 | 本项目是否实现 | 说明 |
| --- | --- | --- |
| Observability | ✅ | 读取阶段 3 的结构化 Trace，规范化事件，可回放 |
| Audit | ✅ | 确定性规则 + LLM 语义审计 + 证据交叉校验 + 风险汇总 |
| Protection | ⚠️ 仅接口 | 可设计执行前检查入口，但**离线审计无法阻止已经完成的操作**；本阶段不宣称具备运行时阻断能力 |

审计器明确不做：

- 不重新实现或调用 LangGraph 业务 Agent；
- 不执行工业数据工具、不查询业务数据库（可选的知识库原文核对除外，且会显式记录）；
- 不修改原始 Trace、业务回答或工业数据；
- 不控制设备、不执行 SQL、不执行文档中的任何指令。

## 2. 总体架构

```text
阶段 3 Agent 执行
  └─ JSONL Trace（权威来源） + 进程内 TraceStore
                    │
                    ▼
        ┌──────────────────────┐
        │ collector             │  文件 / 内存 / 审计事件索引（只读）
        │ normalizer            │  排序、去重、稳定 event_id、脱敏、结构问题
        └──────────┬───────────┘
                   ▼
        ┌──────────────────────┐
        │ evidence index        │  统计值、质量、引用、数值集合、工具入参
        └───────┬──────────────┘
                │
      ┌─────────┴──────────┐
      ▼                    ▼
 ┌──────────┐        ┌─────────────┐
 │ Rule R1–R10 │      │ LLM Judge   │  （rule_only / llm_only / hybrid）
 └────┬─────┘        └──────┬──────┘
      └─────────┬───────────┘
                ▼
     ┌─────────────────────┐
     │ scoring             │  冲突记录、风险等级、人工复核标记
     └──────────┬──────────┘
                ▼
     ┌─────────────────────┐
     │ repository          │  独立审计库（幂等、版本隔离、追加式）
     └──────────┬──────────┘
                ▼
     FastAPI /api/v1/audit/*
```

模块职责：

| 文件 | 职责 |
| --- | --- |
| `config.py` | 审计版本、规则阈值、Judge 配置、审计库安全校验 |
| `schemas.py` | 事件 / 规则结果 / Judge 输出 / 报告的数据契约（Pydantic） |
| `collector.py` | 从 JSONL / 内存 / 数据库索引读取 Trace；路径穿越与大小限制 |
| `normalizer.py` | 排序、去重、稳定 event_id、脱敏、结构问题记录、Trace 摘要 |
| `evidence.py` | 证据索引、数值支持判断、统计口径查询、可选的外部 Chunk 核对 |
| `rules/deterministic.py` | R1–R10 确定性规则 |
| `judge/prompts.py` | Judge Prompt、claim / 指令样文本抽取 |
| `judge/judge.py` | 结构化输出解析、有限重试、聚焦拒答判断、失败降级 |
| `scoring.py` | findings、风险等级、规则与 Judge 冲突检测 |
| `engine.py` | 三种模式的编排、幂等键、持久化、回放 |
| `replay.py` | 历史 Trace 回放与重新审计 |
| `repository.py` | 审计库读写（与业务只读连接分离） |

## 3. Trace 数据契约

### 3.1 原始事件（阶段 3，权威来源）

阶段 3 的事件字段：`trace_id, seq, event_type, node, tool, status, started_at, ended_at,
duration_ms, input_summary, output_summary, error_category, error_code, evidence_refs, meta`。

事件类型：

`run_start → intent → plan → (node_start/node_end) → (tool_call → tool_result [→ tool_retry] → decision)* → answer → evidence → run_end`

阶段 4 **只新增** `evidence` 事件（`meta.manifest`），不改变任何既有事件的语义。
`evidence` 事件由 `app/agent/evidence_manifest.py` 在 `finalize` 节点构建：

- 输入只来自**已经返回**的工具观测，不发起新查询；
- 有界：回答 ≤8000 字符、片段摘录 ≤600 字符、事实 ≤60 条、引用 ≤60 条；
- 文档片段只保留有限摘录 + `quote_sha256`，不保存整段原文；
- 包含统计值（min/max/mean/std/count）、窗口、设备、质量、故障事件、知识引用、
  grounding、回答分节与执行指标（steps/rounds/tool_call_count）；
- 不包含连接串、密码、API Key（另有 `safe_text` 兜底脱敏）。

### 3.2 规范化事件（审计侧）

```text
event_id            同 trace 内唯一（E0004 或带内容哈希后缀）
trace_id
sequence_number     0 起，按 seq 排序后的稳定顺序
event_type
node_name / tool_name
timestamp / started_at / duration_ms
status / error_type / error_code
input_summary / output_summary   （递归脱敏）
evidence_refs / meta
schema_version      audit-event-v1
source_index        原始文件/列表下标
raw_sequence_number 原始 seq（缺失为 null）
```

规范化阶段记录的 `problems`：`missing_run_start`、`missing_run_end`、`missing_sequence`、
`sequence_collision`、`out_of_order_events`、`duplicate_events`、`trace_id_mismatch`、
`invalid_json_line`、`unknown_manifest_version`、`manifest_build_error` 等。
这些问题由 R9 决定是 FAIL 还是 UNKNOWN；**规范化本身不做风险判断**。

### 3.3 三层数据严格分离

1. **原始执行事件**：只读，来自阶段 3；
2. **审计检测结果**：规则/Judge 产生，带 `source=rule|judge`；
3. **审计器诊断**：解析失败、模型超时、外部读取、幂等复用等，写在 `diagnostics`。

Judge 的结论不会伪装成原始事件；规则结果也不会写回 Trace。

## 4. 确定性规则引擎

规则清单与阈值见 [`docs/audit_rules.md`](audit_rules.md)。统一要求：

- 每条结果带 `rule_id / rule_name / category / severity / verdict / explanation /
  evidence_refs / related_event_ids / details`；
- `verdict ∈ PASS / FAIL / NOT_APPLICABLE / UNKNOWN`；
- 证据不足必须 `UNKNOWN`，**不得默认 PASS**；
- 规则不调用 LLM、不访问业务数据库、不执行工具；
- 规则内部异常会被引擎捕获为 `UNKNOWN`（不会变成通过）。

## 5. LLM Judge

### 5.1 模型与配置

- 默认复用本机 Ollama `qwen2.5:7b-instruct`（与业务 Agent 共用模型服务，但 Prompt、解析、
  异常处理完全独立）；
- `temperature=0`、`max_tokens=1800`、`timeout=120s`、有限重试 1 次；
- 记录：模型名、provider、prompt 版本、输入 SHA-256、耗时、token（如 Ollama 返回）、
  attempts、原始输出长度。

### 5.2 输入结构

```text
USER_QUESTION（不可信）
EXECUTION_METADATA（审计器采集：状态、请求窗口、设备、grounding、质量限制、injection_flags）
AGENT_ANSWER（不可信，含全部段落）
ANSWER_UNCERTAINTIES / ANSWER_KNOWLEDGE_POINTS
DATA_EVIDENCE（统计值、质量、故障事件、工具状态）
CLAIMS_TO_CHECK（程序拆分的待核查陈述，C1…Cn）
INSTRUCTION_LIKE_LINES_IN_ANSWER（程序检测到的指令样句子）
KNOWLEDGE_EVIDENCE（不可信摘录 + citation quote）
```

### 5.3 输出契约

每个任务必须输出：
`task, verdict, risk_category, confidence, claims[], evidence_refs, explanation,
requires_human_review`；claim 级结论为 `supported / partially_supported / unsupported /
insufficient_evidence`。5 个任务：

| 任务 | 风险类别 |
| --- | --- |
| `task_alignment` | `task_misalignment` |
| `evidence_support` | `unsupported_claim` |
| `overreach` | `overreach` |
| `injection_influence` | `prompt_injection_influence` |
| `false_refusal` | `unnecessary_refusal` |

健壮性处理：

- 代码围栏/前后解释文本可被剥离；
- 任务名写成占位符（如 `a|b|c`）时，按唯一的 `risk_category` 反推任务；
- 缺少任务 → 该任务 `UNKNOWN`（要求人工复核）；
- 非法 JSON → 有限重试；仍失败 → `J_JUDGE_UNAVAILABLE` UNKNOWN；
- `confidence` 只记录模型自报值，不参与风险等级计算，也不当作校准概率；
- 当回答“看起来是拒答”且存在数据/知识证据、但主 Judge 未判 fail 时，追加一次
  短输入的**聚焦拒答判断**（`false_refusal`），其失败同样降级为 UNKNOWN。

## 6. 混合审计策略

| 模式 | 执行内容 | 适用场景 |
| --- | --- | --- |
| `rule_only` | R1–R10 | 秒级回归、确定性合规检查 |
| `llm_only` | 5 个语义任务 | 语义问题研究、与规则对比 |
| `hybrid` | 规则 + Judge + 冲突检测 | 默认模式 |

合并规则：

1. 先执行确定性规则（不依赖 LLM）；
2. 明确的参数/权限/预算/数值/引用违规直接记录为 FAIL；
3. 证据支持性、意图偏移、过度推断、注入影响、错误拒答交给 Judge；
4. 规则 FAIL 与 Judge PASS 在重叠主题上冲突时，记录 `rule_judge_conflict`，
   **保留规则 FAIL** 并要求人工复核；
5. Judge 不可用/解析失败 → UNKNOWN，不产生 PASS；
6. 风险等级只来自**已确认 FAIL** 的最高严重度；只有 UNKNOWN 时等级为 `UNKNOWN`。

风险等级映射（不是分数相加，也不是正确率保证）：

| 条件 | 等级 |
| --- | --- |
| 任一 CRITICAL | `critical` |
| 任一 HIGH | `high` |
| 任一 MEDIUM | `medium` |
| 仅 LOW | `low` |
| 无 FAIL、有 UNKNOWN | `unknown` |
| 无 FAIL、无 UNKNOWN | `none` |

## 7. 审计结果数据模型

迁移 `0008_agent_audit`（只新增表，不触碰工业数据与 RAG 表）：

### `audit_traces`

`trace_id`（唯一）、`source`、`source_ref`、`schema_version`、`agent_version`、`question`、
`execution_status`、`intent`、`started_at`、`ended_at`、`event_count`、`trace_digest`、
`raw_digest`、`redaction_applied`、`problems`、`ingested_at`。

### `audit_trace_events`

规范化事件索引：`(trace_id, event_id)` 唯一、`sequence_number`、`event_type`、`node_name`、
`tool_name`、`event_time`、`duration_ms`、`status`、`input_summary`、`output_summary`、
`evidence_refs`、`error_type`、`error_code`、`schema_version`、`source_index`。

> 权威来源仍是 JSONL；该表只是索引/回放缓存，冲突时以文件为准。

### `audit_runs`

`audit_id`（唯一）、`trace_id`、`audit_mode`、`audit_version`、`rule_config_version`、
`prompt_version`、`judge_model/provider`、`status`、`overall_risk`、`total_findings`、
`high_risk_findings`、`requires_human_review`、`trace_digest`、`input_digest`、
`trace_schema_version`、`trace_source`、`event_count`、`started_at`、`completed_at`、
`duration_ms`、`judge_calls`、`judge_prompt_tokens`、`judge_completion_tokens`、`summary`、
`results`（完整结果矩阵 JSONB）、`judge_metadata`、`diagnostics`、`external_reads`、
`config_snapshot`、`error`、`created_at`。

自然键：`(trace_id, audit_mode, audit_version, rule_config_version, trace_digest)`。
相同键重复审计返回已有记录；不同审计版本/规则配置/Trace 内容生成新的 `audit_id`，**不覆盖旧结果**。

### `audit_findings`

`finding_id`（唯一）、`audit_id`（FK）、`trace_id`、`rule_id`、`rule_name`、`risk_category`、
`severity`、`verdict`、`source`、`related_event_ids`、`evidence_refs`、`explanation`、
`confidence`、`requires_human_review`、`review_status`、`details`、`created_at`。

只保存 FAIL/UNKNOWN；PASS/NOT_APPLICABLE 保留在 `audit_runs.results` 中。

### 数据库安全

- 审计写库使用 `AUDIT_DATABASE_URL`，与业务只读连接（`DATABASE_URL` / `RAG_DATABASE_URL`）分离；
- 配置层拒绝把审计库指向主库 `industrial_agent_audit`（除非显式设置
  `AUDIT_ALLOW_MAIN_DATABASE=true`）；
- 本阶段只迁移隔离库（`iaa_audit_dev` / `*_test`），**主库仍为 0004**；
- 迁移不修改 `sensor_readings`、`documents`、`document_chunks` 或任何 Embedding。

## 8. FastAPI 审计接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/audit/traces/{trace_id}` | 执行链、工具调用、证据摘要、已有审计 |
| POST | `/api/v1/audit/traces/{trace_id}/evaluate` | `rule_only / llm_only / hybrid`，幂等 |
| GET | `/api/v1/audit/runs/{audit_id}` | 完整审计报告（含逐条结果与配置快照） |
| GET | `/api/v1/audit/runs` | 审计运行列表（trace/模式/风险筛选） |
| GET | `/api/v1/audit/findings` | 按风险类别、严重度、trace、复核状态筛选 |
| GET | `/api/v1/audit/summary` | 审计次数、风险分布、规则/Judge 分布、平均耗时 |
| GET | `/api/v1/audit/rules` | 规则元信息（可解释性） |
| PATCH | `/api/v1/audit/findings/{id}/review` | 人工复核状态（不改变风险结论） |

约束：

- 请求体 `extra="forbid"`，客户端不能提交事件、SQL 或代码；
- `trace_id` 字符集校验，拒绝路径穿越；
- 分页硬上限（findings 200、runs 100、offset ≤100000）；
- 响应只包含脱敏后的事件摘要与证据引用，不回显连接串/密码/API Key；
- 可选 `X-Audit-Token`（`AUDIT_API_TOKEN`）；未配置时**只应绑定本机或隔离环境**，
  不做复杂 RBAC；
- 审计库不可用 → 503；审计失败不会被标记为通过。

## 9. 回放与运行后自动审计

- **离线回放**：只读 JSONL/内存/审计事件索引，不重新执行工具或 Agent；
- 若显式开启 `AUDIT_VERIFY_KNOWLEDGE_OFFLINE=true`，审计会读取 `document_chunks`
  核对摘录逐字出现，并在 `external_reads` 记录库名（掩码）、Chunk ID、读取时间与内容哈希；
  此时报告 `offline_only=false`，不能称为“完全基于冻结 Trace 的纯离线回放”；
- **运行后自动审计**：`AUDIT_AUTO_ON_CHAT=true` 时，Agent API 在回答后触发审计，
  通过 `X-Audit-Id / X-Audit-Risk / X-Audit-Status` 返回摘要；审计失败只记录日志，
  不修改回答或 Trace，也不影响原始响应。

## 10. 可复现性与局限

- 相同 Trace + 相同 audit_version + 相同 `rule_config_version`：确定性规则结果稳定；
- 每次运行记录 trace digest、规则配置哈希、Judge 模型、prompt 版本、输入哈希；
- `temperature=0` 不能保证不同设备/模型版本输出完全一致；
- 7B Judge 在长回答上注意力有限：程序会显式列出待核查 claim 与指令样文本，
  但仍可能漏检；因此高风险结论要求人工复核；
- 审计器与业务 Agent 使用同一基础模型，可能存在共同偏差；
- 离线审计不构成运行时安全拦截；对已完成的危险操作只能事后发现。

指标口径与实测结果见 [`docs/audit_evaluation.md`](audit_evaluation.md)，
规则细节见 [`docs/audit_rules.md`](audit_rules.md)。
