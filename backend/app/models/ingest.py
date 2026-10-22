"""导入审计与窗口级质量缺陷。

`ingest_runs` 让每次导入都可审计：谁、从哪个文件、导入了多少、跳过多少、耗时多久。
`data_gaps` 显式表达采样中断：缺口是“相邻两条记录之间”的信息，
不属于任何单条记录，因此不放进 `sensor_readings.quality_flags`。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IngestRun(Base):
    __tablename__ = "ingest_runs"
    __table_args__ = (
        # partial = 部分成功（中途失败但已有 chunk 提交，或按策略跳过了冲突行）
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'partial', 'dry_run')",
            name="status_valid",
        ),
        Index("ix_ingest_runs_source_started_at", "source_id", "started_at"),
        {"comment": "每次导入的可审计日志"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_sources.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'running'"))

    csv_path: Mapped[str | None] = mapped_column(Text)
    chunk_size: Mapped[int | None] = mapped_column(Integer)
    max_rows: Mapped[int | None] = mapped_column(BigInteger)
    dry_run: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))

    attempted_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    inserted_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    conflict_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    invalid_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    gap_rows_written: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    frozen_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))

    importer_version: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<IngestRun {self.id} {self.status} inserted={self.inserted_rows}>"


class DataGap(Base):
    __tablename__ = "data_gaps"
    __table_args__ = (
        # 同一来源、同一区间、同一规则版本只记一次（重复导入幂等）
        UniqueConstraint(
            "source_id",
            "gap_start",
            "gap_end",
            "rule_version",
            name="uq_data_gaps_source_window_rule",
        ),
        CheckConstraint("gap_end > gap_start", name="gap_end_after_start"),
        CheckConstraint("missing_samples > 0", name="missing_samples_positive"),
        Index("ix_data_gaps_equipment_gap_start", "equipment_id", "gap_start"),
        {"comment": "窗口级采样缺口（≥30 s）；不写入单行质量标记"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    equipment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("equipment.id", ondelete="RESTRICT"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_sources.id", ondelete="RESTRICT"), nullable=False
    )
    gap_start: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    gap_end: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    gap_seconds: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    missing_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    threshold_seconds: Mapped[Decimal] = mapped_column(
        Numeric(8, 3), nullable=False, server_default=text("30.0")
    )
    rule_version: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'gap-v1'")
    )
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<DataGap {self.gap_start}~{self.gap_end} missing={self.missing_samples}>"
