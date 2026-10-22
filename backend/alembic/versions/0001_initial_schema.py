"""Phase 1B 初始表结构：设备、来源、传感器观测、故障事件、导入日志、缺口。

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-10-22

说明：
- 同时启用 pgvector 扩展（本阶段不建向量列，只保证后续 RAG 阶段无需改集群）。
- 所有表结构由本迁移管理，应用启动时不会自动建表。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ANALOG_COLUMNS = (
    "tp2",
    "tp3",
    "h1",
    "dv_pressure",
    "reservoirs",
    "oil_temperature",
    "motor_current",
)

_DIGITAL_COLUMNS = (
    "comp",
    "dv_eletric",
    "towers",
    "mpg",
    "lps",
    "pressure_switch",
    "oil_level",
    "caudal_impulses",
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "equipment",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("equipment_code", sa.String(64), nullable=False, unique=True),
        sa.Column("equipment_name", sa.String(128), nullable=False),
        sa.Column("equipment_type", sa.String(64), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        comment="设备档案；MetroPT-3 使用逻辑编号 metropt3-apu-01，非厂商序列号",
    )

    op.create_table(
        "data_sources",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("source_name", sa.String(128), nullable=False),
        sa.Column("source_url", sa.String(512)),
        sa.Column("source_file", sa.String(256), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("file_bytes", sa.BigInteger()),
        sa.Column("license", sa.String(64)),
        sa.Column("doi", sa.String(64)),
        sa.Column(
            "timezone_status", sa.String(32), nullable=False, server_default=sa.text("'unknown'")
        ),
        sa.Column("timezone_note", sa.Text()),
        sa.Column("nominal_sample_interval_s", sa.Numeric(8, 3)),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("note", sa.Text()),
        sa.UniqueConstraint("source_file", "file_sha256", name="uq_data_sources_file_sha256"),
        sa.CheckConstraint(
            "timezone_status IN ('unknown', 'utc_declared', 'local_declared')",
            name="ck_data_sources_timezone_status_valid",
        ),
        sa.CheckConstraint("file_sha256 ~ '^[0-9a-f]{64}$'", name="ck_data_sources_file_sha256_hex"),
        comment="原始数据来源登记：数据身份 = 文件名 + SHA-256",
    )

    op.create_table(
        "sensor_readings",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "equipment_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("equipment.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("data_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_row_number", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("source_timestamp_raw", sa.String(19), nullable=False),
        *[sa.Column(c, sa.Numeric(8, 4), nullable=False) for c in _ANALOG_COLUMNS],
        *[sa.Column(c, sa.SmallInteger(), nullable=False) for c in _DIGITAL_COLUMNS],
        sa.Column("quality_flags", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "quality_rule_version",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'frozen-v1'"),
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            " AND ".join(f"{c} IN (0, 1)" for c in _DIGITAL_COLUMNS),
            name="ck_sensor_readings_digital_signals_binary",
        ),
        sa.CheckConstraint("quality_flags >= 0", name="ck_sensor_readings_quality_flags_non_negative"),
        sa.CheckConstraint(
            "observed_at = (source_timestamp_raw)::timestamp",
            name="ck_sensor_readings_observed_at_matches_source_text",
        ),
        comment="MetroPT-3 原始观测；equipment_id + observed_at 建联合索引",
    )

    op.create_index(
        "uq_sensor_readings_source_row",
        "sensor_readings",
        ["source_id", "source_row_number"],
        unique=True,
    )
    op.create_index(
        "ix_sensor_readings_equipment_observed_at",
        "sensor_readings",
        ["equipment_id", "observed_at"],
    )
    op.create_index(
        "ix_sensor_readings_quality_flags",
        "sensor_readings",
        ["equipment_id", "observed_at"],
        postgresql_where=sa.text("quality_flags <> 0"),
    )

    op.create_table(
        "fault_events",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "equipment_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("equipment.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("data_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("event_code", sa.String(16), nullable=False, unique=True),
        sa.Column("pdf_label", sa.String(64), nullable=False),
        sa.Column("start_time", sa.DateTime(timezone=False), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=False), nullable=False),
        sa.Column("failure_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(32)),
        sa.Column("fault_description", sa.Text()),
        sa.Column("maintenance_note", sa.Text()),
        sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column(
            "verification_status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'verified'"),
        ),
        sa.Column("verification_note", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("end_time > start_time", name="ck_fault_events_end_after_start"),
        sa.CheckConstraint(
            "verification_status IN ('verified', 'typo_suspected', 'pending')",
            name="ck_fault_events_verification_status_valid",
        ),
        comment="官方故障区间（PDF Failure Information），不生成逐行标签",
    )
    op.create_index(
        "ix_fault_events_equipment_start_time", "fault_events", ["equipment_id", "start_time"]
    )

    op.create_table(
        "ingest_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "source_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("data_sources.id", ondelete="RESTRICT"),
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'running'")),
        sa.Column("csv_path", sa.Text()),
        sa.Column("chunk_size", sa.Integer()),
        sa.Column("max_rows", sa.BigInteger()),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_seconds", sa.Numeric(12, 3)),
        sa.Column("attempted_rows", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("inserted_rows", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("conflict_rows", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("invalid_rows", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("gap_rows_written", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("frozen_rows", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("importer_version", sa.String(32)),
        sa.Column("notes", sa.Text()),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'dry_run')",
            name="ck_ingest_runs_status_valid",
        ),
        comment="每次导入的可审计日志",
    )
    op.create_index("ix_ingest_runs_source_started_at", "ingest_runs", ["source_id", "started_at"])

    op.create_table(
        "data_gaps",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "equipment_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("equipment.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("data_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("gap_start", sa.DateTime(timezone=False), nullable=False),
        sa.Column("gap_end", sa.DateTime(timezone=False), nullable=False),
        sa.Column("gap_seconds", sa.Numeric(12, 3), nullable=False),
        sa.Column("missing_samples", sa.Integer(), nullable=False),
        sa.Column(
            "threshold_seconds", sa.Numeric(8, 3), nullable=False, server_default=sa.text("30.0")
        ),
        sa.Column("rule_version", sa.String(32), nullable=False, server_default=sa.text("'gap-v1'")),
        sa.Column(
            "detected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "source_id", "gap_start", "gap_end", "rule_version", name="uq_data_gaps_source_window_rule"
        ),
        sa.CheckConstraint("gap_end > gap_start", name="ck_data_gaps_gap_end_after_start"),
        sa.CheckConstraint("missing_samples > 0", name="ck_data_gaps_missing_samples_positive"),
        comment="窗口级采样缺口（≥30 s）；不写入单行质量标记",
    )
    op.create_index("ix_data_gaps_equipment_gap_start", "data_gaps", ["equipment_id", "gap_start"])


def downgrade() -> None:
    op.drop_table("data_gaps")
    op.drop_index("ix_ingest_runs_source_started_at", table_name="ingest_runs")
    op.drop_table("ingest_runs")
    op.drop_index("ix_fault_events_equipment_start_time", table_name="fault_events")
    op.drop_table("fault_events")
    op.drop_index("ix_sensor_readings_quality_flags", table_name="sensor_readings")
    op.drop_index("ix_sensor_readings_equipment_observed_at", table_name="sensor_readings")
    op.drop_index("uq_sensor_readings_source_row", table_name="sensor_readings")
    op.drop_table("sensor_readings")
    op.drop_table("data_sources")
    op.drop_table("equipment")
    # 说明：不回滚 CREATE EXTENSION vector——扩展可能被后续对象依赖，
    # 且删除扩展会影响共享集群。确需删除时须人工确认。
