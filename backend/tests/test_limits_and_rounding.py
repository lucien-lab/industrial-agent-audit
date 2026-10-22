"""P2-6（结果集上限/分页）与 P2-1（缺失样本取整口径）回归测试。"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text

from app.models.quality import estimate_missing_samples
from app.services import queries
from app.services.metropt_import import import_metropt_csv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _equipment_id(session):
    return queries.get_equipment(session, "metropt3-apu-01").id


# --------------------------------------------------------------------------- #
# P2-6：记录列表必须分页，且拒绝非法参数
# --------------------------------------------------------------------------- #
def test_reading_list_rejects_invalid_limit(session, engine, real_head_csv: Path) -> None:
    import_metropt_csv(real_head_csv, engine=engine)
    eid = _equipment_id(session)
    window = (datetime(2020, 2, 1), datetime(2020, 3, 1))

    with pytest.raises(ValueError, match="不能超过"):
        queries.readings_between(session, eid, *window, limit=queries.MAX_READING_PAGE_SIZE + 1)
    with pytest.raises(ValueError, match=">= 1"):
        queries.readings_between(session, eid, *window, limit=0)
    with pytest.raises(ValueError, match="offset"):
        queries.readings_between(session, eid, *window, offset=-1)
    with pytest.raises(ValueError, match="整数"):
        queries.readings_between(session, eid, *window, limit="100")  # type: ignore[arg-type]


def test_reading_list_default_page_size(session, engine, real_head_csv: Path) -> None:
    import_metropt_csv(real_head_csv, engine=engine)
    eid = _equipment_id(session)
    rows = queries.readings_between(session, eid, datetime(2020, 2, 1), datetime(2020, 3, 1))
    assert len(rows) == 400  # 小于默认页大小
    assert queries.DEFAULT_READING_PAGE_SIZE == 1000
    assert queries.MAX_READING_PAGE_SIZE == 5000


def test_reading_pagination_is_stable_and_complete(session, engine, real_head_csv: Path) -> None:
    import_metropt_csv(real_head_csv, engine=engine)
    eid = _equipment_id(session)
    start, end = datetime(2020, 2, 1), datetime(2020, 3, 1)
    total = queries.count_readings(session, eid, start, end)

    collected = []
    offset = 0
    while len(collected) < total:
        page = queries.readings_between(session, eid, start, end, limit=64, offset=offset)
        assert page, "分页提前返回空页"
        collected.extend(page)
        offset += len(page)

    assert len(collected) == total == 400
    ids = [r.id for r in collected]
    assert len(set(ids)) == total, "分页不得重复返回同一行"
    timestamps = [r.observed_at for r in collected]
    assert timestamps == sorted(timestamps)


def test_frozen_candidates_reject_oversized_limit(session, engine, real_head_csv: Path) -> None:
    import_metropt_csv(real_head_csv, engine=engine)
    eid = _equipment_id(session)
    with pytest.raises(ValueError):
        queries.frozen_candidates(session, eid, limit=queries.MAX_READING_PAGE_SIZE + 1)


def test_small_lists_enforce_their_own_cap(session, engine, real_head_csv: Path) -> None:
    import_metropt_csv(real_head_csv, engine=engine)
    eid = _equipment_id(session)
    with pytest.raises(ValueError, match="不能超过"):
        queries.list_fault_events(session, eid, limit=queries.MAX_SMALL_LIST_SIZE + 1)
    with pytest.raises(ValueError, match="不能超过"):
        queries.list_gaps(session, eid, datetime(2020, 2, 1), datetime(2020, 3, 1),
                          limit=queries.MAX_SMALL_LIST_SIZE + 1)
    with pytest.raises(ValueError, match="不能超过"):
        queries.list_equipment(session, limit=queries.MAX_SMALL_LIST_SIZE + 1)


def test_aggregation_is_not_capped(session, engine, real_head_csv: Path) -> None:
    """聚合查询只返回一行汇总，不应被记录列表的 limit 规则限制。"""
    import_metropt_csv(real_head_csv, engine=engine)
    eid = _equipment_id(session)
    stats = queries.analog_statistics(session, eid, datetime(2020, 2, 1), datetime(2020, 3, 1))
    assert len(stats) == 7
    assert all(item.count == 400 for item in stats)


# --------------------------------------------------------------------------- #
# P2-1：缺失样本取整口径
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (30, 2),      # 恰为阈值
        (35, 3),      # 3.5 -> half-up 4 -> 3
        (45, 4),      # 4.5 -> half-up 5 -> 4（旧实现会算成 3）
        (60, 5),
        (2505, 250),  # 250.5 -> 251 -> 250（旧实现 249）
        (77305, 7730),
    ],
)
def test_estimate_missing_samples_uses_half_up(seconds: int, expected: int) -> None:
    assert estimate_missing_samples(seconds) == expected


def test_estimate_matches_postgres_round(session) -> None:
    """Python 侧口径必须与 PostgreSQL `round(numeric)` 完全一致。"""
    for seconds in (30, 35, 45, 55, 60, 245, 2505, 12065, 77305):
        pg_value = session.execute(
            text("SELECT GREATEST(round(CAST(:s AS numeric) / 10)::int - 1, 0)"),
            {"s": seconds},
        ).scalar()
        assert estimate_missing_samples(seconds) == pg_value, seconds


def test_gap_rows_store_half_up_value(session, engine, tmp_path: Path) -> None:
    """1,405 s 的缺口 = 140.5 个步长 -> 140 个缺失样本（half-up）。"""
    header = (
        "Unnamed: 0,timestamp,TP2,TP3,H1,DV_pressure,Reservoirs,"
        "Oil_temperature,Motor_current,COMP,DV_eletric,Towers,MPG,LPS,"
        "Pressure_switch,Oil_level,Caudal_impulses"
    )
    rows = [
        "0,2020-02-01 00:00:00,1.0,9.0,8.0,0.0,9.0,50.0,1.0,1.0,0.0,1.0,1.0,0.0,1.0,1.0,1.0",
        "10,2020-02-01 00:23:25,1.1,9.1,8.1,0.1,9.1,50.1,1.1,1.0,0.0,1.0,1.0,0.0,1.0,1.0,1.0",
    ]
    path = tmp_path / "half_up_gap.csv"
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")

    import_metropt_csv(path, engine=engine)
    gap = session.execute(
        text("SELECT gap_seconds, missing_samples FROM data_gaps")
    ).one()
    assert gap.gap_seconds == 1405
    assert gap.missing_samples == 140


def test_recompute_script_is_read_only_by_default(session, engine, synthetic_csv: Path) -> None:
    import_metropt_csv(synthetic_csv, engine=engine)  # 1 处 60 s 缺口 -> 5
    session.execute(text("UPDATE data_gaps SET missing_samples = 4"))
    session.commit()

    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "recompute_gap_samples.py"), "--json"],
        cwd=PROJECT_ROOT,
        env={**__import__("os").environ},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert '"gaps_changed": 1' in completed.stdout
    assert '"applied": false' in completed.stdout
    # 只读模式不得修改数据
    assert session.execute(text("SELECT missing_samples FROM data_gaps")).scalar() == 4


def test_recompute_script_apply_on_test_database(session, engine, synthetic_csv: Path) -> None:
    import_metropt_csv(synthetic_csv, engine=engine)
    session.execute(text("UPDATE data_gaps SET missing_samples = 4"))
    session.commit()

    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "recompute_gap_samples.py"),
            "--apply",
            "--json",
        ],
        cwd=PROJECT_ROOT,
        env={**__import__("os").environ},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert '"updated_rows": 1' in completed.stdout
    assert session.execute(text("SELECT missing_samples FROM data_gaps")).scalar() == 5


def test_recompute_script_apply_handles_half_step_gap(
    session, engine, tmp_path: Path
) -> None:
    """回归：Δ 恰为 x.5 个步长时 `--apply` 必须真的写库。

    旧实现的 UPDATE 用 `round(gap_seconds / :step)`，而 `:step` 以浮点绑定，
    PostgreSQL 会走 `round(double precision)`（银行家舍入）：1405/10=140.5 → 140，
    与 half-up 的 141 相差 1，导致 dry-run 报“需修订”，实际却更新 0 行。
    """
    header = (
        "Unnamed: 0,timestamp,TP2,TP3,H1,DV_pressure,Reservoirs,"
        "Oil_temperature,Motor_current,COMP,DV_eletric,Towers,MPG,LPS,"
        "Pressure_switch,Oil_level,Caudal_impulses"
    )
    rows = [
        "0,2020-02-01 00:00:00,1.0,9.0,8.0,0.0,9.0,50.0,1.0,1.0,0.0,1.0,1.0,0.0,1.0,1.0,1.0",
        "10,2020-02-01 00:23:25,1.1,9.1,8.1,0.1,9.1,50.1,1.1,1.0,0.0,1.0,1.0,0.0,1.0,1.0,1.0",
    ]
    path = tmp_path / "half_step_gap.csv"
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")

    import_metropt_csv(path, engine=engine)
    session.execute(text("UPDATE data_gaps SET missing_samples = 139"))
    session.commit()

    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "recompute_gap_samples.py"), "--apply", "--json"],
        cwd=PROJECT_ROOT,
        env={**__import__("os").environ},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert '"updated_rows": 1' in completed.stdout
    assert '"remaining_diff_rows": 0' in completed.stdout
    assert session.execute(text("SELECT missing_samples FROM data_gaps")).scalar() == 140


def test_recompute_script_refuses_production_without_flag(database_url: str) -> None:
    """非 *_test 库默认拒绝写入（用生产库 URL 验证，但不会执行任何 UPDATE）。"""
    env_file = PROJECT_ROOT / ".env"
    production_url = None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            production_url = line.split("=", 1)[1].strip()
    if not production_url:
        pytest.skip("缺少 DATABASE_URL")

    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "recompute_gap_samples.py"),
            "--database-url",
            production_url,
            "--apply",
        ],
        cwd=PROJECT_ROOT,
        env={**__import__("os").environ},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 5
    assert "不是 *_test" in completed.stderr
