"""领域对象 → Pydantic Schema 的转换（保持 Router 只做参数与响应装配）。"""

from __future__ import annotations

from app.schemas.common import EquipmentBrief, EquipmentOut, Pagination, WindowInfo
from app.services.catalog import TimeWindow
from app.services.equipment_info import EquipmentSnapshot


def equipment_brief(snapshot: EquipmentSnapshot) -> EquipmentBrief:
    return EquipmentBrief(
        equipment_id=snapshot.equipment_code,
        equipment_name=snapshot.equipment_name,
        equipment_type=snapshot.equipment_type,
    )


def equipment_out(snapshot: EquipmentSnapshot) -> EquipmentOut:
    return EquipmentOut(
        id=snapshot.id,
        equipment_id=snapshot.equipment_code,
        equipment_name=snapshot.equipment_name,
        equipment_type=snapshot.equipment_type,
        description=snapshot.description,
        created_at=snapshot.created_at,
        first_observed_at=snapshot.first_observed_at,
        last_observed_at=snapshot.last_observed_at,
    )


def window_info(window: TimeWindow) -> WindowInfo:
    return WindowInfo(
        start=window.start,
        end=window.end,
        duration_seconds=window.duration_seconds,
    )


def pagination(*, limit: int, offset: int, total: int, returned: int) -> Pagination:
    return Pagination(
        limit=limit,
        offset=offset,
        total=total,
        returned=returned,
        has_more=offset + returned < total,
    )
