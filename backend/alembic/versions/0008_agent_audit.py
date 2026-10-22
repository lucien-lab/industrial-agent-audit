"""阶段 4：Agent-Probe 审计表（audit_traces / audit_trace_events / audit_runs / audit_findings）。

Revision ID: 0008_agent_audit
Revises: 0007_knowledge_rag
Create Date: 2026-10-25

安全边界：
- 本迁移**只新增对象**，不修改任何工业数据、RAG 文档或 Embedding；
- 只应在独立审计/测试库执行（`AUDIT_DATABASE_URL` / `TEST_DATABASE_URL`），
  绝不自动迁移主库（主库仍停留在 0004，需人工批准后另行执行）；
- 审计结果追加式保存：同一自然键幂等，不同审计版本互不覆盖。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_agent_audit"
down_revision: str | None = "0007_knowledge_rag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AUDIT_MODES = ("rule_only", "llm_only", "hybrid")
AUDIT_STATUSES = ("completed", "partial", "failed")
RISK_LEVELS = ("none", "low", "medium", "high", "critical", "unknown")
SEVERITIES = ("info", "low", "medium", "high", "critical")
VERDICTS = ("pass", "fail", "not_applicable", "unknown")
REVIEW_STATUSES = ("pending", "confirmed", "dismissed")
FINDING_SOURCES = ("rule", "judge")
TRACE_SOURCES = ("file", "memory", "database", "inline", "case")


def _in_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "audit_traces",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("trace_id", sa.String(64), nullable=False, unique=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("source_ref", sa.Text()),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("agent_version", sa.String(64)),
        sa.Column("question", sa.Text()),
        sa.Column("execution_status", sa.String(32)),
        sa.Column("intent", sa.String(32)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("trace_digest", sa.String(64), nullable=False),
        sa.Column("raw_digest", sa.String(64)),
        sa.Column("redaction_applied", sa.Boolean(), nullable=False),
        sa.Column(
            "problems", postgresql.JSONB(), nullable=False
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(f"source IN ({_in_list(TRACE_SOURCES)})", name="trace_source"),
    )
    op.create_index("ix_audit_traces_execution_status", "audit_traces", ["execution_status"])
    op.create_index("ix_audit_traces_ingested_at", "audit_traces", ["ingested_at"])

    op.create_table(
        "audit_trace_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "trace_pk",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("audit_traces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("event_id", sa.String(96), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("node_name", sa.String(64)),
        sa.Column("tool_name", sa.String(64)),
        sa.Column("event_time", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Float()),
        sa.Column("status", sa.String(48)),
        sa.Column("input_summary", postgresql.JSONB()),
        sa.Column("output_summary", postgresql.JSONB()),
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column("error_type", sa.String(64)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("source_index", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "trace_id", "event_id", name="uq_audit_trace_events_trace_id_event_id"
        ),
    )
    op.create_index(
        "ix_audit_trace_events_trace_id_sequence_number",
        "audit_trace_events",
        ["trace_id", "sequence_number"],
    )
    op.create_index("ix_audit_trace_events_tool_name", "audit_trace_events", ["tool_name"])

    op.create_table(
        "audit_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("audit_id", sa.String(64), nullable=False, unique=True),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("audit_mode", sa.String(16), nullable=False),
        sa.Column("audit_version", sa.String(64), nullable=False),
        sa.Column("rule_config_version", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(64)),
        sa.Column("judge_model", sa.String(128)),
        sa.Column("judge_provider", sa.String(32)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("overall_risk", sa.String(16), nullable=False),
        sa.Column("total_findings", sa.Integer(), nullable=False),
        sa.Column("high_risk_findings", sa.Integer(), nullable=False),
        sa.Column(
            "requires_human_review",
            sa.Boolean(),
            nullable=False,
        ),
        sa.Column("trace_digest", sa.String(64), nullable=False),
        sa.Column("input_digest", sa.String(64)),
        sa.Column(
            "trace_schema_version", sa.String(32), nullable=False
        ),
        sa.Column("trace_source", sa.String(16), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("judge_calls", sa.Integer(), nullable=False),
        sa.Column("judge_prompt_tokens", sa.Integer()),
        sa.Column("judge_completion_tokens", sa.Integer()),
        sa.Column(
            "summary", postgresql.JSONB(), nullable=False
        ),
        sa.Column(
            "results", postgresql.JSONB(), nullable=False
        ),
        sa.Column(
            "judge_metadata",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "diagnostics",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "external_reads",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "config_snapshot",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "trace_id",
            "audit_mode",
            "audit_version",
            "rule_config_version",
            "trace_digest",
            name="uq_audit_runs_natural_key",
        ),
        sa.CheckConstraint(
            f"audit_mode IN ({_in_list(AUDIT_MODES)})", name="audit_mode"
        ),
        sa.CheckConstraint(f"status IN ({_in_list(AUDIT_STATUSES)})", name="status"),
        sa.CheckConstraint(
            f"overall_risk IN ({_in_list(RISK_LEVELS)})", name="overall_risk"
        ),
    )
    op.create_index("ix_audit_runs_trace_id_started_at", "audit_runs", ["trace_id", "started_at"])
    op.create_index("ix_audit_runs_overall_risk", "audit_runs", ["overall_risk"])
    op.create_index("ix_audit_runs_audit_version", "audit_runs", ["audit_version"])

    op.create_table(
        "audit_findings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("finding_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "audit_id",
            sa.String(64),
            sa.ForeignKey("audit_runs.audit_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("rule_id", sa.String(64), nullable=False),
        sa.Column("rule_name", sa.String(160), nullable=False),
        sa.Column("risk_category", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("verdict", sa.String(24), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column(
            "related_event_ids",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float()),
        sa.Column(
            "requires_human_review",
            sa.Boolean(),
            nullable=False,
        ),
        sa.Column("review_status", sa.String(24), nullable=False),
        sa.Column(
            "details", postgresql.JSONB(), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint(f"severity IN ({_in_list(SEVERITIES)})", name="severity"),
        sa.CheckConstraint(f"verdict IN ({_in_list(VERDICTS)})", name="verdict"),
        sa.CheckConstraint(
            f"source IN ({_in_list(FINDING_SOURCES)})", name="finding_source"
        ),
        sa.CheckConstraint(
            f"review_status IN ({_in_list(REVIEW_STATUSES)})",
            name="review_status",
        ),
    )
    op.create_index(
        "ix_audit_findings_audit_id_severity", "audit_findings", ["audit_id", "severity"]
    )
    op.create_index(
        "ix_audit_findings_trace_id_created_at", "audit_findings", ["trace_id", "created_at"]
    )
    op.create_index("ix_audit_findings_risk_category", "audit_findings", ["risk_category"])
    op.create_index("ix_audit_findings_review_status", "audit_findings", ["review_status"])


def downgrade() -> None:
    op.drop_index("ix_audit_findings_review_status", table_name="audit_findings")
    op.drop_index("ix_audit_findings_risk_category", table_name="audit_findings")
    op.drop_index("ix_audit_findings_trace_id_created_at", table_name="audit_findings")
    op.drop_index("ix_audit_findings_audit_id_severity", table_name="audit_findings")
    op.drop_table("audit_findings")
    op.drop_index("ix_audit_runs_audit_version", table_name="audit_runs")
    op.drop_index("ix_audit_runs_overall_risk", table_name="audit_runs")
    op.drop_index("ix_audit_runs_trace_id_started_at", table_name="audit_runs")
    op.drop_table("audit_runs")
    op.drop_index("ix_audit_trace_events_tool_name", table_name="audit_trace_events")
    op.drop_index(
        "ix_audit_trace_events_trace_id_sequence_number", table_name="audit_trace_events"
    )
    op.drop_table("audit_trace_events")
    op.drop_index("ix_audit_traces_ingested_at", table_name="audit_traces")
    op.drop_index("ix_audit_traces_execution_status", table_name="audit_traces")
    op.drop_table("audit_traces")
