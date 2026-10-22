"""真实工业数据只读验证（全量 1,516,948 行）。

**不写任何数据**：只发 GET 请求 + 只读 SQL 交叉验证，并用内容指纹前后比对
证明 API 调用没有修改原始记录。

需要一份已迁移到 head 且含全量真实数据的库，通过环境变量提供，例如：

    IAA_FULLDATA_DATABASE_URL=postgresql+psycopg://app_user:***@127.0.0.1:5432/iaa_rehearsal2_test

未提供时本模块整体跳过（不影响常规测试）。
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.api.app import create_app
from app.db.session import dispose_engines

pytestmark = pytest.mark.slow

FULL_URL = os.environ.get("IAA_FULLDATA_DATABASE_URL", "").strip()

if not FULL_URL:
    pytest.skip(
        "未设置 IAA_FULLDATA_DATABASE_URL，跳过全量真实数据只读验证",
        allow_module_level=True,
    )

# 全量数据的时间边界（真实数据集实测值）
DATA_START = "2020-02-01T00:00:00"
DATA_END = "2020-09-01T04:00:00"
TOTAL_ROWS = 1_516_948
FINGERPRINT_SQL = """
SELECT md5(string_agg(concat_ws('|', observed_at, tp2, tp3, h1, dv_pressure, reservoirs,
        oil_temperature, motor_current, comp, dv_eletric, towers, mpg, lps,
        pressure_switch, oil_level, caudal_impulses, quality_flags), '||' ORDER BY id))
FROM sensor_readings
"""


@pytest.fixture(scope="module")
def full_engine():
    engine = create_engine(FULL_URL, pool_pre_ping=True, future=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def full_client(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", FULL_URL)
    dispose_engines()
    client = TestClient(create_app(), raise_server_exceptions=False)
    try:
        yield client
    finally:
        client.close()
        dispose_engines()  # 后续用例回到测试库


def test_full_dataset_counts(full_engine) -> None:
    with full_engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM sensor_readings")) == TOTAL_ROWS
        assert conn.scalar(text("SELECT count(*) FROM fault_events")) == 4
        assert conn.scalar(text("SELECT count(*) FROM data_gaps")) == 332


def test_readings_cover_entire_dataset(full_client) -> None:
    response = full_client.get(
        "/api/v1/equipment/metropt3-apu-01/readings",
        params={"start_time": DATA_START, "end_time": DATA_END, "limit": 1},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["pagination"]["total"] == TOTAL_ROWS
    assert body["pagination"]["returned"] == 1
    item = body["items"][0]
    assert item["observed_at"] == "2020-02-01T00:00:00"
    assert len(item["values"]) == 15


def test_equipment_span_matches_dataset(full_client) -> None:
    body = full_client.get("/api/v1/equipment/metropt3-apu-01").json()
    assert body["first_observed_at"] == "2020-02-01T00:00:00"
    assert body["last_observed_at"] == "2020-09-01T03:59:50"


def test_official_faults_are_complete(full_client) -> None:
    body = full_client.get("/api/v1/equipment/metropt3-apu-01/faults").json()
    assert body["pagination"]["total"] == 4
    events = {item["event_code"]: item for item in body["items"]}
    assert set(events) == {"E1", "E2", "E3", "E4"}
    assert events["E1"]["start_time"] == "2020-04-18T00:00:00"
    assert events["E2"]["verification_status"] == "typo_suspected"
    assert all(item["failure_type"] == "Air leak" for item in events.values())


def test_stats_match_sql_on_real_window(full_client, full_engine) -> None:
    start, end = "2020-04-18T00:00:00", "2020-04-19T00:00:00"
    body = full_client.get(
        "/api/v1/equipment/metropt3-apu-01/stats",
        params={"start_time": start, "end_time": end, "sensor": ["tp2", "comp"]},
    ).json()
    with full_engine.connect() as conn:
        reference = conn.execute(
            text(
                """
                SELECT count(*), min(tp2), max(tp2), avg(tp2), stddev_samp(tp2),
                       count(*) FILTER (WHERE comp = 1)
                FROM sensor_readings
                WHERE equipment_id = (SELECT id FROM equipment WHERE equipment_code = :code)
                  AND observed_at >= :start AND observed_at < :end
                """
            ),
            {"code": "metropt3-apu-01", "start": start, "end": end},
        ).one()
    analog = body["analog"][0]
    assert body["row_count"] == reference[0]
    assert analog["count"] == reference[0]
    assert analog["min"] == float(reference[1])
    assert analog["max"] == float(reference[2])
    assert analog["mean"] == pytest.approx(float(reference[3]))
    assert analog["std"] == pytest.approx(float(reference[4]), rel=1e-12)
    assert body["digital"][0]["count_1"] == reference[5]


def test_quality_on_real_full_range(full_client) -> None:
    body = full_client.get(
        "/api/v1/equipment/metropt3-apu-01/quality",
        params={"start_time": DATA_START, "end_time": DATA_END},
    ).json()
    assert body["actual_samples"] == TOTAL_ROWS
    assert body["expected_samples"] > body["actual_samples"]
    assert 0.8 < body["sample_coverage"] < 0.9
    assert body["gap_count"] >= 300
    assert body["frozen_candidate_count"] > 0
    assert body["max_gap_seconds"] > 3600
    assert any(w["code"] == "missing_samples_estimated" for w in body["warnings"])


def test_api_calls_do_not_modify_raw_data(full_client, full_engine) -> None:
    """关键回归：一串只读请求前后，原始记录的内容指纹必须完全一致。"""
    with full_engine.connect() as conn:
        before = conn.scalar(text(FINGERPRINT_SQL))
    requests = [
        ("/api/v1/equipment", None),
        ("/api/v1/equipment/metropt3-apu-01", None),
        (
            "/api/v1/equipment/metropt3-apu-01/readings",
            {"start_time": "2020-07-15T00:00:00", "end_time": "2020-07-16T00:00:00"},
        ),
        (
            "/api/v1/equipment/metropt3-apu-01/stats",
            {"start_time": "2020-07-15T00:00:00", "end_time": "2020-07-16T00:00:00"},
        ),
        (
            "/api/v1/equipment/metropt3-apu-01/faults",
            {"start_time": DATA_START, "end_time": DATA_END},
        ),
        (
            "/api/v1/equipment/metropt3-apu-01/quality",
            {"start_time": "2020-07-15T00:00:00", "end_time": "2020-07-16T00:00:00"},
        ),
    ]
    for path, params in requests:
        assert full_client.get(path, params=params).status_code == 200, path
    with full_engine.connect() as conn:
        after = conn.scalar(text(FINGERPRINT_SQL))
    assert before == after
    assert before == "f40c3ace26caedd77d9931fac8d2bfcc"
