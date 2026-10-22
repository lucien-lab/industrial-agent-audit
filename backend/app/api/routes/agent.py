"""Agent 接口（阶段 3）。

- `POST /api/v1/agent/chat`：一次问答（意图识别 → 工具编排 → 证据整合 → 回答）；
- `GET  /api/v1/agent/traces/{trace_id}`：查看一次执行的 Trace 摘要（可选完整事件流）。

边界（与 `docs/agent_design.md` 一致）：
- Router 只做 HTTP 装配与响应转换，**不包含** Agent 推理逻辑；
- 客户端不能提交工具名、SQL 或任意代码，工具由服务端规划器选择；
- 只读：Agent 的数据库会话在连接层强制 `default_transaction_read_only=on`；
- 不暴露连接串、API Key 或内部堆栈；错误统一走 `app.api.errors` 的 `{"error": {...}}` 结构。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Response

from app.agent.errors import TraceNotFound
from app.agent.service import AgentRequest, get_trace, run_agent
from app.schemas.agent import AgentChatRequest, AgentChatResponse, AgentTraceResponse
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/api/v1/agent", tags=["Agent"])

_AGENT_ERRORS = {
    404: {"model": ErrorResponse, "description": "trace_id 不存在或已过期（进程内只保留最近 N 条）"},
    422: {"model": ErrorResponse, "description": "请求参数不合法（问题为空/超长、设备编号字符集、时间格式）"},
    503: {"model": ErrorResponse, "description": "数据库/知识库/模型服务不可用"},
}


@router.post(
    "/chat",
    response_model=AgentChatResponse,
    summary="工业设备运维 Agent 问答",
    description=(
        "由 LangGraph 编排的单 Agent + 多工具流程：\n\n"
        "1. **意图分析**：区分纯知识问题、纯数据问题、混合分析问题；识别设备、时间窗、测点；\n"
        "2. **工具规划**：按意图生成有序计划（不会对所有问题都调用全部工具）；\n"
        "3. **工具执行**：每次调用都有超时与有限重试，失败按类别处理；\n"
        "4. **结果检查**：必要时补充查询（异常判断补对照窗口、无数据补质量查询、检索为空换关键词），\n"
        "   并受最大工具调用次数（默认 8）、最大决策轮次（默认 5）与整轮超时（默认 180 s）约束；\n"
        "5. **证据整合**：数据事实逐个编号（D1、D2…），文档片段编号（K1、K2…）；\n"
        "6. **回答生成**：数值必须命中事实清单，知识结论必须带逐字摘录（引用不合格会被丢弃并记录）。\n\n"
        "**明确不做什么**：不执行 SQL、不写数据库、不控制设备；没有证据时明确说明无法确认。"
    ),
    responses=_AGENT_ERRORS,
)
def chat(payload: AgentChatRequest, response: Response) -> AgentChatResponse:
    result = run_agent(
        AgentRequest(
            question=payload.question,
            equipment_id=payload.equipment_id,
            start_time=payload.start_time,
            end_time=payload.end_time,
            sensors=payload.sensors,
            planner=payload.planner,
        ),
        include_trace=payload.include_trace,
    )
    response.headers["X-Agent-Trace-Id"] = result.trace_id
    return AgentChatResponse.model_validate(result.to_dict())


@router.get(
    "/traces/{trace_id}",
    response_model=AgentTraceResponse,
    summary="查看一次 Agent 执行的 Trace",
    description=(
        "返回结构化执行事件摘要：执行状态、意图、工具序列、耗时、错误与证据引用。\n\n"
        "`include_events=true` 时返回完整事件流（run_start / intent / plan / tool_call / tool_result / "
        "decision / answer / run_end），供后续 Agent-Probe 消费。事件摘要只包含白名单字段，"
        "不含连接串、密码或 API Key。\n\n"
        "Trace 保存在进程内（有界 LRU，默认 200 条），同时按配置写入 "
        "`data/processed/agent/traces/trace-<id>.jsonl`。本阶段**不做**风险评分或 LLM 审计。"
    ),
    responses={404: {"model": ErrorResponse, "description": "trace_id 不存在"}},
)
def trace_detail(
    trace_id: str,
    include_events: bool = Query(default=False, description="是否返回完整事件流"),
) -> AgentTraceResponse:
    summary = get_trace(trace_id)
    if summary is None:
        raise TraceNotFound(
            f"trace_id {trace_id!r} 不存在（进程内只保留最近的执行记录）", trace_id=trace_id
        )
    payload = dict(summary)
    payload["event_count"] = len(payload.get("events") or [])
    if not include_events:
        payload.pop("events", None)
    return AgentTraceResponse.model_validate(payload)


__all__ = ["router"]
