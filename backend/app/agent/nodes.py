"""LangGraph 节点实现：意图 → 规划 → 执行 → 检查 → 证据整合 → 结束。

节点职责与硬约束：
- `analyze`：只做意图/槽位识别，不调用工具；
- `plan`：生成有序工具计划（规则或 LLM），按预算裁剪；
- `execute`：执行**一个**工具调用（含超时/有限重试），记录结构化事件；
- `check`：判断是否需要继续查询（补基准窗口、补质量、换检索方法），并检查预算；
- `compose`：证据整合 + 回答生成（数值接地、引用逐字校验）；
- `finalize`：写入结束事件，给出明确终态。

所有节点都不允许把未执行的操作写进结果，也不允许在缺数据时生成数值。
"""

from __future__ import annotations

import time
from typing import Any

from app.agent.answer import compose_answer
from app.agent.config import AgentSettings
from app.agent.context import AgentContext
from app.agent.errors import blocks_dependents
from app.agent.evidence import build_fact_sheet
from app.agent.intent import (
    IntentResult,
    PlannedCall,
    Slots,
    analyze,
    apply_budget,
    baseline_window,
    build_plan,
)
from app.agent.planner_llm import plan_with_llm
from app.agent.state import (
    BUDGET_EXHAUSTED,
    COMPLETED,
    FAILED,
    INSUFFICIENT_EVIDENCE,
    REFUSED,
    TIMEOUT,
    deadline_passed,
    tools_used,
)
from app.agent.tools.base import run_tool
from app.agent.tools.registry import (
    TOOL_DATA_QUALITY,
    TOOL_KNOWLEDGE,
    TOOL_SENSOR_STATISTICS,
    build_registry,
)

MAX_EXECUTION_NOTES = 8

OUT_OF_SCOPE_MESSAGE = (
    "该问题不属于本系统的工业设备数据与技术知识查询范围，因此不提供回答。"
    "本系统只做只读查询：不执行 SQL、不修改数据、不控制设备。"
)

SECURITY_MESSAGE = (
    "问题中包含试图改变系统规则的内容或要求执行写操作/设备控制，已被拒绝。"
    "本系统只提供只读的数据查询与知识检索，不会执行问题中的任何指令。"
)


def _append_once(existing: list[str], items: list[str]) -> list[str]:
    out = list(existing)
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


