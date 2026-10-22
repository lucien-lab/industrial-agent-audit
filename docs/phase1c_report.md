# Phase 1C 报告：FastAPI 工业设备数据查询服务

- 执行日期：2026-10-22
- 范围：**只读查询服务**（Router → Service → Repository → PostgreSQL）+ 接口契约 + 测试与文档
- 明确不包含：RAG 知识库、Agent / LangGraph / Tool Calling、Agent-Probe 审计、前端
- 生产库状态：**仍为 `0004_coverage_missing_time`，本阶段未做任何迁移或写入**（只读核验见 §1.3）

---

## 1. 环境与安全边界

### 1.1 运行时

| 组件 | 版本（实测） |
| --- | --- |
| Python | 3.11.15（Conda `ms_pointcloud_midterm`） |
| FastAPI | 0.141.1 |
| Starlette | 1.9.0 |
| Uvicorn | 0.54.0 |
| Pydantic | 2.13.4 |
| SQLAlchemy | 2.1.6 |
| psycopg | 3.3.4 |
| PostgreSQL | 17.11（Docker，pgvector 0.8.7） |
| pytest / pytest-cov | 9.1.1 / 覆盖率 96% |

依赖通过 `conda install -n ms_pointcloud_midterm -c conda-forge fastapi uvicorn` 安装，
版本记录在 `backend/requirements.txt`。

### 1.2 本阶段使用的三个数据库（互不串用）

| 库 | 版本 | 数据 | 用途 |
| --- | --- | --- | --- |
| `industrial_agent_audit`（主库/生产） | `0004` **（未改动）** | 1,516,948 行 | 仅只读核验，未连接写入 |
| `iaa_rehearsal2_test`（阶段 1B 全量副本） | `0006` | 1,516,948 行 | 真实数据只读验证 + 性能测试 |
| `industrial_agent_audit_test`（测试库） | 迁移到 `head`（conftest 自动执行） | 每个用例先 TRUNCATE，再导入真实 CSV 头部样本 | 单元测试与 API 集成测试 |

数据准备方式：**没有重新导入全量数据、没有清空任何库**。测试数据来自真实 CSV 头部
（400 行）与官方 `fault_intervals.csv`（4 条故障事件），以及一个人工构造的小型规则数据集
（13 行，用于精确断言缺口/冻结边界）。

**兼容性结论**：API 只依赖 `equipment`、`sensor_readings`、`fault_events`、`data_gaps` 与
函数 `sensor_coverage`，这些对象在 `0004` 与 `0006` 中均存在，因此 API 在主库当前版本
（`0004`）上同样可只读运行；`/ready` 会如实报告 `schema_ok=false`（版本不一致）而不拒绝服务。

### 1.3 未写作保证

- 全库内容指纹（17 字段 md5）在 API 调用前后**完全一致**：`f40c3ace26caedd77d9931fac8d2bfcc`
  （测试 `test_api_calls_do_not_modify_raw_data` 每次运行都会比对）。
- 服务层所有事务在请求结束时 `rollback`，本服务不提供任何写接口。
- 主库只读核验：`scripts/migration_audit.py --compare` → **数据一致性 PASS / 结构变更：无**。

---

## 2. FastAPI 架构说明

### 2.1 分层与文件

```
backend/app/
├── main.py                     # ASGI 入口：uvicorn app.main:app
├── api/
│   ├── app.py                  # 应用工厂：中间件、异常处理器、路由注册、lifespan 释放连接池
│   ├── app_meta.py             # 服务名/版本/OpenAPI 描述与标签
│   ├── deps.py                 # 请求级 Session 依赖 + 查询参数声明（含示例与边界）
│   ├── errors.py               # 统一异常 → JSON 错误结构（404/422/503/500）
│   ├── serializers.py          # dataclass → Pydantic Schema
│   └── routes/                 # health / equipment / readings / stats / faults / quality
├── schemas/                    # common.py（错误、分页、窗口）+ responses.py（各接口响应）
└── services/
    ├── queries.py              # Repository：SQLAlchemy 查询与聚合（阶段 1B 起沿用，本阶段扩展）
    ├── catalog.py              # 传感器白名单、时间窗解析、分页校验（无 HTTP 依赖）
    ├── equipment_info.py       # get_equipment_info / list_equipment_infos
    ├── sensor_history.py       # get_sensor_history
    ├── sensor_statistics.py    # get_sensor_statistics
    ├── fault_events.py         # get_fault_events
    ├── data_quality.py         # get_data_quality
    └── errors.py               # 领域异常（不与 HTTP 状态码耦合）
```

