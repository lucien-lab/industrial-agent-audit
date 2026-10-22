"""equipment：设备档案。

当前只有 MetroPT-3 一台逻辑设备（`metropt3-apu-01`）。
它是**数据集逻辑编号**，不是厂商序列号，不得被解读为真实资产台账。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Equipment(Base):
    __tablename__ = "equipment"
    __table_args__ = {
        "comment": "设备档案；MetroPT-3 使用逻辑编号 metropt3-apu-01，非厂商序列号"
    }

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # 逻辑编号：数据集内稳定、可复现，不含伪造的厂商编号
    equipment_code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    equipment_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 例如 air_production_unit（空气生产单元 / 压缩机）
    equipment_type: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<Equipment {self.equipment_code}>"
