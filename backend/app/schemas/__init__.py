"""Pydantic Schema 统一出口。"""

from app.schemas.common import (
    EquipmentBrief,
    EquipmentOut,
    ErrorDetail,
    ErrorResponse,
    Pagination,
    WindowInfo,
)
from app.schemas.responses import (
    AnalogStatsOut,
    DigitalStatsOut,
    EquipmentListResponse,
    FaultEventOut,
    FaultListResponse,
    FaultSourceOut,
    HealthResponse,
    QualityResponse,
    QualityWarningOut,
    ReadinessResponse,
    ReadingListResponse,
    ReadingOut,
    StatisticsResponse,
)

__all__ = [
    "AnalogStatsOut",
    "DigitalStatsOut",
    "EquipmentBrief",
    "EquipmentListResponse",
    "EquipmentOut",
    "ErrorDetail",
    "ErrorResponse",
    "FaultEventOut",
    "FaultListResponse",
    "FaultSourceOut",
    "HealthResponse",
    "Pagination",
    "QualityResponse",
    "QualityWarningOut",
    "ReadinessResponse",
    "ReadingListResponse",
    "ReadingOut",
    "StatisticsResponse",
    "WindowInfo",
]