**复用而非重写**：Repository 层沿用阶段 1B 的 `queries.py`（同一套 SQL 与索引假设），
本阶段只新增「数字量统计」「标准差/首末时间」「冻结候选计数」「缺口重叠统计」等
阶段 1B 没有的查询，并把半开区间语义集中在一处实现。

### 2.2 关键设计决策

| 决策 | 说明 |
| --- | --- |
| 半开区间的实现 | `[start, end)` ≡ `[start, end − 1µs]`；依据 DB CHECK `observed_at = (source_timestamp_raw)::timestamp` 保证时间戳精确到秒（`queries.half_open_upper_bound()`），因此直接复用阶段 1B 的闭区间查询，不重写 SQL |
| 时间无时区 | 参数用正则白名单校验；`Z` / `+08:00` 一律 **422 `timezone_not_supported`**，绝不静默换算成 UTC |
| 传感器白名单 | 15 个列名（7 模拟量 + 8 数字量）；任何其它字符串（含注入尝试）→ 422，杜绝任意列名/SQL |
| 分页硬上限 | `limit` 默认 100、上限 1000（API 层），叠加 Repository 的 5000 上限，取更严格者；超限 422 而不是静默截断 |
| 时间窗上限 | 366 天（数据集跨度 213 天），超出 → 422 `window_too_large`，防止无界聚合 |
| 聚合位置 | 全部在 PostgreSQL 内完成（`min/max/avg/stddev_samp`、`count(*) FILTER`、`lag() OVER`、`sensor_coverage`），Python 只做装箱 |
| 会话管理 | 请求级 Session，结束统一 `rollback` + `close`；引擎按 URL 缓存，进程退出时 `dispose()` |
| 错误安全 | 响应只含 `code/message/details/request_id`；SQL、连接串、密码、堆栈只出现在服务端日志 |
| Agent-ready | 5 个 Service 函数以 `Session` 为第一参数、返回 dataclass、不 import FastAPI；Router 只做装配 |

### 2.3 错误码与状态码

| HTTP | code | 触发条件 |
| --- | --- | --- |
| 404 | `equipment_not_found` | 设备编号不存在 |
| 422 | `invalid_parameter` | 参数缺失/类型错误/超上限/格式非法（含 FastAPI 自身校验失败） |
| 422 | `invalid_time_range` | `end_time <= start_time` |
| 422 | `timezone_not_supported` | 时间带偏移（`Z`、`+08:00`、`-0500`） |
| 422 | `unsupported_sensor` | 传感器名不在白名单 |
| 422 | `window_too_large` | 时间窗 > 366 天 |
| 503 | `database_unavailable` | 连接被拒绝/中断（附 `Retry-After: 5`） |
| 500 | `internal_error` | 其它异常（不回显内部细节） |

---

## 3. 接口清单

