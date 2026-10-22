"""工业设备数据查询服务（只读）。

本阶段只做最小可用查询集合，供验证脚本、测试与后续 Agent 工具复用：
设备信息、时间窗记录、计数、模拟量统计、官方故障事件、冻结候选、
采样缺口与覆盖度。

约定：
- 所有时间参数都是 **naive** datetime（与 `timestamp without time zone` 对应），
  调用方不得传入 UTC 时间冒充源时间。
- 聚合只基于库内原始值，不做插值、不做异常剔除；需要排除冻结候选时显式传参。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal as D

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import (
    ANALOG_COLUMNS,
    DIGITAL_COLUMNS,
    DataGap,
    Equipment,
    FaultEvent,
    SensorReading,
)
from app.models.quality import NOMINAL_STEP_SECONDS, QUALITY_FLAG_FROZEN_CANDIDATE

# --------------------------------------------------------------------------- #
# 结果集上限（P2-6）
#
# 原始记录列表按时间升序返回，必须分页：默认 1,000 行，单次最多 5,000 行。
# 超出范围或非法值直接报错，而不是静默截断（避免调用方误以为拿到了全量）。
# 聚合查询（min/max/avg/覆盖度）不受此限制——它们本就只返回一行汇总。
# --------------------------------------------------------------------------- #
DEFAULT_READING_PAGE_SIZE = 1000
MAX_READING_PAGE_SIZE = 5000
DEFAULT_SMALL_LIST_SIZE = 100
MAX_SMALL_LIST_SIZE = 1000


def _validate_page(limit: int, offset: int, *, max_limit: int, name: str = "limit") -> None:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError(f"{name} 必须是整数，收到 {limit!r}")
    if limit < 1:
        raise ValueError(f"{name} 必须 >= 1，收到 {limit}")
    if limit > max_limit:
        raise ValueError(f"{name} 不能超过 {max_limit}，收到 {limit}；请使用 offset 分页")
    if not isinstance(offset, int) or isinstance(offset, bool):
        raise ValueError(f"offset 必须是整数，收到 {offset!r}")
    if offset < 0:
        raise ValueError(f"offset 必须 >= 0，收到 {offset}")


@dataclass(frozen=True)
class AnalogColumnStats:
    """模拟量单列统计。

    `stddev` 使用 PostgreSQL `stddev_samp`（样本标准差，除以 n-1），
    与 pandas `Series.std()` 的默认 `ddof=1` 一致；样本数 < 2 时为 None。
    `first_timestamp` / `last_timestamp` 为该列可见行的最早/最晚观测时间。
    """

    column: str
    count: int
    minimum: D | None
    maximum: D | None
    average: D | None
    stddev: D | None = None
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None


@dataclass(frozen=True)
class DigitalColumnStats:
    """数字量（0/1）单列统计。

    `transitions` = 相邻两行取值发生变化的次数（按 `observed_at, id` 排序，
    首行不计入变化，因为它是窗口起点处的初始状态）；
    若窗口被截断，首行之前的真实状态未知，因此该值描述的是“窗口内观察到的变化次数”。
    """

    column: str
    count: int
    count_0: int
    count_1: int
    ratio_0: float | None
    ratio_1: float | None
    transitions: int
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None


@dataclass(frozen=True)
class GapOverlapStats:
    """窗口内/与窗口相交的采样缺口统计。

    - `gap_count`：与窗口相交的缺口个数（含跨越窗口边界的缺口）；
    - `gap_duration_seconds`：缺口落在窗口内的**实际秒数**之和（按窗口裁剪）；
    - `missing_samples_total`：相交缺口的 `missing_samples` 直接相加（不裁剪，
      属于 Phase 1B 的既有口径，用于与历史报告对齐）；
    - `estimated_missing_seconds`：按 Phase 1B 加权口径估计的窗口内缺失时长
      （`missing_samples * step * min(重叠秒数/缺口秒数, 1)`），缺口不足一个步长的
      时钟抖动不会被算作缺失。
    """

    gap_count: int
    gap_duration_seconds: float
    max_gap_seconds: float
    missing_samples_total: int
    estimated_missing_seconds: float


@dataclass(frozen=True)
class CoverageStats:
    """时间窗覆盖度。两种口径含义不同，不可混用：

    - `coverage_ratio`：实际行数 / 期望行数（按标称步长的行数口径，封顶 1.0）
    - `time_coverage_ratio`：1 − 窗口内缺口时长 / 窗口时长（时间口径，与 Phase 1A 一致）
    """

    observed_rows: int
    expected_rows: int
    coverage_ratio: float | None
    time_coverage_ratio: float | None
    gap_count: int
    missing_samples: int
    max_gap_seconds: float


def get_equipment(session: Session, equipment_code: str) -> Equipment | None:
    return session.scalars(
        select(Equipment).where(Equipment.equipment_code == equipment_code)
    ).one_or_none()


def list_equipment(
    session: Session, *, limit: int = DEFAULT_SMALL_LIST_SIZE, offset: int = 0
) -> list[Equipment]:
    _validate_page(limit, offset, max_limit=MAX_SMALL_LIST_SIZE)
    return list(
        session.scalars(
            select(Equipment).order_by(Equipment.equipment_code).limit(limit).offset(offset)
        )
    )


def count_readings(
    session: Session,
    equipment_id: uuid.UUID,
    start: datetime,
    end: datetime,
    *,
    exclude_frozen: bool = False,
) -> int:
    stmt = select(func.count()).select_from(SensorReading).where(
        SensorReading.equipment_id == equipment_id,
        SensorReading.observed_at >= start,
        SensorReading.observed_at <= end,
    )
    if exclude_frozen:
        stmt = stmt.where(SensorReading.quality_flags.op("&")(QUALITY_FLAG_FROZEN_CANDIDATE) == 0)
    return int(session.scalar(stmt) or 0)


def readings_between(
    session: Session,
    equipment_id: uuid.UUID,
    start: datetime,
    end: datetime,
    *,
    limit: int = DEFAULT_READING_PAGE_SIZE,
    offset: int = 0,
    exclude_frozen: bool = False,
) -> list[SensorReading]:
    """按 (observed_at, id) 升序分页返回时间窗内的原始观测。

    limit 默认 1,000、上限 5,000；总量请用 `count_readings()` 获取后翻页。
    """
    _validate_page(limit, offset, max_limit=MAX_READING_PAGE_SIZE)
    stmt = (
        select(SensorReading)
        .where(
            SensorReading.equipment_id == equipment_id,
            SensorReading.observed_at >= start,
            SensorReading.observed_at <= end,
        )
        .order_by(SensorReading.observed_at, SensorReading.id)
        .limit(limit)
        .offset(offset)
    )
    if exclude_frozen:
        stmt = stmt.where(SensorReading.quality_flags.op("&")(QUALITY_FLAG_FROZEN_CANDIDATE) == 0)
    return list(session.scalars(stmt))


def analog_statistics(
    session: Session,
    equipment_id: uuid.UUID,
    start: datetime,
    end: datetime,
    *,
    exclude_frozen: bool = False,
) -> list[AnalogColumnStats]:
    """7 路模拟量的 min / max / avg / stddev_samp / 首末时间（库内原始值，无插值）。"""
    columns = []
    for name in ANALOG_COLUMNS:
        column = getattr(SensorReading, name)
        columns.extend(
            [
                func.min(column).label(f"{name}_min"),
                func.max(column).label(f"{name}_max"),
                func.avg(column).label(f"{name}_avg"),
                func.count(column).label(f"{name}_cnt"),
                func.stddev_samp(column).label(f"{name}_std"),
                func.min(SensorReading.observed_at).label(f"{name}_first"),
                func.max(SensorReading.observed_at).label(f"{name}_last"),
            ]
        )
    stmt = select(*columns).where(
        SensorReading.equipment_id == equipment_id,
        SensorReading.observed_at >= start,
        SensorReading.observed_at <= end,
    )
    if exclude_frozen:
        stmt = stmt.where(SensorReading.quality_flags.op("&")(QUALITY_FLAG_FROZEN_CANDIDATE) == 0)
    row = session.execute(stmt).one()
    mapping = row._mapping
    result: list[AnalogColumnStats] = []
    for name in ANALOG_COLUMNS:
        result.append(
            AnalogColumnStats(
                column=name,
                count=int(mapping[f"{name}_cnt"] or 0),
                minimum=mapping[f"{name}_min"],
                maximum=mapping[f"{name}_max"],
                average=mapping[f"{name}_avg"],
                stddev=mapping[f"{name}_std"],
                first_timestamp=mapping[f"{name}_first"],
                last_timestamp=mapping[f"{name}_last"],
            )
        )
    return result


def digital_statistics(
    session: Session,
    equipment_id: uuid.UUID,
    start: datetime,
    end: datetime,
    *,
    columns: tuple[str, ...] | list[str] = DIGITAL_COLUMNS,
    exclude_frozen: bool = False,
) -> list[DigitalColumnStats]:
    """数字量的 0/1 计数、占比与状态切换次数（单次扫描，数据库内聚合）。

    切换次数用窗口函数 `lag()` 计算，只统计窗口内相邻行之间的变化：
    窗口首行没有前一行，因此不计为一次切换。
    """
    selected = tuple(columns)
    if not selected:
        return []
    for name in selected:
        if name not in DIGITAL_COLUMNS:
            raise ValueError(f"不是数字量列：{name!r}；可选 {list(DIGITAL_COLUMNS)}")

    base_filters = [
        SensorReading.equipment_id == equipment_id,
        SensorReading.observed_at >= start,
        SensorReading.observed_at <= end,
    ]
    if exclude_frozen:
        base_filters.append(
            SensorReading.quality_flags.op("&")(QUALITY_FLAG_FROZEN_CANDIDATE) == 0
        )

    lag_columns = [
        func.lag(getattr(SensorReading, name))
        .over(order_by=(SensorReading.observed_at, SensorReading.id))
        .label(f"{name}_prev")
        for name in selected
    ]
    inner = (
        select(
            SensorReading.observed_at,
            *(getattr(SensorReading, name) for name in selected),
            *lag_columns,
        )
        .where(*base_filters)
        .subquery()
    )

    aggregates = [
        func.count().label("row_count"),
        func.min(inner.c.observed_at).label("first_ts"),
        func.max(inner.c.observed_at).label("last_ts"),
    ]
    for name in selected:
        col = inner.c[name]
        prev = inner.c[f"{name}_prev"]
        aggregates.extend(
            [
                func.count().filter(col == 0).label(f"{name}_c0"),
                func.count().filter(col == 1).label(f"{name}_c1"),
                func.count().filter((prev.is_not(None)) & (col != prev)).label(f"{name}_tr"),
            ]
        )
    row = session.execute(select(*aggregates).select_from(inner)).one()
    mapping = row._mapping
    # 首末时间与计数在同一次扫描内完成，避免再扫一遍窗口
    first_ts, last_ts = mapping["first_ts"], mapping["last_ts"]

    result: list[DigitalColumnStats] = []
    for name in selected:
        count_0 = int(mapping[f"{name}_c0"] or 0)
        count_1 = int(mapping[f"{name}_c1"] or 0)
        observed = count_0 + count_1
        result.append(
            DigitalColumnStats(
                column=name,
                count=observed,
                count_0=count_0,
                count_1=count_1,
                ratio_0=(count_0 / observed) if observed else None,
                ratio_1=(count_1 / observed) if observed else None,
                transitions=int(mapping[f"{name}_tr"] or 0),
                first_timestamp=first_ts,
                last_timestamp=last_ts,
            )
        )
    return result


def list_fault_events(
    session: Session,
    equipment_id: uuid.UUID | None = None,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = DEFAULT_SMALL_LIST_SIZE,
    offset: int = 0,
) -> list[FaultEvent]:
    _validate_page(limit, offset, max_limit=MAX_SMALL_LIST_SIZE)
    stmt = select(FaultEvent).order_by(FaultEvent.start_time, FaultEvent.id).limit(limit).offset(offset)
    if equipment_id is not None:
        stmt = stmt.where(FaultEvent.equipment_id == equipment_id)
    if start is not None:
        stmt = stmt.where(FaultEvent.end_time >= start)
    if end is not None:
        stmt = stmt.where(FaultEvent.start_time <= end)
    return list(session.scalars(stmt))


def frozen_candidates(
    session: Session,
    equipment_id: uuid.UUID,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = DEFAULT_READING_PAGE_SIZE,
    offset: int = 0,
) -> list[SensorReading]:
    """被标记为冻结候选的行（不删除、不改值，仅标记）。分页规则同 readings_between。"""
    _validate_page(limit, offset, max_limit=MAX_READING_PAGE_SIZE)
    stmt = (
        select(SensorReading)
        .where(
            SensorReading.equipment_id == equipment_id,
            SensorReading.quality_flags.op("&")(QUALITY_FLAG_FROZEN_CANDIDATE) != 0,
        )
        .order_by(SensorReading.observed_at, SensorReading.id)
        .limit(limit)
        .offset(offset)
    )
    if start is not None:
        stmt = stmt.where(SensorReading.observed_at >= start)
    if end is not None:
        stmt = stmt.where(SensorReading.observed_at <= end)
    return list(session.scalars(stmt))


def frozen_segments(
    session: Session, equipment_id: uuid.UUID, *, limit: int = DEFAULT_SMALL_LIST_SIZE, offset: int = 0
) -> list[dict[str, object]]:
    """冻结候选连续区间（由视图 sensor_frozen_segments 聚合）。"""
    _validate_page(limit, offset, max_limit=MAX_SMALL_LIST_SIZE)
    rows = session.execute(
        text(
            """
            SELECT segment_start, segment_end, row_count, first_source_row, last_source_row
            FROM sensor_frozen_segments
            WHERE equipment_id = :eid
            ORDER BY segment_start
            LIMIT :lim OFFSET :off
            """
        ),
        {"eid": equipment_id, "lim": limit, "off": offset},
    ).all()
    return [
        {
            "segment_start": r.segment_start,
            "segment_end": r.segment_end,
            "row_count": int(r.row_count),
            "first_source_row": int(r.first_source_row),
            "last_source_row": int(r.last_source_row),
        }
        for r in rows
    ]


def list_gaps(
    session: Session,
    equipment_id: uuid.UUID,
    start: datetime,
    end: datetime,
    *,
    limit: int = DEFAULT_SMALL_LIST_SIZE,
    offset: int = 0,
) -> list[DataGap]:
    _validate_page(limit, offset, max_limit=MAX_SMALL_LIST_SIZE)
    stmt = (
        select(DataGap)
        .where(
            DataGap.equipment_id == equipment_id,
            DataGap.gap_start < end,
            DataGap.gap_end > start,
        )
        .order_by(DataGap.gap_seconds.desc(), DataGap.gap_start)
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt))


def half_open_upper_bound(end: datetime) -> datetime:
    """把半开区间上界 `end` 转成等价闭区间上界 `end - 1µs`。

    依据：`observed_at` 由源文本 `YYYY-MM-DD HH:MM:SS` 解析而来，且数据库 CHECK 约束
    `observed_at = (source_timestamp_raw)::timestamp` 保证它**不含微秒**。
    因此 `observed_at <= end - 1µs` 与 `observed_at < end` 严格等价，
    可复用既有的闭区间查询函数实现 `[start, end)` 语义，而不必重写一遍 SQL。
    """
    return end - timedelta(microseconds=1)


def count_frozen_candidates(
    session: Session, equipment_id: uuid.UUID, start: datetime, end: datetime
) -> int:
    """时间窗内被标记为冻结候选的行数（只读位掩码，不改数据）。"""
    stmt = (
        select(func.count())
        .select_from(SensorReading)
        .where(
            SensorReading.equipment_id == equipment_id,
            SensorReading.observed_at >= start,
            SensorReading.observed_at <= end,
            SensorReading.quality_flags.op("&")(QUALITY_FLAG_FROZEN_CANDIDATE) != 0,
        )
    )
    return int(session.scalar(stmt) or 0)


def count_flagged_readings(
    session: Session, equipment_id: uuid.UUID, start: datetime, end: datetime
) -> int:
    """时间窗内任一质量位被置位的行数。"""
    stmt = (
        select(func.count())
        .select_from(SensorReading)
        .where(
            SensorReading.equipment_id == equipment_id,
            SensorReading.observed_at >= start,
            SensorReading.observed_at <= end,
            SensorReading.quality_flags != 0,
        )
    )
    return int(session.scalar(stmt) or 0)


def equipment_time_span(
    session: Session, equipment_id: uuid.UUID
) -> tuple[datetime | None, datetime | None]:
    """设备全部观测的最早/最晚时间（用于判断窗口是否落在数据范围之外）。

    用 `ORDER BY observed_at LIMIT 1` 走索引两端各取一行，
    而不是 `min()/max()` 聚合——后者在 150 万行上要扫完整段索引（实测 +47 ms）。
    """
    spans = equipment_spans(session, [equipment_id])
    return spans.get(equipment_id, (None, None))


def equipment_spans(
    session: Session, equipment_ids: list[uuid.UUID] | tuple[uuid.UUID, ...]
) -> dict[uuid.UUID, tuple[datetime | None, datetime | None]]:
    """批量取多台设备的观测时间跨度（单条 SQL，每台设备两次索引端取数）。"""
    ids = list(equipment_ids)
    if not ids:
        return {}
    rows = session.execute(
        text(
            """
            SELECT e.id, first_row.observed_at AS first_observed_at,
                   last_row.observed_at AS last_observed_at
            FROM equipment e
            LEFT JOIN LATERAL (
                SELECT observed_at FROM sensor_readings
                WHERE equipment_id = e.id ORDER BY observed_at LIMIT 1
            ) AS first_row ON true
            LEFT JOIN LATERAL (
                SELECT observed_at FROM sensor_readings
                WHERE equipment_id = e.id ORDER BY observed_at DESC LIMIT 1
            ) AS last_row ON true
            WHERE e.id = ANY(:ids)
            """
        ),
        {"ids": ids},
    ).all()
    return {row.id: (row.first_observed_at, row.last_observed_at) for row in rows}


def count_fault_events(
    session: Session,
    equipment_id: uuid.UUID,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    """与时间窗相交的官方故障事件总数（与 list_fault_events 使用同一筛选口径）。"""
    stmt = select(func.count()).select_from(FaultEvent).where(
        FaultEvent.equipment_id == equipment_id
    )
    if start is not None:
        stmt = stmt.where(FaultEvent.end_time >= start)
    if end is not None:
        stmt = stmt.where(FaultEvent.start_time <= end)
    return int(session.scalar(stmt) or 0)


def gap_overlap_stats(
    session: Session,
    equipment_id: uuid.UUID,
    start: datetime,
    end: datetime,
    *,
    step_seconds: float = NOMINAL_STEP_SECONDS,
) -> GapOverlapStats:
    """窗口内缺口的重叠时长统计（时间口径），与 `coverage()` 的行数/时间覆盖率口径配套。"""
    row = session.execute(
        text(
            """
            WITH overlapping AS (
                SELECT g.gap_start,
                       g.gap_end,
                       g.gap_seconds,
                       g.missing_samples,
                       GREATEST(g.gap_start, CAST(:start AS timestamp)) AS clip_start,
                       LEAST(g.gap_end, CAST(:end AS timestamp)) AS clip_end
                FROM data_gaps g
                WHERE g.equipment_id = :eid
                  AND g.gap_start <= CAST(:end AS timestamp)
                  AND g.gap_end > CAST(:start AS timestamp)
            )
            SELECT count(*)::bigint AS gap_count,
                   COALESCE(sum(EXTRACT(epoch FROM (clip_end - clip_start))), 0)::float8
                       AS gap_duration_seconds,
                   COALESCE(max(gap_seconds), 0)::float8 AS max_gap_seconds,
                   COALESCE(sum(missing_samples), 0)::bigint AS missing_samples_total,
                   COALESCE(sum(
                       missing_samples * CAST(:step AS float8) *
                       LEAST(
                           EXTRACT(epoch FROM (clip_end - clip_start))
                           / NULLIF(EXTRACT(epoch FROM (gap_end - gap_start)), 0),
                           1.0
                       )
                   ), 0)::float8 AS estimated_missing_seconds
            FROM overlapping
            """
        ),
        {"eid": equipment_id, "start": start, "end": end, "step": step_seconds},
    ).one()
    return GapOverlapStats(
        gap_count=int(row.gap_count),
        gap_duration_seconds=float(row.gap_duration_seconds),
        max_gap_seconds=float(row.max_gap_seconds),
        missing_samples_total=int(row.missing_samples_total),
        estimated_missing_seconds=float(row.estimated_missing_seconds),
    )


def coverage(
    session: Session,
    equipment_id: uuid.UUID,
    start: datetime,
    end: datetime,
    *,
    step_seconds: float = NOMINAL_STEP_SECONDS,
) -> CoverageStats:
    """调用数据库函数 `sensor_coverage` 计算时间窗覆盖度。"""
    row = session.execute(
        text(
            "SELECT observed_rows, expected_rows, coverage_ratio, time_coverage_ratio,"
            " gap_count, missing_samples, max_gap_seconds"
            " FROM sensor_coverage(:eid, :start, :end, :step)"
        ),
        {"eid": equipment_id, "start": start, "end": end, "step": step_seconds},
    ).one()
    return CoverageStats(
        observed_rows=int(row.observed_rows),
        expected_rows=int(row.expected_rows),
        coverage_ratio=row.coverage_ratio,
        time_coverage_ratio=row.time_coverage_ratio,
        gap_count=int(row.gap_count),
        missing_samples=int(row.missing_samples),
        max_gap_seconds=float(row.max_gap_seconds),
    )
