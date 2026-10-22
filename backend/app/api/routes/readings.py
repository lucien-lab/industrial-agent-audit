"""传感器历史数据接口。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import (
    EndTimeQuery,
    ExcludeFrozenQuery,
    LimitQuery,
    OffsetQuery,
    SensorQuery,
    SessionDep,
    StartTimeQuery,
)
from app.api.serializers import equipment_brief, pagination, window_info
from app.schemas.common import ErrorResponse
from app.schemas.responses import ReadingListResponse, ReadingOut
from app.services import sensor_history as service

router = APIRouter(prefix="/api/v1/equipment/{equipment_id}", tags=["传感器历史数据"])

COMMON_ERRORS = {
    404: {"model": ErrorResponse, "description": "设备不存在"},
    422: {"model": ErrorResponse, "description": "参数不合法 / 时间带时区 / 传感器不支持"},
}


@router.get(
    "/readings",
    response_model=ReadingListResponse,
    summary="按时间窗分页查询原始观测",
    description=(
        "返回指定时间窗内的原始传感器记录。\n\n"
        "**边界**：`[start_time, end_time)`，包含起点、不包含终点。\n\n"
        "**排序**：`(observed_at, id)` 升序，因此翻页不会重复或遗漏。\n\n"
        "**分页**：`limit` 默认 100、**硬上限 1000**（超过返回 422，不静默截断）；"
        "总量见 `pagination.total`。\n\n"
        "**列投影**：`sensor` 只能取白名单内的 15 个列名（模拟量 7 + 数字量 8），"
        "客户端不能传入任意 SQL 列名；省略表示返回全部 15 路。\n\n"
        "**质量位**：默认不排除任何行；`exclude_frozen=true` 时仅按质量位过滤"
        "（冻结候选），不删除也不修改数据。"
    ),
    responses=COMMON_ERRORS,
)
def get_readings(
    equipment_id: str,
    session: SessionDep,
    start_time: StartTimeQuery,
    end_time: EndTimeQuery,
    sensor: SensorQuery = None,
    limit: LimitQuery = 100,
    offset: OffsetQuery = 0,
    exclude_frozen: ExcludeFrozenQuery = False,
) -> ReadingListResponse:
    page = service.get_sensor_history(
        session,
        equipment_id,
        start=start_time,
        end=end_time,
        sensors=sensor,
        limit=limit,
        offset=offset,
        exclude_frozen=exclude_frozen,
    )
    return ReadingListResponse(
        equipment=equipment_brief(page.equipment),
        window=window_info(page.window),
        sensors=list(page.sensors),
        items=[
            ReadingOut(
                id=row.id,
                observed_at=row.observed_at,
                source_row_number=row.source_row_number,
                source_id=row.source_id,
                quality_flags=row.quality_flags,
                quality_labels=list(row.quality_labels),
                frozen_candidate=row.frozen_candidate,
                values=row.values,
            )
            for row in page.items
        ],
        pagination=pagination(
            limit=page.limit, offset=page.offset, total=page.total, returned=len(page.items)
        ),
        exclude_frozen=exclude_frozen,
    )
