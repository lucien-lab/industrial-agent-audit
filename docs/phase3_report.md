# 阶段 3 报告：基于 LangGraph 的工业设备智能运维 Agent

- 版本：`phase3-agent-v1`（回答提示词 `agent-prompt-v1`，规划提示词 `agent-planner-v1`）
- 完成日期：2026-10-24
- 范围：**仅阶段 3**。未开发 Agent-Probe 审计引擎、未做多 Agent 协作、未做前端、
  未连接真实设备、未执行任何数据库迁移、未重建 RAG 知识库。
- 阶段 2 状态说明：**阶段 2 未做独立验收**（按任务要求跳过）。本报告中所有涉及 RAG 的内容
  都只是“阶段 2 开发的现状 + 阶段 3 的消费结果”，**不得**表述为阶段 2 已正式验收通过。

## 1. 交付物

### 1.1 代码

| 模块 | 文件 | 说明 |
| --- | --- | --- |
| Agent 配置 | `backend/app/agent/config.py` | 预算/超时/返回量上限/行为开关（全部环境变量） |
| 状态 | `backend/app/agent/state.py` | `AgentState` + 初始状态 + 校验 + 预算工具 |
| 意图与规划 | `backend/app/agent/intent.py` | 槽位抽取、意图分类、工具计划、预算裁剪 |
| LLM 规划器 | `backend/app/agent/planner_llm.py` | 可选的“模型在注册表内选工具”，失败回退规则 |
| 运行时上下文 | `backend/app/agent/context.py` | 只读数据库会话（数据/知识库分离）、模型客户端 |
| 工具 | `backend/app/agent/tools/{base,data_tools,knowledge_tools,registry}.py` | 6 个只读工具 + 契约 + 超时/重试执行器 |
| 证据整合 | `backend/app/agent/evidence.py` | 事实清单（D 标签）、知识片段（K 标签）、数值接地 |
| 回答生成 | `backend/app/agent/answer.py` | LLM 叙述 + 确定性渲染 + 引用逐字校验 + 确诊措辞检查 |
| 工作流 | `backend/app/agent/{nodes,graph}.py` | LangGraph `StateGraph`（6 节点 / 3 处条件分支） |
| Trace | `backend/app/agent/trace.py` | 结构化事件、进程内存储、JSONL 落盘、摘要脱敏 |
| 服务层 | `backend/app/agent/service.py` | `run_agent()` / `get_trace()`，HTTP 无关 |
| API | `backend/app/api/routes/agent.py`、`backend/app/schemas/agent.py` | `POST /api/v1/agent/chat`、`GET /api/v1/agent/traces/{id}` |

对既有模块的最小改动：

| 文件 | 改动 | 原因 |
| --- | --- | --- |
| `backend/app/rag/config.py` | 新增 `ensure_loopback_no_proxy()` | 开发机开启系统 HTTP 代理时，httpx 会把发往 `127.0.0.1:11434` 的 Ollama 请求交给代理并得到 502；现在把 loopback 主机写入 `NO_PROXY`（只影响本机模型服务，不影响外部 API 的代理设置）。这是环境适配，不改变 RAG 逻辑。 |
| `backend/app/api/errors.py` | 注册 `AgentError` 处理器与状态码映射 | 统一错误结构 |
| `backend/app/api/app.py`、`app_meta.py` | 注册 Agent 路由与文档标签 | HTTP 装配 |
| `backend/app/agent/context.py` 依赖的 `db/session.py` | 未修改 | 复用连接池工厂 |
| 数据库 | **未修改** | 主库仍在 `0004`；RAG 库仍在 `0007`；本阶段无迁移 |

### 1.2 测试与评估

