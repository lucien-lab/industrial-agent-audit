"""传感器历史数据接口：时间窗、分页、列投影、参数校验、注入防护。"""

from __future__ import annotations

from sqlalchemy import text

BASE = "/api/v1/equipment/metropt3-apu-01/readings"


def _get(api_client, **params):
    return api_client.get(BASE, params=params)


def test_readings_in_window(api_client, api_dataset) -> None:
    response = _get(
        api_client,
        start_time="2020-02-01T00:00:00",
        end_time="2020-02-01T00:01:00",
        sensor=["tp2", "comp"],
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sensors"] == ["tp2", "comp"]
    assert body["window"]["boundary"] == "[start, end)"
    assert body["pagination"]["total"] > 0
    first = body["items"][0]
    assert set(first["values"]) == {"tp2", "comp"}
    assert isinstance(first["values"]["tp2"], float)
    assert first["values"]["comp"] in (0, 1)
    # 稳定排序
    timestamps = [item["observed_at"] for item in body["items"]]
    assert timestamps == sorted(timestamps)


def test_readings_are_left_closed_right_open(api_client, api_dataset, engine) -> None:
    """真实数据头部存在 ±1 s 时钟抖动（00:00:00, :10, :19, :29, :39 …）。

    `end_time=00:00:39` 必须排除恰好等于上界的那一行；`end_time=00:00:40` 才包含它。
    """
    below = _get(
        api_client,
        start_time="2020-02-01T00:00:00",
        end_time="2020-02-01T00:00:39",
        limit=1000,
    ).json()
    assert [item["observed_at"] for item in below["items"]] == [
        "2020-02-01T00:00:00",
        "2020-02-01T00:00:10",
        "2020-02-01T00:00:19",
        "2020-02-01T00:00:29",
    ]
    inclusive_upper = _get(
        api_client,
        start_time="2020-02-01T00:00:00",
        end_time="2020-02-01T00:00:40",
        limit=1000,
    ).json()
    assert [item["observed_at"] for item in inclusive_upper["items"]][-1] == (
        "2020-02-01T00:00:39"
    )
    # 与 SQL 半开区间计数交叉验证
    with engine.connect() as conn:
        sql_total = conn.execute(
            text(
                "SELECT count(*) FROM sensor_readings"
                " WHERE observed_at >= :start AND observed_at < :end"
            ),
            {"start": "2020-02-01 00:00:00", "end": "2020-02-01 00:00:39"},
        ).scalar()
    assert sql_total == below["pagination"]["total"] == 4


def test_readings_empty_window(api_client, api_dataset) -> None:
    response = _get(
        api_client, start_time="2030-01-01T00:00:00", end_time="2030-01-02T00:00:00"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["pagination"]["total"] == 0
    assert body["pagination"]["has_more"] is False


def test_readings_pagination_has_no_gaps_or_duplicates(api_client, api_dataset) -> None:
    """分页无重复、无遗漏：三页拼起来等于一次取回的全部行。"""
    common = {
        "start_time": "2020-02-01T00:00:00",
        "end_time": "2020-02-02T00:00:00",
        "sensor": "tp2",
    }
    full = _get(api_client, **common, limit=1000).json()
    total = full["pagination"]["total"]
    assert total == 400  # 真实 CSV 头部 400 行

    collected: list[int] = []
    for offset in (0, 150, 300):
        page = _get(api_client, **common, limit=150, offset=offset).json()
        assert page["pagination"]["total"] == total
        collected.extend(item["id"] for item in page["items"])
    assert len(collected) == total
    assert len(set(collected)) == total
    assert collected == sorted(collected)
    assert collected == [item["id"] for item in full["items"]]


def test_readings_default_limit_is_100(api_client, api_dataset) -> None:
    body = _get(
        api_client,
        start_time="2020-02-01T00:00:00",
        end_time="2020-02-02T00:00:00",
    ).json()
    assert body["pagination"]["limit"] == 100
    assert body["pagination"]["returned"] == 100
    assert body["pagination"]["has_more"] is True
    assert len(body["sensors"]) == 15  # 默认投影全部 15 路


def test_readings_accepts_max_limit(api_client, api_dataset) -> None:
    response = _get(
        api_client,
        start_time="2020-02-01T00:00:00",
        end_time="2020-02-02T00:00:00",
        limit=1000,
    )
    assert response.status_code == 200
    assert response.json()["pagination"]["returned"] == 400


def test_readings_rejects_limit_above_hard_cap(api_client, api_dataset) -> None:
    response = _get(
        api_client,
        start_time="2020-02-01T00:00:00",
        end_time="2020-02-02T00:00:00",
        limit=1001,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_parameter"


def test_readings_rejects_negative_offset(api_client, api_dataset) -> None:
    response = _get(
        api_client,
        start_time="2020-02-01T00:00:00",
        end_time="2020-02-02T00:00:00",
        offset=-1,
    )
    assert response.status_code == 422


def test_readings_missing_time_params(api_client, api_dataset) -> None:
    response = api_client.get(BASE)
    assert response.status_code == 422
    errors = response.json()["error"]["details"]["errors"]
    assert {err["field"] for err in errors} == {"query.start_time", "query.end_time"}


def test_readings_invalid_time_format(api_client, api_dataset) -> None:
    response = _get(api_client, start_time="yesterday", end_time="2020-02-02T00:00:00")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_parameter"


def test_readings_invalid_time_range(api_client, api_dataset) -> None:
    response = _get(
        api_client, start_time="2020-02-01T00:10:00", end_time="2020-02-01T00:00:00"
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_time_range"


def test_readings_rejects_timezone_offsets(api_client, api_dataset) -> None:
    for start in ("2020-02-01T00:00:00+08:00", "2020-02-01T00:00:00Z"):
        response = _get(api_client, start_time=start, end_time="2020-02-02T00:00:00")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "timezone_not_supported"


def test_readings_rejects_window_over_limit(api_client, api_dataset) -> None:
    response = _get(api_client, start_time="2020-01-01", end_time="2022-01-01")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "window_too_large"


def test_readings_rejects_unknown_sensor(api_client, api_dataset) -> None:
    response = _get(
        api_client,
        start_time="2020-02-01T00:00:00",
        end_time="2020-02-01T00:01:00",
        sensor="not_a_sensor",
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "unsupported_sensor"
    assert "not_a_sensor" in error["message"]


def test_readings_rejects_sql_injection_attempt(api_client, api_dataset, engine) -> None:
    """列名只能来自白名单：注入字符串被当作未知传感器拒绝，表结构完好。"""
    response = _get(
        api_client,
        start_time="2020-02-01T00:00:00",
        end_time="2020-02-01T00:01:00",
        sensor="tp2; DROP TABLE sensor_readings; --",
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_sensor"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM sensor_readings")).scalar() == 400


def test_readings_exclude_frozen_only_filters_flags(api_client, api_quality_dataset) -> None:
    params = {
        "start_time": "2020-02-01T00:00:00",
        "end_time": "2020-02-01T00:10:00",
        "limit": 1000,
    }
    all_rows = _get(api_client, **params).json()
    unfrozen = _get(api_client, **params, exclude_frozen=True).json()
    assert all_rows["pagination"]["total"] == 13
    assert unfrozen["pagination"]["total"] == 9
    assert all(item["frozen_candidate"] is False for item in unfrozen["items"])
    assert sum(item["frozen_candidate"] for item in all_rows["items"]) == 4
    # 原始值没有被修改：两者的相同行数值一致
    kept = {item["id"]: item["values"] for item in unfrozen["items"]}
    for item in all_rows["items"]:
        if item["id"] in kept:
            assert item["values"] == kept[item["id"]]
