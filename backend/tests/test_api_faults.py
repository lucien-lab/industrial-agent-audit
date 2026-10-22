"""官方故障事件接口：区间重叠语义、边界、分页与来源信息。"""

from __future__ import annotations

BASE = "/api/v1/equipment/metropt3-apu-01/faults"


def _faults(api_client, **params):
    response = api_client.get(BASE, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_all_faults_without_window(api_client, api_dataset) -> None:
    body = _faults(api_client)
    assert body["pagination"]["total"] == 4  # 官方 4 条故障事件
    assert body["window"] is None
    codes = [item["event_code"] for item in body["items"]]
    assert codes == ["E1", "E2", "E3", "E4"]
    for item in body["items"]:
        assert item["start_time"] < item["end_time"]
        assert item["duration_seconds"] > 0
        # 无窗口时重叠区间等于事件本身
        assert item["overlap_start"] == item["start_time"]
        assert item["overlap_end"] == item["end_time"]
        assert item["source"] is not None
        assert item["source"]["source_file"].endswith(".pdf")
        assert item["verification_status"] in {"verified", "typo_suspected", "pending"}
    assert "区间级" in body["sample_label_semantics"]


def test_fault_window_containing_event(api_client, api_dataset) -> None:
    body = _faults(
        api_client, start_time="2020-04-01T00:00:00", end_time="2020-05-01T00:00:00"
    )
    assert body["pagination"]["total"] == 1
    item = body["items"][0]
    assert item["event_code"] == "E1"
    assert item["overlap_start"] == item["start_time"] == "2020-04-18T00:00:00"
    assert item["overlap_end"] == item["end_time"] == "2020-04-18T23:59:00"
    assert item["overlap_seconds"] == 86340.0


def test_fault_partial_overlap_is_clipped(api_client, api_dataset) -> None:
    body = _faults(
        api_client, start_time="2020-04-18T12:00:00", end_time="2020-04-19T00:00:00"
    )
    item = body["items"][0]
    assert item["event_code"] == "E1"
    assert item["overlap_start"] == "2020-04-18T12:00:00"
    assert item["overlap_end"] == "2020-04-18T23:59:00"
    assert item["overlap_seconds"] == 43140.0


def test_fault_touching_boundaries_is_not_overlap(api_client, api_dataset) -> None:
    """事件区间半开 [start, end)：恰好首尾相接不算重叠。"""
    starts_at_event_end = _faults(
        api_client, start_time="2020-04-18T23:59:00", end_time="2020-04-19T00:00:00"
    )
    assert starts_at_event_end["pagination"]["total"] == 0

    ends_at_event_start = _faults(
        api_client, start_time="2020-04-17T00:00:00", end_time="2020-04-18T00:00:00"
    )
    assert ends_at_event_start["pagination"]["total"] == 0


def test_fault_window_without_overlap(api_client, api_dataset) -> None:
    body = _faults(
        api_client, start_time="2020-01-01T00:00:00", end_time="2020-01-02T00:00:00"
    )
    assert body["items"] == []
    assert body["pagination"]["total"] == 0


def test_fault_pagination(api_client, api_dataset) -> None:
    first = _faults(api_client, limit=2, offset=0)
    assert first["pagination"]["returned"] == 2
    assert first["pagination"]["has_more"] is True
    second = _faults(api_client, limit=2, offset=2)
    assert second["pagination"]["returned"] == 2
    assert second["pagination"]["has_more"] is False
    codes = [item["event_code"] for item in first["items"] + second["items"]]
    assert codes == ["E1", "E2", "E3", "E4"]


def test_fault_requires_paired_window(api_client, api_dataset) -> None:
    response = api_client.get(BASE, params={"start_time": "2020-04-01T00:00:00"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_parameter"


def test_fault_rejects_timezone(api_client, api_dataset) -> None:
    response = api_client.get(
        BASE,
        params={"start_time": "2020-04-01T00:00:00Z", "end_time": "2020-05-01T00:00:00Z"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "timezone_not_supported"


def test_fault_rejects_invalid_limit(api_client, api_dataset) -> None:
    response = api_client.get(BASE, params={"limit": 1001})
    assert response.status_code == 422