| # | 方法与路径 | 说明 | 主要参数 | 错误 |
| --- | --- | --- | --- | --- |
| 1 | `GET /health` | 进程存活，不访问数据库 | — | — |
| 2 | `GET /ready` | 数据库连通性、pgvector、Alembic 版本 | — | 503 |
| 3 | `GET /api/v1/equipment` | 设备列表 + 观测跨度 | `limit`,`offset` | 422 |
| 4 | `GET /api/v1/equipment/{equipment_id}` | 设备详情 | — | 404 |
| 5 | `GET /api/v1/equipment/{equipment_id}/readings` | 历史观测分页 | `start_time`,`end_time`,`sensor`,`limit`,`offset`,`exclude_frozen` | 404/422 |
| 6 | `GET /api/v1/equipment/{equipment_id}/stats` | 模拟量/数字量统计 | `start_time`,`end_time`,`sensor`,`exclude_frozen` | 404/422 |
| 7 | `GET /api/v1/equipment/{equipment_id}/faults` | 官方故障事件（窗口重叠） | `start_time?`,`end_time?`,`limit`,`offset` | 404/422 |
| 8 | `GET /api/v1/equipment/{equipment_id}/quality` | 采样质量与覆盖率 | `start_time`,`end_time` | 404/422 |

Swagger：`/docs`；OpenAPI：`/openapi.json`（39 KB，含每个接口的说明、参数示例、错误响应与响应示例）。
所有接口在 Swagger 中带 `Try it out` 可用（真实数据环境实测可调试）。

### 3.1 指标口径（写在响应里，可供下游机器读取）

| 指标 | 定义 |
| --- | --- |
| `actual_samples` | 窗口内真实存在的观测行数 |
| `expected_samples` | `ceil(窗口秒数 / 10)`，10 s 为标称采样间隔 |
| `sample_coverage` | `actual / expected`，**不封顶**（>1 表示比标称更密，会给提示） |
| `time_coverage` | `1 − 估计缺失秒数 / 窗口秒数`（缺失按 `missing_samples × 10 s × 窗口重叠比例`） |
| `gap_count` / `gap_duration_seconds` | 与窗口相交的缺口数 / 缺口落在窗口内的实际秒数 |
| `missing_samples_estimate` | 估计缺失样本数（四舍五入 half-up，**估计值**，响应中有固定提示） |
| `frozen_candidate_count` | 冻结候选行数（**不等于设备故障**，响应中有固定提示） |
| `std` | `stddev_samp`（样本标准差，ddof=1，与 pandas 默认一致），`count<2` 为 null |
| `transitions` | 窗口内相邻行之间 0/1 状态切换次数（窗口首行不计） |

---

## 4. 关键请求与响应示例

以下内容均为在**全量真实数据副本**上的真实响应（截取）。

### 4.1 历史观测（含质量位语义）

```http
GET /api/v1/equipment/metropt3-apu-01/readings
    ?start_time=2020-04-18T00:00:00&end_time=2020-04-18T00:02:00&sensor=tp2&sensor=comp&limit=1
```

```json
{
  "window": {"start": "2020-04-18T00:00:00", "end": "2020-04-18T00:02:00",
             "duration_seconds": 120.0, "boundary": "[start, end)",
             "timezone": "naive (source timestamps carry no offset)"},
  "sensors": ["tp2", "comp"],
  "items": [{
    "id": 582565,
    "observed_at": "2020-04-18T00:00:01",
    "source_row_number": 5625640,
    "source_id": "0b2cfc5f-4262-4305-b6a2-0ae2e72b4275",
    "quality_flags": 1,
    "quality_labels": ["frozen_candidate"],
    "frozen_candidate": true,
    "values": {"tp2": -0.018, "comp": 1}
  }],
  "pagination": {"limit": 1, "offset": 0, "total": 10, "returned": 1, "has_more": true}
}
```

### 4.2 统计（模拟量与数字量语义分开）

```http
GET .../stats?start_time=2020-04-18T00:00:00&end_time=2020-04-18T01:00:00&sensor=tp2&sensor=comp
```

```json
{
  "row_count": 309,
  "nominal_step_seconds": 10.0,
  "analog": [{
    "sensor": "tp2", "kind": "analog", "count": 309,
    "min": -0.018, "max": 9.104, "mean": 6.000058252427184,
    "std": 3.940599797344232,
    "std_method": "stddev_samp (sample standard deviation, ddof=1)",
    "first_timestamp": "2020-04-18T00:00:01", "last_timestamp": "2020-04-18T00:59:50"
  }],
  "digital": [{
    "sensor": "comp", "kind": "digital", "count": 309,
    "count_0": 238, "count_1": 71,
    "ratio_0": 0.7702265372168284, "ratio_1": 0.2297734627831715,
    "transitions": 33,
    "first_timestamp": "2020-04-18T00:00:01", "last_timestamp": "2020-04-18T00:59:50"
  }]
}
```

