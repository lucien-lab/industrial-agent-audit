"""data_sources：原始数据来源登记。

数据身份 = 文件名 + SHA-256。源行号与 source_id 一起构成观测的唯一身份，
因此重复导入同一文件不会产生重复行（见 sensor_readings 唯一约束）。

`timezone_status` 是本项目对“时间含义”的显式声明：源时间戳不带偏移，
官方未声明时区，因此默认 `unknown`，禁止静默按 UTC 解释。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
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

TIMEZONE_STATUSES = ("unknown", "utc_declared", "local_declared")


class DataSource(Base):
    __tablename__ = "data_sources"
    __table_args__ = (
        # 数据身份以内容哈希为准（P1-A）：同一 SHA-256 只能登记一次，
        # 文件名只是元数据。保留 (source_file, file_sha256) 作为历史兼容约束。
        UniqueConstraint("file_sha256", name="uq_data_sources_content_sha256"),
        UniqueConstraint("source_file", "file_sha256", name="uq_data_sources_file_sha256"),
        CheckConstraint(
            "timezone_status IN ('unknown', 'utc_declared', 'local_declared')",
            name="timezone_status_valid",
        ),
        CheckConstraint("file_sha256 ~ '^[0-9a-f]{64}$'", name="file_sha256_hex"),
        {"comment": "原始数据来源登记：数据身份 = 文件名 + SHA-256"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    source_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(512))
    source_file: Mapped[str] = mapped_column(String(256), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_bytes: Mapped[int | None] = mapped_column(BigInteger)
    license: Mapped[str | None] = mapped_column(String(64))
    doi: Mapped[str | None] = mapped_column(String(64))
    # 源时间戳的时区状态：unknown / utc_declared / local_declared
    timezone_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'unknown'")
    )
    timezone_note: Mapped[str | None] = mapped_column(Text)
    # 实测标称采样间隔（秒），用于覆盖度计算；不是官方宣称值
    nominal_sample_interval_s: Mapped[float | None] = mapped_column(Numeric(8, 3))
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    note: Mapped[str | None] = mapped_column(Text)

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<DataSource {self.source_file} sha256={self.file_sha256[:8]}…>"