| 文件 | 内容 |
| --- | --- |
| `backend/tests/test_agent_state.py` | State 字段/校验/预算/摘要脱敏 |
| `backend/tests/test_agent_tools.py` | 注册表、Pydantic 校验、超时、重试、错误分类、空结果 |
| `backend/tests/test_agent_intent.py` | 三类意图、时间窗/测点抽取、注入拒绝、计划与预算 |
| `backend/tests/test_agent_answer.py` | 事实标签、数值接地、引用逐字校验、降级路径 |
| `backend/tests/test_agent_graph.py` | 端到端工作流（隔离测试库 + 真实 Service + 替身模型） |
| `backend/tests/test_agent_api.py` | HTTP 接口、参数校验、Trace 查询、敏感信息 |
| `backend/tests/test_agent_security.py` | 注入、SQL 注入、只读约束、越权工具、文档注入 |
| `backend/tests/test_agent_e2e_real.py` | **真实模型 + 真实 RAG + 真实数据**端到端（默认跳过，`IAA_AGENT_E2E=1` 启用） |
| `scripts/evaluate_agent.py` | 评估脚本（工具选择/数值/引用/无依据/耗时 + 故障注入） |
| `knowledge/eval/agent_questions.json` | 开发评估集 51 题 |
| `knowledge/eval/agent_holdout.json` | 留出集 11 题 |

### 1.3 文档

`docs/agent_design.md`（工作流/State/决策机制/证据规则/Trace）、
`docs/agent_tools.md`（工具契约）、`docs/agent_evaluation.md`（指标与全部结果）、
本报告，以及 `README.md`、`docs/architecture.md`、`docs/roadmap.md` 的更新。

## 2. 能力验证（对应验收清单）

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| LangGraph Agent 能运行 | ✅ | `backend/app/agent/graph.py`；`test_agent_graph.py` 18 项；真实端到端 3 项 |
| 能自主选择工具 | ✅ | 规则规划器：工具完全匹配率开发集 1.000 / 留出集 0.818（`docs/agent_evaluation.md`） |
| 能完成纯知识问题 | ✅ | 开发集 10 题全通过；例：TP2/TP3 含义、CAGI、检漏方法 |
| 能完成纯数据问题 | ✅ | 开发集 14 题全通过；数值与直接 Service 查询一致（23/23 数值检查） |
| 能完成混合分析任务 | ✅ | 开发集 8 题全通过；示例见 §3 |
| 能正确调用既有 Service | ✅ | 工具只调用 `services/*`，Agent 内无 SQL（`test_agent_tools.py::test_run_tool_is_keyword_only_and_never_writes`） |
| 能处理异常与超时 | ✅ | 故障注入：知识库不可达 / 统计超时 / 预算不足 → 3/3 状态符合预期且无编造 |
| 有最大执行次数限制 | ✅ | `AGENT_MAX_TOOL_CALLS=8`、`AGENT_MAX_ROUNDS=5`、`AGENT_MAX_GRAPH_STEPS=24`；`test_max_tool_calls_budget`、`test_max_rounds_budget` |
| 有真实数据与文档引用 | ✅ | 每个数值带 `[D#]` + Service/表/窗口来源；每条知识带 `[K#]` + 文档/页码/Chunk ID |
| 能区分事实与推断 | ✅ | 固定分节：数据依据 / 技术知识依据 / “推断与需关注问题（非已确认结论）” / 不确定性说明 |
| 能处理证据不足 | ✅ | 空窗口、缺时间窗、设备不存在、知识检索为空、预算耗尽均返回明确说明（`insufficient_evidence` / `refused`） |
| 能记录执行 Trace | ✅ | `run_start → intent → plan → tool_call/tool_result → decision → answer → run_end`；JSONL + 内存 |
| FastAPI Agent 接口正常 | ✅ | `POST /api/v1/agent/chat`、`GET /api/v1/agent/traces/{trace_id}`（含 `include_events`） |
| 至少 30 条评估问题 | ✅ | 51 条（开发集）+ 11 条（留出集）+ 3 个故障注入场景 |
| 输出真实评估指标 | ✅ | `docs/agent_evaluation.md`、`knowledge/eval/agent_eval_summary.json` 等 |
| 自动化测试通过 | ✅ | `420 passed / 4 skipped`，覆盖率 91%；真实端到端 `IAA_AGENT_E2E=1` 3 passed |
| 原有模块无回归 | ✅ | 阶段 1A–2 的 267 项测试全部通过（含新增后共 420 项） |
| 不修改生产数据 | ✅ | 所有 Agent 会话 `default_transaction_read_only=on`；`test_agent_security.py::test_agent_run_never_writes_business_tables` 对比运行前后行数 |

