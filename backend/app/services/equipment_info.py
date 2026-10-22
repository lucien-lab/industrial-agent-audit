"""设备信息 Service（对应未来 Agent Tool：`get_equipment_info`）。

职责：把 `equipment` 表与观测时间跨度整理成稳定的领域对象；
不依赖 FastAPI、不拼 HTTP 响应。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Equipment
from app.services import queries
from app.services.catalog import validate_page
from app.services.errors import EquipmentNotFound


@dataclass(frozen=True)
class EquipmentSnapshot:
    """设备档案 + 观测时间跨度。

    注意：`equipment_code` 是数据集逻辑编号（如 `metropt3-apu-01`），不是厂商序列号。
    """

    id: uuid.UUID
    equipment_code: str
    equipment_name: str
    equipment_type: str
    description: str | None
    created_at: datetime
    first_observed_at: datetime | None
    last_observed_at: datetime | None


def _to_snapshot(equipment, span: tuple[datetime | None, datetime | None]) -> EquipmentSnapshot:
    return EquipmentSnapshot(
        id=equipment.id,
        equipment_code=equipment.equipment_code,
        equipment_name=equipment.equipment_name,
        equipment_type=equipment.equipment_type,
        description=equipment.description,
        created_at=equipment.created_at,
        first_observed_at=span[0],
        last_observed_at=span[1],
    )


def _spans(
    session: Session, equipment_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[datetime | None, datetime | None]]:
    """一次 SQL 取回当前页设备的观测跨度（每台设备走索引两端，避免全表聚合与 N+1）。"""
    return queries.equipment_spans(session, equipment_ids)


def resolve_equipment(session: Session, equipment_code: str):
    """按逻辑编号取设备；不存在时抛 `EquipmentNotFound`（API 层映射为 404）。"""
    equipment = queries.get_equipment(session, equipment_code)
    if equipment is None:
        raise EquipmentNotFound(f"设备 {equipment_code!r} 不存在", equipment_code=equipment_code)
    return equipment


def get_equipment_info(session: Session, equipment_code: str) -> EquipmentSnapshot:
    """单台设备详情。"""
    equipment = resolve_equipment(session, equipment_code)
    span = queries.equipment_time_span(session, equipment.id)
    return _to_snapshot(equipment, span)


def list_equipment_infos(
    session: Session, *, limit: int = 100, offset: int = 0
) -> tuple[list[EquipmentSnapshot], int]:
    """设备列表（按编号排序，稳定分页）+ 总数。"""
    limit, offset = validate_page(limit, offset)
    rows = queries.list_equipment(session, limit=limit, offset=offset)
    total = int(session.scalar(select(func.count()).select_from(Equipment)) or 0)
    spans = _spans(session, [row.id for row in rows])
    return [
        _to_snapshot(row, spans.get(row.id, (None, None))) for row in rows
    ], total
