"""工业数据只读工具（复用 Phase 1C Service，不重复写 SQL）。

工具清单：
- `get_equipment_info`：设备档案 + 观测时间跨度；
- `get_sensor_history`：时间段内的原始观测（限行数）；
- `get_sensor_statistics`：时间段内的模拟量/数字量统计；
- `get_fault_events`：与时间窗相交的官方故障事件（区间级事实）；
- `get_data_quality`：采样覆盖率、缺口、冻结候选等质量指标。

语义边界（与 Service 层一致，工具层只做包装，不改口径）：
- 时间窗左闭右开 `[start, end)`，只接受无时区 ISO 8601；
- 所有工具只读；会话默认 `default_transaction_read_only=on`；
- 空结果（`status="empty"`）与查询失败（异常）严格区分。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent.tools.base import AgentToolContext, EvidenceItem, ToolResult
from app.models import NOMINAL_STEP_SECONDS
from app.services import data_quality as quality_service
from app.services import equipment_info as equipment_service
from app.services import fault_events as fault_service
from app.services import sensor_history as history_service
from app.services import sensor_statistics as statistics_service
from app.services.catalog import SENSOR_KINDS, parse_naive_datetime

# 工具级硬上限（与 app.agent.config 的默认值一致；配置只能更严格地裁剪）
MAX_HISTORY_ROWS = 200
MAX_FAULT_EVENTS = 20
MAX_SENSORS = len(SENSOR_KINDS)

# 严格字符集：设备编号与时间字符串都用白名单正则校验，
# 既挡住注入尝试，也避免把非法值送进数据库层。
_EQUIPMENT_CODE_RE = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
_NAIVE_TIME_RE = r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?$"
_SENSOR_RE = re.compile(r"^[a-z_]{1,32}$")


class EquipmentInput(BaseModel):
    """设备级工具输入。"""

    model_config = ConfigDict(extra="forbid")

    equipment_code: str = Field(
        min_length=1,
        max_length=64,
        pattern=_EQUIPMENT_CODE_RE,
        description="设备逻辑编号，例如 metropt3-apu-01",
    )


class WindowInput(EquipmentInput):
    """带时间窗的工具输入。"""

    start_time: str = Field(
        min_length=8, max_length=32, pattern=_NAIVE_TIME_RE, description="窗口起点（含），无时区"
    )
    end_time: str = Field(
        min_length=8, max_length=32, pattern=_NAIVE_TIME_RE, description="窗口终点（不含），无时区"
    )

    @model_validator(mode="after")
    def _check_window(self) -> "WindowInput":
        """在**进入 Service 之前**就拒绝非法时间（复用 Phase 1C 的解析口径）。"""
        try:
            start = parse_naive_datetime(self.start_time, field="start_time")
            end = parse_naive_datetime(self.end_time, field="end_time")
        except Exception as exc:  # noqa: BLE001 - 统一转成 Pydantic 校验错误
            raise ValueError(f"时间不合法：{exc}") from exc
        if end <= start:
            raise ValueError("end_time 必须晚于 start_time（区间为左闭右开 [start, end)）")
        return self


class StatisticsInput(WindowInput):
    sensors: list[str] | None = Field(
        default=None, max_length=MAX_SENSORS, description="传感器列名白名单，省略表示全部 15 路"
    )
    exclude_frozen: bool = Field(
        default=False, description="是否排除冻结候选行（默认 false，不静默丢弃记录）"
    )

    @field_validator("sensors")
    @classmethod
    def _check_sensors(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unknown = [name for name in value if name not in SENSOR_KINDS]
        if unknown:
            raise ValueError(f"不支持的传感器：{unknown}；可选值：{sorted(SENSOR_KINDS)}")
        return value


class HistoryInput(StatisticsInput):
    limit: int = Field(default=100, ge=1, le=MAX_HISTORY_ROWS, description="返回行数上限")
    offset: int = Field(default=0, ge=0, le=10_000, description="偏移量")


class FaultEventsInput(EquipmentInput):
    start_time: str | None = Field(default=None, max_length=32, pattern=_NAIVE_TIME_RE)
    end_time: str | None = Field(default=None, max_length=32, pattern=_NAIVE_TIME_RE)
    limit: int = Field(default=10, ge=1, le=MAX_FAULT_EVENTS)


class QualityInput(WindowInput):
    pass


# --------------------------------------------------------------------------- #
# 工具实现
# --------------------------------------------------------------------------- #
def _equipment_payload(snapshot) -> dict[str, Any]:
    return {
        "equipment_code": snapshot.equipment_code,
        "equipment_name": snapshot.equipment_name,
        "equipment_type": snapshot.equipment_type,
        "equipment_id": str(snapshot.id),
        "description": snapshot.description,
        "first_observed_at": (
            snapshot.first_observed_at.isoformat() if snapshot.first_observed_at else None
        ),
        "last_observed_at": (
            snapshot.last_observed_at.isoformat() if snapshot.last_observed_at else None
        ),
    }


def _window_payload(window) -> dict[str, Any]:
    return {
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "duration_seconds": window.duration_seconds,
        "boundary": "[start, end)",
    }


def _data_evidence(
    *, tool: str, service: str, table: str, equipment_code: str, window: dict | None, detail: dict
) -> EvidenceItem:
    source = {
        "kind": "database",
        "service": service,
        "table": table,
        "equipment_code": equipment_code,
        "window": window,
        "read_only": True,
    }
    return EvidenceItem(
        kind="data", origin_tool=tool, source=source, detail=detail, quote=None
    )


def get_equipment_info(ctx: AgentToolContext, payload: BaseModel) -> ToolResult:
    params = EquipmentInput.model_validate(payload)
    with ctx.open_data_session() as session:
        snapshot = equipment_service.get_equipment_info(session, params.equipment_code)
    data = {
        "equipment": _equipment_payload(snapshot),
        "source": {"service": "services.equipment_info.get_equipment_info", "table": "equipment"},
    }
    evidence = [
        _data_evidence(
            tool="get_equipment_info",
            service="services.equipment_info.get_equipment_info",
            table="equipment",
            equipment_code=snapshot.equipment_code,
            window=None,
            detail=data["equipment"],
        )
    ]
    return ToolResult(status="ok", data=data, evidence=evidence)


def get_sensor_statistics(ctx: AgentToolContext, payload: BaseModel) -> ToolResult:
    params = StatisticsInput.model_validate(payload)
    with ctx.open_data_session() as session:
        stats = statistics_service.get_sensor_statistics(
            session,
            params.equipment_code,
            start=params.start_time,
            end=params.end_time,
            sensors=params.sensors,
            exclude_frozen=params.exclude_frozen,
        )
    window = _window_payload(stats.window)
    analog = [
        {
            "sensor": item.sensor,
            "count": item.count,
            "min": item.minimum,
            "max": item.maximum,
            "mean": item.mean,
            "std": item.std,
            "std_method": item.std_method,
            "first_timestamp": item.first_timestamp.isoformat() if item.first_timestamp else None,
            "last_timestamp": item.last_timestamp.isoformat() if item.last_timestamp else None,
        }
        for item in stats.analog
    ]
    digital = [
        {
            "sensor": item.sensor,
            "count": item.count,
            "count_0": item.count_0,
            "count_1": item.count_1,
            "ratio_0": item.ratio_0,
            "ratio_1": item.ratio_1,
            "transitions": item.transitions,
        }
        for item in stats.digital
    ]
    data = {
        "equipment": _equipment_payload(stats.equipment),
        "window": window,
        "sensors": list(stats.sensors),
        "row_count": stats.row_count,
        "nominal_step_seconds": stats.nominal_step_seconds,
        "exclude_frozen": stats.exclude_frozen,
        "analog": analog,
        "digital": digital,
    }
    evidence = [
        _data_evidence(
            tool="get_sensor_statistics",
            service="services.sensor_statistics.get_sensor_statistics",
            table="sensor_readings",
            equipment_code=stats.equipment.equipment_code,
            window=window,
            detail={
                "row_count": stats.row_count,
                "analog": analog,
                "digital": digital,
                "std_method": statistics_service.STD_METHOD,
            },
        )
    ]
    return ToolResult(status="ok" if stats.row_count else "empty", data=data, evidence=evidence)


def get_sensor_history(ctx: AgentToolContext, payload: BaseModel) -> ToolResult:
    params = HistoryInput.model_validate(payload)
    with ctx.open_data_session() as session:
        page = history_service.get_sensor_history(
            session,
            params.equipment_code,
            start=params.start_time,
            end=params.end_time,
            sensors=params.sensors,
            limit=min(params.limit, MAX_HISTORY_ROWS),
            offset=params.offset,
            exclude_frozen=params.exclude_frozen,
        )
    window = _window_payload(page.window)
    rows = [
        {
            "observed_at": item.observed_at.isoformat(),
            "values": item.values,
            "quality_labels": list(item.quality_labels),
            "frozen_candidate": item.frozen_candidate,
            "source_row_number": item.source_row_number,
        }
        for item in page.items
    ]
    data = {
        "equipment": _equipment_payload(page.equipment),
        "window": window,
        "sensors": list(page.sensors),
        "total": page.total,
        "returned": len(rows),
        "rows": rows,
        "has_more": page.has_more,
        "source": {"service": "services.sensor_history.get_sensor_history", "table": "sensor_readings"},
    }
    evidence = [
        _data_evidence(
            tool="get_sensor_history",
            service="services.sensor_history.get_sensor_history",
            table="sensor_readings",
            equipment_code=page.equipment.equipment_code,
            window=window,
            detail={"total": page.total, "returned": len(rows), "sensors": list(page.sensors)},
        )
    ]
    return ToolResult(status="ok" if page.total else "empty", data=data, evidence=evidence)


def get_fault_events(ctx: AgentToolContext, payload: BaseModel) -> ToolResult:
    params = FaultEventsInput.model_validate(payload)
    with ctx.open_data_session() as session:
        page = fault_service.get_fault_events(
            session,
            params.equipment_code,
            start=params.start_time,
            end=params.end_time,
            limit=min(params.limit, MAX_FAULT_EVENTS),
        )
    window = _window_payload(page.window) if page.window else None
    events = [
        {
            "event_code": item.event_code,
            "pdf_label": item.pdf_label,
            "start_time": item.start_time.isoformat(),
            "end_time": item.end_time.isoformat(),
            "duration_seconds": item.duration_seconds,
            "overlap_seconds": item.overlap_seconds,
            "failure_type": item.failure_type,
            "severity": item.severity,
            "fault_description": item.fault_description,
            "maintenance_note": item.maintenance_note,
            "source_reference": item.source_reference,
            "verification_status": item.verification_status,
            "verification_note": item.verification_note,
            "source": (
                {
                    "source_file": item.source.source_file,
                    "source_name": item.source.source_name,
                    "source_url": item.source.source_url,
                    "license": item.source.license,
                    "doi": item.source.doi,
                    "timezone_status": item.source.timezone_status,
                }
                if item.source
                else None
            ),
        }
        for item in page.items
    ]
    data = {
        "equipment": _equipment_payload(page.equipment),
        "window": window,
        "total": page.total,
        "returned": len(events),
        "events": events,
        "sample_label_semantics": page.sample_label_semantics,
    }
    evidence = [
        _data_evidence(
            tool="get_fault_events",
            service="services.fault_events.get_fault_events",
            table="fault_events",
            equipment_code=page.equipment.equipment_code,
            window=window,
            detail={"total": page.total, "events": [
                {"event_code": event["event_code"], "start_time": event["start_time"],
                 "end_time": event["end_time"], "failure_type": event["failure_type"]}
                for event in events
            ]},
        )
    ]
    return ToolResult(status="ok" if page.total else "empty", data=data, evidence=evidence)


def get_data_quality(ctx: AgentToolContext, payload: BaseModel) -> ToolResult:
    params = QualityInput.model_validate(payload)
    with ctx.open_data_session() as session:
        quality = quality_service.get_data_quality(
            session, params.equipment_code, start=params.start_time, end=params.end_time
        )
    window = _window_payload(quality.window)
    warnings = [
        {"code": item.code, "severity": item.severity, "message": item.message}
        for item in quality.warnings
    ]
    data = {
        "equipment": _equipment_payload(quality.equipment),
        "window": window,
        "nominal_step_seconds": quality.nominal_step_seconds,
        "actual_samples": quality.actual_samples,
        "expected_samples": quality.expected_samples,
        "sample_coverage": quality.sample_coverage,
        "time_coverage": quality.time_coverage,
        "gap_count": quality.gap_count,
        "gap_duration_seconds": quality.gap_duration_seconds,
        "missing_samples_estimate": quality.missing_samples_estimate,
        "max_gap_seconds": quality.max_gap_seconds,
        "frozen_candidate_count": quality.frozen_candidate_count,
        "frozen_candidate_ratio": quality.frozen_candidate_ratio,
        "records_with_quality_flags": quality.records_with_quality_flags,
        "window_inside_data_range": quality.window_inside_data_range,
        "data_first_observed_at": (
            quality.data_first_observed_at.isoformat() if quality.data_first_observed_at else None
        ),
        "data_last_observed_at": (
            quality.data_last_observed_at.isoformat() if quality.data_last_observed_at else None
        ),
        "warnings": warnings,
        "source": {"service": "services.data_quality.get_data_quality", "table": "sensor_readings"},
    }
    evidence = [
        _data_evidence(
            tool="get_data_quality",
            service="services.data_quality.get_data_quality",
            table="sensor_readings",
            equipment_code=quality.equipment.equipment_code,
            window=window,
            detail={
                "actual_samples": quality.actual_samples,
                "expected_samples": quality.expected_samples,
                "sample_coverage": quality.sample_coverage,
                "time_coverage": quality.time_coverage,
                "gap_count": quality.gap_count,
                "frozen_candidate_count": quality.frozen_candidate_count,
                "nominal_step_seconds": NOMINAL_STEP_SECONDS,
            },
        )
    ]
    return ToolResult(
        status="ok" if quality.actual_samples else "empty", data=data, evidence=evidence
    )


__all__ = [
    "EquipmentInput",
    "FaultEventsInput",
    "HistoryInput",
    "MAX_FAULT_EVENTS",
    "MAX_HISTORY_ROWS",
    "QualityInput",
    "StatisticsInput",
    "WindowInput",
    "get_data_quality",
    "get_equipment_info",
    "get_fault_events",
    "get_sensor_history",
    "get_sensor_statistics",
]
