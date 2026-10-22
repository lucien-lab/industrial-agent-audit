"""入库数据与原始 CSV 的独立一致性交叉验证（隔离测试库，2 万行真实样本）。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text

from app.services.metropt_import import import_metropt_csv

SAMPLE_ROWS = 20_000


@pytest.fixture(scope="module")
def sample_csv(real_csv_path: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    target = tmp_path_factory.mktemp("consistency") / "metropt3_head_20000.csv"
    frame = pd.read_csv(real_csv_path, nrows=SAMPLE_ROWS, dtype=str, keep_default_na=False)
    frame.to_csv(target, index=False)
    return target


@pytest.fixture(scope="module")
def imported(module_session, engine, sample_csv: Path):
    return import_metropt_csv(sample_csv, engine=engine, chunk_size=5_000)


def test_row_count_matches(module_session, imported) -> None:
    assert imported.inserted_rows == SAMPLE_ROWS
    assert int(module_session.scalar(text("SELECT count(*) FROM sensor_readings"))) == SAMPLE_ROWS


def test_time_bounds_match(module_session, sample_csv: Path, imported) -> None:
    frame = pd.read_csv(sample_csv, dtype=str, keep_default_na=False)
    timestamps = pd.to_datetime(frame["timestamp"], format="%Y-%m-%d %H:%M:%S")
    span = module_session.execute(
        text("SELECT min(observed_at), max(observed_at) FROM sensor_readings")
    ).one()
    assert span[0] == timestamps.min().to_pydatetime()
    assert span[1] == timestamps.max().to_pydatetime()


def test_analog_statistics_match(module_session, sample_csv: Path, imported) -> None:
    frame = pd.read_csv(sample_csv, dtype=str, keep_default_na=False)
    mapping = {
        "tp2": "TP2",
        "tp3": "TP3",
        "h1": "H1",
        "dv_pressure": "DV_pressure",
        "reservoirs": "Reservoirs",
        "oil_temperature": "Oil_temperature",
        "motor_current": "Motor_current",
    }
    db_stats = module_session.execute(
        text(
            "SELECT "
            + ", ".join(
                f"min({c}), max({c}), avg({c})" for c in mapping
            )
            + " FROM sensor_readings"
        )
    ).one()
    for index, (db_col, csv_col) in enumerate(mapping.items()):
        series = pd.to_numeric(frame[csv_col], errors="coerce").round(4)
        assert float(db_stats[index * 3]) == pytest.approx(series.min(), abs=1e-9)
        assert float(db_stats[index * 3 + 1]) == pytest.approx(series.max(), abs=1e-9)
        assert float(db_stats[index * 3 + 2]) == pytest.approx(series.mean(), abs=1e-6)


def test_source_row_numbers_complete(module_session, sample_csv: Path, imported) -> None:
    frame = pd.read_csv(sample_csv, dtype=str, keep_default_na=False)
    expected = frame["Unnamed: 0"].astype("int64").tolist()
    actual = [
        r[0]
        for r in module_session.execute(text("SELECT source_row_number FROM sensor_readings ORDER BY id"))
    ]
    assert actual == expected
    assert len(set(actual)) == len(actual)


def test_no_precision_loss(module_session, sample_csv: Path, imported) -> None:
    """逐行抽查关键列：入库值 == round(原始值, 4)。"""
    frame = pd.read_csv(sample_csv, dtype=str, keep_default_na=False).head(2000)
    rows = module_session.execute(
        text(
            "SELECT source_row_number, motor_current, oil_temperature FROM sensor_readings"
            " ORDER BY source_row_number LIMIT 2000"
        )
    ).all()
    for row, (_, raw) in zip(rows, frame.iterrows()):
        assert row[0] == int(raw["Unnamed: 0"])
        assert row[1] == Decimal(str(round(float(raw["Motor_current"]), 4)))
        assert row[2] == Decimal(str(round(float(raw["Oil_temperature"]), 4)))


def test_frozen_flags_match_independent_recomputation(module_session, sample_csv: Path, imported) -> None:
    frame = pd.read_csv(sample_csv, dtype=str, keep_default_na=False)
    analog = ["TP2", "TP3", "H1", "DV_pressure", "Reservoirs", "Oil_temperature", "Motor_current"]
    same = None
    for column in analog:
        series = pd.to_numeric(frame[column], errors="coerce").round(4)
        equal = series.eq(series.shift(1))
        same = equal if same is None else (same & equal)
    expected = int(same.iloc[1:].sum())

    actual = int(
        module_session.scalar(text("SELECT count(*) FROM sensor_readings WHERE quality_flags <> 0"))
    )
    assert actual == expected == imported.frozen_rows


def test_gap_count_matches_independent_recomputation(module_session, sample_csv: Path, imported) -> None:
    frame = pd.read_csv(sample_csv, dtype=str, keep_default_na=False)
    timestamps = pd.to_datetime(frame["timestamp"], format="%Y-%m-%d %H:%M:%S")
    intervals = timestamps.diff().dt.total_seconds().dropna()
    expected = int((intervals >= 30).sum())
    actual = int(module_session.scalar(text("SELECT count(*) FROM data_gaps")))
    assert actual == expected == imported.gap_rows_written


def test_reimport_keeps_counts_stable(module_session, engine, sample_csv: Path, imported) -> None:
    before = int(module_session.scalar(text("SELECT count(*) FROM sensor_readings")))
    again = import_metropt_csv(sample_csv, engine=engine, chunk_size=5_000)
    after = int(module_session.scalar(text("SELECT count(*) FROM sensor_readings")))
    assert again.inserted_rows == 0
    assert again.conflict_rows == SAMPLE_ROWS
    assert before == after


def test_timestamp_text_roundtrip(module_session, imported) -> None:
    mismatches = int(
        module_session.scalar(
            text(
                "SELECT count(*) FROM sensor_readings"
                " WHERE to_char(observed_at, 'YYYY-MM-DD HH24:MI:SS') <> source_timestamp_raw"
            )
        )
    )
    assert mismatches == 0
