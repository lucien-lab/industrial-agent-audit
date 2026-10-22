"""fault_events：官方公开故障事件（区间级事实，不是逐行标签）。

数据来源：`Data Description_Metro.pdf` → Failure Information 表（4 次 Air leak）。
只导入官方原文区间，不编造事件；PDF 中的编号重复（两个 #1、缺 #2）与
“30 Apr”疑似笔误均按原文保留，只通过 `verification_status` 标注判断。

重要：本表不生成任何逐行故障标签，也不把区间内每一行视为独立故障样本。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

VERIFICATION_STATUSES = ("verified", "typo_suspected", "pending")


class FaultEvent(Base):
    __tablename__ = "fault_events"
    __table_args__ = (
        CheckConstraint("end_time > start_time", name="end_after_start"),
        CheckConstraint(
            "verification_status IN ('verified', 'typo_suspected', 'pending')",
            name="verification_status_valid",
        ),
        Index("ix_fault_events_equipment_start_time", "equipment_id", "start_time"),
        {"comment": "官方故障区间（PDF Failure Information），不生成逐行标签"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    equipment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("equipment.id", ondelete="RESTRICT"), nullable=False
    )
    # 故障报告来自 PDF 文件本身，同样登记在 data_sources 中
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_sources.id", ondelete="RESTRICT"), nullable=False
    )

    # 本项目内部编号 E1–E4，保证可重复导入
    event_code: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    # PDF 原文标签，例如 '#1'、'#1（PDF 中编号重复）'
    pdf_label: Mapped[str] = mapped_column(String(64), nullable=False)

    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)

    failure_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str | None] = mapped_column(String(32))
    fault_description: Mapped[str | None] = mapped_column(Text)
    # PDF 维护记录原文（可能为空，不做静默纠正）
    maintenance_note: Mapped[str | None] = mapped_column(Text)

    source_reference: Mapped[str] = mapped_column(Text, nullable=False)
    verification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'verified'")
    )
    verification_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<FaultEvent {self.event_code} {self.start_time}~{self.end_time}>"
