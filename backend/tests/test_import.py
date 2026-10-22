"""导入流程：写入、幂等、日志与精度保真（使用真实 CSV 头部样本）。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from app.services.metropt_import import import_metropt_csv


def test_import_head_sample(session, engine, real_head_csv: Path) -> None:
    stats = import_metropt_csv(real_head_csv, engine=engine, chunk_size=150)
    assert stats.attempted_rows == 400
    assert stats.invalid_rows == 0
    assert stats.inserted_rows == 400
    assert stats.conflict_rows == 0

    total = session.scalar(text("SELECT count(*) FROM sensor_readings"))
    assert int(total) == 400

    first = session.execute(
        text(
            "SELECT source_row_number, observed_at, source_timestamp_raw, tp2, motor_current,"
            " quality_flags, quality_rule_version"
            " FROM sensor_readings ORDER BY source_row_number LIMIT 1"
        )
    ).one()
    assert first[0] == 0
    assert first[1].strftime("%Y-%m-%d %H:%M:%S") == "2020-02-01 00:00:00"
    assert first[2] == "2020-02-01 00:00:00"
    assert first[3] == Decimal("-0.0120")
    assert first[4] == Decimal("0.0400")
    assert first[5] == 0
    assert first[6] == "frozen-v1"

    row_numbers = [
        r[0] for r in session.execute(text("SELECT source_row_number FROM sensor_readings ORDER BY id"))
    ]
    assert row_numbers[:3] == [0, 10, 20]
    assert all(b - a == 10 for a, b in zip(row_numbers, row_numbers[1:]))


def test_import_records_ingest_run(session, engine, real_head_csv: Path) -> None:
    stats = import_metropt_csv(real_head_csv, engine=engine, chunk_size=400)
    run = session.execute(
        text(
            "SELECT status, attempted_rows, inserted_rows, conflict_rows, invalid_rows,"
            " frozen_rows, gap_rows_written, duration_seconds, importer_version,"
            " started_at, finished_at"
            " FROM ingest_runs WHERE id = :id"
        ),
        {"id": stats.run_id},
    ).one()
    assert run[0] == "succeeded"
    assert (run[1], run[2], run[3], run[4]) == (400, 400, 0, 0)
    assert run[7] is not None and run[7] >= 0
    assert run[8].startswith("metropt-importer/")
    assert run[9] is not None and run[10] is not None


def test_reimport_is_idempotent(session, engine, real_head_csv: Path) -> None:
    first = import_metropt_csv(real_head_csv, engine=engine, chunk_size=200)
    assert first.inserted_rows == 400
    before = int(session.scalar(text("SELECT count(*) FROM sensor_readings")))

    second = import_metropt_csv(real_head_csv, engine=engine, chunk_size=200)
    assert second.inserted_rows == 0
    assert second.conflict_rows == 400

    after = int(session.scalar(text("SELECT count(*) FROM sensor_readings")))
    assert before == after == 400


def test_gap_rows_written_once(session, engine, synthetic_csv: Path) -> None:
    import_metropt_csv(synthetic_csv, engine=engine)
    gaps = int(session.scalar(text("SELECT count(*) FROM data_gaps")))
    assert gaps == 1
    import_metropt_csv(synthetic_csv, engine=engine)
    assert int(session.scalar(text("SELECT count(*) FROM data_gaps"))) == gaps


def test_dry_run_does_not_touch_database(session, engine, real_head_csv: Path) -> None:
    stats = import_metropt_csv(real_head_csv, engine=None, dry_run=True)
    assert stats.dry_run is True
    assert stats.valid_rows == 400
    assert stats.inserted_rows == 0
    assert stats.run_id is None
    assert int(session.scalar(text("SELECT count(*) FROM sensor_readings"))) == 0
    assert int(session.scalar(text("SELECT count(*) FROM ingest_runs"))) == 0


def test_max_rows_limits_import(session, engine, real_head_csv: Path) -> None:
    stats = import_metropt_csv(real_head_csv, engine=engine, max_rows=37, chunk_size=10)
    assert stats.attempted_rows == 37
    assert stats.inserted_rows == 37
    assert int(session.scalar(text("SELECT count(*) FROM sensor_readings"))) == 37


def test_source_and_equipment_registered_once(session, engine, real_head_csv: Path) -> None:
    import_metropt_csv(real_head_csv, engine=engine)
    import_metropt_csv(real_head_csv, engine=engine)
    assert int(session.scalar(text("SELECT count(*) FROM equipment"))) == 1
    assert int(session.scalar(text("SELECT count(*) FROM data_sources"))) == 1

    source = session.execute(
        text("SELECT timezone_status, nominal_sample_interval_s, license FROM data_sources")
    ).one()
    assert source[0] == "unknown"
    assert source[1] == Decimal("10.000")
    assert source[2] == "CC BY 4.0"


def test_imported_values_have_no_precision_loss(session, engine, real_head_csv: Path) -> None:
    """入库值与 CSV 四舍五入到 4 位小数的值逐个一致。"""
    import pandas as pd

    import_metropt_csv(real_head_csv, engine=engine)
    frame = pd.read_csv(real_head_csv, dtype=str, keep_default_na=False)
    rows = session.execute(
        text(
            "SELECT source_row_number, tp2, tp3, h1, dv_pressure, reservoirs,"
            " oil_temperature, motor_current FROM sensor_readings"
            " ORDER BY source_row_number"
        )
    ).all()
    assert len(rows) == len(frame)
    columns = ["TP2", "TP3", "H1", "DV_pressure", "Reservoirs", "Oil_temperature", "Motor_current"]
    for row, (_, raw) in zip(rows, frame.iterrows()):
        assert row[0] == int(raw["Unnamed: 0"])
        for index, column in enumerate(columns, start=1):
            expected = Decimal(str(round(float(raw[column]), 4)))
            assert row[index] == expected, f"行 {row[0]} 列 {column}"