### 4.3 官方故障事件（区间级事实 + 来源）

```http
GET .../faults?start_time=2020-04-18T00:00:00&end_time=2020-04-19T00:00:00
```

```json
{
  "pagination": {"limit": 100, "offset": 0, "total": 1, "returned": 1, "has_more": false},
  "sample_label_semantics": "故障事件是区间级事实；区间内的传感器记录不会被自动视为已确认故障样本，本项目不在 sensor_readings 上生成逐行故障标签。",
  "items": [{
    "event_code": "E1", "pdf_label": "#1",
    "start_time": "2020-04-18T00:00:00", "end_time": "2020-04-18T23:59:00",
    "duration_seconds": 86340.0,
    "overlap_start": "2020-04-18T00:00:00", "overlap_end": "2020-04-18T23:59:00",
    "overlap_seconds": 86340.0,
    "failure_type": "Air leak", "severity": "High stress",
    "verification_status": "verified",
    "source": {"source_file": "Data Description_Metro.pdf", "license": "CC BY 4.0",
               "doi": "10.24432/C5VW3R", "timezone_status": "unknown"}
  }]
}
```

### 4.4 数据质量（E1 当天，真实数据）

```http
GET .../quality?start_time=2020-04-18T00:00:00&end_time=2020-04-19T00:00:00
```

```json
{
  "nominal_step_seconds": 10.0,
  "actual_samples": 8663,
  "expected_samples": 8640,
  "sample_coverage": 1.002662037037037,
  "time_coverage": 0.9960648148147693,
  "gap_count": 1,
  "gap_duration_seconds": 352.0,
  "estimated_missing_seconds": 340.0,
  "missing_samples_estimate": 34,
  "max_gap_seconds": 352.0,
  "frozen_candidate_count": 91,
  "frozen_candidate_ratio": 0.010504444187925661,
  "records_with_quality_flags": 91,
  "window_inside_data_range": true,
  "warnings": [
    {"code": "missing_samples_estimated", "severity": "info", "message": "…估计值…"},
    {"code": "sample_coverage_above_one", "severity": "info", "message": "样本覆盖率 1.0027 > 1…"}
  ]
}
```

> `sample_coverage` 略大于 1 是**如实结果**（当天 8,663 行 > 8,640 个标称样本），
> 未做封顶处理；`time_coverage` 用 Phase 1B 的缺口加权口径，两者含义不同，不可混用。

### 4.5 错误示例

```json
{"error": {"code": "timezone_not_supported",
           "message": "start_time='2020-04-18T00:00:00+08:00' 带有时间偏移；本服务只接受无时区时间…",
           "details": {"field": "start_time"},
           "request_id": "3ee2d6488e844c8d8d968d18594afb49"}}
```

---

## 5. 测试结果

```
cd backend && python -m pytest --cov=app
→ 193 passed，0 failed，总覆盖率 96%（阶段 1B 为 110 / 92%）
```

### 5.1 测试文件与覆盖点

