"""传感器历史数据 Service（对应未来 Agent Tool：`get_sensor_history`）。

语义约定：
- 时间窗左闭右开 `[start, end)`；
- 稳定排序 `(observed_at, id)`，因此分页不会重复或遗漏；
- `limit` 默认 100、硬上限 1000（与 Phase 1B Repository 的 5,000 上限叠加，
  取更严格者）；
- 只按白名单投影列，客户端无法指定任意 SQL 列名。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import QUALITY_FLAG_FROZEN_CANDIDATE, describe_flags

from app.services import queries
from app.services.catalog import (
    DEFAULT_PAGE_SIZE,
    SENSOR_KINDS,
    TimeWindow,
    make_time_window,
    normalize_sensors,
    validate_page,
)
from app.services.equipment_info import EquipmentSnapshot, get_equipment_info


@dataclass(frozen=True)
class SensorHistoryRow:
    """一条原始观测（只投影被请求的传感器列）。"""

    id: int
    observed_at: datetime
    source_row_number: int
    source_id: uuid.UUID
    quality_flags: int
    quality_labels: tuple[str, ...]
    frozen_candidate: bool
    values: dict[str, float | int]


@dataclass(frozen=True)
class SensorHistoryPage:
    equipment: EquipmentSnapshot
    window: TimeWindow
    sensors: tuple[str, ...]
    items: list[SensorHistoryRow]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


def _value(reading, name: str) -> float | int:
    raw = getattr(reading, name)
    if SENSOR_KINDS[name] == "digital":
        return int(raw)
    # NUMERIC(8,4) -> float：4 位小数在 float64 内可精确表示，不引入额外误差
    return float(raw)


def get_sensor_history(
    session: Session,
    equipment_code: str,
    *,
    start: str | datetime,
    end: str | datetime,
    sensors: str | list[str] | tuple[str, ...] | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
    exclude_frozen: bool = False,
) -> SensorHistoryPage:
    """按时间窗分页读取原始观测。

    `exclude_frozen=True` 时排除“冻结候选”行（仅按质量位过滤，不删除、不改值）；
    默认 False，以保证默认视图与库内原始数据完全一致。
    """
    window = make_time_window(start, end)
    limit, offset = validate_page(limit, offset)
    selected = normalize_sensors(sensors)
    equipment = get_equipment_info(session, equipment_code)

    # [start, end) 与闭区间 [start, end-1µs] 等价（源时间戳精确到秒，DB CHECK 保证无微秒）
    closed_end = window.inclusive_end
    total = queries.count_readings(
        session, equipment.id, window.start, closed_end, exclude_frozen=exclude_frozen
    )
    rows = queries.readings_between(
        session,
        equipment.id,
        window.start,
        closed_end,
        limit=limit,
        offset=offset,
        exclude_frozen=exclude_frozen,
    )
    items = [
        SensorHistoryRow(
            id=int(reading.id),
            observed_at=reading.observed_at,
            source_row_number=int(reading.source_row_number),
            source_id=reading.source_id,
            quality_flags=int(reading.quality_flags),
            quality_labels=tuple(describe_flags(int(reading.quality_flags))),
            frozen_candidate=bool(int(reading.quality_flags) & QUALITY_FLAG_FROZEN_CANDIDATE),
            values={name: _value(reading, name) for name in selected},
        )
        for reading in rows
    ]
    return SensorHistoryPage(
        equipment=equipment,
        window=window,
        sensors=selected,
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )
