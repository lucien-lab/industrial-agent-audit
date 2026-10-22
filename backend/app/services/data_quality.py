"""数据质量与采样覆盖率 Service（对应未来 Agent Tool：`get_data_quality`）。

口径全部复用 Phase 1B 的实现（`app.services.queries.coverage()` → 数据库函数
`sensor_coverage`），只把窗口边界改成 API 约定的左闭右开 `[start, end)`：

- `actual_samples`：窗口内实际观测行数（真实存在的记录数，不做任何修补）；
- `expected_samples`：按标称步长 10 s 计算的应有样本数 = `ceil(窗口秒数 / 10)`；
- `sample_coverage` = `actual_samples / expected_samples`，**不做 1.0 封顶**，
  >1 表示采样比标称更密（会给出 `sample_coverage_above_one` 提示）；
- `time_coverage` = `1 - 估计缺失秒数 / 窗口秒数`（Phase 1B 时间口径，
  缺口内缺失时长按 `missing_samples × 10 s × 窗口重叠比例` 估计）；
- `gap_count` / `gap_duration_seconds`：与窗口相交的缺口个数，以及缺口落在窗口内的**实际秒数**；
- `missing_samples_estimate`：把估计缺失时长换算成样本数的**估计值**（四舍五入 half-up），
  不是精确观测值——缺口时刻的真实采样情况无法从现有数据得知；
- `frozen_candidate_count`：窗口内被标记为“冻结候选”的行数（仅是疑似记录端卡死，
  **不能直接认定为设备故障**）；
- 质量记录不静默丢弃：窗口内带质量位的行数单独统计（`records_with_quality_flags`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from app.models import NOMINAL_STEP_SECONDS, estimate_missing_samples

from app.services import queries
from app.services.catalog import TimeWindow, make_time_window
from app.services.equipment_info import EquipmentSnapshot, get_equipment_info

# 覆盖率低于该阈值时给出 warning（阈值仅为提示，不改变任何统计值）
COVERAGE_WARNING_THRESHOLD = 0.95

DEFINITIONS: dict[str, str] = {
    "actual_samples": "窗口 [start, end) 内真实存在的观测行数",
    "expected_samples": "按标称采样间隔 10 s 计算的应有样本数 = ceil(窗口秒数 / 10)",
    "sample_coverage": "actual_samples / expected_samples（样本口径，未封顶；>1 表示比标称更密）",
    "time_coverage": "1 - 估计缺失秒数 / 窗口秒数（时间口径，缺口按 missing_samples×10s 估计）",
    "gap_count": "与窗口相交的采样缺口个数（相邻记录间隔 ≥ 30 s 记为缺口）",
    "gap_duration_seconds": "缺口落在窗口内的实际秒数之和（按窗口裁剪）",
    "missing_samples_estimate": "估计缺失样本数 = round_half_up(估计缺失秒数 / 10)，属估计值",
    "frozen_candidate_count": "窗口内冻结候选行数（疑似记录端卡死，不能直接认定为故障）",
    "records_with_quality_flags": "窗口内任一质量位被置位的行数（不丢弃、仅统计）",
}


@dataclass(frozen=True)
class QualityWarning:
    code: str
    severity: str  # info | warning
    message: str


@dataclass(frozen=True)
class DataQuality:
    equipment: EquipmentSnapshot
    window: TimeWindow
    nominal_step_seconds: float
    actual_samples: int
    expected_samples: int
    sample_coverage: float | None
    time_coverage: float | None
    gap_count: int
    gap_duration_seconds: float
    estimated_missing_seconds: float
    missing_samples_estimate: int
    missing_samples_total_of_overlapping_gaps: int
    max_gap_seconds: float
    frozen_candidate_count: int
    frozen_candidate_ratio: float | None
    records_with_quality_flags: int
    data_first_observed_at: datetime | None
    data_last_observed_at: datetime | None
    window_inside_data_range: bool | None
    warnings: list[QualityWarning]
    definitions: dict[str, str] = field(default_factory=lambda: dict(DEFINITIONS))


def _half_up_int(value: float) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _build_warnings(
    *,
    actual_samples: int,
    sample_coverage: float | None,
    time_coverage: float | None,
    gap_count: int,
    frozen_candidate_count: int,
    window_inside: bool | None,
) -> list[QualityWarning]:
    warnings: list[QualityWarning] = [
        QualityWarning(
            code="missing_samples_estimated",
            severity="info",
            message=(
                "missing_samples_estimate 是基于标称步长 10 s 的**估计值**，"
                "不是对缺失采样的精确观测"
            ),
        )
    ]
    if actual_samples == 0:
        warnings.append(
            QualityWarning(
                code="no_readings_in_window",
                severity="warning",
                message="该时间窗内没有任何观测记录；覆盖率指标为 null 或 0，不代表设备停机",
            )
        )
    if sample_coverage is not None and sample_coverage < COVERAGE_WARNING_THRESHOLD:
        warnings.append(
            QualityWarning(
                code="sample_coverage_below_threshold",
                severity="warning",
                message=(
                    f"样本覆盖率 {sample_coverage:.4f} 低于阈值 {COVERAGE_WARNING_THRESHOLD}；"
                    "建议结合缺口明细判断数据可用性"
                ),
            )
        )
    if sample_coverage is not None and sample_coverage > 1.0:
        warnings.append(
            QualityWarning(
                code="sample_coverage_above_one",
                severity="info",
                message=(
                    f"样本覆盖率 {sample_coverage:.4f} > 1，说明窗口内记录比 10 s 标称间隔更密"
                    "（或窗口未对齐到采样节拍），未做任何截断"
                ),
            )
        )
    if time_coverage is not None and time_coverage < COVERAGE_WARNING_THRESHOLD:
        warnings.append(
            QualityWarning(
                code="time_coverage_below_threshold",
                severity="warning",
                message=f"时间覆盖率 {time_coverage:.4f} 低于阈值 {COVERAGE_WARNING_THRESHOLD}",
            )
        )
    if gap_count > 0:
        warnings.append(
            QualityWarning(
                code="data_gaps_present",
                severity="info",
                message=f"窗口内有 {gap_count} 处采样缺口（相邻记录间隔 ≥ 30 s）",
            )
        )
    if frozen_candidate_count > 0:
        warnings.append(
            QualityWarning(
                code="frozen_candidates_present",
                severity="warning",
                message=(
                    f"窗口内有 {frozen_candidate_count} 行为冻结候选；"
                    "它只表示 7 路模拟量与上一条记录逐位相同，不能直接认定为设备故障或停机"
                ),
            )
        )
    if window_inside is False:
        warnings.append(
            QualityWarning(
                code="window_partially_outside_data_range",
                severity="info",
                message="查询窗口部分或全部落在设备观测时间范围之外",
            )
        )
    return warnings


def get_data_quality(
    session: Session,
    equipment_code: str,
    *,
    start: str | datetime,
    end: str | datetime,
) -> DataQuality:
    """时间窗内的采样质量指标。"""
    window = make_time_window(start, end)
    equipment = get_equipment_info(session, equipment_code)
    step = NOMINAL_STEP_SECONDS
    closed_end = window.inclusive_end

    cov = queries.coverage(session, equipment.id, window.start, closed_end, step_seconds=step)
    gaps = queries.gap_overlap_stats(
        session, equipment.id, window.start, window.end, step_seconds=step
    )
    frozen = queries.count_frozen_candidates(session, equipment.id, window.start, closed_end)
    flagged = queries.count_flagged_readings(session, equipment.id, window.start, closed_end)
    first_seen, last_seen = equipment.first_observed_at, equipment.last_observed_at

    actual = cov.observed_rows
    expected = cov.expected_rows
    # 样本覆盖率不封顶：宁可暴露“比标称更密”，也不修改原始统计结果
    sample_coverage = (actual / expected) if expected > 0 else None
    time_coverage = cov.time_coverage_ratio
    missing_estimate = _half_up_int(gaps.estimated_missing_seconds / step) if step > 0 else 0
    window_inside: bool | None = None
    if first_seen is not None and last_seen is not None:
        window_inside = window.start >= first_seen and window.end <= last_seen

    warnings = _build_warnings(
        actual_samples=actual,
        sample_coverage=sample_coverage,
        time_coverage=time_coverage,
        gap_count=gaps.gap_count,
        frozen_candidate_count=frozen,
        window_inside=window_inside,
    )

    return DataQuality(
        equipment=equipment,
        window=window,
        nominal_step_seconds=step,
        actual_samples=actual,
        expected_samples=expected,
        sample_coverage=sample_coverage,
        time_coverage=time_coverage,
        gap_count=gaps.gap_count,
        gap_duration_seconds=gaps.gap_duration_seconds,
        estimated_missing_seconds=gaps.estimated_missing_seconds,
        missing_samples_estimate=missing_estimate,
        missing_samples_total_of_overlapping_gaps=gaps.missing_samples_total,
        max_gap_seconds=gaps.max_gap_seconds,
        frozen_candidate_count=frozen,
        frozen_candidate_ratio=(frozen / actual) if actual else None,
        records_with_quality_flags=flagged,
        data_first_observed_at=first_seen,
        data_last_observed_at=last_seen,
        window_inside_data_range=window_inside,
        warnings=warnings,
    )


# `estimate_missing_samples` 在本模块被复用为“缺口 → 样本数”的统一口径参考，
# 这里保留导入并暴露给调用方（例如未来 Agent 需要解释某个缺口的估计样本数）。
__all__ = [
    "COVERAGE_WARNING_THRESHOLD",
    "DEFINITIONS",
    "DataQuality",
    "QualityWarning",
    "estimate_missing_samples",
    "get_data_quality",
]
