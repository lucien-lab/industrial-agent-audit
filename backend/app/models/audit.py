"""审计数据模型（阶段 4，仅写入独立审计库）。

三类数据严格分离：
- `audit_traces` / `audit_trace_events`：阶段 3 执行的**规范化只读索引**
  （权威来源仍是 Agent 的 JSONL；这里只是为了 API 查询与回放，冲突时以文件为准）；
- `audit_runs`：一次审计运行（版本、模式、耗时、风险等级、完整结果矩阵）；
- `audit_findings`：可定位到 Trace 事件与证据的审计发现（FAIL/UNKNOWN）。

约束：
- 审计结果只追加，不覆盖历史版本；同一自然键（trace + 模式 + 版本 + 配置 + Trace 摘要）
  幂等复用已有记录；
- 不存储连接串、密码或 API Key：字段只保存脱敏后的摘要与业务标识。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

AUDIT_MODES = ("rule_only", "llm_only", "hybrid")
AUDIT_STATUSES = ("completed", "partial", "failed")
RISK_LEVELS = ("none", "low", "medium", "high", "critical", "unknown")
SEVERITIES = ("info", "low", "medium", "high", "critical")
VERDICTS = ("pass", "fail", "not_applicable", "unknown")
REVIEW_STATUSES = ("pending", "confirmed", "dismissed")
FINDING_SOURCES = ("rule", "judge")
TRACE_SOURCES = ("file", "memory", "database", "inline", "case")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class AuditTrace(Base):
    """一次执行链的索引元数据（不替代原始 JSONL）。"""

    __tablename__ = "audit_traces"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="file")
    source_ref: Mapped[str | None] = mapped_column(Text())
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_version: Mapped[str | None] = mapped_column(String(64))
    question: Mapped[str | None] = mapped_column(Text())
    execution_status: Mapped[str | None] = mapped_column(String(32))
    intent: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trace_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_digest: Mapped[str | None] = mapped_column(String(64))
    redaction_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    problems: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    __table_args__ = (
        CheckConstraint(f"source IN ({_in_list(TRACE_SOURCES)})", name="trace_source"),
        Index("ix_audit_traces_execution_status", "execution_status"),
        Index("ix_audit_traces_ingested_at", "ingested_at"),
    )


class AuditTraceEvent(Base):
    """规范化事件索引（同一 trace 内 event_id 唯一）。"""

    __tablename__ = "audit_trace_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_pk: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audit_traces.id", ondelete="CASCADE"), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[str] = mapped_column(String(96), nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    node_name: Mapped[str | None] = mapped_column(String(64))
    tool_name: Mapped[str | None] = mapped_column(String(64))
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float())
    status: Mapped[str | None] = mapped_column(String(48))
    input_summary: Mapped[dict | list | None] = mapped_column(JSONB)
    output_summary: Mapped[dict | list | None] = mapped_column(JSONB)
    evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    source_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("trace_id", "event_id", name="uq_audit_trace_events_trace_id_event_id"),
        Index("ix_audit_trace_events_trace_id_sequence_number", "trace_id", "sequence_number"),
        Index("ix_audit_trace_events_tool_name", "tool_name"),
    )


class AuditRun(Base):
    """一次审计运行（同一 Trace 可用不同版本重复审计，互不覆盖）。"""

    __tablename__ = "audit_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    audit_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    audit_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    audit_version: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_config_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    judge_model: Mapped[str | None] = mapped_column(String(128))
    judge_provider: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    overall_risk: Mapped[str] = mapped_column(String(16), nullable=False)
    total_findings: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    high_risk_findings: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requires_human_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trace_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    input_digest: Mapped[str | None] = mapped_column(String(64))
    trace_schema_version: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    trace_source: Mapped[str] = mapped_column(String(16), nullable=False, default="file")
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    judge_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    judge_prompt_tokens: Mapped[int | None] = mapped_column(Integer())
    judge_completion_tokens: Mapped[int | None] = mapped_column(Integer())
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    results: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    judge_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    diagnostics: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    external_reads: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    config_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    __table_args__ = (
        UniqueConstraint(
            "trace_id",
            "audit_mode",
            "audit_version",
            "rule_config_version",
            "trace_digest",
            name="uq_audit_runs_natural_key",
        ),
        CheckConstraint(f"audit_mode IN ({_in_list(AUDIT_MODES)})", name="audit_mode"),
        CheckConstraint(f"status IN ({_in_list(AUDIT_STATUSES)})", name="status"),
        CheckConstraint(f"overall_risk IN ({_in_list(RISK_LEVELS)})", name="overall_risk"),
        Index("ix_audit_runs_trace_id_started_at", "trace_id", "started_at"),
        Index("ix_audit_runs_overall_risk", "overall_risk"),
        Index("ix_audit_runs_audit_version", "audit_version"),
    )


class AuditFinding(Base):
    """可定位到事件与证据的审计发现（只保存 FAIL / UNKNOWN）。"""

    __tablename__ = "audit_findings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    finding_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    audit_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("audit_runs.audit_id", ondelete="CASCADE"), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_name: Mapped[str] = mapped_column(String(160), nullable=False)
    risk_category: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    verdict: Mapped[str] = mapped_column(String(24), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    related_event_ids: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    explanation: Mapped[str] = mapped_column(Text(), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float())
    requires_human_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    review_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )

    __table_args__ = (
        CheckConstraint(f"severity IN ({_in_list(SEVERITIES)})", name="severity"),
        CheckConstraint(f"verdict IN ({_in_list(VERDICTS)})", name="verdict"),
        CheckConstraint(f"source IN ({_in_list(FINDING_SOURCES)})", name="finding_source"),
        CheckConstraint(f"review_status IN ({_in_list(REVIEW_STATUSES)})", name="review_status"),
        Index("ix_audit_findings_audit_id_severity", "audit_id", "severity"),
        Index("ix_audit_findings_trace_id_created_at", "trace_id", "created_at"),
        Index("ix_audit_findings_risk_category", "risk_category"),
        Index("ix_audit_findings_review_status", "review_status"),
    )


__all__ = [
    "AUDIT_MODES",
    "AUDIT_STATUSES",
    "FINDING_SOURCES",
    "REVIEW_STATUSES",
    "RISK_LEVELS",
    "SEVERITIES",
    "TRACE_SOURCES",
    "VERDICTS",
    "AuditFinding",
    "AuditRun",
    "AuditTrace",
    "AuditTraceEvent",
]
