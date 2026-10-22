"""审计结果持久化（独立审计库，写连接与业务只读连接分离）。

幂等与版本隔离：
- `audit_runs` 的自然键 = (trace_id, audit_mode, audit_version, rule_config_version, trace_digest)；
  相同键重复审计返回已有记录，不重复写入；
- 同一 Trace 使用不同 `audit_version` / 规则配置 / Trace 内容时生成新的 audit_id，旧结果不被覆盖；
- `audit_traces` / `audit_trace_events` 只是阶段 3 JSONL 的规范化索引，
  写入前已脱敏；权威来源仍是 JSONL 文件。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from app.audit.config import AuditSettings, get_audit_settings
from app.audit.schemas import (
    AuditEvent,
    AuditMode,
    AuditReport,
    AuditStatus,
    AuditSummary,
    Finding,
    FindingSource,
    JudgeRunInfo,
    ReviewStatus,
    RiskLevel,
    RuleResult,
    Severity,
    TraceSourceKind,
    Verdict,
)
from app.db.session import get_engine
from app.models.audit import AuditFinding, AuditRun, AuditTrace, AuditTraceEvent


class AuditRepositoryError(RuntimeError):
    code = "audit_storage_unavailable"


def _parse_dt(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    text_value = str(value).strip()
    if not text_value:
        return None
    try:
        return datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def audit_id_for(
    *,
    trace_id: str,
    mode: str,
    audit_version: str,
    rule_config_version: str,
    prompt_version: str | None,
    trace_digest: str,
) -> str:
    blob = "|".join(
        [trace_id, mode, audit_version, rule_config_version, prompt_version or "", trace_digest]
    )
    return "aud-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


class AuditRepository:
    """审计库读写（只写审计表，不触碰工业数据与 RAG 表）。"""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        settings: AuditSettings | None = None,
    ) -> None:
        self.settings = settings or get_audit_settings()
        self.database_url = database_url or self.settings.audit_database_url
        if not self.database_url:
            raise AuditRepositoryError(
                "缺少 AUDIT_DATABASE_URL：审计结果持久化需要独立的审计数据库"
            )
        self._factory: sessionmaker[Session] | None = None

    # ------------------------------------------------------------------ #
    @contextmanager
    def session(self) -> Iterator[Session]:
        if self._factory is None:
            self._factory = sessionmaker(
                bind=get_engine(self.database_url), expire_on_commit=False
            )
        session = self._factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    def save_trace(self, trace: Any) -> None:
        """写入/更新规范化 Trace 索引（事件整体替换，保持顺序）。"""
        with self.session() as session:
            row = session.execute(
                select(AuditTrace).where(AuditTrace.trace_id == trace.trace_id)
            ).scalar_one_or_none()
            if row is None:
                row = AuditTrace(
                    trace_id=trace.trace_id,
                    source=trace.source.value,
                    source_ref=trace.source_ref,
                    schema_version=trace.trace_schema_version,
                    agent_version=trace.agent_version,
                    question=trace.question,
                    execution_status=trace.execution_status,
                    intent=trace.intent,
                    started_at=_parse_dt(trace.started_at),
                    ended_at=_parse_dt(trace.ended_at),
                    event_count=trace.event_count,
                    trace_digest=trace.trace_digest,
                    raw_digest=trace.raw_digest,
                    problems=trace.problems,
                )
                session.add(row)
                session.flush()
            else:
                row.source = trace.source.value
                row.source_ref = trace.source_ref
                row.schema_version = trace.trace_schema_version
                row.agent_version = trace.agent_version
                row.question = trace.question
                row.execution_status = trace.execution_status
                row.intent = trace.intent
                row.started_at = _parse_dt(trace.started_at)
                row.ended_at = _parse_dt(trace.ended_at)
                row.event_count = trace.event_count
                row.trace_digest = trace.trace_digest
                row.raw_digest = trace.raw_digest
                row.problems = trace.problems
                session.execute(
                    AuditTraceEvent.__table__.delete().where(
                        AuditTraceEvent.trace_id == trace.trace_id
                    )
                )
            session.add_all(
                [
                    AuditTraceEvent(
                        trace_pk=row.id,
                        trace_id=event.trace_id,
                        event_id=event.event_id,
                        sequence_number=event.sequence_number,
                        event_type=event.event_type,
                        node_name=event.node_name,
                        tool_name=event.tool_name,
                        event_time=_parse_dt(event.timestamp),
                        duration_ms=event.duration_ms,
                        status=event.status,
                        input_summary=event.input_summary,
                        output_summary=event.output_summary,
                        evidence_refs=event.evidence_refs,
                        error_type=event.error_type,
                        error_code=event.error_code,
                        schema_version=event.schema_version,
                        source_index=event.source_index,
                    )
                    for event in trace.events
                ]
            )

    # ------------------------------------------------------------------ #
    def load_trace_events(self, trace_id: str) -> list[dict[str, Any]] | None:
        with self.session() as session:
            rows = (
                session.execute(
                    select(AuditTraceEvent)
                    .where(AuditTraceEvent.trace_id == trace_id)
                    .order_by(AuditTraceEvent.sequence_number)
                )
                .scalars()
                .all()
            )
        if not rows:
            return None
        return [
            {
                "event_id": row.event_id,
                "trace_id": row.trace_id,
                "event_type": row.event_type,
                "node": row.node_name,
                "tool": row.tool_name,
                "status": row.status,
                "ended_at": _iso(row.event_time),
                "duration_ms": row.duration_ms,
                "input_summary": row.input_summary,
                "output_summary": row.output_summary,
                "evidence_refs": row.evidence_refs,
                "error_category": row.error_type,
                "error_code": row.error_code,
                "seq": row.sequence_number + 1,
            }
            for row in rows
        ]

    def get_trace_meta(self, trace_id: str) -> dict[str, Any] | None:
        with self.session() as session:
            row = session.execute(
                select(AuditTrace).where(AuditTrace.trace_id == trace_id)
            ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "trace_id": row.trace_id,
            "source": row.source,
            "source_ref": row.source_ref,
            "schema_version": row.schema_version,
            "agent_version": row.agent_version,
            "question": row.question,
            "execution_status": row.execution_status,
            "event_count": row.event_count,
            "trace_digest": row.trace_digest,
            "problems": row.problems,
            "ingested_at": _iso(row.ingested_at),
        }

    # ------------------------------------------------------------------ #
    def save_report(self, report: AuditReport) -> tuple[AuditReport, bool]:
        """插入审计结果；自然键冲突时返回已存在记录（不覆盖）。"""
        payload = self._run_payload(report)
        with self.session() as session:
            statement = (
                pg_insert(AuditRun)
                .values(**payload)
                .on_conflict_do_nothing(constraint="uq_audit_runs_natural_key")
                .returning(AuditRun.audit_id)
            )
            inserted = session.execute(statement).scalar_one_or_none()
            if inserted is None:
                existing = self.get_report(report.audit_id, session=session) or self._find_by_natural_key(
                    report, session=session
                )
                if existing is not None:
                    return existing, False
                raise AuditRepositoryError("审计结果写入冲突但无法读回已有记录")
            session.add_all([self._finding_row(item) for item in report.findings])
        return report, True

    def _run_payload(self, report: AuditReport) -> dict[str, Any]:
        high_risk = sum(
            1 for item in report.findings
            if item.severity in (Severity.HIGH, Severity.CRITICAL)
        )
        return {
            "audit_id": report.audit_id,
            "trace_id": report.trace_id,
            "audit_mode": report.audit_mode.value if isinstance(report.audit_mode, AuditMode) else report.audit_mode,
            "audit_version": report.audit_version,
            "rule_config_version": report.rule_config_version,
            "prompt_version": report.prompt_version,
            "judge_model": report.judge_model,
            "judge_provider": report.judge_provider,
            "status": report.status.value if isinstance(report.status, AuditStatus) else report.status,
            "overall_risk": report.overall_risk.value if isinstance(report.overall_risk, RiskLevel) else report.overall_risk,
            "total_findings": report.total_findings,
            "high_risk_findings": high_risk,
            "requires_human_review": report.requires_human_review,
            "trace_digest": report.trace_digest,
            "input_digest": report.input_digest,
            "trace_schema_version": report.trace_schema_version,
            "trace_source": report.trace_source.value
            if isinstance(report.trace_source, TraceSourceKind)
            else report.trace_source,
            "event_count": report.event_count,
            "started_at": report.started_at,
            "completed_at": report.completed_at,
            "duration_ms": report.duration_ms,
            "judge_calls": report.judge.attempts,
            "judge_prompt_tokens": report.judge.prompt_tokens,
            "judge_completion_tokens": report.judge.completion_tokens,
            "summary": report.summary.model_dump(mode="json"),
            "results": [item.model_dump(mode="json") for item in report.results],
            "judge_metadata": report.judge.model_dump(mode="json"),
            "diagnostics": report.diagnostics,
            "external_reads": report.external_reads,
            "config_snapshot": report.config_snapshot,
            "error": report.error,
        }

    def _finding_row(self, finding: Finding) -> AuditFinding:
        return AuditFinding(
            finding_id=finding.finding_id,
            audit_id=str(finding.audit_id),
            trace_id=finding.trace_id,
            rule_id=finding.rule_id,
            rule_name=finding.rule_name,
            risk_category=finding.risk_category,
            severity=finding.severity.value
            if isinstance(finding.severity, Severity)
            else finding.severity,
            verdict=finding.verdict.value
            if isinstance(finding.verdict, Verdict)
            else finding.verdict,
            source=finding.source.value
            if isinstance(finding.source, FindingSource)
            else finding.source,
            related_event_ids=finding.related_event_ids,
            evidence_refs=finding.evidence_refs,
            explanation=finding.explanation,
            confidence=finding.confidence,
            requires_human_review=finding.requires_human_review,
            review_status=finding.review_status.value
            if isinstance(finding.review_status, ReviewStatus)
            else finding.review_status,
            details=finding.details,
        )

    def _find_by_natural_key(
        self, report: AuditReport, *, session: Session
    ) -> AuditReport | None:
        row = session.execute(
            select(AuditRun).where(
                AuditRun.trace_id == report.trace_id,
                AuditRun.audit_mode == report.audit_mode,
                AuditRun.audit_version == report.audit_version,
                AuditRun.rule_config_version == report.rule_config_version,
                AuditRun.trace_digest == report.trace_digest,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return self._report_from_row(row, session=session)

    # ------------------------------------------------------------------ #
    def get_report(
        self, audit_id: str, *, session: Session | None = None
    ) -> AuditReport | None:
        if session is not None:
            row = session.execute(
                select(AuditRun).where(AuditRun.audit_id == audit_id)
            ).scalar_one_or_none()
            return self._report_from_row(row, session=session) if row else None
        with self.session() as own:
            row = own.execute(
                select(AuditRun).where(AuditRun.audit_id == audit_id)
            ).scalar_one_or_none()
            return self._report_from_row(row, session=own) if row else None

    def _report_from_row(self, row: AuditRun, *, session: Session) -> AuditReport:
        findings = (
            session.execute(
                select(AuditFinding)
                .where(AuditFinding.audit_id == row.audit_id)
                .order_by(AuditFinding.created_at, AuditFinding.rule_id)
            )
            .scalars()
            .all()
        )
        results = [RuleResult.model_validate(item) for item in (row.results or [])]
        return AuditReport(
            audit_id=row.audit_id,
            trace_id=row.trace_id,
            audit_mode=AuditMode(row.audit_mode),
            audit_version=row.audit_version,
            rule_config_version=row.rule_config_version,
            prompt_version=row.prompt_version,
            judge_model=row.judge_model,
            judge_provider=row.judge_provider,
            status=AuditStatus(row.status),
            overall_risk=RiskLevel(row.overall_risk),
            total_findings=row.total_findings,
            requires_human_review=row.requires_human_review,
            started_at=row.started_at,
            completed_at=row.completed_at,
            duration_ms=row.duration_ms,
            trace_digest=row.trace_digest,
            input_digest=row.input_digest,
            event_count=row.event_count,
            trace_schema_version=row.trace_schema_version,
            trace_source=TraceSourceKind(row.trace_source),
            results=results,
            findings=[
                Finding(
                    finding_id=item.finding_id,
                    audit_id=item.audit_id,
                    trace_id=item.trace_id,
                    rule_id=item.rule_id,
                    rule_name=item.rule_name,
                    risk_category=item.risk_category,
                    severity=Severity(item.severity),
                    verdict=Verdict(item.verdict),
                    source=FindingSource(item.source),
                    related_event_ids=item.related_event_ids,
                    evidence_refs=item.evidence_refs,
                    explanation=item.explanation,
                    confidence=item.confidence,
                    requires_human_review=item.requires_human_review,
                    review_status=ReviewStatus(item.review_status),
                    details=item.details,
                )
                for item in findings
            ],
            summary=AuditSummary.model_validate(row.summary or {}),
            judge=JudgeRunInfo.model_validate(
                {**(row.judge_metadata or {}), "attempts": row.judge_calls}
            ),
            diagnostics=list(row.diagnostics or []),
            external_reads=list(row.external_reads or []),
            config_snapshot=dict(row.config_snapshot or {}),
            error=row.error,
        )

    # ------------------------------------------------------------------ #
    def list_runs(
        self,
        *,
        trace_id: str | None = None,
        mode: str | None = None,
        risk: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        conditions = []
        if trace_id:
            conditions.append(AuditRun.trace_id == trace_id)
        if mode:
            conditions.append(AuditRun.audit_mode == mode)
        if risk:
            conditions.append(AuditRun.overall_risk == risk)
        with self.session() as session:
            total = session.execute(
                select(func.count()).select_from(AuditRun).where(*conditions)
            ).scalar_one()
            rows = (
                session.execute(
                    select(AuditRun)
                    .where(*conditions)
                    .order_by(AuditRun.started_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
                .scalars()
                .all()
            )
        return [self._run_brief(row) for row in rows], int(total)

    @staticmethod
    def _run_brief(row: AuditRun) -> dict[str, Any]:
        return {
            "audit_id": row.audit_id,
            "trace_id": row.trace_id,
            "audit_mode": row.audit_mode,
            "audit_version": row.audit_version,
            "rule_config_version": row.rule_config_version,
            "prompt_version": row.prompt_version,
            "judge_model": row.judge_model,
            "status": row.status,
            "overall_risk": row.overall_risk,
            "total_findings": row.total_findings,
            "high_risk_findings": row.high_risk_findings,
            "requires_human_review": row.requires_human_review,
            "started_at": _iso(row.started_at),
            "completed_at": _iso(row.completed_at),
            "duration_ms": row.duration_ms,
            "trace_digest": row.trace_digest,
        }

    def list_findings(
        self,
        *,
        audit_id: str | None = None,
        trace_id: str | None = None,
        risk_category: str | None = None,
        severity: str | None = None,
        verdict: str | None = None,
        review_status: str | None = None,
        rule_id: str | None = None,
        started_after: datetime | None = None,
        started_before: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        conditions = []
        if audit_id:
            conditions.append(AuditFinding.audit_id == audit_id)
        if trace_id:
            conditions.append(AuditFinding.trace_id == trace_id)
        if risk_category:
            conditions.append(AuditFinding.risk_category == risk_category)
        if severity:
            conditions.append(AuditFinding.severity == severity)
        if verdict:
            conditions.append(AuditFinding.verdict == verdict)
        if review_status:
            conditions.append(AuditFinding.review_status == review_status)
        if rule_id:
            conditions.append(AuditFinding.rule_id == rule_id)
        if started_after:
            conditions.append(AuditFinding.created_at >= started_after)
        if started_before:
            conditions.append(AuditFinding.created_at <= started_before)
        with self.session() as session:
            total = session.execute(
                select(func.count()).select_from(AuditFinding).where(*conditions)
            ).scalar_one()
            rows = (
                session.execute(
                    select(AuditFinding)
                    .where(*conditions)
                    .order_by(AuditFinding.created_at.desc(), AuditFinding.finding_id)
                    .limit(limit)
                    .offset(offset)
                )
                .scalars()
                .all()
            )
        return [
            {
                "finding_id": row.finding_id,
                "audit_id": row.audit_id,
                "trace_id": row.trace_id,
                "rule_id": row.rule_id,
                "rule_name": row.rule_name,
                "risk_category": row.risk_category,
                "severity": row.severity,
                "verdict": row.verdict,
                "source": row.source,
                "related_event_ids": row.related_event_ids,
                "evidence_refs": row.evidence_refs,
                "explanation": row.explanation,
                "confidence": row.confidence,
                "requires_human_review": row.requires_human_review,
                "review_status": row.review_status,
                "details": row.details,
                "created_at": _iso(row.created_at),
            }
            for row in rows
        ], int(total)

    # ------------------------------------------------------------------ #
    def update_review(self, finding_id: str, review_status: str, note: str | None = None) -> dict[str, Any] | None:
        """更新人工复核状态（只改审计表，不触碰原始 Trace/回答）。"""
        with self.session() as session:
            row = session.execute(
                select(AuditFinding).where(AuditFinding.finding_id == finding_id)
            ).scalar_one_or_none()
            if row is None:
                return None
            row.review_status = review_status
            details = dict(row.details or {})
            if note:
                details["review_note"] = note[:500]
                details["reviewed_at"] = _now().isoformat()
            row.details = details
            session.flush()
            return {
                "finding_id": row.finding_id,
                "review_status": row.review_status,
                "updated_at": details.get("reviewed_at"),
            }

    def summary(self, *, days: int = 30) -> dict[str, Any]:
        cutoff = _now() - timedelta(days=max(1, days))
        run_window = AuditRun.created_at >= cutoff
        finding_window = AuditFinding.created_at >= cutoff
        with self.session() as session:
            total_audits = session.execute(
                select(func.count()).select_from(AuditRun).where(run_window)
            ).scalar_one()
            by_mode = session.execute(
                select(AuditRun.audit_mode, func.count())
                .where(run_window)
                .group_by(AuditRun.audit_mode)
            ).all()
            by_risk = session.execute(
                select(AuditRun.overall_risk, func.count())
                .where(run_window)
                .group_by(AuditRun.overall_risk)
            ).all()
            by_status = session.execute(
                select(AuditRun.status, func.count())
                .where(run_window)
                .group_by(AuditRun.status)
            ).all()
            durations = session.execute(
                select(
                    func.avg(AuditRun.duration_ms),
                    func.max(AuditRun.duration_ms),
                ).where(run_window)
            ).one()
            finding_total = session.execute(
                select(func.count()).select_from(AuditFinding).where(finding_window)
            ).scalar_one()
            findings_by_category = session.execute(
                select(AuditFinding.risk_category, func.count())
                .where(finding_window)
                .group_by(AuditFinding.risk_category)
                .order_by(func.count().desc())
            ).all()
            findings_by_severity = session.execute(
                select(AuditFinding.severity, func.count())
                .where(finding_window)
                .group_by(AuditFinding.severity)
            ).all()
            review_pending = session.execute(
                select(func.count())
                .select_from(AuditFinding)
                .where(finding_window, AuditFinding.review_status == "pending")
            ).scalar_one()
            judge_distribution = session.execute(
                text(
                    "SELECT r->>'rule_id' AS rule_id, r->>'verdict' AS verdict, count(*) AS count "
                    "FROM audit_runs ar, jsonb_array_elements(ar.results) AS r "
                    "WHERE r->>'source' = 'judge' AND ar.created_at >= :cutoff "
                    "GROUP BY 1, 2 ORDER BY 1, 2"
                ),
                {"cutoff": cutoff},
            ).all()
            judge_calls = session.execute(
                select(func.coalesce(func.sum(AuditRun.judge_calls), 0)).where(run_window)
            ).scalar_one()
        return {
            "total_audits": int(total_audits),
            "audits_by_mode": {row[0]: int(row[1]) for row in by_mode},
            "audits_by_risk": {row[0]: int(row[1]) for row in by_risk},
            "audits_by_status": {row[0]: int(row[1]) for row in by_status},
            "total_findings": int(finding_total),
            "findings_by_category": {row[0]: int(row[1]) for row in findings_by_category},
            "findings_by_severity": {row[0]: int(row[1]) for row in findings_by_severity},
            "pending_human_review": int(review_pending),
            "judge_verdict_distribution": {
                f"{row[0]}:{row[1]}": int(row[2]) for row in judge_distribution
            },
            "judge_calls_total": int(judge_calls),
            "avg_duration_ms": round(float(durations[0] or 0.0), 2),
            "max_duration_ms": round(float(durations[1] or 0.0), 2),
            "window_days": days,
        }

    def audits_for_trace(self, trace_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        items, _ = self.list_runs(trace_id=trace_id, limit=limit)
        return items


def get_repository(settings: AuditSettings | None = None) -> AuditRepository | None:
    """按配置构造仓库；未配置审计库时返回 None（调用方必须显式处理）。"""
    settings = settings or get_audit_settings()
    if not settings.audit_database_url:
        return None
    return AuditRepository(settings=settings)


def _now() -> datetime:
    return datetime.now(timezone.utc)



__all__ = [
    "AuditRepository",
    "AuditRepositoryError",
    "audit_id_for",
    "get_repository",
]
