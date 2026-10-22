"""官方故障事件接口。"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import LimitQuery, OffsetQuery, SessionDep
from app.api.serializers import equipment_brief, pagination, window_info
from app.schemas.common import ErrorResponse
from app.schemas.responses import FaultEventOut, FaultListResponse, FaultSourceOut
from app.services import fault_events as service

router = APIRouter(prefix="/api/v1/equipment/{equipment_id}", tags=["故障事件"])

OptionalStart = Query(
    default=None,
    description="可选窗口起点（含），无时区 ISO 8601；与 end_time 必须成对出现",
    examples=["2020-04-01T00:00:00"],
)
OptionalEnd = Query(
    default=None,
    description="可选窗口终点（不含），无时区 ISO 8601；与 start_time 必须成对出现",
    examples=["2020-05-01T00:00:00"],
)


@router.get(
    "/faults",
    response_model=FaultListResponse,
    summary="查询官方故障事件",
    description=(
        "返回与查询窗口**相交**的官方故障事件（来自 PDF Failure Information 的区间级事实）。\n\n"
        "**重叠判定**：`event.start_time < end_time AND event.end_time > start_time`，"
        "因此刚好首尾相接的区间不会被算作重叠。响应同时给出 `overlap_start` / "
        "`overlap_end` / `overlap_seconds`。\n\n"
        "**重要语义**：故障区间内的传感器记录**不会**被自动视为已确认故障样本；"
        "本服务不在 `sensor_readings` 上生成逐行故障标签（见 `sample_label_semantics`）。\n\n"
        "`start_time` / `end_time` 可同时省略（表示不设时间边界），必须成对出现。"
    ),
    responses={
        404: {"model": ErrorResponse, "description": "设备不存在"},
        422: {"model": ErrorResponse, "description": "参数不合法 / 时间带时区"},
    },
)
def get_faults(
    equipment_id: str,
    session: SessionDep,
    start_time: str | None = OptionalStart,
    end_time: str | None = OptionalEnd,
    limit: LimitQuery = 100,
    offset: OffsetQuery = 0,
) -> FaultListResponse:
    page = service.get_fault_events(
        session, equipment_id, start=start_time, end=end_time, limit=limit, offset=offset
    )
    return FaultListResponse(
        equipment=equipment_brief(page.equipment),
        window=window_info(page.window) if page.window else None,
        items=[
            FaultEventOut(
                id=item.id,
                event_code=item.event_code,
                pdf_label=item.pdf_label,
                start_time=item.start_time,
                end_time=item.end_time,
                duration_seconds=item.duration_seconds,
                overlap_start=item.overlap_start,
                overlap_end=item.overlap_end,
                overlap_seconds=item.overlap_seconds,
                failure_type=item.failure_type,
                severity=item.severity,
                fault_description=item.fault_description,
                maintenance_note=item.maintenance_note,
                source_reference=item.source_reference,
                verification_status=item.verification_status,
                verification_note=item.verification_note,
                source=(
                    FaultSourceOut(
                        id=item.source.id,
                        source_file=item.source.source_file,
                        source_name=item.source.source_name,
                        source_url=item.source.source_url,
                        license=item.source.license,
                        doi=item.source.doi,
                        timezone_status=item.source.timezone_status,
                    )
                    if item.source
                    else None
                ),
            )
            for item in page.items
        ],
        pagination=pagination(
            limit=page.limit, offset=page.offset, total=page.total, returned=len(page.items)
        ),
        sample_label_semantics=page.sample_label_semantics,
    )
