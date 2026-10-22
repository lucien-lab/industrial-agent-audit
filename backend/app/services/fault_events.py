"""官方故障事件 Service（对应未来 Agent Tool：`get_fault_events`）。

语义边界（很重要）：
- 故障事件是**区间级事实**（来自 PDF Failure Information），不是逐行标签；
- 区间与查询窗口按“半开区间相交”判定：`event.start < window.end and event.end > window.start`，
  因此刚好首尾相接的区间不会被误判为重叠；
- 响应中显式说明“区间内的传感器记录不等于已确认故障样本”，
  避免下游把 `fault_events` 当成逐行标注使用。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DataSource

from app.services import queries
from app.services.catalog import DEFAULT_PAGE_SIZE, TimeWindow, make_time_window, validate_page
from app.services.equipment_info import EquipmentSnapshot, get_equipment_info
from app.services.errors import InvalidParameter

SAMPLE_LABEL_SEMANTICS = (
    "故障事件是区间级事实；区间内的传感器记录不会被自动视为已确认故障样本，"
    "本项目不在 sensor_readings 上生成逐行故障标签。"
)


@dataclass(frozen=True)
class FaultSource:
    id: uuid.UUID
    source_file: str
    source_name: str
    source_url: str | None
    license: str | None
    doi: str | None
    timezone_status: str


@dataclass(frozen=True)
class FaultEventItem:
    id: uuid.UUID
    event_code: str
    pdf_label: str
    start_time: datetime
    end_time: datetime
    duration_seconds: float
    overlap_start: datetime
    overlap_end: datetime
    overlap_seconds: float
    failure_type: str
    severity: str | None
    fault_description: str | None
    maintenance_note: str | None
    source_reference: str
    verification_status: str
    verification_note: str | None
    source: FaultSource | None


@dataclass(frozen=True)
class FaultEventPage:
    equipment: EquipmentSnapshot
    window: TimeWindow
    items: list[FaultEventItem]
    total: int
    limit: int
    offset: int
    sample_label_semantics: str = SAMPLE_LABEL_SEMANTICS

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


def _window_for_optional_bounds(
    start: str | datetime | None, end: str | datetime | None
) -> TimeWindow | None:
    """故障事件允许省略任一端：省略即“不设该侧边界”。"""
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise InvalidParameter(
            "start_time 与 end_time 必须同时提供或同时省略（省略表示不设该侧边界）",
            fields=["start_time", "end_time"],
        )
    return make_time_window(start, end)


def get_fault_events(
    session: Session,
    equipment_code: str,
    *,
    start: str | datetime | None = None,
    end: str | datetime | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> FaultEventPage:
    """按时间窗分页返回与窗口相交的官方故障事件。"""
    limit, offset = validate_page(limit, offset)
    equipment = get_equipment_info(session, equipment_code)
    window = _window_for_optional_bounds(start, end)

    # 半开区间相交：[start, end) 与 [event.start, event.end) 有交集
    #   <=> event.start < end 且 event.end > start
    # 复用 Phase 1B 的闭区间筛选函数时，用 (start+1µs, end-1µs) 精确表达上面的严格不等号
    # （源时间戳精确到秒，DB CHECK 保证无微秒）。
    lower = window.start + timedelta(microseconds=1) if window else None
    upper = window.end - timedelta(microseconds=1) if window else None

    total = queries.count_fault_events(
        session, equipment.id, start=lower, end=upper
    )
    events = queries.list_fault_events(
        session, equipment.id, start=lower, end=upper, limit=limit, offset=offset
    )

    source_ids = {event.source_id for event in events}
    sources: dict[uuid.UUID, DataSource] = {}
    if source_ids:  # 一次 IN 查询取回全部来源，避免 N+1
        for source in session.scalars(select(DataSource).where(DataSource.id.in_(source_ids))):
            sources[source.id] = source

    items: list[FaultEventItem] = []
    for event in events:
        duration = (event.end_time - event.start_time).total_seconds()
        if window is None:
            overlap_start, overlap_end = event.start_time, event.end_time
        else:
            overlap_start = max(event.start_time, window.start)
            overlap_end = min(event.end_time, window.end)
        overlap_seconds = max((overlap_end - overlap_start).total_seconds(), 0.0)
        source = sources.get(event.source_id)
        items.append(
            FaultEventItem(
                id=event.id,
                event_code=event.event_code,
                pdf_label=event.pdf_label,
                start_time=event.start_time,
                end_time=event.end_time,
                duration_seconds=duration,
                overlap_start=overlap_start,
                overlap_end=overlap_end,
                overlap_seconds=overlap_seconds,
                failure_type=event.failure_type,
                severity=event.severity,
                fault_description=event.fault_description,
                maintenance_note=event.maintenance_note,
                source_reference=event.source_reference,
                verification_status=event.verification_status,
                verification_note=event.verification_note,
                source=(
                    FaultSource(
                        id=source.id,
                        source_file=source.source_file,
                        source_name=source.source_name,
                        source_url=source.source_url,
                        license=source.license,
                        doi=source.doi,
                        timezone_status=source.timezone_status,
                    )
                    if source is not None
                    else None
                ),
            )
        )

    return FaultEventPage(
        equipment=equipment,
        window=window,
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )
