# Agent 工具契约（阶段 3）

- 代码：`backend/app/agent/tools/`
- 注册表：`backend/app/agent/tools/registry.py`（6 个工具，全部只读）
- 参考实现：数据工具包装阶段 1C Service，知识工具包装阶段 2 检索服务

## 0. 通用规则

| 规则 | 说明 |
| --- | --- |
| 只读 | 会话连接参数 `default_transaction_read_only=on`；工具实现不包含任何 `INSERT/UPDATE/DELETE/DDL` |
| 无 SQL | 工具只调用 Service 函数；Agent 代码里没有手写 SQL |
| 参数校验 | 每个工具一个 Pydantic 模型（`extra="forbid"`），设备编号/时间/传感器名都有白名单正则或列表 |
| 返回量上限 | 历史 200 行、故障 20 条、知识 8 段、传感器 15 路 |
| 来源保留 | 每次返回都带 `source`（Service 名、表名、时间窗、`read_only=True`） |
| 空结果 vs 失败 | 合法查询无数据 → `status="empty"`（不是错误）；异常 → 抛错并映射成 `error_category` |
| 超时 | 线程级硬超时（按工具类型配置）+ 数据库 `statement_timeout=45s` |
| 重试 | 仅 `timeout` / `transient` 类别，次数由 `AGENT_TOOL_MAX_RETRIES` 控制 |

工具注册表结构（`ToolSpec`）：`name / category / description / input_model / impl /
requires_equipment / max_return_items / timeout_s / max_retries / read_only`。

调用路径：`run_tool(spec, params, ctx)` → Pydantic 校验 → 线程池执行（超时） → 异常分类 →
`ToolOutcome(status, data, evidence, attempts, elapsed_ms, error)`。

---

## 1. `get_equipment_info`

用途：确认设备存在、给出观测时间跨度（异常判断的上下文，也用于“缺少时间窗”时提示可用范围）。

| 项 | 内容 |
| --- | --- |
| 输入 | `equipment_code: str`（`^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$`） |
| 复用 | `services.equipment_info.get_equipment_info`（表 `equipment`） |
| 输出 | `equipment{equipment_code, equipment_name, equipment_type, equipment_id, description, first_observed_at, last_observed_at}`、`source` |
| 失败 | 设备不存在 → `not_found`（阻断后续依赖设备的工具） |
| 超时 | `AGENT_DATA_TOOL_TIMEOUT_S`（默认 20 s） |

## 2. `get_sensor_history`

用途：查看原始观测明细（限行数），用于“某时刻读数/明细”类问题。

| 项 | 内容 |
| --- | --- |
| 输入 | `equipment_code`、`start_time`、`end_time`（无时区 ISO 8601）、`sensors?`、`limit`（1–200，默认 100）、`offset`（≤10000）、`exclude_frozen`（默认 false） |
| 复用 | `services.sensor_history.get_sensor_history`（表 `sensor_readings`） |
| 输出 | `equipment`、`window`、`sensors`、`total`、`returned`、`rows[{observed_at, values, quality_labels, frozen_candidate, source_row_number}]`、`has_more`、`source` |
| 空结果 | 窗口内无记录 → `empty` |
| 说明 | 时间窗左闭右开 `[start, end)`；默认**不排除**冻结候选行（不静默丢数据） |

## 3. `get_sensor_statistics`

用途：数值问题的主力工具（最大/最小/均值/标准差/0-1 计数/状态切换）。

| 项 | 内容 |
| --- | --- |
| 输入 | `equipment_code`、`start_time`、`end_time`、`sensors?`（15 路白名单）、`exclude_frozen`（默认 false） |
| 复用 | `services.sensor_statistics.get_sensor_statistics`（聚合在数据库内完成） |
| 输出 | `equipment`、`window`、`sensors`、`row_count`、`nominal_step_seconds`、`exclude_frozen`、`analog[{sensor,count,min,max,mean,std,std_method,first_timestamp,last_timestamp}]`、`digital[{sensor,count,count_0,count_1,ratio_0,ratio_1,transitions}]` |
| 统计口径 | 模拟量 `std` 为样本标准差（`stddev_samp`，ddof=1）；数字量 `transitions` 为相邻行 0/1 变化次数；无插值、无异常剔除 |
| 空结果 | `row_count=0` → `empty`（随后 Agent 会补一次质量查询解释原因） |
| 超时 | `AGENT_STATISTICS_TIMEOUT_S`（默认 40 s） |

## 4. `get_fault_events`

用途：官方登记故障事件（区间级事实）。

| 项 | 内容 |
| --- | --- |
| 输入 | `equipment_code`、`start_time?`、`end_time?`（同时省略表示不设边界）、`limit`（1–20） |
| 复用 | `services.fault_events.get_fault_events`（表 `fault_events` + `data_sources`） |
| 输出 | `total`、`events[{event_code, pdf_label, start_time, end_time, duration_seconds, overlap_seconds, failure_type, severity, fault_description, maintenance_note, source_reference, verification_status, verification_note, source{...}}]`、`sample_label_semantics` |
| 语义 | 区间相交判定 `event.start < window.end and event.end > window.start`；区间内读数**不等于**已确认故障样本 |
| 空结果 | 无相交事件 → `empty`（数据事实会明确写“0 条不等于无故障”） |

## 5. `get_data_quality`

用途：判断数据可用性（覆盖率、缺口、冻结候选、窗口是否在观测范围内）。

| 项 | 内容 |
| --- | --- |
| 输入 | `equipment_code`、`start_time`、`end_time` |
| 复用 | `services.data_quality.get_data_quality`（`sensor_coverage` 数据库函数） |
| 输出 | `actual_samples`、`expected_samples`、`sample_coverage`（不封顶）、`time_coverage`、`gap_count`、`gap_duration_seconds`、`max_gap_seconds`、`missing_samples_estimate`（估计值）、`frozen_candidate_count`、`frozen_candidate_ratio`、`records_with_quality_flags`、`window_inside_data_range`、`warnings[{code,severity,message}]` |
| 语义 | 冻结候选只表示 7 路模拟量与上一条记录逐位相同，**不能**认定为设备故障或停机 |
| 空结果 | `actual_samples=0` → `empty` |

## 6. `search_technical_knowledge`

用途：技术知识检索（原始片段，不生成回答）。

| 项 | 内容 |
| --- | --- |
| 输入 | `query`（2–300 字符）、`top_k`（1–8，默认 5）、`method`（`hybrid`/`vector`/`keyword`）、`doc_keys?`（≤10，字符集受限）、`equipment_scopes?`（4 个枚举）、`min_similarity?`（0–1） |
| 复用 | `services.knowledge.search_knowledge` → `KnowledgeRetriever`（pgvector + 关键词 + RRF），**不**调用 `ask_knowledge`，不重建索引、不重新生成向量 |
| 输出 | `query`、`method`、`top_k`、`returned`、`chunks[{chunk_id, document_id, doc_key, document_title, page_start, page_end, section_title, section_path, equipment_scope, doc_type, trust_level, source_url, license, chunking_version, embedding_model, citation_label, score, vector_similarity, keyword_score, rrf_score, matched_terms, content_chars, excerpt}]`、`retrieval{elapsed_ms, embedding_elapsed_ms, candidate_counts, filters, warnings}`、`source{engine, embedding_model, embedding_version, chunking_version}` |
| 过滤 | 默认只检索 `metropt3_direct` + `generic_compressor`；`model_specific_other` / `unknown` 默认不参与 |
| 失败 | Embedding/检索不可用 → `transient`；索引版本不匹配 → `config_error`（阻断） |
| 超时 | `AGENT_KNOWLEDGE_TOOL_TIMEOUT_S`（默认 60 s） |

---

## 7. 证据对象

数据工具返回 `EvidenceItem(kind="data", origin_tool, source, detail)`；
知识工具每个片段返回 `EvidenceItem(kind="knowledge", source{doc_key, chunk_id, page...}, detail{score...}, quote==excerpt)`。

在回答阶段，它们被整理成带标签的事实清单：

```json
{
  "label": "D2",
  "kind": "data",
  "fact_type": "statistics_analog",
  "text": "Motor_current（电机电流） 在窗口内 6301 个样本，最小 0.035，最大 6.36，均值 0.4512，样本标准差 1.302（stddev_samp，ddof=1）…",
  "source": {"tool": "get_sensor_statistics", "service": "services.sensor_statistics.get_sensor_statistics",
             "table": "sensor_readings", "window": {"start": "...", "end": "..."}, "read_only": true}
}
```

```json
{
  "label": "K1",
  "kind": "knowledge",
  "chunk_id": 42,
  "document_id": "...",
  "doc_key": "metropt3-data-description",
  "document_title": "MetroPT-3 Dataset — Data Description",
  "page_start": 2,
  "page_end": 2,
  "equipment_scope": "metropt3_direct",
  "trust_level": "official_primary",
  "score": 0.0328,
  "quote": "TP2 (bar) – the measure of the pressure on the compressor."
}
```

## 8. 错误码与错误类别对照

| 工具层 code | 类别 | HTTP（若透传到 API） |
| --- | --- | --- |
| `equipment_not_found` | `not_found` | 404 |
| `invalid_parameter` / `invalid_time_range` / `timezone_not_supported` / `unsupported_sensor` / `window_too_large` / `tool_invalid_input` | `invalid_input` | 422 |
| `tool_timeout` / `llm_timeout` | `timeout` | 504 |
| `embedding_error` / `llm_unavailable` / `llm_rate_limited` / `retrieval_error` / `database_unavailable` | `transient` | 503 |
| `index_model_mismatch` / `embedding_dimension_mismatch` | `config_error` | 503 |
| 其它 | `internal` / `service_error` | 500 |

注意：Agent 的 `/chat` 接口在工具失败时通常仍返回 **200**，把失败体现在
`execution_status` / `errors` / `warnings` 里；只有请求本身非法（参数、时区）才返回 4xx。

## 9. 调用示例（Python，不经 HTTP）

```python
from app.agent.config import get_agent_settings
from app.agent.context import AgentContext
from app.agent.tools.base import run_tool
from app.agent.tools.registry import build_registry, get_tool_spec

ctx = AgentContext(settings=get_agent_settings())
registry = build_registry(ctx.settings)
outcome = run_tool(
    get_tool_spec(registry, "get_sensor_statistics"),
    {"equipment_code": "metropt3-apu-01",
     "start_time": "2020-04-17T00:00:00",
     "end_time": "2020-04-18T00:00:00",
     "sensors": ["motor_current"]},
    ctx,
)
print(outcome.status, outcome.data["analog"][0]["max"])
ctx.close()
```

## 10. 不允许的操作（工具层强制）

| 请求 | 结果 |
| --- | --- |
| 客户端提交工具名/SQL | HTTP 422（`extra="forbid"`），Service 层也没有入口 |
| `equipment_code` 带引号或分号 | Pydantic 正则拒绝（`invalid_input`） |
| 未知传感器名 | Pydantic 白名单拒绝 |
| 超出 366 天的时间窗 | Service 抛 `window_too_large` |
| 带时区的时间 | Service 抛 `timezone_not_supported`（HTTP 422） |
| 任何写入 | 事务只读，数据库拒绝（`ReadOnlySqlTransaction`） |
