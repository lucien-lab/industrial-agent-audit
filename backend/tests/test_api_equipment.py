"""设备接口：列表、详情、不存在。"""

from __future__ import annotations


def test_list_equipment(api_client, api_dataset) -> None:
    response = api_client.get("/api/v1/equipment")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["equipment_id"] == "metropt3-apu-01"
    assert item["equipment_type"] == "air_production_unit"
    # 观测时间跨度来自 sensor_readings（真实数据头部样本）
    assert item["first_observed_at"].startswith("2020-02-01T00:00:00")
    assert item["last_observed_at"] > item["first_observed_at"]
    assert item["id"]


def test_equipment_detail(api_client, api_dataset) -> None:
    response = api_client.get("/api/v1/equipment/metropt3-apu-01")
    assert response.status_code == 200
    body = response.json()
    assert body["equipment_id"] == "metropt3-apu-01"
    assert "逻辑标识" in (body["description"] or "")


def test_unknown_equipment_returns_404(api_client, api_dataset) -> None:
    response = api_client.get("/api/v1/equipment/no-such-device")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "equipment_not_found"
    assert error["details"]["equipment_code"] == "no-such-device"
    assert error["request_id"]


def test_equipment_list_respects_limit(api_client, api_dataset) -> None:
    response = api_client.get("/api/v1/equipment", params={"limit": 1, "offset": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"] == []


def test_equipment_list_rejects_invalid_limit(api_client, api_dataset) -> None:
    response = api_client.get("/api/v1/equipment", params={"limit": 0})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_parameter"
