"""设备、历史数据、统计、故障、质量、健康检查的响应 Schema。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import (
    EquipmentBrief,
    EquipmentOut,
    Pagination,
    WindowInfo,
)


# --------------------------------------------------------------------------- #
# 健康检查
# --------------------------------------------------------------------------- #
class HealthResponse(BaseModel):
    status: str = Field(description="进程存活状态：ok")
    service: str = Field(description="服务名")
    version: str = Field(description="应用版本")
    time: datetime = Field(description="服务端当前时间（含时区，仅用于存活探测）")


class ReadinessResponse(BaseModel):
    status: str = Field(description="ok / degraded / unavailable")
    database: dict[str, object] = Field(description="数据库连通性、库名、服务端版本")
    schema_version: str | None = Field(default=None, description="实际 Alembic 版本")
    schema_expected_version: str | None = Field(default=None, description="代码期望的 Alembic 版本")
    schema_ok: bool = Field(description="实际版本与期望版本是否一致")


# --------------------------------------------------------------------------- #
# 设备
# --------------------------------------------------------------------------- #
class EquipmentListResponse(BaseModel):
    items: list[EquipmentOut] = Field(description="设备列表（按编号升序）")
    total: int = Field(description="设备总数")


# --------------------------------------------------------------------------- #
# 历史数据
# --------------------------------------------------------------------------- #
class ReadingOut(BaseModel):
    id: int = Field(description="sensor_readings 主键（自增 BIGINT）")
    observed_at: datetime = Field(description="观测时间（源时间戳，无时区）")
    source_row_number: int = Field(description="源文件首列原值（步长 10，非行序号）")
    source_id: uuid.UUID = Field(description="来源 ID（data_sources）")
    quality_flags: int = Field(description="行级质量位掩码（0=无标记）")
    quality_labels: list[str] = Field(description="质量位对应的规则名，例如 frozen_candidate")
    frozen_candidate: bool = Field(description="是否被标记为冻结候选（疑似记录端卡死）")
    values: dict[str, float | int] = Field(
        description="被请求的传感器取值，键为数据库列名（如 tp2、comp）；模拟量为浮点，数字量为 0/1"
    )


class ReadingListResponse(BaseModel):
    equipment: EquipmentBrief
    window: WindowInfo
    sensors: list[str] = Field(description="本次投影的传感器列（白名单内）")
    items: list[ReadingOut]
    pagination: Pagination
    exclude_frozen: bool = Field(description="是否已按质量位排除冻结候选行")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "equipment": {
                    "equipment_id": "metropt3-apu-01",
                    "equipment_name": "MetroPT-3 空气生产单元",
                    "equipment_type": "air_production_unit",
                },
                "window": {
                    "start": "2020-04-18T00:00:00",
                    "end": "2020-04-18T00:05:00",
                    "duration_seconds": 300.0,
                    "boundary": "[start, end)",
                    "timezone": "naive (source timestamps carry no offset)",
                },
                "sensors": ["tp2", "comp"],
                "items": [
                    {
                        "id": 123456,
                        "observed_at": "2020-04-18T00:00:00",
                        "source_row_number": 6761610,
                        "source_id": "0b2cfc5f-4262-4305-b6a2-0ae2e72b4275",
                        "quality_flags": 0,
                        "quality_labels": [],
                        "frozen_candidate": False,
                        "values": {"tp2": -0.021, "comp": 1},
                    }
                ],
                "pagination": {
                    "limit": 100,
                    "offset": 0,
                    "total": 30,
                    "returned": 1,
                    "has_more": False,
                },
                "exclude_frozen": False,
            }
        }
    )


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
class AnalogStatsOut(BaseModel):
    sensor: str = Field(description="传感器列名")
    kind: str = Field(default="analog", description="统计类型：analog")
    count: int = Field(description="参与统计的非空样本数")
    min: float | None = Field(default=None, description="最小值")
    max: float | None = Field(default=None, description="最大值")
    mean: float | None = Field(default=None, description="算术平均值")
    std: float | None = Field(default=None, description="样本标准差（stddev_samp，ddof=1）")
    std_method: str = Field(description="标准差口径说明")
    first_timestamp: datetime | None = Field(default=None, description="该列首个观测时间")
    last_timestamp: datetime | None = Field(default=None, description="该列最后一个观测时间")


class DigitalStatsOut(BaseModel):
    sensor: str = Field(description="传感器列名")
    kind: str = Field(default="digital", description="统计类型：digital")
    count: int = Field(description="参与统计的样本数")
    count_0: int = Field(description="取值 0 的样本数")
    count_1: int = Field(description="取值 1 的样本数")
    ratio_0: float | None = Field(default=None, description="取值 0 的占比")
    ratio_1: float | None = Field(default=None, description="取值 1 的占比")
    transitions: int = Field(description="窗口内相邻行之间的 0/1 状态切换次数（首行不计）")
    first_timestamp: datetime | None = Field(default=None, description="首个观测时间")
    last_timestamp: datetime | None = Field(default=None, description="最后一个观测时间")


class StatisticsResponse(BaseModel):
    equipment: EquipmentBrief
    window: WindowInfo
    sensors: list[str] = Field(description="被请求的传感器（白名单内）")
    row_count: int = Field(description="窗口内观测行数")
    nominal_step_seconds: float = Field(description="标称采样间隔（秒）")
    analog: list[AnalogStatsOut] = Field(default_factory=list, description="模拟量统计")
    digital: list[DigitalStatsOut] = Field(default_factory=list, description="数字量统计")
    exclude_frozen: bool = Field(description="是否已排除冻结候选行")
    semantics: list[str] = Field(default_factory=list, description="统计口径说明（逐条可读）")


# --------------------------------------------------------------------------- #
# 故障事件
# --------------------------------------------------------------------------- #
class FaultSourceOut(BaseModel):
    id: uuid.UUID
    source_file: str = Field(description="来源文件名")
    source_name: str = Field(description="来源名称")
    source_url: str | None = None
    license: str | None = None
    doi: str | None = None
    timezone_status: str = Field(description="源时间戳时区状态：unknown / utc_declared / local_declared")


class FaultEventOut(BaseModel):
    id: uuid.UUID
    event_code: str = Field(description="本项目内部编号 E1–E4")
    pdf_label: str = Field(description="PDF 原文标签（保留原文的编号重复）")
    start_time: datetime = Field(description="故障开始时间（含）")
    end_time: datetime = Field(description="故障结束时间（不含）")
    duration_seconds: float = Field(description="事件总时长（秒）")
    overlap_start: datetime = Field(description="与查询窗口的重叠起点")
    overlap_end: datetime = Field(description="与查询窗口的重叠终点")
    overlap_seconds: float = Field(description="与查询窗口的重叠时长（秒）")
    failure_type: str = Field(description="故障类型")
    severity: str | None = Field(default=None, description="严重程度（原文未给出时为 null）")
    fault_description: str | None = Field(default=None, description="官方故障描述")
    maintenance_note: str | None = Field(default=None, description="维护记录原文")
    source_reference: str = Field(description="来源引用（PDF 章节/表格）")
    verification_status: str = Field(description="核验状态：verified / typo_suspected / pending")
    verification_note: str | None = Field(default=None, description="核验备注")
    source: FaultSourceOut | None = Field(default=None, description="来源登记信息")


class FaultListResponse(BaseModel):
    equipment: EquipmentBrief
    window: WindowInfo | None = Field(
        default=None, description="查询窗口；未提供 start_time/end_time 时为 null（不设边界）"
    )
    items: list[FaultEventOut]
    pagination: Pagination
    sample_label_semantics: str = Field(
        description="故障事件为区间级事实，区间内传感器记录不视为已确认故障样本"
    )


# --------------------------------------------------------------------------- #
# 数据质量
# --------------------------------------------------------------------------- #
class QualityWarningOut(BaseModel):
    code: str = Field(description="提示码")
    severity: str = Field(description="info / warning")
    message: str = Field(description="可读说明")


class QualityResponse(BaseModel):
    equipment: EquipmentBrief
    window: WindowInfo
    nominal_step_seconds: float = Field(description="标称采样间隔（秒）")
    actual_samples: int = Field(description="窗口内实际观测行数")
    expected_samples: int = Field(description="按标称间隔计算的应有样本数")
    sample_coverage: float | None = Field(description="样本覆盖率（未封顶）")
    time_coverage: float | None = Field(description="时间覆盖率（按缺口估计缺失时长）")
    gap_count: int = Field(description="与窗口相交的缺口个数")
    gap_duration_seconds: float = Field(description="缺口落在窗口内的实际秒数")
    estimated_missing_seconds: float = Field(description="估计缺失秒数（按缺口比例分摊）")
    missing_samples_estimate: int = Field(description="估计缺失样本数（四舍五入，属估计值）")
    missing_samples_total_of_overlapping_gaps: int = Field(
        description="相交缺口的 missing_samples 之和（Phase 1B 口径，不按窗口裁剪）"
    )
    max_gap_seconds: float = Field(description="相交缺口中的最大缺口时长（秒）")
    frozen_candidate_count: int = Field(description="冻结候选行数（不等于故障）")
    frozen_candidate_ratio: float | None = Field(description="冻结候选占比")
    records_with_quality_flags: int = Field(description="带任一质量位的行数")
    data_first_observed_at: datetime | None = Field(description="设备最早观测时间")
    data_last_observed_at: datetime | None = Field(description="设备最晚观测时间")
    window_inside_data_range: bool | None = Field(description="窗口是否完全落在观测范围内")
    warnings: list[QualityWarningOut] = Field(description="质量提示（不修改任何统计值）")
    definitions: dict[str, str] = Field(description="指标定义，逐项可读")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "equipment": {
                    "equipment_id": "metropt3-apu-01",
                    "equipment_name": "MetroPT-3 空气生产单元",
                    "equipment_type": "air_production_unit",
                },
                "window": {
                    "start": "2020-04-18T00:00:00",
                    "end": "2020-04-19T00:00:00",
                    "duration_seconds": 86400.0,
                    "boundary": "[start, end)",
                    "timezone": "naive (source timestamps carry no offset)",
                },
                "nominal_step_seconds": 10.0,
                "actual_samples": 8494,
                "expected_samples": 8640,
                "sample_coverage": 0.9831,
                "time_coverage": 0.9954,
                "gap_count": 2,
                "gap_duration_seconds": 400.0,
                "estimated_missing_seconds": 390.0,
                "missing_samples_estimate": 39,
                "missing_samples_total_of_overlapping_gaps": 39,
                "max_gap_seconds": 240.0,
                "frozen_candidate_count": 120,
                "frozen_candidate_ratio": 0.0141,
                "records_with_quality_flags": 120,
                "data_first_observed_at": "2020-02-01T00:00:00",
                "data_last_observed_at": "2020-09-01T03:59:50",
                "window_inside_data_range": True,
                "warnings": [
                    {
                        "code": "missing_samples_estimated",
                        "severity": "info",
                        "message": "missing_samples_estimate 是基于标称步长 10 s 的估计值",
                    }
                ],
                "definitions": {"actual_samples": "窗口内真实存在的观测行数"},
            }
        }
    )
