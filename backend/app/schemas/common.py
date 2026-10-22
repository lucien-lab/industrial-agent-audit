"""Pydantic Schema：跨模块契约（HTTP 请求/响应）。

约定：
- 时间字段统一使用**无时区** ISO 8601（源数据时间戳不含偏移，不做任何换算）；
- 所有响应都显式声明单位与语义（秒、比率、样本数），避免下游猜测；
- Schema 只负责序列化与文档，不含业务逻辑与 SQL。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ErrorDetail(BaseModel):
    code: str = Field(description="机器可读错误码，例如 equipment_not_found")
    message: str = Field(description="面向调用方的错误说明（不含 SQL、连接串或内部栈）")
    details: dict[str, object] = Field(
        default_factory=dict, description="补充信息，如允许取值、阈值、字段名"
    )
    request_id: str | None = Field(default=None, description="请求标识，便于与服务端日志关联")


class ErrorResponse(BaseModel):
    error: ErrorDetail = Field(description="统一错误结构")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": {
                    "code": "equipment_not_found",
                    "message": "设备 'unknown-device' 不存在",
                    "details": {"equipment_code": "unknown-device"},
                    "request_id": "0f0f8f0a-1e5d-4f0f-9f2a-3f4b5c6d7e8f",
                }
            }
        }
    )


class WindowInfo(BaseModel):
    start: datetime = Field(description="窗口起点（含）")
    end: datetime = Field(description="窗口终点（不含）")
    duration_seconds: float = Field(description="窗口时长（秒）")
    boundary: str = Field(
        default="[start, end)", description="边界语义：左闭右开，包含 start，不包含 end"
    )
    timezone: str = Field(
        default="naive (source timestamps carry no offset)",
        description="时间语义：请求与响应均为无时区时间，不做 UTC 换算",
    )


class Pagination(BaseModel):
    limit: int = Field(description="本次返回上限")
    offset: int = Field(description="本次偏移量")
    total: int = Field(description="满足条件的总条数")
    returned: int = Field(description="本次实际返回条数")
    has_more: bool = Field(description="是否还有下一页")


class EquipmentBrief(BaseModel):
    equipment_id: str = Field(description="设备逻辑编号（如 metropt3-apu-01）")
    equipment_name: str = Field(description="设备名称")
    equipment_type: str = Field(description="设备类型")


class EquipmentOut(EquipmentBrief):
    id: uuid.UUID = Field(description="设备主键（UUID）")
    description: str | None = Field(default=None, description="设备说明")
    created_at: datetime = Field(description="档案创建时间")
    first_observed_at: datetime | None = Field(
        default=None, description="该设备最早观测时间（无数据时为 null）"
    )
    last_observed_at: datetime | None = Field(
        default=None, description="该设备最晚观测时间（无数据时为 null）"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "equipment_id": "metropt3-apu-01",
                "equipment_name": "MetroPT-3 空气生产单元",
                "equipment_type": "air_production_unit",
                "id": "1fbac23e-5503-4632-83c7-57e967ad9237",
                "description": "MetroPT-3 数据集对应的逻辑设备编号（非厂商序列号）",
                "created_at": "2026-10-20T10:00:00Z",
                "first_observed_at": "2020-02-01T00:00:00",
                "last_observed_at": "2020-09-01T03:59:50",
            }
        }
    )
