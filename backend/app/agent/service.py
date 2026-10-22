"""Agent 服务层：一次问答的端到端编排（Router 只做 HTTP 装配）。

职责：
- 校验并归一化请求（问题长度、设备编号字符集、时间格式）；
- 创建 Trace 记录器与运行时上下文（独立的只读数据库会话）；
- 调用 LangGraph 工作流，把最终 State 组装成稳定的响应结构；
- 统一异常处理：任何未预期异常都返回明确的失败状态 + trace_id，不返回半成品答案。

明确不做：
- 不做 HTTP 状态码映射（由 `app.api.errors` 处理）；
- 不做工具实现（由 `app.agent.tools` 负责）；
- 不写数据库、不调用设备控制接口。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from app.agent import AGENT_VERSION
from app.agent.config import AgentSettings, get_agent_settings
from app.agent.context import AgentContext
from app.agent.errors import AgentInputError
from app.agent.graph import run_graph
from app.agent.state import FAILED, initial_state, tools_used
from app.agent.trace import TraceRecorder, TraceStore, get_trace_store
from app.services.catalog import parse_naive_datetime

logger = logging.getLogger("industrial_agent_audit.agent")

_EQUIPMENT_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


@dataclass(frozen=True)
class AgentRequest:
    """一次 Agent 请求（与 HTTP 层解耦，脚本/评估直接构造）。"""

    question: str
    equipment_id: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    sensors: list[str] | None = None
    planner: str | None = None
    top_k: int | None = None


@dataclass
class AgentResponse:
    trace_id: str
    question: str
    execution_status: str
    intent: str
    answer: str
    answer_status: str
    answer_mode: str
    tools_used: list[str]
    tool_calls: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    warnings: list[str]
    errors: list[dict[str, Any]]
    grounding: dict[str, Any]
    llm: dict[str, Any]
    plan: list[dict[str, Any]]
    facts: dict[str, Any]
    sections: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    total_duration_ms: float = 0.0
    agent_version: str = AGENT_VERSION
    prompt_version: str = ""
    trace: dict[str, Any] | None = None
    rounds: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "question": self.question,
            "answer": self.answer,
            "execution_status": self.execution_status,
            "intent": self.intent,
            "answer_status": self.answer_status,
            "answer_mode": self.answer_mode,
            "tools_used": self.tools_used,
            "tool_calls": self.tool_calls,
            "plan": self.plan,
            "evidence": self.evidence,
            "citations": self.citations,
            "warnings": self.warnings,
            "errors": self.errors,
            "grounding": self.grounding,
            "llm": self.llm,
            "facts": self.facts,
            "sections": self.sections,
            "timings_ms": self.timings,
            "total_duration_ms": round(self.total_duration_ms, 2),
            "rounds": self.rounds,
            "agent_version": self.agent_version,
            "prompt_version": self.prompt_version,
            "trace": self.trace,
        }


def _normalize_request(request: AgentRequest, settings: AgentSettings) -> dict[str, Any]:
    question = " ".join(str(request.question or "").split())
    if not question:
        raise AgentInputError("question 不能为空")
    if len(question) > settings.max_question_chars:
        raise AgentInputError(
            f"question 长度 {len(question)} 超过上限 {settings.max_question_chars}",
            max_chars=settings.max_question_chars,
        )
    equipment_id = (request.equipment_id or "").strip() or None
    if equipment_id is not None and not _EQUIPMENT_CODE_RE.match(equipment_id):
        raise AgentInputError(
            "equipment_id 只能包含字母、数字、点、下划线、冒号与连字符（最长 64）",
            field="equipment_id",
        )
    for field_name, value in (("start_time", request.start_time), ("end_time", request.end_time)):
        if value:
            parse_naive_datetime(value, field=field_name)  # 复用 Phase 1C 的时间校验（抛领域异常）
    payload: dict[str, Any] = {
        "equipment_id": equipment_id,
        "start_time": request.start_time,
        "end_time": request.end_time,
        "sensors": list(request.sensors) if request.sensors else None,
        "planner": (request.planner or "").strip().lower() or None,
        "top_k": request.top_k,
    }
    return {"question": question, "request": payload}


def run_agent(
    request: AgentRequest,
    *,
    settings: AgentSettings | None = None,
    ctx: AgentContext | None = None,
    chat_model: Any | None = None,
    embedder: Any | None = None,
    trace_store: TraceStore | None = None,
    include_trace: bool = False,
) -> AgentResponse:
    """执行一次 Agent 问答。"""
    settings = settings or get_agent_settings()
    normalized = _normalize_request(request, settings)
    question = normalized["question"]
    payload = normalized["request"]

    trace_id = TraceRecorder.new_trace_id()
    recorder = TraceRecorder(
        trace_id=trace_id,
        trace_dir=settings.trace_dir if settings.trace_write_files else None,
        write_files=settings.trace_write_files,
        store=trace_store if trace_store is not None else get_trace_store(),
    )
    own_context = ctx is None
    runtime = ctx or AgentContext(settings=settings, recorder=recorder)
    runtime.recorder = recorder
    if chat_model is not None:
        runtime._chat_model = chat_model  # 显式注入（测试/评估）
    if embedder is not None:
        runtime.embedder = embedder

    recorder.record(
        "run_start",
        node="entry",
        status="running",
        input_summary={
            "query": question,
            "equipment_code": payload.get("equipment_id"),
            "start_time": payload.get("start_time"),
            "end_time": payload.get("end_time"),
            "sensors": payload.get("sensors"),
        },
        meta={
            "agent_version": AGENT_VERSION,
            "planner": payload.get("planner") or settings.planner,
            "budgets": {
                "max_tool_calls": settings.max_tool_calls,
                "max_rounds": settings.max_rounds,
                "total_timeout_s": settings.total_timeout_s,
            },
            "sources": runtime.describe_sources(),
        },
    )

    state = initial_state(
        trace_id=trace_id,
        question=question,
        request=payload,
        total_timeout_s=settings.total_timeout_s,
    )
    started = time.perf_counter()
    try:
        final_state = run_graph(ctx=runtime, settings=settings, initial=state)
    except Exception as exc:  # noqa: BLE001 - 任何异常都要给出明确失败状态
        logger.exception("Agent 执行失败 trace_id=%s", trace_id)
        recorder.record(
            "run_end",
            node="entry",
            status=FAILED,
            error_category="internal",
            error_code=type(exc).__name__,
            output_summary={"status": FAILED},
        )
        summary = recorder.store.get(trace_id)
        return AgentResponse(
            trace_id=trace_id,
            question=question,
            execution_status=FAILED,
            intent="unknown",
            answer=(
                "Agent 执行过程中出现内部错误，未生成结论。"
                f"请携带 trace_id={trace_id} 反馈。"
            ),
            answer_status="failed",
            answer_mode="none",
            tools_used=[],
            tool_calls=[],
            evidence=[],
            citations=[],
            warnings=[f"内部错误：{type(exc).__name__}"],
            errors=[{"category": "internal", "code": type(exc).__name__}],
            grounding={"ok": False},
            llm={},
            plan=[],
            facts={},
            timings={"total_llm_wall": (time.perf_counter() - started) * 1000},
            total_duration_ms=(time.perf_counter() - started) * 1000,
            trace=(summary.to_summary_dict() if summary else None) if include_trace else None,
        )
    finally:
        if own_context:
            runtime.close()

    answer = final_state.get("answer") or {}
    facts = final_state.get("facts") or {}
    intent = final_state.get("intent") or {}
    total_ms = recorder.elapsed_ms()
    elapsed_s = (time.perf_counter() - started) * 1000

    response = AgentResponse(
        trace_id=trace_id,
        question=question,
        execution_status=str(final_state.get("status") or FAILED),
        intent=str(intent.get("kind") or "unknown"),
        answer=str(answer.get("answer") or ""),
        answer_status=str(answer.get("status") or "failed"),
        answer_mode=str(answer.get("answer_mode") or "none"),
        tools_used=tools_used(final_state),
        tool_calls=list(final_state.get("tool_calls") or []),
        evidence=list(answer.get("citations") or []),
        citations=list(answer.get("citations") or []),
        warnings=list(dict.fromkeys(list(final_state.get("warnings") or []) + list(answer.get("warnings") or []))),
        errors=list(final_state.get("errors") or []),
        grounding=dict(answer.get("grounding") or {}),
        llm=dict(answer.get("llm") or {}),
        plan=list(final_state.get("plan") or []),
        facts=facts,
        sections=dict(answer.get("sections") or {}),
        timings={
            "graph_wall": round(elapsed_s, 2),
            "reported_total": total_ms,
        },
        total_duration_ms=total_ms,
        prompt_version=str(answer.get("prompt_version") or ""),
        trace=(final_state.get("trace") if include_trace else None),
        rounds=int(final_state.get("rounds") or 0),
    )
    if include_trace and response.trace is None:
        summary = recorder.store.get(trace_id)
        response.trace = summary.to_summary_dict() if summary else None
    return response


def get_trace(trace_id: str, *, store: TraceStore | None = None) -> dict[str, Any] | None:
    """读取一次执行的 Trace 摘要（不含敏感信息）。"""
    summary = (store or get_trace_store()).get(trace_id)
    if summary is None:
        return None
    return summary.to_dict()


__all__ = ["AgentRequest", "AgentResponse", "get_trace", "run_agent"]