class AgentNodes:
    """节点集合（绑定运行时依赖，便于测试注入替身）。"""

    def __init__(self, ctx: AgentContext, settings: AgentSettings) -> None:
        self.ctx = ctx
        self.settings = settings
        self.registry = build_registry(settings)

    # ------------------------------------------------------------------ #
    def _warn(self, message: str) -> None:
        """节点内的（非阻塞）告警只进 Trace，不进状态时由调用方显式返回。"""
        if self.ctx.recorder is not None:
            self.ctx.recorder.record(
                "decision", node="warn", status="warning", output_summary={"reason": message}
            )

    def _bump(self, state: dict[str, Any]) -> int:
        return int(state.get("steps", 0)) + 1

    # ------------------------------------------------------------------ #
    def analyze(self, state: dict[str, Any]) -> dict[str, Any]:
        steps = self._bump(state)
        recorder = self.ctx.recorder
        if recorder is not None:
            recorder.record(
                "node_start",
                node="analyze",
                status="running",
                input_summary={"query": state.get("question", "")},
            )
        started = time.perf_counter()
        intent, slots = analyze(state["question"], state["request"], self.settings)
        if recorder is not None:
            recorder.record(
                "intent",
                node="analyze",
                status=intent.kind,
                duration_ms=(time.perf_counter() - started) * 1000,
                output_summary={
                    "intent": intent.kind,
                    "reason": intent.reason,
                    "planned": None,
                },
                meta={"signals": list(intent.signals)},
            )
            recorder.record(
                "node_end",
                node="analyze",
                status="ok",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        return {
            "steps": steps,
            "intent": {**intent.as_dict(), "slots": slots.as_dict()},
        }

    # ------------------------------------------------------------------ #
    def plan(self, state: dict[str, Any]) -> dict[str, Any]:
        steps = self._bump(state)
        intent = state["intent"]
        kind = str(intent.get("kind"))
        slots = Slots.from_dict(intent.get("slots") or {})
        intent_obj = IntentResult(
            kind=kind,
            reason=str(intent.get("reason") or ""),
            signals=tuple(intent.get("signals") or ()),
            needs_time_window=bool(intent.get("needs_time_window")),
            clarification=intent.get("clarification"),
            security_flags=tuple(intent.get("security_flags") or ()),
        )
        planner = str(state["request"].get("planner") or self.settings.planner)
        warnings: list[str] = []
        planner_meta: dict[str, Any] = {"planner": "rule", "status": "ok"}

        if kind in ("out_of_scope", "unsupported"):
            calls: list[PlannedCall] = []
        elif planner == "llm":
            calls, planner_meta = plan_with_llm(
                question=state["question"],
                intent=intent_obj,
                slots=slots,
                settings=self.settings,
                chat_model=self.ctx.chat_model,
            )
            if planner_meta.get("status") != "ok":
                warnings.append(
                    f"LLM 规划未生效（{planner_meta.get('status')}），已回退规则规划"
                )
        else:
            calls = build_plan(intent_obj, slots, self.settings)

        calls, budget_warnings = apply_budget(calls, self.settings)
        warnings.extend(budget_warnings)
        plan_dicts = [call.as_dict() for call in calls]

        if self.ctx.recorder is not None:
            self.ctx.recorder.record(
                "plan",
                node="plan",
                status="ok",
                output_summary={"planned": [call.tool for call in calls]},
                meta={"planner": planner, "planner_meta": planner_meta},
            )
        return {
            "steps": steps,
            "plan": plan_dicts,
            "pending": [dict(item) for item in plan_dicts],
            "cursor": 0,
            "warnings": warnings,
            "status": state.get("status", "running"),
            "plan_truncated": bool(budget_warnings),
            "planner_meta": planner_meta,
        }

    # ------------------------------------------------------------------ #
    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        steps = self._bump(state)
        pending = list(state.get("pending") or [])
        if not pending:  # pragma: no cover - 路由保证不会走到这里
            return {"steps": steps}
        call = pending[0]
        rest = pending[1:]
        tool = str(call.get("tool"))
        blocked = list(state.get("blocked_tools") or [])
        tool_call_count = int(state.get("tool_call_count", 0))

        if tool in blocked:
            if self.ctx.recorder is not None:
                self.ctx.recorder.record(
                    "decision",
                    node="execute",
                    tool=tool,
                    status="skipped",
                    output_summary={"reason": "前置查询表明设备/配置不可用，跳过该工具"},
                )
            return {"steps": steps, "pending": rest, "cursor": int(state.get("cursor", 0)) + 1}

        if tool_call_count >= self.settings.max_tool_calls:
            warning = f"已达到最大工具调用次数 {self.settings.max_tool_calls}，停止继续查询"
            if self.ctx.recorder is not None:
                self.ctx.recorder.record(
                    "decision", node="execute", tool=tool, status="budget", output_summary={"reason": warning}
                )
            return {
                "steps": steps,
                "pending": [],
                "status": BUDGET_EXHAUSTED,
                "warnings": [warning],
            }

        if deadline_passed(state):
            return {
                "steps": steps,
                "pending": [],
                "status": TIMEOUT,
                "warnings": [f"整轮执行超过 {self.settings.total_timeout_s}s 上限，已终止"],
            }

        spec = self.registry[tool]
        recorder = self.ctx.recorder
        if recorder is not None:
            recorder.record(
                "tool_call",
                node="execute",
                tool=tool,
                status="running",
                input_summary=call.get("params") or {},
                meta={"reason": call.get("reason"), "stage": call.get("stage")},
            )
        outcome = run_tool(
            spec,
            call.get("params") or {},
            self.ctx,
            deadline_epoch=float(state.get("deadline_epoch") or 0.0),
        )
        if recorder is not None:
            recorder.record(
                "tool_result",
                node="execute",
                tool=tool,
                status=outcome.status,
                duration_ms=outcome.elapsed_ms,
                output_summary=outcome.summary(),
                error_category=outcome.error_category,
                error_code=outcome.error_code,
                evidence_refs=[
                    str(item.source.get("doc_key") or item.source.get("table") or item.origin_tool)
                    for item in outcome.evidence[:5]
                ],
            )
            if outcome.attempts > 1:
                recorder.record(
                    "tool_retry",
                    node="execute",
                    tool=tool,
                    status="retried",
                    output_summary={"count": outcome.attempts - 1},
                )

        record = {
            "tool": tool,
            "params": outcome.input,
            "status": outcome.status,
            "attempts": outcome.attempts,
            "elapsed_ms": round(outcome.elapsed_ms, 2),
            "reason": call.get("reason"),
            "stage": call.get("stage", "initial"),
            "error": outcome.error,
            "evidence_count": len(outcome.evidence),
        }
        updates: dict[str, Any] = {
            "steps": steps,
            "pending": rest,
            "cursor": int(state.get("cursor", 0)) + 1,
            "tool_calls": [record],
            "observations": [outcome.to_dict()],
            "tool_call_count": tool_call_count + 1,
        }
        if outcome.error is not None:
            updates["errors"] = [
                {
                    "tool": tool,
                    "category": outcome.error.get("category"),
                    "code": outcome.error.get("code"),
                    "message": outcome.error.get("message"),
                }
            ]
            if outcome.error.get("blocks_dependents"):
                dependent = sorted(
                    {
                        str(item.get("tool"))
                        for item in rest
                        if item.get("depends_on_equipment")
                    }
                    | {tool}
                )
                updates["blocked_tools"] = dependent
                updates["warnings"] = [
                    f"工具 {tool} 失败（{outcome.error.get('category')}），"
                    f"跳过依赖设备数据的后续工具：{dependent}"
                ]
        if outcome.status == "empty":
            updates["warnings"] = updates.get("warnings", []) + [
                f"工具 {tool} 在给定条件下返回空结果（合法查询，无匹配数据）"
            ]
        return updates

    # ------------------------------------------------------------------ #
    def check(self, state: dict[str, Any]) -> dict[str, Any]:
        """判断是否继续查询；同时执行预算与超时检查。"""
        steps = self._bump(state)
        recorder = self.ctx.recorder
        pending = list(state.get("pending") or [])
        tool_call_count = int(state.get("tool_call_count", 0))
        rounds = int(state.get("rounds", 0))
        plan = list(state.get("plan") or [])

        if deadline_passed(state):
            return {
                "steps": steps,
                "pending": [],
                "rounds": rounds,
                "status": TIMEOUT,
                "warnings": [f"整轮执行超过 {self.settings.total_timeout_s}s 上限，已终止"],
            }

        followups = [] if pending else self._followups(state)
        if pending and (tool_call_count >= self.settings.max_tool_calls):
            return {
                "steps": steps,
                "pending": [],
                "rounds": rounds,
                "status": BUDGET_EXHAUSTED,
                "warnings": [
                    f"已达到最大工具调用次数 {self.settings.max_tool_calls}，停止继续查询"
                ],
            }
        if pending and rounds >= self.settings.max_rounds:
            return {
                "steps": steps,
                "pending": [],
                "rounds": rounds,
                "status": BUDGET_EXHAUSTED,
                "warnings": [f"已达到最大决策轮次 {self.settings.max_rounds}，停止继续查询"],
            }
        if pending:
            # 继续执行 = 进入下一轮
            rounds += 1
            if rounds > self.settings.max_rounds:
                return {
                    "steps": steps,
                    "pending": [],
                    "rounds": rounds - 1,
                    "status": BUDGET_EXHAUSTED,
                    "warnings": [
                        f"已达到最大决策轮次 {self.settings.max_rounds}，停止继续查询"
                    ],
                }
            return {"steps": steps, "pending": pending, "rounds": rounds}
        if followups:
            if tool_call_count + len(followups) > self.settings.max_tool_calls:
                followups = []
                if recorder is not None:
                    recorder.record(
                        "decision",
                        node="check",
                        status="budget",
                        output_summary={"reason": "补充查询会超出工具调用预算，已跳过"},
                    )
            elif rounds >= self.settings.max_rounds:
                followups = []
            else:
                plan = plan + [call.as_dict() for call in followups]
                pending = [call.as_dict() for call in followups]
                if recorder is not None:
                    recorder.record(
                        "decision",
                        node="check",
                        status="followup",
                        output_summary={"planned": [call.tool for call in followups]},
                        meta={"reasons": [call.reason for call in followups]},
                    )
                return {
                    "steps": steps,
                    "plan": plan,
                    "pending": pending,
                    "rounds": rounds + 1,
                }

        if recorder is not None:
            recorder.record(
                "decision",
                node="check",
                status="finish" if not pending else "continue",
                output_summary={
                    "rounds": rounds,
                    "tool_calls": tool_call_count,
                    "remaining": len(pending),
                },
            )
        return {"steps": steps, "pending": pending, "rounds": rounds}

    def _followups(self, state: dict[str, Any]) -> list[PlannedCall]:
        """基于已有观测决定是否需要“再查一次”（每轮最多 1 个补充调用）。"""
        intent = state.get("intent") or {}
        slots = Slots.from_dict(intent.get("slots") or {})
        observations = list(state.get("observations") or [])
        successes = [item for item in observations if item.get("status") in ("ok", "empty")]
        tools_done = [item.get("tool") for item in observations]

        stats = [item for item in successes if item.get("tool") == TOOL_SENSOR_STATISTICS]
        knowledge = [item for item in successes if item.get("tool") == TOOL_KNOWLEDGE]

        # 1) “是否异常”：需要同长度的前一窗口作为确定性对照
        if slots.asks_anomaly and len(stats) == 1 and stats[0].get("status") == "ok":
            window = (stats[0].get("data") or {}).get("window") or {}
            baseline = baseline_window(str(window.get("start")), str(window.get("end")))
            if baseline:
                params: dict[str, Any] = {
                    "equipment_code": slots.equipment_code,
                    "start_time": baseline[0],
                    "end_time": baseline[1],
                }
                if slots.sensors:
                    params["sensors"] = list(slots.sensors)
                return [
                    PlannedCall(
                        tool=TOOL_SENSOR_STATISTICS,
                        params=params,
                        reason=(
                            "异常判断需要对照：查询目标窗口之前等长的紧邻窗口，"
                            "用同一口径做描述性比较（不是判定阈值）"
                        ),
                        stage="followup",
                    )
                ]

        # 2) 目标窗口无数据：补一次质量查询，区分“没有记录”和“查询失败”
        if stats and all(item.get("status") == "empty" for item in stats):
            if TOOL_DATA_QUALITY not in tools_done:
                window = (stats[0].get("data") or {}).get("window") or {}
                return [
                    PlannedCall(
                        tool=TOOL_DATA_QUALITY,
                        params={
                            "equipment_code": slots.equipment_code,
                            "start_time": window.get("start"),
                            "end_time": window.get("end"),
                        },
                        reason="统计无数据，查询采样质量以说明是缺口、窗口越界还是确实没有记录",
                        stage="followup",
                    )
                ]

        # 3) 知识检索为空：换关键词检索再试一次（不同检索信号，不是重复同一查询）
        if knowledge and all(item.get("status") == "empty" for item in knowledge):
            if not state.get("knowledge_retry_done"):
                query = str(((knowledge[0].get("data") or {}).get("query")) or slots.knowledge_query)
                return [
                    PlannedCall(
                        tool=TOOL_KNOWLEDGE,
                        params={"query": query, "top_k": self.settings.max_knowledge_chunks, "method": "keyword"},
                        reason="混合检索无结果，改用关键词检索重试一次（不同检索信号）",
                        depends_on_equipment=False,
                        stage="followup",
                    )
                ]
        return []

    # ------------------------------------------------------------------ #
    def compose(self, state: dict[str, Any]) -> dict[str, Any]:
        steps = self._bump(state)
        intent = state.get("intent") or {}
        kind = str(intent.get("kind"))
        slots = Slots.from_dict(intent.get("slots") or {})
        observations = list(state.get("observations") or [])
        fact_sheet = build_fact_sheet(
            observations, max_knowledge_chunks=self.settings.max_knowledge_chunks
        )
        refusal, refusal_status, execution_notes = self._decide_refusal(state, fact_sheet, kind)
        if kind not in ("out_of_scope", "unsupported"):
            execution_notes = _append_once(list(slots.notes), execution_notes)
        execution_notes = _append_once(
            execution_notes, list(dict.fromkeys(state.get("warnings") or []))
        )[:MAX_EXECUTION_NOTES]

        model = None
        if self.settings.answer_mode != "deterministic":
            try:
                model = self.ctx.chat_model
            except Exception as exc:  # noqa: BLE001 - 模型创建失败不应导致无回答
                model = None
                execution_notes.append(f"语言模型不可用（{type(exc).__name__}），使用确定性渲染")

        prior_status = str(state.get("status") or "running")
        force_reason = None
        if prior_status == BUDGET_EXHAUSTED or state.get("plan_truncated"):
            force_reason = (
                "本次执行因工具调用/轮次预算限制提前结束，证据不完整；"
                "回答改用确定性渲染，只陈述已获取到的证据"
            )
        elif prior_status == TIMEOUT:
            force_reason = (
                "本次执行超过整轮超时限制提前结束，证据不完整；"
                "回答改用确定性渲染，只陈述已获取到的证据"
            )
        composed = compose_answer(
            question=state["question"],
            fact_sheet=fact_sheet,
            intent_kind=kind,
            slots=slots,
            settings=self.settings,
            chat_model=model,
            refusal=refusal,
            refusal_status=refusal_status,
            execution_notes=execution_notes,
            force_deterministic_reason=force_reason,
        )

        if refusal is not None:
            status = REFUSED if kind == "out_of_scope" else INSUFFICIENT_EVIDENCE
        elif composed.status == "answered":
            status = COMPLETED
        else:
            status = INSUFFICIENT_EVIDENCE
        # 预算/超时是真实发生过的终止原因，不能被“有回答”覆盖
        if prior_status in (BUDGET_EXHAUSTED, TIMEOUT):
            status = prior_status
        elif state.get("plan_truncated"):
            status = BUDGET_EXHAUSTED

        if self.ctx.recorder is not None:
            self.ctx.recorder.record(
                "answer",
                node="compose",
                status=composed.status,
                output_summary={
                    "answer_status": composed.status,
                    "mode": composed.mode,
                    "labels": [item["label"] for item in composed.citations][:12],
                },
                error_category=None if composed.grounding.get("ok") else "grounding_warning",
                meta={
                    "numeric_violations": composed.grounding.get("numeric_violations", [])[:6],
                    "citation_issues": len(composed.grounding.get("citation_issues", [])),
                },
            )
        return {
            "steps": steps,
            "facts": fact_sheet.as_dict(),
            "answer": composed.to_dict(),
            "status": status,
            "warnings": composed.warnings,
        }

    def _decide_refusal(
        self, state: dict[str, Any], fact_sheet, kind: str
    ) -> tuple[str | None, str, list[str]]:
        """判断是否必须拒答/请求补充信息，并给出执行说明。"""
        intent = state.get("intent") or {}
        slots = Slots.from_dict(intent.get("slots") or {})
        notes: list[str] = []
        observations = list(state.get("observations") or [])
        errors = [item for item in observations if item.get("status") == "error"]

        if kind == "out_of_scope":
            flags = intent.get("security_flags") or []
            message = SECURITY_MESSAGE if flags else OUT_OF_SCOPE_MESSAGE
            if flags:
                notes.append(f"安全护栏命中：{list(flags)[:3]}")
            return message, "refused", notes

        if kind == "unsupported":
            message = str(intent.get("clarification") or "无法判断问题类型，请补充设备、时间范围与关注测点。")
            return message, "insufficient_evidence", notes

        equipment_errors = [
            item for item in errors if (item.get("error") or {}).get("code") == "equipment_not_found"
        ]
        if equipment_errors:
            code = slots.equipment_code
            message = (
                f"设备 {code!r} 在工业数据库中不存在，因此无法查询其运行数据。"
                "请确认设备编号（数据集中唯一的逻辑编号为 "
                f"{self.settings.default_equipment_code}）。"
            )
            if fact_sheet.has_knowledge:
                message += " 下面只保留与设备无关的技术知识检索结果。"
            return message, "insufficient_evidence", notes + ["设备不存在，数据查询已被阻断"]

        if bool(intent.get("needs_time_window")):
            hint = ""
            if fact_sheet.equipment:
                hint = (
                    f"该设备在数据集中的观测范围为 {fact_sheet.equipment.get('first_observed_at')} "
                    f"至 {fact_sheet.equipment.get('last_observed_at')}。"
                )
            message = (
                "缺少可用的时间范围，无法进行数据查询。"
                "请在问题中给出无时区日期（如 2020年4月17日 / 2020-04-17），"
                "或通过 start_time 与 end_time 传入窗口。" + hint
            )
            return message, "insufficient_evidence", notes

        data_tools = [
            item
            for item in observations
            if item.get("tool")
            in (TOOL_SENSOR_STATISTICS, TOOL_DATA_QUALITY, "get_sensor_history")
        ]
        if data_tools and all(item.get("status") == "empty" for item in data_tools):
            window = (fact_sheet.window or {})
            message = (
                f"在窗口 [{window.get('start')}, {window.get('end')}) 内没有查到任何观测记录"
                "（空结果，不是查询失败）。"
            )
            if fact_sheet.quality:
                message += (
                    f"质量查询显示实际样本 {fact_sheet.quality.get('actual_samples')} / 应有 "
                    f"{fact_sheet.quality.get('expected_samples')}，"
                    f"窗口是否落在观测范围内：{fact_sheet.quality.get('window_inside_data_range')}。"
                )
            message += "因此无法给出该时间段的数值结论。请调整时间范围或确认设备数据覆盖情况。"
            return message, "insufficient_evidence", notes

        if kind == "knowledge_only" and not fact_sheet.has_knowledge:
            message = (
                "现有知识库没有检索到与该问题相关的文档片段，因此无法确认。"
                "请补充设备型号、部件名称或文档线索后重试；本系统不会在无证据时给出结论。"
            )
            return message, "insufficient_evidence", notes

        if kind == "mixed_analysis" and not fact_sheet.has_knowledge:
            notes.append("知识检索没有返回可引用片段，回答只包含数据部分")
        if not fact_sheet.has_data and not fact_sheet.has_knowledge:
            message = (
                "本次没有任何成功的工具返回结果，无法给出有证据的回答。"
                "请检查设备编号与时间范围，或稍后重试。"
            )
            return message, "insufficient_evidence", notes

        for item in observations:
            if item.get("status") == "error":
                error = item.get("error") or {}
                notes.append(
                    f"工具 {item.get('tool')} 失败：{error.get('category')}/{error.get('code')}"
                )
        return None, "answered", notes

    # ------------------------------------------------------------------ #
    def finalize(self, state: dict[str, Any]) -> dict[str, Any]:
        steps = self._bump(state)
        status = str(state.get("status") or "running")
        if status == "running":
            status = COMPLETED if state.get("answer") else FAILED
        answer = state.get("answer") or {}
        evidence_refs = [item.get("label") for item in (answer.get("citations") or [])]
        summary: dict[str, Any] | None = None
        if self.ctx.recorder is not None:
            summary = self.ctx.recorder.finish(
                question=state.get("question", ""),
                execution_status=status,
                intent=str((state.get("intent") or {}).get("kind") or "unknown"),
                tools_used=tools_used(state),
                tool_calls=int(state.get("tool_call_count", 0)),
                rounds=int(state.get("rounds", 0)),
                evidence_refs=[str(label) for label in evidence_refs if label],
                errors=list(state.get("errors") or []),
                warnings=list(state.get("warnings") or []),
            ).to_summary_dict()
        return {
            "steps": steps,
            "status": status,
            "answer": {**answer, "execution_status": status} if answer else answer,
            "trace": summary,
        }


__all__ = ["AgentNodes", "MAX_EXECUTION_NOTES", "OUT_OF_SCOPE_MESSAGE", "SECURITY_MESSAGE"]
