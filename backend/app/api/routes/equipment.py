"""设备信息接口。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import LimitQuery, OffsetQuery, SessionDep
from app.api.serializers import equipment_out
from app.schemas.common import ErrorResponse
from app.schemas.responses import EquipmentListResponse, EquipmentOut
from app.services import equipment_info as service

router = APIRouter(prefix="/api/v1/equipment", tags=["设备"])


@router.get(
    "",
    response_model=EquipmentListResponse,
    summary="设备列表",
    description=(
        "返回设备档案列表（按 `equipment_code` 升序，稳定分页）。\n\n"
        "同时给出每台设备的观测时间跨度（`first_observed_at` / `last_observed_at`），"
        "用于判断某个时间窗内是否可能有数据。"
    ),
    responses={422: {"model": ErrorResponse, "description": "分页参数不合法"}},
)
def list_equipment(
    session: SessionDep,
    limit: LimitQuery = 100,
    offset: OffsetQuery = 0,
) -> EquipmentListResponse:
    snapshots, total = service.list_equipment_infos(session, limit=limit, offset=offset)
    return EquipmentListResponse(items=[equipment_out(item) for item in snapshots], total=total)


@router.get(
    "/{equipment_id}",
    response_model=EquipmentOut,
    summary="设备详情",
    description=(
        "按设备逻辑编号查询详情。\n\n"
        "`equipment_id` 是数据集逻辑编号（`metropt3-apu-01`），不是厂商序列号；"
        "编号不存在返回 404。"
    ),
    responses={
        404: {"model": ErrorResponse, "description": "设备不存在"},
    },
)
def get_equipment(equipment_id: str, session: SessionDep) -> EquipmentOut:
    snapshot = service.get_equipment_info(session, equipment_id)
    return equipment_out(snapshot)
