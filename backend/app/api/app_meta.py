"""应用级元信息（版本号、服务名、OpenAPI 描述），供 Router 与应用工厂共用。"""

from __future__ import annotations

SERVICE_NAME = "industrial-agent-audit-api"
APP_VERSION = "2.0.0"

TAGS_METADATA = [
    {
        "name": "健康检查",
        "description": "进程存活（/health）与数据库就绪（/ready）。",
    },
    {
        "name": "设备",
        "description": "设备档案查询。`equipment_id` 是数据集逻辑编号（如 `metropt3-apu-01`），"
        "不是厂商序列号。",
    },
    {
        "name": "传感器历史数据",
        "description": "按时间窗分页返回原始观测。时间窗左闭右开 `[start_time, end_time)`，"
        "时间无时区；`limit` 默认 100、硬上限 1000。",
    },
    {
        "name": "传感器统计",
        "description": "模拟量（min/max/mean/std）与数字量（0/1 计数、占比、状态切换次数）统计，"
        "全部在数据库内聚合。",
    },
    {
        "name": "故障事件",
        "description": "官方公开故障区间（PDF Failure Information）。区间为半开 `[start, end)`，"
        "区间内的传感器记录**不会**被自动视为已确认故障样本。",
    },
    {
        "name": "知识库",
        "description": "工业设备知识 RAG：混合检索（pgvector + 关键词 RRF）、有证据的回答、"
        "文档来源与页码引用。默认只检索 MetroPT-3 直接关联与同类型通用资料，"
        "通用资料不会被视为本设备专属参数。",
    },
    {
        "name": "Agent",
        "description": "LangGraph 单 Agent + 多工具：意图识别、工具规划与执行、证据整合与回答生成。"
        "只读；有最大工具调用次数/轮次/超时限制；数据结论带工具来源，知识结论带文档逐字摘录；"
        "执行过程以结构化 Trace 事件记录（本阶段不做风险评分或 LLM 审计）。",
    },
    {
        "name": "数据质量",
        "description": "采样覆盖率、缺口、冻结候选等质量指标。"
        "缺失样本数为估计值，冻结候选不等于设备故障。",
    },
]

DESCRIPTION = """
工业设备数据查询服务（阶段 1C）。

## 数据与口径
- 数据来源：MetroPT-3 公开数据集（1,516,948 条真实传感器记录、4 条官方故障事件、332 处采样缺口）。
- 存储：PostgreSQL 17 + pgvector，SQLAlchemy 2.x ORM，Alembic 管理结构。

## 时间语义（重要）
- 源时间戳 **不含时区**（`timestamp without time zone`），官方未声明时区。
- 本服务只接受**无时区**的 ISO 8601 时间字符串，例如 `2020-04-18T00:00:00`。
- 带偏移的时间（`Z`、`+08:00`）会被**明确拒绝**（422 `timezone_not_supported`），
  不做任何隐式 UTC 换算。
- 所有区间为**左闭右开** `[start_time, end_time)`：包含起点，不包含终点。

## 错误结构
所有错误统一返回：

```json
{"error": {"code": "equipment_not_found", "message": "...", "details": {}, "request_id": "..."}}
```

| HTTP | code | 场景 |
| --- | --- | --- |
| 404 | equipment_not_found | 设备编号不存在 |
| 422 | invalid_parameter | 参数缺失/类型错误/超出上限 |
| 422 | invalid_time_range | end_time 不晚于 start_time |
| 422 | timezone_not_supported | 时间带时区偏移 |
| 422 | unsupported_sensor | 传感器名称不在白名单 |
| 422 | window_too_large | 时间窗超过 366 天 |
| 503 | database_unavailable | 数据库不可用 |
| 500 | internal_error | 未知服务端错误（不回显 SQL/连接串） |
"""
