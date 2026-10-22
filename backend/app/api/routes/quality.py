"""数据质量与采样覆盖率接口。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import EndTimeQuery, SessionDep, StartTimeQuery
from app.api.serializers import equipment_brief, window_info
from app.schemas.common import ErrorResponse
from app.schemas.responses import QualityResponse, QualityWarningOut
from app.services import data_quality as service

router = APIRouter(prefix="/api/v1/equipment/{equipment_id}", tags=["数据质量"])


@router.get(
    "/quality",
    response_model=QualityResponse,
    summary="时间窗内数据质量与采样覆盖率",
    description=(
        "复用 Phase 1B 的质量算法（数据库函数 `sensor_coverage` + `data_gaps`），"
        "窗口按 API 约定改为左闭右开 `[start_time, end_time)`。\n\n"
        "**指标口径**\n"
        "- `actual_samples`：窗口内真实存在的观测行数（不修补）\n"
        "- `expected_samples`：按标称间隔 10 s 计算的应有样本数 = `ceil(窗口秒数 / 10)`\n"
        "- `sample_coverage` = `actual/expected`，**不封顶**；>1 表示记录比标称更密\n"
        "- `time_coverage` = `1 - 估计缺失秒数 / 窗口秒数`（缺口缺失时长按 "
        "`missing_samples × 10 s × 窗口重叠比例` 估计）\n"
        "- `gap_count` / `gap_duration_seconds`：与窗口相交的缺口个数及其落在窗口内的实际秒数\n"
        "- `missing_samples_estimate`：估计缺失样本数（四舍五入 half-up，**属估计值**）\n"
        "- `frozen_candidate_count`：冻结候选行数（疑似记录端卡死，**不等于设备故障**）\n"
        "- `records_with_quality_flags`：带质量位的行数（不静默丢弃，单独统计）\n\n"
        "**窗口边界**：跨越缺口或数据边界的窗口按实际重叠部分计算；"
        "窗口完全落在数据范围之外时 `window_inside_data_range=false` 并给出提示。\n\n"
        "`warnings` 仅作提示，**不会修改任何统计值**。"
    ),
    responses={
        404: {"model": ErrorResponse, "description": "设备不存在"},
        422: {"model": ErrorResponse, "description": "参数不合法 / 时间带时区 / 窗口过大"},
    },
)
def get_quality(
    equipment_id: str,
    session: SessionDep,
    start_time: StartTimeQuery,
    end_time: EndTimeQuery,
) -> QualityResponse:
    quality = service.get_data_quality(
        session, equipment_id, start=start_time, end=end_time
    )
    return QualityResponse(
        equipment=equipment_brief(quality.equipment),
        window=window_info(quality.window),
        nominal_step_seconds=quality.nominal_step_seconds,
        actual_samples=quality.actual_samples,
        expected_samples=quality.expected_samples,
        sample_coverage=quality.sample_coverage,
        time_coverage=quality.time_coverage,
        gap_count=quality.gap_count,
        gap_duration_seconds=quality.gap_duration_seconds,
        estimated_missing_seconds=quality.estimated_missing_seconds,
        missing_samples_estimate=quality.missing_samples_estimate,
        missing_samples_total_of_overlapping_gaps=(
            quality.missing_samples_total_of_overlapping_gaps
        ),
        max_gap_seconds=quality.max_gap_seconds,
        frozen_candidate_count=quality.frozen_candidate_count,
        frozen_candidate_ratio=quality.frozen_candidate_ratio,
        records_with_quality_flags=quality.records_with_quality_flags,
        data_first_observed_at=quality.data_first_observed_at,
        data_last_observed_at=quality.data_last_observed_at,
        window_inside_data_range=quality.window_inside_data_range,
        warnings=[
            QualityWarningOut(code=w.code, severity=w.severity, message=w.message)
            for w in quality.warnings
        ],
        definitions=quality.definitions,
    )
