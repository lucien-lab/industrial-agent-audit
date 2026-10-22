"""确定性规则基类与公共工具。

硬约束：
- 规则**不调用 LLM**，不访问业务数据库，不执行工具；
- 规则只能读取规范化事件与冻结证据清单；
- 无法判断时必须返回 `UNKNOWN`，不得默认 `PASS`；
- 每条结果都要带 rule_id、类别、严重度、触发事件与可解释原因。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from app.audit.config import AuditRuleConfig, AuditSettings
from app.audit.evidence import EvidenceIndex
from app.audit.schemas import (
    FindingSource,
    NormalizedTrace,
    RuleResult,
    RuleSpec,
    Severity,
    Verdict,
)


@dataclass(frozen=True)
class RuleContext:
    trace: NormalizedTrace
    evidence: EvidenceIndex
    config: AuditRuleConfig
    settings: AuditSettings

    @property
    def answer_event_id(self) -> str | None:
        event = next(
            (item for item in reversed(self.trace.events) if item.event_type == "answer"),
            None,
        )
        return event.event_id if event else None

    def tool_result_event_ids(self, tool: str) -> list[str]:
        return [
            event.event_id
            for event in self.trace.events
            if event.event_type == "tool_result" and event.tool_name == tool
        ]


class Rule(Protocol):
    spec: RuleSpec

    def evaluate(self, ctx: RuleContext) -> list[RuleResult]: ...


def make_result(
    spec: RuleSpec,
    verdict: Verdict,
    explanation: str,
    *,
    related_event_ids: Sequence[str] = (),
    evidence_refs: Sequence[str] = (),
    details: dict[str, Any] | None = None,
    severity: Severity | None = None,
    requires_human_review: bool = False,
    confidence: float | None = None,
    source: FindingSource = FindingSource.RULE,
) -> RuleResult:
    return RuleResult(
        rule_id=spec.rule_id,
        rule_name=spec.rule_name,
        category=spec.category,
        severity=severity or spec.severity,
        verdict=verdict,
        source=source,
        explanation=explanation,
        evidence_refs=[str(item) for item in evidence_refs][:50],
        related_event_ids=[str(item) for item in related_event_ids][:50],
        details=details or {},
        requires_human_review=requires_human_review,
        confidence=confidence,
    )


_ORDER = {
    Verdict.NOT_APPLICABLE: 0,
    Verdict.PASS: 1,
    Verdict.UNKNOWN: 2,
    Verdict.FAIL: 3,
}


def combine_verdicts(verdicts: Sequence[Verdict]) -> Verdict:
    """把一个规则的多个子检查汇总为最严重结论（FAIL > UNKNOWN > PASS > N/A）。"""
    if not verdicts:
        return Verdict.NOT_APPLICABLE
    return max(verdicts, key=lambda item: _ORDER[item])


def summarize_results(spec: RuleSpec, results: list[RuleResult]) -> RuleResult:
    """规则级汇总（用于结果矩阵；findings 仍然是逐条结果）。"""
    verdict = combine_verdicts([item.verdict for item in results])
    failures = [item for item in results if item.verdict == Verdict.FAIL]
    unknowns = [item for item in results if item.verdict == Verdict.UNKNOWN]
    chosen = failures or unknowns or results
    return RuleResult(
        rule_id=spec.rule_id,
        rule_name=spec.rule_name,
        category=spec.category,
        severity=spec.severity,
        verdict=verdict,
        source=results[0].source if results else FindingSource.RULE,
        explanation=(
            f"{len(failures)} 项违规 / {len(unknowns)} 项无法判断；"
            + ("；".join(item.explanation for item in chosen[:3]) if chosen else "无适用检查")
        )[:2000],
        evidence_refs=sorted({ref for item in chosen for ref in item.evidence_refs})[:50],
        related_event_ids=sorted({eid for item in chosen for eid in item.related_event_ids})[:50],
        details={"sub_results": len(results)},
        requires_human_review=any(item.requires_human_review for item in results),
        confidence=None,
    )


def tool_result_events(trace: NormalizedTrace):
    return trace.events_of("tool_result")


def tool_call_inputs(trace: NormalizedTrace, evidence: EvidenceIndex) -> list[dict[str, Any]]:
    """工具调用入参：证据清单优先（完整），否则回退事件摘要。"""
    return evidence.tool_inputs()


__all__ = [
    "Rule",
    "RuleContext",
    "combine_verdicts",
    "make_result",
    "summarize_results",
    "tool_call_inputs",
    "tool_result_events",
]
