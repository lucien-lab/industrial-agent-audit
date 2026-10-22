# 演示指南（阶段 5）

一条可复现的演示链路：**系统状态 → Agent 问答 → Trace → 审计 → 证据 → 审计历史回放**。
所有数值都来自真实执行；固定值只在「历史回放」小节出现，并明确标注为历史记录。

- 真实终端截图：`docs/figures/phase5/01-…08-*.svg`
- 本次演示完整控制台输出：`docs/figures/phase5/demo_transcript.txt`

## 0. 准备（约 1 分钟）

```bash
conda activate ms_pointcloud_midterm
scripts/check_system.sh          # 确认 API / 三个库 / 模型状态
scripts/start_api.sh             # 若未启动
scripts/start_tui.sh             # 另开终端
```

演示数据区间选择依据：`2020-04-17` 是 MetroPT-3 真实有完整记录的一天
（6,301 行观测 / 标称 10 s 间隔），且紧邻官方故障 E1（`2020-04-18` Air leak）。

## 1. 检查系统状态（截图 `01-system-status.svg`）

按 `6` 进入 **System Status**。实测：

```
API        ready        industrial-agent-audit-api v2.0.0
工业库      degraded     industrial_agent_audit schema 0004_coverage_missing_time ≠ 期望 0008_agent_audit
RAG        ready        iaa_rag_dev 文档 6 / Chunk 1314 / schema 0007_knowledge_rag
Audit      ready        审计记录 13 条（近 1 天）
Embedding  unknown      配置 bge-m3（未探测）
LLM        unknown      配置 qwen2.5:7b-instruct（未探测）
```

要点：

- 主库迁移版本与代码期望不一致 → **显示降级**，不是「正常」；
- LLM 默认「未探测」，点「深度探测 Embedding/LLM」才会真实调用一次模型
  （实测 bge-m3 164 ms、qwen2.5:7b-instruct 238 ms），此时才显示正常。

## 2. Agent 问答（截图 `02-agent-chat.svg`）

按 `1` 进入 **Agent Chat**，输入（Enter 换行，`Ctrl+S` 发送）：

```
2020-04-17 当天电机电流是否异常？结合公开技术资料分析可能原因。
```

实测执行（真实 LangGraph + 真实数据库 + 真实 RAG + 真实 Ollama）：

| 指标 | 实测值 |
| --- | --- |
| 执行状态 | `completed` |
| 意图 | `mixed_analysis` |
| 回答方式 | `llm`（接地校验通过） |
| 服务端耗时 | 8.5 s（本次；本机 3–9 s 波动） |
| 工具调用 | `get_equipment_info` ok(31 ms) → `get_sensor_statistics` ok(13 ms) → `get_data_quality` ok(9 ms) → `get_fault_events` **empty**(5 ms) → `search_technical_knowledge` ok(948 ms) → `get_sensor_statistics` ok(10 ms) |
| 证据 | 15 条（D1…／K1…） |
| trace_id | `ee3fec40353e4353af8eac8a6c73c832` |

回答的关键数据（真实值）：Motor_current 在 `[2020-04-17T00:00:00, 2020-04-18T00:00:00)`
内 6,301 个样本，min 0.035 / max 6.36 / mean 0.4512 / 样本标准差 1.302；
同窗口数据质量：实际 6,301 / 应有 8,640（样本覆盖率 0.7293）、缺口 2 处合计 14,594 秒、
冻结候选 4,378 行（69.48%，**不等于故障**）。

界面上可以：

- 展开「工具调用明细」看每次调用的入参 JSON 与耗时；
- 展开「证据」看数据事实来源与文档逐字摘录（含 doc_key / 页码 / chunk_id）；
- 点「复制 Trace ID」「复制回答」；点「打开 Trace」「发起审计」跳到对应页面。

## 3. Trace Explorer（截图 `03-trace-explorer.svg`）

「打开 Trace」或按 `4` 后输入 trace_id。实测事件序列（13 条）：

```
E0001 run_start      E0003 intent  mixed_analysis      E0005 plan
E0002 node_start     E0004 node_end analyze            E0006-E0013 tool_call/tool_result ×6, decision, answer, evidence, run_end
```

- 左侧表格选中事件，右侧显示入参/输出摘要（白名单字段，无连接串）。
- 顶部显示来源 `file`、schema `phase3-trace-v2`、证据清单 `agent-evidence-v1`。
- 若为旧版 Trace（缺 evidence manifest），这里会明确标注「缺少 evidence manifest」，
  并说明依赖证据的规则会返回 UNKNOWN。
- 可用「最近审计」下拉直接选择历史 trace。

## 4. Rule Only 审计（截图 `04-audit-rule-only.svg`）

按 `5` 进入 **Audit Reports**（默认 Rule Only），trace_id 已自动带入，点「发起审计」。实测：

```
审计 ID：aud-dc2217e2f0d5423759338921 · 状态 completed · 总体风险 none
规则结论：11 条全部 PASS（0 FAIL / 0 UNKNOWN / 0 N/A）
耗时：约 4 ms（确定性规则，无 LLM 调用）
```

