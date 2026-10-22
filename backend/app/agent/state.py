"""LangGraph Agent State 定义与校验。

状态设计原则：
1. **显式**：所有决策输入（问题、意图、计划、观测、错误、预算）都在 State 里，
   不依赖节点内部的隐藏变量，便于测试与后续审计；
2. **可追加**：`tool_calls` / `observations` / `errors` / `warnings` 使用
   `Annotated[list, operator.add]` 归约，节点只返回新增增量，天然保留执行历史；
3. **有界**：`tool_call_count` / `rounds` / `deadline_epoch` 是硬预算，节点必须检查；
4. **可终止**：`status` 只有有限取值，最终状态明确（见 `FINAL_STATUSES`）。
"""

from __future__ import annotations

import operator
import time
from typing import Annotated, Any, Literal, TypedDict

from app.agent.errors import AgentInputError

# 执行状态（终态与进行态）
RUNNING = "running"
COMPLETED = "completed"
INSUFFICIENT_EVIDENCE = "insufficient_evidence"
REFUSED = "refused"
FAILED = "failed"
BUDGET_EXHAUSTED = "budget_exhausted"
TIMEOUT = "timeout"

FINAL_STATUSES: tuple[str, ...] = (
    COMPLETED,
    INSUFFICIENT_EVIDENCE,
    REFUSED,
    FAILED,
    BUDGET_EXHAUSTED,
    TIMEOUT,
)

IntentKind = Literal[
    "knowledge_only",
    "data_only",
    "mixed_analysis",
    "out_of_scope",
    "unsupported",
]


class AgentState(TypedDict, total=False):
    """一次 Agent 执行的全部状态。"""

    # ---- 请求 ----
    trace_id: str
    question: str
    request: dict[str, Any]  # 归一化后的请求（equipment_id / 时间窗 / 传感器 / top_k / planner）

    # ---- 意图与计划 ----
    intent: dict[str, Any]  # {kind, reason, signals, slots}
    plan: list[dict[str, Any]]  # 本次运行的完整计划（按顺序）
    pending: list[dict[str, Any]]  # 待执行的工具调用队列
    cursor: int  # 已消费的计划位置（用于判断“是否还需要继续查询”）

    # ---- 执行记录 ----
    tool_calls: Annotated[list[dict[str, Any]], operator.add]
    observations: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[dict[str, Any]], operator.add]
    warnings: Annotated[list[str], operator.add]

    # ---- 预算 ----
    rounds: int
    tool_call_count: int
    steps: int
    deadline_epoch: float  # 绝对截止时间（time.time()），节点必须检查

    # ---- 结果 ----
    status: str
    answer: dict[str, Any] | None
    facts: dict[str, Any] | None

    # ---- 控制标记 ----
    blocked_tools: list[str]  # 因设备不存在等原因被阻断的工具名
    knowledge_retry_done: bool
    plan_truncated: bool  # 计划是否因预算被裁剪（终态应标记 budget_exhausted）
    planner_meta: dict[str, Any]  # 规划器来源与状态（rule / llm）

    # ---- Trace ----
    trace: dict[str, Any] | None  # 执行摘要（供 API 与 Agent-Probe 消费）


def initial_state(
    *,
    trace_id: str,
    question: str,
    request: dict[str, Any],
    total_timeout_s: float,
) -> AgentState:
    """构造初始状态（唯一入口，避免各处手写字典时漏字段）。"""
    return AgentState(
        trace_id=trace_id,
        question=question,
        request=request,
        intent={},
        plan=[],
        pending=[],
        cursor=0,
        tool_calls=[],
        observations=[],
        errors=[],
        warnings=[],
        rounds=0,
        tool_call_count=0,
        steps=0,
        deadline_epoch=time.time() + total_timeout_s,
        status=RUNNING,
        answer=None,
        facts=None,
        blocked_tools=[],
        knowledge_retry_done=False,
        plan_truncated=False,
        planner_meta={},
        trace=None,
    )


_REQUIRED_KEYS = ("trace_id", "question", "request", "intent", "plan", "status")


def validate_state(state: AgentState) -> None:
    """状态完整性校验（节点/测试入口调用）。"""
    missing = [key for key in _REQUIRED_KEYS if key not in state]
    if missing:
        raise AgentInputError(f"Agent State 缺少必需字段：{missing}", missing=missing)
    if not isinstance(state.get("plan"), list):
        raise AgentInputError("Agent State.plan 必须是列表")
    for key in ("rounds", "tool_call_count", "steps"):
        value = state.get(key, 0)
        if not isinstance(value, int) or value < 0:
            raise AgentInputError(f"Agent State.{key} 必须是非负整数，当前为 {value!r}")
    if state.get("status") not in (RUNNING, *FINAL_STATUSES):
        raise AgentInputError(f"Agent State.status 取值非法：{state.get('status')!r}")


def is_final(state: AgentState) -> bool:
    return state.get("status") in FINAL_STATUSES


def deadline_passed(state: AgentState, *, now: float | None = None) -> bool:
    """是否已超过整轮执行截止时间。"""
    return (now if now is not None else time.time()) >= float(state.get("deadline_epoch", 0.0))


def remaining_tool_budget(state: AgentState, max_tool_calls: int) -> int:
    return max(max_tool_calls - int(state.get("tool_call_count", 0)), 0)


def tools_used(state: AgentState) -> list[str]:
    """按首次调用顺序去重列出实际执行的工具（来自工具调用记录，不含计划）。"""
    seen: list[str] = []
    for call in state.get("tool_calls", []):
        name = call.get("tool")
        if name and name not in seen:
            seen.append(name)
    return seen


__all__ = [
    "BUDGET_EXHAUSTED",
    "COMPLETED",
    "FAILED",
    "FINAL_STATUSES",
    "INSUFFICIENT_EVIDENCE",
    "REFUSED",
    "RUNNING",
    "TIMEOUT",
    "AgentState",
    "IntentKind",
    "deadline_passed",
    "initial_state",
    "is_final",
    "remaining_tool_budget",
    "tools_used",
    "validate_state",
]