| 文件 | 覆盖内容 | 用例数 |
| --- | --- | --- |
| `tests/test_api_health.py` | `/health`（含数据库故障时仍存活）、`/ready`（含 503） | 4 |
| `tests/test_api_equipment.py` | 列表、详情、404、分页边界 | 5 |
| `tests/test_api_readings.py` | 时间窗、左闭右开边界、空窗口、分页无重漏、默认/最大 `limit`、非法时间、时区、窗口上限、非法传感器、SQL 注入、冻结过滤 | 17 |
| `tests/test_api_stats.py` | 模拟量 min/max/mean/std、数字量 0/1 与切换次数、传感器选择、空窗口、冻结排除 | 7 |
| `tests/test_api_faults.py` | 无窗口、包含、部分重叠、首尾相接不重叠、无重叠、分页、成对参数、时区 | 9 |
| `tests/test_api_quality.py` | 全窗口指标、窗口落在缺口内裁剪、窗口在数据范围内外、无缺口窗口、参数校验 | 7 |
| `tests/test_api_errors.py` | 错误结构、`request_id` 回显、503、500 脱敏、连接释放、OpenAPI 文档、Swagger 页面、ASGI 入口 | 9 |
| `tests/test_api_services.py` | 时间解析白名单、时区拒绝、窗口校验、传感器归一化、分页边界 | 13 |
| `tests/test_api_real_data.py` | 全量 1,516,948 行只读验证（行数、故障、统计对齐 SQL、质量、指纹不变） | 7 |
| 既有 1B 测试 | 导入、幂等、约束、取整、脚本、查询等 | 110（**无回归**） |

### 5.2 真实数据只读验证（`tests/test_api_real_data.py`）

需要全量库（`IAA_FULLDATA_DATABASE_URL`，本次指向 `iaa_rehearsal2_test`），未设置则整模块跳过：

- `sensor_readings` = 1,516,948、`fault_events` = 4、`data_gaps` = 332；
- `/readings` 全窗口 `pagination.total` = 1,516,948；
- 设备观测跨度 = `2020-02-01T00:00:00` ~ `2020-09-01T03:59:50`；
- 4 条官方故障事件（E1 起点、E2 `typo_suspected`、failure_type 全为 Air leak）；
- E1 当天 `stats` 与直接 SQL（`count/min/max/avg/stddev_samp/FILTER`）逐项对齐，`std` 相对误差 < 1e-12；
- 全窗口 `quality`：`actual=1,516,948`、`sample_coverage≈0.824`、`gap_count≥300`、`frozen>0`；
- **API 调用前后内容指纹一致**（`f40c3ace…`）——证明只读。

### 5.3 未使用假数据

- 结构性断言用真实 CSV 头部（400 行）与官方故障事件；
- 缺口/冻结的精确数值断言用人工构造的 13 行数据集（明确标注为规则测试数据，不作为任何业务统计依据）；
- 性能与规模断言全部来自全量真实数据副本。

---

## 6. 数据查询正确性

| 校验点 | 方法 | 结果 |
| --- | --- | --- |
| 左闭右开边界 | 真实数据 00:00:00/:10/:19/:29 序列；`end=00:00:39` 排除 :39，`end=00:00:40` 包含 | 通过 |
| 分页无重漏 | 3 页（150+150+100）拼合 = 一次取回 400 行，id 严格递增无重复 | 通过 |
| 分页 total 一致 | 每页 `pagination.total` 相同，`has_more` 正确 | 通过 |
| 模拟量统计 | 与 SQL `min/max/avg/stddev_samp` 对齐；小数据集与 `statistics.stdev` 一致 | 通过 |
| 数字量统计 | 已知序列 `1×5 → 0×6 → 1×2` ⇒ `transitions=2`、`count_0=6`、`count_1=7` | 通过 |
| 缺口/覆盖率 | 构造 400 s 缺口：`missing_samples=39`、`time_coverage=0.35`；窗口落在缺口内部时按 100/400 裁剪（`estimated=97.5 s`、`time_coverage=0.025`） | 通过 |
| 冻结候选 | 构造 4 行逐位相同 ⇒ `frozen_candidate_count=4`；`exclude_frozen=true` 后行数 13→9 且数值不变 | 通过 |
| 故障重叠 | 包含/部分覆盖/首尾相接/无交集四种情形数值正确（首尾相接不计重叠） | 通过 |
| 无数据窗口 | 2030 年窗口返回空列表 + `total=0`，不报错 | 通过 |
| 数据未被修改 | 全量指纹前后一致 + 注入用例后行数不变 | 通过 |

---

## 7. SQL 性能结果（实测，全量 1,516,948 行副本）