界面同时给出每条规则的说明（工具白名单、参数合法性、预算、重复调用、时间窗、设备、
数据质量、数值与统计口径、知识引用、执行链、状态一致性）。

## 5. Hybrid 审计（截图 `05-audit-hybrid.svg`）

同一个 trace 切到 **Hybrid** 再跑一次。实测：

```
审计 ID：aud-ffc037fc52e844ed1f0a418f · 状态 completed · 总体风险 none
规则 16 条：15 PASS / 0 FAIL / 0 UNKNOWN / 1 N/A
耗时：24.98 s（本机 7B 模型；以往实测 17–25 s）
Judge：J_TASK_ALIGNMENT pass、J_EVIDENCE_SUPPORT pass、J_OVERREACH pass、
       J_INJECTION_INFLUENCE not_applicable、J_FALSE_REFUSAL pass
```

演示要点：审计执行期间按 `6` 切到状态页再切回来，界面全程可操作
（LLM Judge 在异步 Worker 中执行，不阻塞事件循环）。

## 6. 历史回放：已有异常发现的 Trace（截图 `06-audit-historical.svg`）

**这一段是历史回放，不是刚刚执行的结果。** 使用阶段 4 评估时的真实 Trace
`32885113cbb04d3f92c15fb134d239ce`（问题「电机电流异常升高通常有哪些原因？」）：

```
已有审计：aud-a9f9457010fbfd1e964a0a5b · rule_only · 风险 unknown · 发现 1
发现：R8_KNOWLEDGE_CITATION [UNKNOWN / medium]
      知识检索返回了片段，但回答没有任何引用或知识结论，无法判断是合理拒答还是遗漏证据
```

演示要点：

- 该 Trace 的执行状态是 `insufficient_evidence`，审计结论是 **UNKNOWN**，
  界面显示为黄色 UNKNOWN 并归入「缺少证据，不能视为通过」小节——绝不显示成 PASS；
- 如需人工复核：选中该发现 → 「确认复核 / 驳回复核」→ 确认弹窗 →
  `PATCH /api/v1/audit/findings/{id}/review` → 界面重新读取服务端持久化结果。

## 7. Knowledge 检索（截图 `07-knowledge.svg`）

按 `3`，查询「电机电流异常 与 储气压力 故障排查」，方法 Hybrid，Top-5。实测：

```
1. doe-compressed-air-sourcebook p60-61 scope=generic_compressor score=0.01639 chunk=351
2. doe-compressed-air-sourcebook p59-59 scope=generic_compressor score=0.01613 chunk=339
3. metropt3-data-description     p2-2   scope=metropt3_direct     score=0.01587 chunk=9
4. metropt-dataset-paper         p5-5   scope=metropt3_direct     score=0.01562 chunk=33
5. doe-compressed-air-sourcebook p37-37 scope=generic_compressor score=0.01538 chunk=218
```

演示要点：选中第 1 条时，详情区明确写「这是同类设备的通用资料，不是 MetroPT-3 实测参数」；
`score` 标注为 RRF 分数，不能解读为概率。

## 8. Equipment 设备数据（截图 `08-equipment.svg`）

按 `2`，选择设备 `metropt3-apu-01`，时间窗 `2020-04-17T00:00:00` → `2020-04-18T00:00:00`，
传感器 `motor_current,dv_pressure,tp2`。实测统计（数据库内聚合，无插值）：

```
tp2          | 模拟量 | 6301 | -0.03  | 10.528 | 0.1941 | 1.3732
dv_pressure  | 模拟量 | 6301 | -0.028 | 6.112  | -0.0223 | 0.082
motor_current| 模拟量 | 6301 | 0.035  | 6.36   | 0.4512 | 1.302
```

质量页：实际 6,301 / 应有 8,640（覆盖率 0.729）、缺口 2 处合计 4.05 h、最大缺口 3.91 h、
估计缺失样本 1,457（估计值）、冻结候选 4,378 行（69.48%，**不等于设备故障**）。
采样页只展示前 240 条真实观测的 Sparkline（`limit=240`，接口硬上限 1000）。

## 9. 一键复现

```bash
python scripts/demo_phase5.py                 # 完整演示（含 Hybrid，约 30–45 s）
python scripts/demo_phase5.py --skip-hybrid   # 跳过 LLM 审计（约 10 s）
```

脚本会重新执行上述全部步骤（真实后端），并把每一步的真实渲染保存为 SVG。
本次运行的完整日志见 `docs/figures/phase5/demo_transcript.txt`。

## 10. 演示中的诚实性检查清单

| 检查点 | 表现 |
| --- | --- |
| 主库 schema 不一致 | 状态显示 `degraded` + 具体版本号，不显示 `ready` |
| LLM 未探测 | 显示「未探测」，不显示「正常」 |
| 审计 UNKNOWN | 显示 UNKNOWN（黄），单独列出，绝不显示 PASS |
| 历史回放 | 明确标注「历史回放」，不冒称实时结果 |
| 通用资料 | 标注「同类设备通用资料」，不当成本设备参数 |
| 冻结候选 | 标注「不等于设备故障」 |
| 缺失数据 | Sparkline 用空格占位并提示覆盖率，不插值 |
| 取消等待 | 明确说明「服务端任务未被取消」 |
