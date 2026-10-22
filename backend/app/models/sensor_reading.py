"""sensor_readings：MetroPT-3 原始监测记录（列式宽表，一行 = 一次多信号观测）。

字段命名直接对应 CSV 列名（小写化，`DV_eletric` 的官方拼写错误原样保留）：

    CSV 列          -> 数据库列
    TP2             -> tp2
    TP3             -> tp3
    H1              -> h1
    DV_pressure     -> dv_pressure
    Reservoirs      -> reservoirs
    Oil_temperature -> oil_temperature
    Motor_current   -> motor_current
    COMP            -> comp
    DV_eletric      -> dv_eletric
    Towers          -> towers
    MPG             -> mpg
    LPS             -> lps
    Pressure_switch -> pressure_switch
    Oil_level       -> oil_level
    Caudal_impulses -> caudal_impulses

关键决策（依据见 docs/database_design.md 与 docs/phase1b_report.md）：

1. `observed_at` 使用 `timestamp without time zone`：源时间戳无偏移，时区未知，
   不静默当作 UTC。原始文本另存 `source_timestamp_raw`，并用 CHECK 约束保证
   两者一致，杜绝导入过程产生“隐性时区换算”。
2. 模拟量使用 `NUMERIC(8,4)`：实测原始文本是 4 位小数值的 float64 序列化噪声
   （|raw − round(raw,4)| < 3e-14，1,516,948 行全部成立），定点存储不丢信息，
   且逐位相等比较仍然有效（冻结判据依赖它）。不使用 float4/float8。
3. 数字量使用 `SMALLINT` + `CHECK IN (0,1)`：实测原始文本只有 '0.0'/'1.0'，
   但语义（极性）未全部核验，因此不用 boolean 伪装成已确认的布尔含义。
4. `source_row_number` 保留 CSV 首列原值（0,10,20,… 步长 10），**不是** 1,2,3…
   的自增行号；它与 `source_id` 一起构成幂等身份。
5. 质量标记是行级位掩码（见 app/models/quality.py）；采样缺口属于窗口级缺陷，
   记录在 `data_gaps` 表，不塞进单行标记。
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
    SmallInteger,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.quality import FROZEN_RULE_VERSION

ANALOG_COLUMNS = (
    "tp2",
    "tp3",
    "h1",
    "dv_pressure",
    "reservoirs",
    "oil_temperature",
    "motor_current",
)

DIGITAL_COLUMNS = (
    "comp",
    "dv_eletric",
    "towers",
    "mpg",
    "lps",
    "pressure_switch",
    "oil_level",
    "caudal_impulses",
)

_DIGITAL_CHECK = " AND ".join(f"{c} IN (0, 1)" for c in DIGITAL_COLUMNS)


class SensorReading(Base):
    __tablename__ = "sensor_readings"
    __table_args__ = (
        # 幂等导入的身份：同一来源文件中的同一行只能存在一次
        Index("uq_sensor_readings_source_row", "source_id", "source_row_number", unique=True),
        # 时间范围查询的主索引
        Index("ix_sensor_readings_equipment_observed_at", "equipment_id", "observed_at"),
        # 质量查询走部分索引，避免全表扫描
        Index(
            "ix_sensor_readings_quality_flags",
            "equipment_id",
            "observed_at",
            postgresql_where=text("quality_flags <> 0"),
        ),
        CheckConstraint(_DIGITAL_CHECK, name="digital_signals_binary"),
        CheckConstraint("quality_flags >= 0", name="quality_flags_non_negative"),
        # 源文本与入库时间必须严格一致：禁止任何隐式时区/格式转换
        CheckConstraint(
            "observed_at = (source_timestamp_raw)::timestamp",
            name="observed_at_matches_source_text",
        ),
        {"comment": "MetroPT-3 原始观测；equipment_id + observed_at 建联合索引"},
    )

    # 事实表使用 BIGINT 自增主键（而非 UUID）：单表千万级、COPY 批量写入，
    # 顺序键的索引局部性与体积都显著优于随机 UUID；业务唯一性由
    # (source_id, source_row_number) 保证。
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    equipment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("equipment.id", ondelete="RESTRICT"), nullable=False
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("data_sources.id", ondelete="RESTRICT"), nullable=False
    )
    # CSV 首列原值，步长 10（0,10,20,…）
    source_row_number: Mapped[int] = mapped_column(Integer, nullable=False)

    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    source_timestamp_raw: Mapped[str] = mapped_column(String(19), nullable=False)

    tp2: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    tp3: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    h1: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    dv_pressure: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    reservoirs: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    oil_temperature: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    motor_current: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)

    comp: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    dv_eletric: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    towers: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    mpg: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    lps: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    pressure_switch: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    oil_level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    caudal_impulses: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    quality_flags: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    quality_rule_version: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text(f"'{FROZEN_RULE_VERSION}'")
    )

    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<SensorReading row={self.source_row_number} at={self.observed_at}>"
