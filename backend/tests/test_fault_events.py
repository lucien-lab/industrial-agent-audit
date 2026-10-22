"""官方故障事件导入：真实性、幂等、与传感器数据的关联方式。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from app.services import dataset as ds
from app.services.metropt_import import import_fault_events, import_metropt_csv


def test_fault_import_inserts_official_events(session, engine) -> None:
    result = import_fault_events(engine=engine)
    assert result["events_in_file"] == 4
    assert result["inserted"] == 4

    rows = session.execute(
        text(
            "SELECT event_code, pdf_label, start_time, end_time, failure_type, severity,"
            " verification_status, maintenance_note, source_reference"
            " FROM fault_events ORDER BY event_code"
        )
    ).all()
    assert [r[0] for r in rows] == ["E1", "E2", "E3", "E4"]
    assert rows[0][1] == "#1"
    assert rows[0][2] == datetime(2020, 4, 18, 0, 0)
    assert rows[0][3] == datetime(2020, 4, 18, 23, 59)
    assert rows[0][4] == "Air leak"
    assert rows[0][5] == "High stress"
    assert all("Failure Information" in r[8] for r in rows)


def test_pdf_label_duplicate_is_preserved(session, engine) -> None:
    import_fault_events(engine=engine)
    labels = [
        r[0]
        for r in session.execute(text("SELECT pdf_label FROM fault_events ORDER BY event_code"))
    ]
    assert labels[:2] == ["#1", "#1（PDF 中编号重复）"]
    assert labels[2:] == ["#3", "#4"]


def test_typo_status_on_e2(session, engine) -> None:
    import_fault_events(engine=engine)
    row = session.execute(
        text("SELECT maintenance_note, verification_status FROM fault_events WHERE event_code = 'E2'")
    ).one()
    assert row[0] == "Maintenance on 30 Apr at 12:00"
    assert row[1] == "typo_suspected"


def test_fault_import_is_idempotent(session, engine) -> None:
    import_fault_events(engine=engine)
    again = import_fault_events(engine=engine)
    assert again["inserted"] == 0
    assert again["skipped_existing"] == 4
    assert int(session.scalar(text("SELECT count(*) FROM fault_events"))) == 4


def test_no_per_row_fault_labels(session, engine, real_head_csv: Path) -> None:
    """故障事件是区间级事实：传感器表不得出现 fault 标签列。"""
    import_metropt_csv(real_head_csv, engine=engine)
    import_fault_events(engine=engine)
    columns = {
        r[0]
        for r in session.execute(
            text(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'sensor_readings'"
            )
        )
    }
    assert not any("fault" in c for c in columns)
    # 区间内每一行仍是原始观测，没有被批量改成“故障样本”
    rows = session.execute(
        text(
            "SELECT count(*) FROM sensor_readings WHERE observed_at >="
            " '2020-04-18 00:00:00' AND observed_at <= '2020-04-18 23:59:00'"
        )
    ).scalar()
    assert int(rows) == 0  # 样本只覆盖 2020-02-01，这里确认查询语义正确


def test_fault_events_link_to_pdf_source(session, engine) -> None:
    import_fault_events(engine=engine)
    row = session.execute(
        text(
            "SELECT ds.source_file, ds.file_sha256, ds.timezone_status"
            " FROM fault_events f JOIN data_sources ds ON ds.id = f.source_id LIMIT 1"
        )
    ).one()
    assert row[0] == ds.PDF_FILENAME
    assert len(row[1]) == 64
    assert row[2] == "unknown"


def test_events_within_csv_span(session, engine) -> None:
    import_fault_events(engine=engine)
    events = session.execute(
        text("SELECT start_time, end_time FROM fault_events ORDER BY start_time")
    ).all()
    for start, end in events:
        assert end > start
        assert datetime(2020, 2, 1) <= start
        assert end <= datetime(2020, 9, 2)