## 3. 示例：用户给出的混合分析问题

问题：

> 这台空气压缩机在 2020 年 4 月 17 日的电流和压力是否异常？结合公开技术资料，可能需要关注哪些问题？

实际执行（真实运行，来自评估记录 C01）：

| 步骤 | 工具 | 结果 | 耗时 |
| --- | --- | --- | ---: |
| 1 | `get_equipment_info` | 设备存在，观测范围 2020-02-01 … 2020-09-01 | 13 ms |
| 2 | `get_sensor_statistics`（目标窗口） | 6301 行；Motor_current max 6.36 / mean 0.4512；TP3 mean 8.4682 等 | 10 ms |
| 3 | `get_data_quality` | 覆盖率 0.7293，缺口 2 处（最长 14092 s），冻结候选 4378 行 | 7 ms |
| 4 | `get_fault_events` | 0 条（空结果，合法查询） | 4 ms |
| 5 | `search_technical_knowledge` | 5 段真实文档片段（含通用资料，标注“非本设备专属”） | 147 ms |
| 6 | `get_sensor_statistics`（补充：前一等长窗口） | 对照窗口 8027 行 | 9 ms |
| 7 | LLM 叙述 + 校验 | `qwen2.5:7b-instruct`，约 9.0 s；端到端 9.25 s | — |

结论段（原文）：

> 需要关注 2020 年 4 月 17 日的电机电流和压缩机压力异常。具体表现为：电机电流均值从对照窗口的
> 1.4867A 下降到目标窗口的 0.4512A（变化 -69.65%），压缩机压力 TP3 的均值从对照窗口的
> 9.012kPa 下降到目标窗口的 8.4682kPa（变化 -6.03%）。这些变化可能与空气泄漏有关，
> 但不能确认故障。[D16]/[D17] 提供了对比数据。

回答同时给出每条数据的来源（工具 + Service + 表 + 时间窗）与固定免责声明
（“不构成故障诊断结论；涉及维修、拆装或参数调整须由具备资质的人员现场确认”）。
所有数值均来自工具返回；`grounding.numeric_violations` 为空。

## 4. 关键设计决策

1. **规则规划优先，LLM 规划可选**：9 题对比中规则计划完全匹配 1.000、LLM 计划 0.778，
   且 LLM 每次多约 1.8 s。默认 `rule`，`llm` 留给后续模型升级对比。
2. **数据事实固定渲染**：模型只写“结论句 + 知识要点 + 推断”，数据小节由事实清单确定性生成，
   因此数值不可能被模型改写；这一点比“让模型自己复述数字”更可审计。
3. **引用校验“逐字 + 标签 + 适用范围”三重**：阶段 2 的引用问题（69.2% 完全正确率）说明
   “文档 ID 存在”不等于“引用支持结论”。本阶段强制逐字摘录，不合格的结论直接丢弃并记录原因；
   适用范围（`generic_compressor`）在回答中显式标注。
4. **只读在连接层强制**：不是靠代码约定，而是 `default_transaction_read_only=on`，
   测试验证写入会被数据库拒绝。
5. **预算显式、终态有限**：`tool_call_count` / `rounds` / `steps` / `deadline_epoch` 全在 State 里；
   终态只有 6 个取值；计划被裁剪也会标记 `budget_exhausted`，避免“看起来完整其实缺数据”。
6. **Trace 与审计分离**：本阶段只产出结构化事件（供阶段 4 的 Agent-Probe 消费），
   不做评分与 LLM 审计，避免提前把“审计结论”混进 Agent 输出。

## 5. 实测指标（摘要）

