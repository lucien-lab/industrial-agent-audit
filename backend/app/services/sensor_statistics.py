"""传感器统计 Service（对应未来 Agent Tool：`get_sensor_statistics`）。

统计口径（必须显式，不能靠猜）：
- 全部聚合在 PostgreSQL 内完成，不把原始行加载到 Python；
- 不做插值、不做异常剔除、不剔除冻结候选（除非调用方显式 `exclude_frozen=True`）；
- 模拟量 `std` 为**样本标准差**（`stddev_samp`，ddof=1，与 pandas `Series.std()` 默认一致），
  样本数 < 2 时为 null；
- 数字量返回 0/1 计数与占比，`transitions` 为窗口内相邻行之间发生的状态切换次数；
- 模拟量与数字量的统计语义不同，因此在响应中分成 `analog` / `digital` 两组。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import NOMINAL_STEP_SECONDS

from app.services import queries
from app.services.catalog import (
    TimeWindow,
    make_time_window,
    normalize_sensors,
    split_sensors,
)
from app.services.equipment_info import EquipmentSnapshot, get_equipment_info

STD_METHOD = "stddev_samp (sample standard deviation, ddof=1)"

SEMANTICS: tuple[str, ...] = (
    "时间窗左闭右开 [start, end)；聚合只覆盖窗口内实际存在的观测行",
    "所有聚合在数据库内完成，无插值、无异常值剔除",
    f"模拟量 std 使用 {STD_METHOD}；count < 2 时为 null",
    "数字量 transitions 指窗口内相邻行之间的 0/1 状态变化次数，窗口首行不计为一次切换",
    "冻结候选默认保留在统计中（不静默丢弃记录）；如需排除请显式 exclude_frozen=true",
)


@dataclass(frozen=True)
class AnalogSensorStats:
    sensor: str
    count: int
    minimum: float | None
    maximum: float | None
    mean: float | None
    std: float | None
    std_method: str
    first_timestamp: datetime | None
    last_timestamp: datetime | None


@dataclass(frozen=True)
class DigitalSensorStats:
    sensor: str
    count: int
    count_0: int
    count_1: int
    ratio_0: float | None
    ratio_1: float | None
    transitions: int
    first_timestamp: datetime | None
    last_timestamp: datetime | None


@dataclass(frozen=True)
class SensorStatistics:
    equipment: EquipmentSnapshot
    window: TimeWindow
    sensors: tuple[str, ...]
    row_count: int
    analog: list[AnalogSensorStats]
    digital: list[DigitalSensorStats]
    nominal_step_seconds: float
    exclude_frozen: bool
    semantics: tuple[str, ...] = SEMANTICS


def _float_or_none(value) -> float | None:
    return None if value is None else float(value)


def get_sensor_statistics(
    session: Session,
    equipment_code: str,
    *,
    start: str | datetime,
    end: str | datetime,
    sensors: str | list[str] | tuple[str, ...] | None = None,
    exclude_frozen: bool = False,
) -> SensorStatistics:
    """时间窗内的模拟量/数字量统计。"""
    window = make_time_window(start, end)
    selected = normalize_sensors(sensors)
    analog_names, digital_names = split_sensors(selected)
    equipment = get_equipment_info(session, equipment_code)
    closed_end = window.inclusive_end

    row_count = queries.count_readings(
        session, equipment.id, window.start, closed_end, exclude_frozen=exclude_frozen
    )

    analog_stats: list[AnalogSensorStats] = []
    if analog_names:
        all_analog = queries.analog_statistics(
            session, equipment.id, window.start, closed_end, exclude_frozen=exclude_frozen
        )
        by_name = {item.column: item for item in all_analog}
        for name in analog_names:
            item = by_name[name]
            analog_stats.append(
                AnalogSensorStats(
                    sensor=name,
                    count=item.count,
                    minimum=_float_or_none(item.minimum),
                    maximum=_float_or_none(item.maximum),
                    mean=_float_or_none(item.average),
                    std=_float_or_none(item.stddev),
                    std_method=STD_METHOD,
                    first_timestamp=item.first_timestamp,
                    last_timestamp=item.last_timestamp,
                )
            )

    digital_stats: list[DigitalSensorStats] = []
    if digital_names:
        rows = queries.digital_statistics(
            session,
            equipment.id,
            window.start,
            closed_end,
            columns=digital_names,
            exclude_frozen=exclude_frozen,
        )
        for item in rows:
            digital_stats.append(
                DigitalSensorStats(
                    sensor=item.column,
                    count=item.count,
                    count_0=item.count_0,
                    count_1=item.count_1,
                    ratio_0=item.ratio_0,
                    ratio_1=item.ratio_1,
                    transitions=item.transitions,
                    first_timestamp=item.first_timestamp,
                    last_timestamp=item.last_timestamp,
                )
            )

    return SensorStatistics(
        equipment=equipment,
        window=window,
        sensors=selected,
        row_count=row_count,
        analog=analog_stats,
        digital=digital_stats,
        nominal_step_seconds=NOMINAL_STEP_SECONDS,
        exclude_frozen=exclude_frozen,
    )
