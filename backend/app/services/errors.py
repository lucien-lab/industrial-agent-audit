"""Service 层领域异常。

设计要点：
- 本模块**不依赖 FastAPI**：Service 只抛领域异常，由 API 层翻译成 HTTP 状态码，
  这样同一套业务函数既能被 HTTP 调用，也能被未来的 Agent Tool 直接调用。
- 异常只携带“可公开”的信息（参数名、允许取值、阈值），绝不包含 SQL、连接串或密码；
  内部细节由 API 层的日志记录。
"""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """Service 层异常基类。"""

    code = "service_error"
    default_message = "服务处理失败"

    def __init__(self, message: str | None = None, **details: Any) -> None:
        self.message = message or self.default_message
        self.details: dict[str, Any] = details
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class EquipmentNotFound(ServiceError):
    """设备编号不存在（HTTP 404）。"""

    code = "equipment_not_found"
    default_message = "设备不存在"


class InvalidParameter(ServiceError):
    """参数取值不合法（HTTP 422）。"""

    code = "invalid_parameter"
    default_message = "参数不合法"


class InvalidTimeRange(ServiceError):
    """时间范围不合法（HTTP 422）。"""

    code = "invalid_time_range"
    default_message = "时间范围不合法"


class TimezoneNotSupported(ServiceError):
    """请求时间带时区偏移；本阶段明确拒绝，不做任何隐式换算（HTTP 422）。"""

    code = "timezone_not_supported"
    default_message = "本服务不接受带时区偏移的时间，请使用无时区的 ISO 8601 字符串"


class UnsupportedSensor(ServiceError):
    """传感器名称不在允许列表内（HTTP 422）。"""

    code = "unsupported_sensor"
    default_message = "不支持的传感器名称"


class WindowTooLarge(ServiceError):
    """查询时间窗超过允许上限（HTTP 422）。"""

    code = "window_too_large"
    default_message = "查询时间窗过大"