| 数据集 | 题数 | 工具完全匹配 | 状态匹配 | 数值正确 | 引用逐字 | 无依据结论 | 平均耗时 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 开发集（规则规划） | 51 | 1.000 | 1.000 | 1.000 (23/23) | 1.000 | 0.000 | 3.87 s |
| 留出集（未调参） | 11 | 0.818 | 0.818 | 1.000 (5/5) | 1.000 | 0.000 | 3.04 s |
| LLM 规划器子集 | 9 | 0.778 | 0.778 | 0.833 (5/6) | 1.000 | 0.000 | 6.01 s |

工具调用成功率：开发集 0.956（91 次调用，4 次失败：设备不存在 ×2、时间参数非法 ×2）；
故障注入场景 3/3 状态符合预期、3/3 无编造。

完整数字、分类别结果、失败案例与方法学限制见 `docs/agent_evaluation.md`。

## 6. 已知限制

继承自阶段 2（未在本阶段解决）：

- 引用**页码级**精度不足：页面级需求满足率开发集 0.90、留出集 0/1（内容正确但引了另一页）；
  未引入 Reranker。
- 多文档联合检索仍弱；中文问题对英文文档的纯关键词检索基本无效。
- 7B 模型（qwen2.5）叙述质量有限：知识要点可能“相关但不够精确”，
  因此会出现“知识结论被逐字校验丢弃”的情况（回答仍给数据部分）。

本阶段新增限制：

1. 规则意图/计划依赖关键词覆盖，罕见措辞会退化为“要求补充信息”（留出集 X08/X10/X11）。
2. `mixed_analysis` 平均 5.4 次工具调用（6.4 s），成本高于纯数据问题。
3. 数值接地是“数字必须在允许集合内”的检查，无法发现“口径/单位错误”；
   也不能发现模型用文字表达的、没有数字的推断错误。
4. 开发集与规则同步演化，指标偏乐观；已用 11 题留出集交叉验证并如实记录失败。
5. Trace 存在进程内（有界 200 条）与本地 JSONL；未做跨进程/持久化查询，
   也不支持按策略版本重审（属阶段 4）。
6. 时间窗语义沿用阶段 1C：只接受无时区时间，带时区直接拒绝（HTTP 422），不做隐式换算。

## 7. 环境与运维说明

| 项 | 值 |
| --- | --- |
| 新增依赖 | `langgraph 1.2.2`（conda-forge 安装到 `ms_pointcloud_midterm`，附带 `langchain-core`、`langgraph-checkpoint` 等；未升级既有依赖） |
| 主数据库 | `industrial_agent_audit`（迁移 `0004`，**未修改、未迁移**） |
| 知识库 | `iaa_rag_dev`（迁移 `0007`，6 文档 / 1,314 Chunk，**未重建**） |
| 模型服务 | 本机 Ollama：`qwen2.5:7b-instruct`、`bge-m3` |
| 新增环境变量 | `AGENT_*`（见 `.env.example`）；`NO_PROXY` 由代码为 loopback 自动补充 |
| Trace 输出 | `data/processed/agent/traces/trace-<trace_id>.jsonl`（可用 `AGENT_TRACE_WRITE_FILES=false` 关闭） |
| 启动 | `uvicorn app.main:app`（与阶段 1C/2 同一工程） |

## 8. 未做事项（明确边界）

- 未实现 Agent-Probe 的风险评分、规则审计、LLM 审计、策略版本管理、待审计恢复。
- 未实现多 Agent 协作、计划器自反思、工具自动生成。
- 未实现前端页面（阶段 5）。
- 未新增数据库表/迁移（Trace 不落库）。
- 未重新导入工业数据、未重建知识库、未修改 Embedding 模型。
- **阶段 2 未做独立验收**，本报告不将其标记为验收通过。

## 9. 下一步建议（不自动执行）

1. 阶段 2 独立验收（补做引用语义人工复核），再决定是否需要 Reranker 或 Chunk 级重排。
2. 依据本报告 §6 的限制，扩充留出集到 30 题以上，并把“失败案例”固化为回归测试。
3. 阶段 4：以本阶段的 Trace 事件契约为输入，建设 Agent-Probe 的规则审计与独立 LLM 评审。
