"""数据质量接口：覆盖率口径、缺口裁剪、冻结候选与提示语。"""

from __future__ import annotations

import pytest

BASE = "/api/v1/equipment/metropt3-apu-01/quality"


def _quality(api_client, start: str, end: str):
    response = api_client.get(BASE, params={"start_time": start, "end_time": end})
    assert response.status_code == 200, response.text
    return response.json()


def test_quality_full_window(api_client, api_quality_dataset) -> None:
    body = _quality(api_client, "2020-02-01T00:00:00", "2020-02-01T00:10:00")
    assert body["actual_samples"] == 13
    assert body["expected_samples"] == 60  # ceil(600 s / 10 s)
    assert body["sample_coverage"] == pytest.approx(13 / 60)
    assert body["gap_count"] == 1
    assert body["gap_duration_seconds"] == 400.0  # 100 s → 500 s
    assert body["max_gap_seconds"] == 400.0
    assert body["estimated_missing_seconds"] == 390.0  # 39 个样本 × 10 s
    assert body["missing_samples_estimate"] == 39
    assert body["missing_samples_total_of_overlapping_gaps"] == 39
    assert body["time_coverage"] == pytest.approx(0.35)  # 1 - 390/600
    assert body["frozen_candidate_count"] == 4
    assert body["records_with_quality_flags"] == 4
    assert body["frozen_candidate_ratio"] == pytest.approx(4 / 13)
    # 该窗口终点 00:10:00 晚于本数据集最后一条记录 00:08:30，
    # 因此窗口并非完全落在观测范围内（如实报告，不做静默扩展）
    assert body["window_inside_data_range"] is False
    codes = {warning["code"] for warning in body["warnings"]}
    assert {"missing_samples_estimated", "sample_coverage_below_threshold",
            "data_gaps_present", "frozen_candidates_present"} <= codes
    assert "sample_coverage" in body["definitions"]
    assert body["nominal_step_seconds"] == 10.0


def test_quality_window_inside_data_range(api_client, api_quality_dataset) -> None:
    body = _quality(api_client, "2020-02-01T00:00:00", "2020-02-01T00:08:30")
    assert body["window_inside_data_range"] is True
    codes = {warning["code"] for warning in body["warnings"]}
    assert "window_partially_outside_data_range" not in codes


def test_quality_window_inside_gap_is_clipped(api_client, api_quality_dataset) -> None:
    """窗口完全落在缺口内部：缺口时长按窗口裁剪，覆盖率不会因此变成负数。"""
    body = _quality(api_client, "2020-02-01T00:03:20", "2020-02-01T00:05:00")
    assert body["actual_samples"] == 0
    assert body["expected_samples"] == 10  # 100 s / 10 s
    assert body["gap_count"] == 1
    assert body["gap_duration_seconds"] == 100.0
    # 缺口 400 s 中有 100 s 落在窗口内 → 39 × 10 × 0.25 = 97.5 s
    assert body["estimated_missing_seconds"] == 97.5
    assert body["missing_samples_estimate"] == 10
    assert body["time_coverage"] == pytest.approx(0.025)
    codes = {warning["code"] for warning in body["warnings"]}
    assert "no_readings_in_window" in codes


def test_quality_window_outside_data_range(api_client, api_quality_dataset) -> None:
    body = _quality(api_client, "2030-01-01T00:00:00", "2030-01-02T00:00:00")
    assert body["actual_samples"] == 0
    assert body["expected_samples"] == 8640
    assert body["sample_coverage"] == 0.0
    assert body["window_inside_data_range"] is False
    codes = {warning["code"] for warning in body["warnings"]}
    assert "window_partially_outside_data_range" in codes


def test_quality_no_gap_window(api_client, api_quality_dataset) -> None:
    body = _quality(api_client, "2020-02-01T00:00:00", "2020-02-01T00:01:00")
    assert body["gap_count"] == 0
    assert body["gap_duration_seconds"] == 0.0
    assert body["missing_samples_estimate"] == 0
    assert body["sample_coverage"] == 1.0  # 6 行 / 6 个应有样本
    assert "data_gaps_present" not in {w["code"] for w in body["warnings"]}


def test_quality_requires_window(api_client, api_quality_dataset) -> None:
    response = api_client.get(BASE)
    assert response.status_code == 422


def test_quality_rejects_timezone(api_client, api_quality_dataset) -> None:
    response = api_client.get(
        BASE,
        params={"start_time": "2020-02-01T00:00:00+00:00", "end_time": "2020-02-01T00:10:00"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "timezone_not_supported"