### 7.1 EXPLAIN ANALYZE（`scripts/api_query_bench.py`）

窗口 = 官方故障 E1 当天（24 h，8,663 行）：

| 查询 | 执行时间 | 计划要点 |
| --- | ---: | --- |
| `count_readings`（分页 total） | **0.59 ms** | Index Only Scan `ix_sensor_readings_equipment_observed_at` |
| `readings_between`（列表，limit 100） | **0.05 ms** | Index Scan + Limit（只读 101 行） |
| `analog_statistics`（7 路模拟量一次聚合） | **6.3 ms** | Index Scan 8,663 行 → Aggregate |
| `digital_statistics`（8 路 `lag()` 窗口函数） | **1.8 ms** | Index Scan + WindowAgg |
| `sensor_coverage()`（数据库函数） | **0.73 ms** | Index Only Scan + data_gaps Seq Scan(332 行) |
| `gap_overlap_stats`（缺口裁剪） | **0.03 ms** | data_gaps Seq Scan(1 行命中) |
| `count_frozen_candidates` | **0.78 ms** | Index Scan（未走部分索引：窗口内命中率低时规划器选主索引更快） |
| `list_fault_events` | **0.02 ms** | data_gaps/fault_events 小表 Seq Scan |

全量窗口（213 天，1,516,418 行）：

| 查询 | 执行时间 |
| --- | ---: |
| `count_readings` | 51 ms |
| `readings_between`（limit 100） | 0.06 ms |
| `analog_statistics` | 364 ms |
| `digital_statistics` | 131 ms |
| `sensor_coverage` | 138 ms |
| `count_frozen_candidates` | 53 ms |
| `gap_overlap_stats` / `list_fault_events` | < 0.1 ms |

### 7.2 HTTP 端到端耗时（uvicorn 实测，3 次取范围）

| 请求 | 耗时 |
| --- | ---: |
| `GET /health` | 0.9–1.0 ms |
| `GET /api/v1/equipment` | 3.9–4.4 ms（优化前 47 ms，见下） |
| `/readings` 1 h / limit 100 | 5.5–8.6 ms |
| `/readings` 1 天 / limit 1000 | 14.8–15.3 ms |
| `/readings` 全量 / limit 100 | 33–36 ms |
| `/stats` 1 天（全部 15 路） | 16.4–19.2 ms |
| `/stats` 全量 213 天（全部 15 路） | **1.62 s**（唯一慢场景，已用 366 天上限约束） |
| `/quality` 1 天 | 5.8–8.5 ms |
| `/quality` 全量 | 127–131 ms |
| `/faults` 1 个月 | 4.7–7.1 ms |

### 7.3 本阶段做的两处性能修正

1. **设备列表 47 ms → 4 ms**：原实现用 `GROUP BY` 求每台设备的 `min/max(observed_at)`，在 150 万行上
   要扫完整段索引；改为 `LEFT JOIN LATERAL … ORDER BY observed_at LIMIT 1` 索引两端各取一行。
2. **数字量统计少扫一遍**：首末时间与 0/1 计数合并到同一次窗口函数扫描中。

### 7.4 已知性能边界

- 全量 213 天 `stats`（15 路信号）≈ 1.6 s：全表聚合无法避免，已由 366 天窗口上限约束；
  如未来需要更快，可引入按小时/天预聚合表或物化视图（本阶段明确不做，避免为演示场景过度设计）。
- 深分页 `offset=1,500,000` 实测 217 ms（索引扫描 + 丢弃），属于 PostgreSQL 常规代价，未做 keyset 分页。

---

## 8. 与 PostgreSQL 的集成情况

