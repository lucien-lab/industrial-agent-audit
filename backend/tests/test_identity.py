"""P1-A / P2-4 回归测试：内容哈希身份、跨来源重叠、部分成功状态。

对应 Phase 1B 验收场景 A–F：
  A 首次导入      B 同文件重导      C 同名不同文件名（同哈希）
  D 断点续跑      E 哈希不同但时间重叠（相同/不同取值）
  F 并发导入

所有用例只使用隔离测试库（conftest 保证库名以 _test 结尾并在用例前 TRUNCATE）。
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text

from app.services import queries
from app.services.dataset import EQUIPMENT_CODE
from app.services.metropt_import import (
    ImportConflictError,
    import_metropt_csv,
)

ANALOG = ["TP2", "TP3", "H1", "DV_pressure", "Reservoirs", "Oil_temperature", "Motor_current"]


@pytest.fixture()
def head_csv(real_head_csv: Path) -> Path:
    """真实 CSV 头部 400 行（不是伪造数据）。"""
    return real_head_csv


def _rows(session) -> int:
    return int(session.scalar(text("SELECT count(*) FROM sensor_readings")) or 0)


def _sources(session) -> int:
    return int(session.scalar(text("SELECT count(*) FROM data_sources")) or 0)


def _reserialized(src: Path, dst: Path) -> Path:
    """同值不同字节：把模拟量改写成固定 4 位小数（哈希变化、取值不变）。"""
    frame = pd.read_csv(src, dtype=str, keep_default_na=False)
    for column in ANALOG:
        frame[column] = frame[column].astype(float).map(lambda v: f"{v:.4f}")
    frame.to_csv(dst, index=False)
    return dst


def _modified(src: Path, dst: Path, start: int = 10, stop: int = 20) -> Path:
    """同时间戳但取值不同：TP2 平移 +0.5。"""
    frame = pd.read_csv(src, dtype=str, keep_default_na=False)
    for index in range(start, stop):
        frame.loc[index, "TP2"] = f"{float(frame.loc[index, 'TP2']) + 0.5:.4f}"
    frame.to_csv(dst, index=False)
    return dst


# --------------------------------------------------------------------------- #
# A / B / C
# --------------------------------------------------------------------------- #
def test_a_first_import_creates_single_source(session, engine, head_csv: Path) -> None:
    stats = import_metropt_csv(head_csv, engine=engine, chunk_size=200)
    assert stats.inserted_rows == 400
    assert stats.source_reused is False
    assert stats.db_hash_unique_enforced is True
    assert _rows(session) == 400
    assert _sources(session) == 1


def test_b_reimport_same_file_is_idempotent(session, engine, head_csv: Path) -> None:
    import_metropt_csv(head_csv, engine=engine, chunk_size=200)
    stats = import_metropt_csv(head_csv, engine=engine, chunk_size=200)
    assert stats.inserted_rows == 0
    assert stats.conflict_rows == 400
    assert stats.source_reused is True
    assert _rows(session) == 400
    assert _sources(session) == 1


def test_c_same_sha256_different_filename_reuses_source(
    session, engine, head_csv: Path, tmp_path: Path
) -> None:
    """字节完全相同、文件名不同：不得重复登记来源，也不得重复观测。"""
    first = import_metropt_csv(head_csv, engine=engine, chunk_size=200)
    assert first.inserted_rows == 400

    renamed = tmp_path / "renamed_copy.csv"
    shutil.copyfile(head_csv, renamed)

    stats = import_metropt_csv(renamed, engine=engine, chunk_size=200)
    assert stats.inserted_rows == 0
    assert stats.source_reused is True
    assert stats.source_file_mismatch is True
    assert stats.source_registered_file == head_csv.name
    assert _rows(session) == 400
    assert _sources(session) == 1, "同一 SHA-256 不得创建第二个数据源"


def test_c2_hash_uniqueness_is_enforced_by_database(engine) -> None:
    """数据库层硬保证：UNIQUE(file_sha256) 必须存在（迁移 0005）。"""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'data_sources'"
                " AND indexdef LIKE '%(file_sha256)%'"
            )
        ).fetchall()
    definitions = " ".join(r[0] for r in row)
    assert "UNIQUE" in definitions


# --------------------------------------------------------------------------- #
# D
# --------------------------------------------------------------------------- #
def test_d_partial_then_full_import_converges(session, engine, head_csv: Path, tmp_path: Path) -> None:
    first_part = tmp_path / "first_part.csv"
    pd.read_csv(head_csv, dtype=str, keep_default_na=False).head(150).to_csv(first_part, index=False)

    run1 = import_metropt_csv(first_part, engine=engine, chunk_size=100)
    assert run1.inserted_rows == 150
    run2 = import_metropt_csv(head_csv, engine=engine, chunk_size=100)
    assert run2.inserted_rows == 250          # 其余 250 行
    assert run2.overlap_skipped_rows == 150   # 前 150 行跨来源同时间戳，被跳过
    assert _rows(session) == 400
    assert int(session.scalar(text("SELECT count(DISTINCT observed_at) FROM sensor_readings"))) == 400


# --------------------------------------------------------------------------- #
# E
# --------------------------------------------------------------------------- #
def test_e1_overlapping_identical_values_are_skipped(
    session, engine, head_csv: Path, tmp_path: Path
) -> None:
    import_metropt_csv(head_csv, engine=engine, chunk_size=200)
    same_values = _reserialized(head_csv, tmp_path / "same_values.csv")

    stats = import_metropt_csv(same_values, engine=engine, chunk_size=200)
    assert stats.inserted_rows == 0
    assert stats.overlap_skipped_rows == 400
    assert stats.timestamp_conflict_rows == 0
    assert stats.partial is False
    assert _rows(session) == 400


def test_e2_overlapping_conflicting_values_abort_without_overwrite(
    session, engine, head_csv: Path, tmp_path: Path
) -> None:
    import_metropt_csv(head_csv, engine=engine, chunk_size=200)
    before = {
        r[0]: r[1]
        for r in session.execute(
            text("SELECT source_row_number, tp2 FROM sensor_readings ORDER BY source_row_number")
        )
    }
    conflicting = _modified(head_csv, tmp_path / "conflicting.csv")

    with pytest.raises(ImportConflictError):
        import_metropt_csv(conflicting, engine=engine, chunk_size=200)

    after = {
        r[0]: r[1]
        for r in session.execute(
            text("SELECT source_row_number, tp2 FROM sensor_readings ORDER BY source_row_number")
        )
    }
    assert after == before, "冲突处理不得覆盖或修改既有观测"
    assert _rows(session) == 400


def test_e3_skip_policy_records_partial_and_conflicts(
    session, engine, head_csv: Path, tmp_path: Path
) -> None:
    import_metropt_csv(head_csv, engine=engine, chunk_size=200)
    conflicting = _modified(head_csv, tmp_path / "conflicting_skip.csv")

    stats = import_metropt_csv(
        conflicting, engine=engine, chunk_size=200, overlap_policy="skip"
    )
    assert stats.inserted_rows == 0
    assert stats.timestamp_conflict_rows == 10
    assert stats.overlap_skipped_rows == 390
    assert stats.partial is True
    assert _rows(session) == 400

    run = session.execute(
        text(
            "SELECT status, notes FROM ingest_runs ORDER BY id DESC LIMIT 1"
        )
    ).one()
    assert run[0] == "partial"
    assert "timestamp_conflicts=10" in run[1]
    assert "partial_commit=true" in run[1]


# --------------------------------------------------------------------------- #
# F
# --------------------------------------------------------------------------- #
def test_f_concurrent_import_creates_single_source(
    session, engine, head_csv: Path, tmp_path: Path
) -> None:
    small = tmp_path / "small.csv"
    pd.read_csv(head_csv, dtype=str, keep_default_na=False).head(200).to_csv(small, index=False)

    errors: list[str] = []
    results = []

    def worker() -> None:
        try:
            results.append(import_metropt_csv(small, engine=engine, chunk_size=100))
        except Exception as exc:  # pragma: no cover - 并发失败会被断言捕获
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert _rows(session) == 200
    assert int(session.scalar(text("SELECT count(DISTINCT source_row_number) FROM sensor_readings"))) == 200
    assert _sources(session) == 1, "并发导入同一文件不得产生重复来源"
    assert sum(r.inserted_rows for r in results) == 200


# --------------------------------------------------------------------------- #
# P2-4：部分成功状态
# --------------------------------------------------------------------------- #
def test_partial_status_when_failure_occurs_after_commit(
    session, engine, real_csv_path: Path, tmp_path: Path
) -> None:
    frame = pd.read_csv(real_csv_path, nrows=25_000, dtype=str, keep_default_na=False)
    frame.loc[15_000, "TP2"] = "1.23456"  # 5 位真实小数 -> 精度校验中止
    bad = tmp_path / "corrupt_25k.csv"
    frame.to_csv(bad, index=False)

    with pytest.raises(Exception):
        import_metropt_csv(bad, engine=engine, chunk_size=10_000)

    # 第一个 chunk 已提交：必须是 partial，而不是“完全回滚”
    run = session.execute(
        text("SELECT status, attempted_rows, inserted_rows, notes FROM ingest_runs ORDER BY id DESC LIMIT 1")
    ).one()
    assert run[0] == "partial"
    assert run[1] == 10_000
    assert run[2] == 10_000
    assert "partial_commit=true" in run[3]
    assert "committed_rows=10000" in run[3]
    assert _rows(session) == 10_000


def test_failed_status_when_nothing_committed(
    session, engine, head_csv: Path, tmp_path: Path
) -> None:
    frame = pd.read_csv(head_csv, dtype=str, keep_default_na=False)
    frame.loc[5, "TP2"] = "1.23456"
    bad = tmp_path / "corrupt_head.csv"
    frame.to_csv(bad, index=False)

    with pytest.raises(Exception):
        import_metropt_csv(bad, engine=engine, chunk_size=100)

    run = session.execute(
        text("SELECT status, inserted_rows FROM ingest_runs ORDER BY id DESC LIMIT 1")
    ).one()
    assert run[0] == "failed"
    assert run[1] == 0
    assert _rows(session) == 0


def test_source_reuse_is_recorded_in_ingest_notes(
    session, engine, head_csv: Path, tmp_path: Path
) -> None:
    import_metropt_csv(head_csv, engine=engine, chunk_size=200)
    renamed = tmp_path / "copy_for_notes.csv"
    shutil.copyfile(head_csv, renamed)
    import_metropt_csv(renamed, engine=engine, chunk_size=200)

    run = session.execute(
        text("SELECT notes FROM ingest_runs ORDER BY id DESC LIMIT 1")
    ).scalar_one()
    assert "source_reused=true" in run
    assert "source_file_mismatch=" in run
    assert "db_hash_unique_enforced=true" in run


def test_equipment_and_source_links_stay_intact(session, engine, head_csv: Path, tmp_path: Path) -> None:
    import_metropt_csv(head_csv, engine=engine, chunk_size=200)
    shutil.copyfile(head_csv, tmp_path / "another_name.csv")
    import_metropt_csv(tmp_path / "another_name.csv", engine=engine, chunk_size=200)

    equipment = queries.get_equipment(session, EQUIPMENT_CODE)
    assert equipment is not None
    assert _rows(session) == 400
    linked = int(
        session.scalar(
            text("SELECT count(*) FROM sensor_readings WHERE equipment_id = :eid"),
            {"eid": equipment.id},
        )
        or 0
    )
    assert linked == 400
