"""传感器统计接口：模拟量口径、数字量口径、选择与空窗口。"""

from __future__ import annotations

import statistics

import pytest

BASE = "/api/v1/equipment/metropt3-apu-01/stats"
WINDOW = {"start_time": "2020-02-01T00:00:00", "end_time": "2020-02-01T00:10:00"}


def _stats(api_client, **params):
    response = api_client.get(BASE, params={**WINDOW, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_analog_statistics(api_client, api_quality_dataset) -> None:
    body = _stats(api_client, sensor="tp2")
    assert body["row_count"] == 13
    analog = body["analog"]
    assert len(analog) == 1
    item = analog[0]
    tp2 = [1.0, 1.1, 1.1, 1.1, 1.1, 1.1, 1.2, 1.25, 1.3, 1.35, 1.4, 1.5, 1.6]
    assert item["count"] == 13
    assert item["min"] == 1.0
    assert item["max"] == 1.6
    assert item["mean"] == pytest.approx(statistics.fmean(tp2))
    # 标准差口径：样本标准差（ddof=1，与 pandas 默认一致）
    assert item["std"] == pytest.approx(statistics.stdev(tp2))
    assert item["std"] != pytest.approx(statistics.pstdev(tp2))
    assert "stddev_samp" in item["std_method"]
    assert item["first_timestamp"] == "2020-02-01T00:00:00"
    assert item["last_timestamp"] == "2020-02-01T00:08:30"


def test_digital_statistics(api_client, api_quality_dataset) -> None:
    body = _stats(api_client, sensor="comp")
    assert body["analog"] == []
    item = body["digital"][0]
    # 序列：1×5 -> 0×6 -> 1×2，共 2 次切换
    assert (item["count_0"], item["count_1"]) == (6, 7)
    assert item["count"] == 13
    assert item["ratio_1"] == pytest.approx(7 / 13)
    assert item["ratio_0"] == pytest.approx(6 / 13)
    assert item["transitions"] == 2
    assert item["kind"] == "digital"


def test_statistics_sensor_selection_and_defaults(api_client, api_quality_dataset) -> None:
    body = _stats(api_client)
    assert body["sensors"] == [
        "tp2",
        "tp3",
        "h1",
        "dv_pressure",
        "reservoirs",
        "oil_temperature",
        "motor_current",
        "comp",
        "dv_eletric",
        "towers",
        "mpg",
        "lps",
        "pressure_switch",
        "oil_level",
        "caudal_impulses",
    ]
    assert len(body["analog"]) == 7
    assert len(body["digital"]) == 8
    assert body["nominal_step_seconds"] == 10.0
    assert any("stddev_samp" in note for note in body["semantics"])

    subset = _stats(api_client, sensor=["tp2", "tp3", "lps"])
    assert [item["sensor"] for item in subset["analog"]] == ["tp2", "tp3"]
    assert [item["sensor"] for item in subset["digital"]] == ["lps"]


def test_statistics_empty_window_returns_nulls(api_client, api_quality_dataset) -> None:
    response = api_client.get(
        BASE,
        params={"start_time": "2030-01-01T00:00:00", "end_time": "2030-01-02T00:00:00"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["row_count"] == 0
    tp2 = body["analog"][0]
    assert tp2["count"] == 0
    assert tp2["min"] is None and tp2["max"] is None and tp2["mean"] is None
    assert tp2["std"] is None
    comp = body["digital"][0]
    assert comp["count"] == 0
    assert comp["transitions"] == 0
    assert comp["ratio_0"] is None


def test_statistics_exclude_frozen(api_client, api_quality_dataset) -> None:
    with_frozen = _stats(api_client, sensor="tp2")
    without = _stats(api_client, sensor="tp2", exclude_frozen=True)
    assert with_frozen["row_count"] == 13
    assert without["row_count"] == 9
    assert without["analog"][0]["count"] == 9
    # 冻结行都是 1.1，被排除后均值应上升
    assert without["analog"][0]["mean"] > with_frozen["analog"][0]["mean"]


def test_statistics_rejects_unknown_sensor(api_client, api_quality_dataset) -> None:
    response = api_client.get(BASE, params={**WINDOW, "sensor": "unknown"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_sensor"


def test_statistics_rejects_timezone(api_client, api_quality_dataset) -> None:
    response = api_client.get(
        BASE,
        params={"start_time": "2020-02-01T00:00:00+08:00", "end_time": "2020-02-01T00:10:00"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "timezone_not_supported"