| 项目 | 结果 |
| --- | --- |
| 连接方式 | psycopg 3 + SQLAlchemy 2.x，引擎按 URL 缓存，`pool_size=5 / max_overflow=5`，`pool_pre_ping=True` |
| 结构管理 | 仍由 Alembic 唯一管理；应用启动**不建表**；本阶段**未新增迁移** |
| 版本兼容 | `0004` 与 `0006` 均可运行；`/ready` 返回 `schema_version` / `schema_expected_version` / `schema_ok` |
| 隔离级别 | 默认 read committed；每个请求一个只读事务，结束 `rollback` |
| 会话释放 | 5 次请求后连接池 `checkedout=0`（测试断言） |
| 索引使用 | 时间窗查询命中 `ix_sensor_readings_equipment_observed_at`；冻结查询使用 `quality_flags <> 0` 部分索引（小窗口时规划器可能选主索引，属正常） |
| 数据库函数 | 复用 `sensor_coverage()`（阶段 1B），半开窗口通过 `end − 1µs` 精确等价实现 |
| 数据库故障表现 | 连接被拒绝 → 503 `database_unavailable` + `Retry-After: 5`；不重试、不隐藏 |
| 写入 | 无任何 INSERT/UPDATE/DELETE；全量指纹前后一致 |

---

## 9. 遗留问题

### 9.1 待批准事项（阻塞生产环境，不影响本阶段交付）

1. 生产库迁移 `0005`/`0006` 与 23 行 `missing_samples` 修订仍待批准（见 Phase 1B 最终验收报告）；
   API 在当前 `0004` 上只读可用，但 `partial` 状态语义与规范化约束名要等迁移完成才对生产生效。
2. `/ready` 在生产库上会返回 `status=degraded / schema_ok=false`（版本 `0004` ≠ `0006`），属预期。

### 9.2 本阶段未实现（明确不在范围）

1. 认证与授权、限流、CORS（当前为只读内网服务，未引入身份体系）。
2. 缓存层（Redis 等）与并发压测：本阶段按要求不做高并发优化。
3. keyset（游标）分页：深分页仍用 `offset`。
4. Agent Tool 封装、LangGraph 编排、Tool Calling 代码——仅提供 5 个可直接复用的 Service 函数。
5. RAG、Agent-Probe、前端。

### 9.3 已知局限

1. `sample_coverage > 1` 会出现在部分窗口（记录比 10 s 标称更密），响应已给出 `sample_coverage_above_one`
   提示，未做截断；若下游需要 0–1 区间需自行处理。
2. `missing_samples_estimate` 是**估计值**（基于标称 10 s 与缺口阈值 30 s），不是精确缺失次数。
3. 冻结候选只是“7 路模拟量逐位相同”的规则标记，不能解读为设备故障或稳态。
4. `fault_events` 是区间级事实，区间内每条记录**不**等于已确认故障样本；本阶段没有逐行标签。
5. 时区仍然未知：API 拒绝带偏移输入，不提供 UTC 视图；未来若确认时区需单独的迁移与契约变更。
6. 全量 `stats`（213 天 × 15 路）1.6 s，超大窗口的慢查询已用 366 天上限兜底。

---

## 10. Phase 1C 验收标准对照

| 验收项 | 证据 | 结果 |
| --- | --- | --- |
| FastAPI 服务正常启动 | `uvicorn app.main:app` 实测启动，`/health` 200 | ✅ |
| Swagger 页面正常显示 | `/docs` 200（含 swagger-ui），`/openapi.json` 200（39 KB） | ✅ |
| 设备查询 API 正确 | 列表/详情来自 `equipment` 表 + 真实观测跨度；未知设备 404 | ✅ |
| 传感器历史数据 API 正确 | 真实记录、列投影、质量位、稳定排序 | ✅ |
| 时间范围查询与分页正确 | 左闭右开边界、3 页无重漏、`total` 一致 | ✅ |
| 模拟量统计正确 | 与 SQL `min/max/avg/stddev_samp` 对齐（含真实数据） | ✅ |
| 数字信号统计正确 | 0/1 计数、占比、`transitions` 由窗口函数计算并与已知序列一致 | ✅ |
| 官方故障事件查询正确 | 4 条真实事件；包含/部分/相接/无交集四种情形验证 | ✅ |
| 数据质量 API 正确 | 复用 Phase 1B 算法；缺口裁剪、窗口内外、冻结计数、提示语 | ✅ |
| 异常请求正确处理 | 404/422（含时区、传感器、窗口、分页）/503/500 全部覆盖，脱敏验证 | ✅ |
| 数据库连接可靠 | `pool_pre_ping`、请求级会话、连接释放断言、`/ready` 探针 | ✅ |
| 返回数据量受到硬性限制 | `limit` 默认 100 / 上限 1000；窗口上限 366 天；超限 422 | ✅ |
| 自动化测试通过 | **193 passed，0 failed，覆盖率 96%** | ✅ |
| 原有 Phase 1B 测试未回归 | 110 项 1B 用例全部通过 | ✅ |
| 数据库原始记录未遭修改 | 全量指纹 `f40c3ace…` 前后一致；无写接口；主库只读核验 PASS | ✅ |
| 文档与实际 API 一致 | 本报告、README、architecture.md、OpenAPI 三处一致（接口/参数/错误码/口径） | ✅ |

