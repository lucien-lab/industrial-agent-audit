"""质量标记：冻结候选、采样缺口与覆盖度（隔离测试库）。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text

from app.services import queries
from app.services.metropt_import import import_metropt_csv


def _equipment_id(session):
    return session.execute(text("SELECT id FROM equipment")).scalar_one()


def test_frozen_rows_are_flagged_but_kept(session, engine, synthetic_csv: Path) -> None:
    import_metropt_csv(synthetic_csv, engine=engine)
    assert int(session.scalar(text("SELECT count(*) FROM sensor_readings"))) == 6
    flagged = session.execute(
        text(
            "SELECT source_row_number, quality_flags, quality_rule_version"
            " FROM sensor_readings WHERE quality_flags <> 0 ORDER BY source_row_number"
        )
    ).all()
    assert [r[0] for r in flagged] == [20, 30, 40]
    assert all(r[1] == 1 for r in flagged)
    assert all(r[2] == "frozen-v1" for r in flagged)


def test_frozen_rows_keep_original_values(session, engine, synthetic_csv: Path) -> None:
    import_metropt_csv(synthetic_csv, engine=engine)
    rows = session.execute(
        text(
            "SELECT source_row_number, tp2, motor_current FROM sensor_readings"
            " ORDER BY source_row_number"
        )
    ).all()
    # 冻结行数值与来源完全一致，没有被删除或改写
    assert rows[1] == (10, Decimal("1.1000"), Decimal("1.1000"))
    assert rows[4] == (40, Decimal("1.1000"), Decimal("1.1000"))


def test_gap_recorded_as_window_not_row_flag(session, engine, synthetic_csv: Path) -> None:
    import_metropt_csv(synthetic_csv, engine=engine)
    gap = session.execute(
        text(
            "SELECT gap_start, gap_end, gap_seconds, missing_samples, threshold_seconds,"
            " rule_version FROM data_gaps"
        )
    ).one()
    assert gap.gap_start == datetime(2020, 2, 1, 0, 0, 40)
    assert gap.gap_end == datetime(2020, 2, 1, 0, 1, 40)
    assert gap.gap_seconds == Decimal("60.000")
    assert gap.missing_samples == 5
    assert gap.threshold_seconds == Decimal("30.000")
    assert gap.rule_version == "gap-v1"
    # 缺口信息不写入单行质量标记
    assert int(session.scalar(text("SELECT count(*) FROM sensor_readings WHERE quality_flags <> 0"))) == 3


def test_frozen_segment_view_aggregates_runs(session, engine, synthetic_csv: Path) -> None:
    import_metropt_csv(synthetic_csv, engine=engine)
    segments = queries.frozen_segments(session, _equipment_id(session))
    assert len(segments) == 1
    assert segments[0]["row_count"] == 3
    assert segments[0]["first_source_row"] == 20
    assert segments[0]["last_source_row"] == 40


def test_coverage_two_metrics(session, engine, synthetic_csv: Path) -> None:
    import_metropt_csv(synthetic_csv, engine=engine)
    eid = _equipment_id(session)
    cov = queries.coverage(
        session, eid, datetime(2020, 2, 1, 0, 0, 0), datetime(2020, 2, 1, 0, 1, 40)
    )
    # 100 s 窗口，标称 10 s -> 期望 11 行，实际 6 行
    assert cov.observed_rows == 6
    assert cov.expected_rows == 11
    assert cov.coverage_ratio is not None and abs(cov.coverage_ratio - 6 / 11) < 1e-9
    # 时间口径：缺口 5 个样本 = 50 s 缺失 / 100 s
    assert cov.time_coverage_ratio is not None
    assert abs(cov.time_coverage_ratio - 0.5) < 1e-9
    assert cov.gap_count == 1
    assert cov.missing_samples == 5


def test_coverage_without_gaps_is_one(session, engine, synthetic_csv: Path) -> None:
    import_metropt_csv(synthetic_csv, engine=engine)
    eid = _equipment_id(session)
    cov = queries.coverage(
        session, eid, datetime(2020, 2, 1, 0, 0, 0), datetime(2020, 2, 1, 0, 0, 40)
    )
    assert cov.gap_count == 0
    assert cov.time_coverage_ratio == 1.0


def test_quality_flags_documented() -> None:
    from app.models.quality import QUALITY_FLAG_DOCS, QUALITY_FLAG_FROZEN_CANDIDATE, describe_flags

    assert QUALITY_FLAG_FROZEN_CANDIDATE in QUALITY_FLAG_DOCS
    assert "frozen_candidate" in QUALITY_FLAG_DOCS[QUALITY_FLAG_FROZEN_CANDIDATE]
    assert describe_flags(QUALITY_FLAG_FROZEN_CANDIDATE) == ["frozen_candidate"]
    assert describe_flags(0) == []
