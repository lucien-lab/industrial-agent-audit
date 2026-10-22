"""LangGraph 工作流装配（单 Agent + 多工具，不使用多 Agent 协作）。

图结构（与 `docs/agent_design.md` 一致）：

    START → analyze ─┬─(out_of_scope/unsupported)──────────────→ compose
                     └─→ plan → execute → check ─┬─(还有待执行/补充查询)→ execute
                                                 └─(否则)→ compose → finalize → END

条件分支只有三处（意图分流、是否继续查询、是否需要继续执行），
终止条件显式：
- 计划执行完且没有补充查询；
- 出现 `not_found` / `config_error` 类错误（阻断依赖工具）；
- 工具调用次数 ≥ `AGENT_MAX_TOOL_CALLS`、决策轮次 ≥ `AGENT_MAX_ROUNDS`、
  节点步数 ≥ `AGENT_MAX_GRAPH_STEPS`、整轮耗时 ≥ `AGENT_TOTAL_TIMEOUT_S`。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agent.config import AgentSettings
from app.agent.context import AgentContext
from app.agent.nodes import AgentNodes
from app.agent.state import (
    BUDGET_EXHAUSTED,
    FAILED,
    TIMEOUT,
    AgentState,
    is_final,
)


def build_graph(ctx: AgentContext, settings: AgentSettings):
    """编译（并返回）Agent 工作流图。"""
    nodes = AgentNodes(ctx, settings)
    graph: StateGraph = StateGraph(AgentState)

    graph.add_node("analyze", nodes.analyze)
    graph.add_node("plan", nodes.plan)
    graph.add_node("execute", nodes.execute)
    graph.add_node("check", nodes.check)
    graph.add_node("compose", nodes.compose)
    graph.add_node("finalize", nodes.finalize)

    graph.add_edge(START, "analyze")

    def route_after_analyze(state: AgentState) -> str:
        if is_final(state) or state.get("status") in (TIMEOUT, BUDGET_EXHAUSTED):
            return "compose"
        kind = (state.get("intent") or {}).get("kind")
        if kind in ("out_of_scope", "unsupported"):
            return "compose"
        return "plan"

    def route_after_plan(state: AgentState) -> str:
        if state.get("status") in (TIMEOUT, BUDGET_EXHAUSTED):
            return "compose"
        return "execute" if state.get("pending") else "compose"

    def route_after_execute(state: AgentState) -> str:
        if int(state.get("steps", 0)) >= settings.max_graph_steps:
            return "compose"
        return "check"

    def route_after_check(state: AgentState) -> str:
        if is_final(state):
            return "compose"
        if int(state.get("steps", 0)) >= settings.max_graph_steps:
            return "compose"
        return "execute" if state.get("pending") else "compose"

    graph.add_conditional_edges(
        "analyze", route_after_analyze, {"plan": "plan", "compose": "compose"}
    )
    graph.add_conditional_edges(
        "plan", route_after_plan, {"execute": "execute", "compose": "compose"}
    )
    graph.add_conditional_edges(
        "execute", route_after_execute, {"check": "check", "compose": "compose"}
    )
    graph.add_conditional_edges(
        "check", route_after_check, {"execute": "execute", "compose": "compose"}
    )
    graph.add_edge("compose", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile()


def run_graph(
    *,
    ctx: AgentContext,
    settings: AgentSettings,
    initial: AgentState,
) -> dict[str, Any]:
    """执行工作流并返回最终 State。"""
    app = build_graph(ctx, settings)
    # recursion_limit 是 LangGraph 的硬保险：步数预算之外再兜一层，避免无限循环
    config = {"recursion_limit": max(settings.max_graph_steps * 2 + 10, 30)}
    result = app.invoke(initial, config=config)
    if not isinstance(result, dict):  # pragma: no cover - LangGraph 契约
        raise RuntimeError("Agent 工作流返回值不是 State 字典")
    result.setdefault("status", FAILED)
    return result


__all__ = ["build_graph", "run_graph"]