**阶段结论：Phase 1C 开发与自测完成，验收标准全部满足。**
未进入 RAG、Agent、Agent-Probe、前端开发；未迁移、未写入生产数据库。

---

## 附录 A：本阶段新增/修改文件

**新增**

| 文件 | 说明 |
| --- | --- |
| `backend/app/api/app.py`, `app_meta.py`, `deps.py`, `errors.py`, `serializers.py` | 应用装配、元信息、依赖、异常处理、序列化 |
| `backend/app/api/routes/{health,equipment,readings,stats,faults,quality}.py` | 6 个路由模块（8 个接口） |
| `backend/app/schemas/{common,responses}.py`, `__init__.py` | 请求/响应 Schema 与示例 |
| `backend/app/services/catalog.py` | 传感器白名单、时间窗解析（拒绝时区）、分页校验 |
| `backend/app/services/errors.py` | 领域异常（与 HTTP 解耦） |
| `backend/app/services/equipment_info.py` | `get_equipment_info` / `list_equipment_infos` |
| `backend/app/services/sensor_history.py` | `get_sensor_history` |
| `backend/app/services/sensor_statistics.py` | `get_sensor_statistics` |
| `backend/app/services/fault_events.py` | `get_fault_events` |
| `backend/app/services/data_quality.py` | `get_data_quality` |
| `backend/app/main.py` | ASGI 入口 |
| `backend/tests/test_api_*.py`（9 个文件） | 单元 + API 集成 + 真实数据只读验证 |
| `scripts/api_query_bench.py` | 只读 EXPLAIN ANALYZE 基准 |
| `docs/phase1c_report.md` | 本报告 |

**修改**

| 文件 | 变更 |
| --- | --- |
| `backend/app/services/queries.py` | 新增 `digital_statistics`、`gap_overlap_stats`、`count_frozen_candidates`、`count_flagged_readings`、`count_fault_events`、`equipment_spans`、`half_open_upper_bound`；`analog_statistics` 增加 `stddev_samp`/首末时间；`equipment_time_span` 改为索引两端取数 |
| `backend/tests/conftest.py` | 新增 `clean_engine`、`api_client`、`api_dataset`、`api_quality_dataset` 装置 |
| `backend/pytest.ini` | 过滤 Starlette 弃用警告 |
| `backend/requirements.txt` | 追加 fastapi / starlette / uvicorn / httpx 版本 |
| `README.md` | 查询服务启动方式、接口表、时间语义、测试分类、性能基准 |
| `docs/architecture.md` | 补充阶段 1C 分层、时间契约、Agent-ready 函数、边界与安全 |
| `docs/roadmap.md` | 阶段 1C 结果与实测数据 |
| `backend/app/api/__init__.py`, `app/schemas/__init__.py` | 包导出（替换原 `.gitkeep` 占位） |

**未修改**：任何 Alembic 迁移脚本、`docker-compose.yml`、`data/raw/**` 原始数据、
生产库数据；未新增数据库对象。
