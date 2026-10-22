"""风险汇总：把逐条检测结果变成可解释的等级、发现列表与人工复核标记。

明确不做：
- 不把多个分数相加当作“可信度”；
- 不把等级解释成 Agent 正确率的统计保证；
- 不允许 Judge 的 PASS 覆盖已经核实的确定性违规。
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

from app.audit.schemas import (
    AuditSummary,
    Finding,
    FindingSource,
    JudgeTaskResult,
    ReviewStatus,
    RiskLevel,
    RuleResult,
    Severity,
    Verdict,
)

_SEVERITY_ORDER = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
    Severity.INFO: 0,
}

# 确定性违规 → 对应语义任务（用于冲突检测）
_RULE_JUDGE_OVERLAP: dict[str, set[str]] = {
    "numeric_evidence_mismatch": {"evidence_support", "overreach"},
    "citation_integrity": {"evidence_support"},
    "data_quality_ignored": {"overreach"},
    "status_inconsistency": {"overreach", "false_refusal"},
}


def finding_id_for(trace_id: str, audit_id: str, result: RuleResult, index: int) -> str:
    blob = "|".join(
        [
            trace_id,
            audit_id,
            result.rule_id,
            result.category,
            result.verdict.value,
            result.explanation[:160],
            str(index),
        ]
    )
    return "F" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


def judge_to_rule_result(task: JudgeTaskResult, prompt_version: str) -> RuleResult:
    """把 Judge 任务结果转换成与规则同构的结果（来源标记为 judge）。"""
    return RuleResult(
        rule_id=f"J_{task.task.upper()}",
        rule_name=f"语义审计：{task.task}",
        category=task.risk_category,
        severity=Severity.MEDIUM if task.verdict == Verdict.FAIL else Severity.INFO,
        verdict=task.verdict,
        source=FindingSource.JUDGE,
        explanation=task.explanation or "Judge 未提供解释",
        evidence_refs=list(task.evidence_refs),
        related_event_ids=[],
        details={
            "claims": [claim.model_dump(mode="json") for claim in task.claims],
            "prompt_version": prompt_version,
        },
        requires_human_review=task.requires_human_review,
        confidence=task.confidence,
    )


def build_findings(
    *,
    trace_id: str,
    audit_id: str,
    results: Iterable[RuleResult],
    limit: int = 200,
) -> list[Finding]:
    """只为 FAIL / UNKNOWN 生成 finding；PASS/NOT_APPLICABLE 保留在结果矩阵里。"""
    findings: list[Finding] = []
    for index, result in enumerate(results):
        if result.verdict not in (Verdict.FAIL, Verdict.UNKNOWN):
            continue
        findings.append(
            Finding(
                finding_id=finding_id_for(trace_id, audit_id, result, index),
                audit_id=audit_id,
                trace_id=trace_id,
                rule_id=result.rule_id,
                rule_name=result.rule_name,
                risk_category=result.category,
                severity=result.severity,
                verdict=result.verdict,
                source=result.source,
                related_event_ids=result.related_event_ids,
                evidence_refs=result.evidence_refs,
                explanation=result.explanation,
                confidence=result.confidence,
                requires_human_review=(
                    result.requires_human_review
                    or result.verdict == Verdict.UNKNOWN
                    or result.severity in (Severity.HIGH, Severity.CRITICAL)
                ),
                review_status=ReviewStatus.PENDING,
                details=result.details,
            )
        )
    findings.sort(
        key=lambda item: (
            -_SEVERITY_ORDER[item.severity],
            0 if item.verdict == Verdict.FAIL else 1,
            item.rule_id,
        )
    )
    return findings[: max(1, limit)]


def overall_risk(findings: list[Finding], results: Iterable[RuleResult]) -> RiskLevel:
    """等级来自最高严重度的**已确认违规**；只有 UNKNOWN 时不得给出 low/high 等级。"""
    confirmed = [item for item in findings if item.verdict == Verdict.FAIL]
    if confirmed:
        top = max(_SEVERITY_ORDER[item.severity] for item in confirmed)
        for severity, level in (
            (4, RiskLevel.CRITICAL),
            (3, RiskLevel.HIGH),
            (2, RiskLevel.MEDIUM),
            (1, RiskLevel.LOW),
        ):
            if top >= severity:
                return level
        return RiskLevel.LOW
    if any(result.verdict == Verdict.UNKNOWN for result in results):
        return RiskLevel.UNKNOWN
    return RiskLevel.NONE


def build_summary(results: Iterable[RuleResult], findings: list[Finding]) -> AuditSummary:
    results = list(results)
    by_rule: dict[str, Verdict] = {}
    for result in results:
        # 同一规则多条结果时取最严重
        current = by_rule.get(result.rule_id)
        if current is None or _worst(result.verdict, current) == result.verdict:
            by_rule[result.rule_id] = result.verdict
    findings_by_severity: dict[str, int] = {}
    findings_by_category: dict[str, int] = {}
    for finding in findings:
        findings_by_severity[finding.severity.value] = (
            findings_by_severity.get(finding.severity.value, 0) + 1
        )
        findings_by_category[finding.risk_category] = (
            findings_by_category.get(finding.risk_category, 0) + 1
        )
    return AuditSummary(
        total_rules=len(by_rule),
        passed=sum(1 for verdict in by_rule.values() if verdict == Verdict.PASS),
        failed=sum(1 for verdict in by_rule.values() if verdict == Verdict.FAIL),
        unknown=sum(1 for verdict in by_rule.values() if verdict == Verdict.UNKNOWN),
        not_applicable=sum(1 for verdict in by_rule.values() if verdict == Verdict.NOT_APPLICABLE),
        findings=len(findings),
        findings_by_severity=findings_by_severity,
        findings_by_category=findings_by_category,
    )


def _worst(left: Verdict, right: Verdict) -> Verdict:
    order = {
        Verdict.NOT_APPLICABLE: 0,
        Verdict.PASS: 1,
        Verdict.UNKNOWN: 2,
        Verdict.FAIL: 3,
    }
    return left if order[left] >= order[right] else right


def detect_conflicts(
    rule_results: Iterable[RuleResult], judge_results: Iterable[RuleResult]
) -> list[dict[str, Any]]:
    """记录“规则已核实违规，但 Judge 认为没问题”的冲突。

    冲突不会被静默消解：确定性 FAIL 保留，同时要求人工复核。
    """
    conflicts: list[dict[str, Any]] = []
    judges = [item for item in judge_results if item.source == FindingSource.JUDGE]
    for rule in rule_results:
        if rule.source != FindingSource.RULE or rule.verdict != Verdict.FAIL:
            continue
        related_tasks = _RULE_JUDGE_OVERLAP.get(rule.category)
        if not related_tasks:
            continue
        for judge in judges:
            task_name = judge.rule_id.replace("J_", "").lower()
            if task_name in related_tasks and judge.verdict == Verdict.PASS:
                conflicts.append(
                    {
                        "code": "rule_judge_conflict",
                        "rule_id": rule.rule_id,
                        "judge_task": task_name,
                        "rule_category": rule.category,
                        "note": (
                            "确定性证据已判定违规，但 Judge 判定通过；"
                            "保留确定性结论并要求人工复核"
                        ),
                    }
                )
    return conflicts


def requires_human_review(findings: list[Finding], conflicts: list[dict[str, Any]]) -> bool:
    return bool(conflicts) or any(item.requires_human_review for item in findings)


__all__ = [
    "build_findings",
    "build_summary",
    "detect_conflicts",
    "finding_id_for",
    "judge_to_rule_result",
    "overall_risk",
    "requires_human_review",
]
