"""查询服务：设备、时间窗、统计、故障事件、冻结候选、覆盖度。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from app.services import queries
from app.services.metropt_import import import_metropt_csv


@pytest.fixture()
def imported_head(session, engine, real_head_csv: Path):
    import_metropt_csv(real_head_csv, engine=engine)
    return queries.get_equipment(session, "metropt3-apu-01")


def test_get_equipment(session, imported_head) -> None:
    assert imported_head is not None
    assert imported_head.equipment_code == "metropt3-apu-01"
    assert imported_head.equipment_type == "air_production_unit"
    assert "逻辑标识" in (imported_head.description or "")
    assert queries.list_equipment(session) == [imported_head]


def test_time_range_count_and_readings(session, imported_head, real_head_csv: Path) -> None:
    import pandas as pd

    eid = imported_head.id
    start = datetime(2020, 2, 1, 0, 0, 0)
    end = datetime(2020, 2, 1, 0, 30, 0)
    frame = pd.read_csv(real_head_csv, dtype=str, keep_default_na=False)
    timestamps = pd.to_datetime(frame["timestamp"], format="%Y-%m-%d %H:%M:%S")
    expected = int(((timestamps >= start) & (timestamps <= end)).sum())
    count = queries.count_readings(session, eid, start, end)
    rows = queries.readings_between(session, eid, start, end, limit=500)
    assert count == len(rows) == expected > 100
    assert rows[0].observed_at >= start
    assert rows[-1].observed_at <= end
    assert all(a.observed_at <= b.observed_at for a, b in zip(rows, rows[1:]))


def test_time_range_excludes_outside(session, imported_head) -> None:
    eid = imported_head.id
    future = queries.count_readings(
        session, eid, datetime(2021, 1, 1), datetime(2021, 1, 2)
    )
    assert future == 0
    assert queries.readings_between(session, eid, datetime(2021, 1, 1), datetime(2021, 1, 2)) == []


def test_analog_statistics_match_sql(session, imported_head) -> None:
    eid = imported_head.id
    start = datetime(2020, 2, 1, 0, 0, 0)
    end = datetime(2020, 2, 1, 0, 10, 0)
    stats = {s.column: s for s in queries.analog_statistics(session, eid, start, end)}
    assert set(stats) == {
        "tp2",
        "tp3",
        "h1",
        "dv_pressure",
        "reservoirs",
        "oil_temperature",
        "motor_current",
    }
    reference = session.execute(
        text(
            "SELECT min(tp2), max(tp2), avg(tp2), count(tp2) FROM sensor_readings"
            " WHERE equipment_id = :eid AND observed_at BETWEEN :start AND :end"
        ),
        {"eid": eid, "start": start, "end": end},
    ).one()
    tp2 = stats["tp2"]
    assert tp2.count == int(reference[3])
    assert tp2.minimum == reference[0]
    assert tp2.maximum == reference[1]
    assert tp2.average == reference[2]


def test_exclude_frozen_option(session, engine, synthetic_csv: Path) -> None:
    import_metropt_csv(synthetic_csv, engine=engine)
    eid = queries.get_equipment(session, "metropt3-apu-01").id
    start = datetime(2020, 2, 1, 0, 0, 0)
    end = datetime(2020, 2, 1, 0, 1, 40)
    assert queries.count_readings(session, eid, start, end) == 6
    assert queries.count_readings(session, eid, start, end, exclude_frozen=True) == 3
    kept = queries.readings_between(session, eid, start, end, exclude_frozen=True)
    assert [r.source_row_number for r in kept] == [0, 10, 50]


def test_frozen_candidates_query(session, imported_head) -> None:
    eid = imported_head.id
    rows = queries.frozen_candidates(session, eid, limit=10)
    assert all(r.quality_flags & 1 for r in rows)
    only_frozen = queries.frozen_candidates(session, eid, limit=10)
    assert len(only_frozen) == len(rows)


def test_list_gaps_and_coverage(session, engine, synthetic_csv: Path) -> None:
    """缺口查询与覆盖度：使用人工构造的含缺口样本，断言精确数值。"""
    import_metropt_csv(synthetic_csv, engine=engine)
    eid = queries.get_equipment(session, "metropt3-apu-01").id
    gaps = queries.list_gaps(
        session, eid, datetime(2020, 2, 1, 0, 0, 0), datetime(2020, 2, 1, 0, 5, 0), limit=50
    )
    assert len(gaps) == 1
    assert gaps[0].missing_samples == 5
    cov = queries.coverage(
        session, eid, datetime(2020, 2, 1, 0, 0, 0), datetime(2020, 2, 1, 0, 1, 40)
    )
    assert cov.observed_rows == 6
    assert cov.expected_rows == 11
    assert 0 < (cov.coverage_ratio or 0) <= 1
    assert 0 < (cov.time_coverage_ratio or 0) <= 1
