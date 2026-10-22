#!/usr/bin/env python
"""Phase 1B 验证脚本：数据库查询、质量标记、覆盖度、幂等与 CSV 交叉核对。

与导入脚本相互独立：
- CSV 一侧用 pandas 全量重算（不复用导入模块的解析逻辑）；
- 数据库一侧只跑 SQL / 查询服务；
- 两边逐项比对，任何不一致都会让脚本以非零状态退出。

用法：
    python scripts/verify_phase1b.py                      # 默认：**只读**校验
    python scripts/verify_phase1b.py --skip-explain       # 只读，跳过 EXPLAIN
    python scripts/verify_phase1b.py --json out.json      # 只读，结果落文件
    python scripts/verify_phase1b.py --write-tests        # 显式写入型测试（仅 *_test 库）

P2-5 修复：默认模式在 PostgreSQL **只读事务**（SET TRANSACTION READ ONLY）中执行，
因此不可能写入 sensor_readings / ingest_runs 等业务表；任何写入都会由数据库直接拒绝。
写入型验证（重复导入检查）必须显式加 --write-tests，且目标库名必须以 _test 结尾。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
for path in (BACKEND_DIR, PROJECT_ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.session import get_engine  # noqa: E402
from app.services import dataset as ds  # noqa: E402
from app.services import queries  # noqa: E402
from app.services.metropt_import import (  # noqa: E402
    ANALOG_COLUMN_MAP,
    DIGITAL_COLUMN_MAP,
    import_metropt_csv,
)

ANALOG = list(ANALOG_COLUMN_MAP.items())  # (csv_name, db_name)
DIGITAL = list(DIGITAL_COLUMN_MAP.items())

GAP_THRESHOLD_S = 30.0
TOLERANCE = 1e-4  # numeric(8,4) 与 float 统计的允许偏差

failures: list[str] = []
checks: list[dict[str, Any]] = []


def record(name: str, expected: Any, actual: Any, ok: bool, note: str = "") -> None:
    checks.append(
        {"check": name, "expected": expected, "actual": actual, "ok": bool(ok), "note": note}
    )
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] {name}: expected={expected!r} actual={actual!r} {note}")
    if not ok:
        failures.append(name)


def close(a: float, b: float, tol: float = TOLERANCE) -> bool:
    return abs(a - b) <= tol


def csv_baseline(
    path: Path, window: tuple[datetime, datetime] | None = None
) -> dict[str, Any]:
    """从原始 CSV 独立重算基线统计（不复用导入模块的解析实现）。"""
    print(f"[csv] 读取并统计 {path.name} …")
    started = time.perf_counter()
    frame = pd.read_csv(
        path,
        dtype=str,
        keep_default_na=False,
        na_filter=False,
        low_memory=False,
    )
    timestamps = pd.to_datetime(frame["timestamp"], format="%Y-%m-%d %H:%M:%S", errors="coerce")
    analog = {csv_col: pd.to_numeric(frame[csv_col], errors="coerce").round(4) for csv_col, _ in ANALOG}
    digital = {csv_col: pd.to_numeric(frame[csv_col], errors="coerce").astype("int64") for csv_col, _ in DIGITAL}

    # 独立实现冻结判据：7 路模拟量同时与上一行相同（四舍五入到 4 位小数后比较）
    same = None
    for series in analog.values():
        equal = series.eq(series.shift(1))
        same = equal if same is None else (same & equal)
    frozen_rows = int(same.iloc[1:].sum())

    intervals = timestamps.diff().dt.total_seconds().dropna()
    gap_count = int((intervals >= GAP_THRESHOLD_S).sum())

    window_stats: dict[str, Any] = {}
    if window is not None:
        w_start, w_end = window
        mask = (timestamps >= w_start) & (timestamps <= w_end)
        window_stats = {
            "rows": int(mask.sum()),
            "first_timestamp": timestamps[mask].min().strftime("%Y-%m-%d %H:%M:%S"),
            "last_timestamp": timestamps[mask].max().strftime("%Y-%m-%d %H:%M:%S"),
            "analog": {
                db_col: {
                    "min": float(analog[csv_col][mask].min()),
                    "max": float(analog[csv_col][mask].max()),
                    "mean": float(analog[csv_col][mask].mean()),
                }
                for csv_col, db_col in ANALOG
            },
        }

    return {
        "rows": int(len(frame)),
        "first_timestamp": timestamps.min().strftime("%Y-%m-%d %H:%M:%S"),
        "last_timestamp": timestamps.max().strftime("%Y-%m-%d %H:%M:%S"),
        "min_row_number": int(frame["Unnamed: 0"].astype("int64").min()),
        "max_row_number": int(frame["Unnamed: 0"].astype("int64").max()),
        "analog": {
            db_col: {
                "min": float(analog[csv_col].min()),
                "max": float(analog[csv_col].max()),
                "mean": float(analog[csv_col].mean()),
            }
            for csv_col, db_col in ANALOG
        },
        "digital_ones": {
            db_col: int(digital[csv_col].sum()) for csv_col, db_col in DIGITAL
        },
        "frozen_rows": frozen_rows,
        "gap_count": gap_count,
        "window": window_stats,
        "elapsed_s": round(time.perf_counter() - started, 2),
    }


def open_readonly_session(engine):
    """在只读事务中打开会话：数据库层面禁止任何写操作。"""
    conn = engine.connect()
    conn.execute(text("SET TRANSACTION READ ONLY"))
    session = Session(bind=conn, expire_on_commit=False)
    return conn, session


def ensure_isolated_write_target(engine) -> str:
    """写入型验证只允许落在 *_test 库，且以真实连接库名为准。"""
    with engine.connect() as conn:
        database = str(conn.execute(text("SELECT current_database()")).scalar())
    if not database.endswith("_test"):
        raise SystemExit(
            f"[拒绝] --write-tests 只允许在 *_[test] 数据库执行，当前实际连接为 {database!r}。"
        )
    return database


def explain(engine, sql: str, params: dict[str, Any] | None = None) -> str:
    with engine.connect() as conn:
        rows = conn.execute(text("EXPLAIN (ANALYZE, BUFFERS) " + sql), params or {}).fetchall()
    return "\n".join(row[0] for row in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 1B 验证")
    parser.add_argument("--json", type=Path, default=None, help="把结果写入 JSON 文件")
    parser.add_argument(
        "--write-tests",
        action="store_true",
        help="执行写入型验证（重复导入）；仅允许在 *_test 库，默认关闭",
    )
    parser.add_argument("--skip-explain", action="store_true")
    args = parser.parse_args(argv)

    get_settings()  # 只用于校验配置存在
    engine = get_engine()
    if args.write_tests:
        database = ensure_isolated_write_target(engine)
        print(f"[mode] read-write-tests（目标库 {database}，必须以 _test 结尾）")
    else:
        print("[mode] read-only（只读事务，数据库层面禁止写入）")
    e1_start = datetime(2020, 4, 18, 0, 0, 0)
    e1_end = datetime(2020, 4, 18, 23, 59, 0)
    baseline = csv_baseline(ds.CSV_PATH, window=(e1_start, e1_end))
    report: dict[str, Any] = {
        "baseline": baseline,
        "mode": "read-write-tests" if args.write_tests else "read-only",
    }

    ro_conn, session = open_readonly_session(engine)
    try:
        equipment = queries.get_equipment(session, ds.EQUIPMENT_CODE)
        if equipment is None:
            print("[FAIL] 设备未登记，请先执行导入", file=sys.stderr)
            return 1
        record("设备查询", ds.EQUIPMENT_CODE, equipment.equipment_code, equipment.equipment_code == ds.EQUIPMENT_CODE)
        eid = equipment.id

        total = int(session.scalar(text("SELECT count(*) FROM sensor_readings")) or 0)
        record("入库总行数", baseline["rows"], total, total == baseline["rows"])
        if total != baseline["rows"]:
            # 本脚本是“完整数据集”交叉核对工具：库内数据量不符时继续断言会产生
            # 大量噪声与越界错误，直接明确中止更有用。
            print(
                f"[中止] 当前库 sensor_readings={total:,} 行，与原始 CSV {baseline['rows']:,} 行不一致；"
                "本脚本只对完整导入的库有效。",
                file=sys.stderr,
            )
            return 1

        span = session.execute(
            text("SELECT min(observed_at), max(observed_at), min(source_row_number),"
                 " max(source_row_number) FROM sensor_readings")
        ).one()
        record(
            "最早时间",
            baseline["first_timestamp"],
            span[0].strftime("%Y-%m-%d %H:%M:%S"),
            span[0].strftime("%Y-%m-%d %H:%M:%S") == baseline["first_timestamp"],
        )
        record(
            "最晚时间",
            baseline["last_timestamp"],
            span[1].strftime("%Y-%m-%d %H:%M:%S"),
            span[1].strftime("%Y-%m-%d %H:%M:%S") == baseline["last_timestamp"],
        )
        record("最小源行号", baseline["min_row_number"], span[2], span[2] == baseline["min_row_number"])
        record("最大源行号", baseline["max_row_number"], span[3], span[3] == baseline["max_row_number"])

        distinct_rows = int(session.scalar(text("SELECT count(DISTINCT source_row_number) FROM sensor_readings")) or 0)
        record("源行号唯一且连续", baseline["rows"], distinct_rows, distinct_rows == baseline["rows"])

        # ---- 时间范围查询 / 计数 ----
        start, end = e1_start, e1_end
        window_count = queries.count_readings(session, eid, start, end)
        # 记录列表接口有强制上限，按 5,000 行分页取回，顺带验证分页本身
        window_rows = []
        while len(window_rows) < window_count:
            page = queries.readings_between(
                session,
                eid,
                start,
                end,
                limit=queries.MAX_READING_PAGE_SIZE,
                offset=len(window_rows),
            )
            if not page:
                break
            window_rows.extend(page)
        window_ref = baseline["window"]
        record(
            "E1 时间窗行数（DB vs CSV 窗口统计）",
            window_ref["rows"],
            window_count,
            window_count == window_ref["rows"] == len(window_rows) == 8657,
            "Phase 1A 报告 E1 区间 8,657 行",
        )
        record(
            "E1 时间窗首行时间（源时间含 1 s 抖动）",
            window_ref["first_timestamp"],
            window_rows[0].observed_at.strftime("%Y-%m-%d %H:%M:%S"),
            window_rows[0].observed_at.strftime("%Y-%m-%d %H:%M:%S")
            == window_ref["first_timestamp"],
        )
        record(
            "时间窗左边界不被越界",
            True,
            window_rows[0].observed_at >= start,
            window_rows[0].observed_at >= start,
        )
        record(
            "时间窗末行时间",
            window_ref["last_timestamp"],
            window_rows[-1].observed_at.strftime("%Y-%m-%d %H:%M:%S"),
            window_rows[-1].observed_at.strftime("%Y-%m-%d %H:%M:%S")
            == window_ref["last_timestamp"],
        )

        # ---- 模拟量统计 vs CSV（同一时间窗）----
        stats = queries.analog_statistics(session, eid, start, end)
        for item in stats:
            ref = window_ref["analog"][item.column]
            record(
                f"E1 {item.column} min",
                round(ref["min"], 4),
                float(item.minimum),
                close(float(item.minimum), ref["min"]),
            )
            record(
                f"E1 {item.column} max",
                round(ref["max"], 4),
                float(item.maximum),
                close(float(item.maximum), ref["max"]),
            )
            record(
                f"E1 {item.column} avg",
                round(ref["mean"], 4),
                float(item.average),
                close(float(item.average), ref["mean"]),
            )

        # ---- 数字量一致性（全量 1 的计数）----
        ones = session.execute(
            text(
                "SELECT "
                + ", ".join(f"sum({db_col}) AS {db_col}" for _, db_col in DIGITAL)
                + " FROM sensor_readings"
            )
        ).one()
        for _, db_col in DIGITAL:
            expected = baseline["digital_ones"][db_col]
            actual = int(getattr(ones, db_col))
            record(f"数字量 {db_col} 取 1 行数", expected, actual, actual == expected)

        # ---- 质量标记 ----
        frozen_total = int(
            session.scalar(text("SELECT count(*) FROM sensor_readings WHERE quality_flags <> 0")) or 0
        )
        record("冻结候选行数（DB vs CSV 独立重算）", baseline["frozen_rows"], frozen_total,
               frozen_total == baseline["frozen_rows"], "Phase 1A 为 50,870 行")
        segments = queries.frozen_segments(session, eid, limit=1000)
        record("冻结候选连续段数", 24, len(segments), len(segments) == 24, "Phase 1A 为 24 段")

        gap_total = int(session.scalar(text("SELECT count(*) FROM data_gaps")) or 0)
        record("采样缺口记录数（DB vs CSV 独立重算）", baseline["gap_count"], gap_total,
               gap_total == baseline["gap_count"], "Phase 1A 为 332 处（≥30 s）")

        # ---- 官方故障事件 ----
        faults = queries.list_fault_events(session, eid)
        record("官方故障事件数", 4, len(faults), len(faults) == 4)
        codes = [f.event_code for f in faults]
        record("故障事件编号", ["E1", "E2", "E3", "E4"], codes, codes == ["E1", "E2", "E3", "E4"])
        e2 = next((f for f in faults if f.event_code == "E2"), None)
        record(
            "E2 维护日期疑笔误标记",
            "typo_suspected",
            e2.verification_status if e2 else None,
            bool(e2 and e2.verification_status == "typo_suspected"),
        )

        # ---- 覆盖度 ----
        cov = queries.coverage(session, eid, start, end)
        record("E1 覆盖度-实际行数", 8657, cov.observed_rows, cov.observed_rows == 8657)
        record("E1 覆盖度-缺失样本", 34, cov.missing_samples, cov.missing_samples == 34)
        record("E1 覆盖度-行比率", 1.0, round(cov.coverage_ratio or 0, 4),
               close(cov.coverage_ratio or 0, 1.0, 1e-9), "抖动导致封顶 1.0")
        record("E1 覆盖度-时间比率", 0.996, round(cov.time_coverage_ratio or 0, 3),
               close(cov.time_coverage_ratio or 0, 0.996, 1e-3), "Phase 1A 报告口径")
        e3_start, e3_end = datetime(2020, 6, 5, 10), datetime(2020, 6, 7, 14, 30)
        cov_e3 = queries.coverage(session, eid, e3_start, e3_end)
        record("E3 行覆盖率", 0.9161, round(cov_e3.coverage_ratio or 0, 4),
               close(cov_e3.coverage_ratio or 0, 0.9161, 1e-3),
               "17315/18901，与时间口径不同，按设计分别返回")
        # E3 窗口末端存在一个跨边界的大缺口（06-07 14:19:39 起，延续到次日）。
        # 函数按“窗口内实际缺失”计入其 621 s 中的 620 s，因此得到 0.9081；
        # Phase 1A 只统计完全落在窗口内的缺口，得 0.9114。两者差异是口径而非
        # 数据不一致，下面两项分别验证：
        record(
            "E3 时间覆盖率（含跨窗口尾部缺口）",
            0.9081,
            round(cov_e3.time_coverage_ratio or 0, 4),
            close(cov_e3.time_coverage_ratio or 0, 0.9081, 1e-3),
            "比 Phase 1A 更严格：计入窗口尾部 620 s 的缺失",
        )
        contained = session.execute(
            text(
                "SELECT 1 - COALESCE(sum(missing_samples),0) * 10.0 /"
                " EXTRACT(epoch FROM (CAST(:end AS timestamp) - CAST(:start AS timestamp)))"
                " FROM data_gaps WHERE equipment_id = :eid"
                " AND gap_start >= :start AND gap_end <= :end"
            ),
            {"eid": eid, "start": e3_start, "end": e3_end},
        ).scalar()
        record(
            "E3 仅统计窗口内完整缺口的覆盖率",
            0.9114,
            round(float(contained), 4),
            close(float(contained), 0.9114, 1e-3),
            "与 Phase 1A 报告 0.9114 一致（口径对齐验证）",
        )

        # ---- 冻结候选可查询 ----
        frozen_rows = queries.frozen_candidates(session, eid, limit=5)
        record("冻结候选可查询", 5, len(frozen_rows), len(frozen_rows) == 5)
        record("冻结候选均带标记", True, all(r.quality_flags != 0 for r in frozen_rows),
               all(r.quality_flags != 0 for r in frozen_rows))

        # ---- 原始时间文本保真 ----
        mismatch = int(
            session.scalar(
                text(
                    "SELECT count(*) FROM sensor_readings"
                    " WHERE to_char(observed_at, 'YYYY-MM-DD HH24:MI:SS') <> source_timestamp_raw"
                )
            )
            or 0
        )
        record("源时间文本与入库时间完全一致", 0, mismatch, mismatch == 0)

        report["ingest_runs"] = session.execute(
            text(
                "SELECT id, status, attempted_rows, inserted_rows, conflict_rows, invalid_rows,"
                " frozen_rows, gap_rows_written, duration_seconds, started_at"
                " FROM ingest_runs ORDER BY id"
            )
        ).mappings().all()
        report["ingest_runs"] = [dict(r) for r in report["ingest_runs"]]
    finally:
        session.close()
        ro_conn.rollback()  # 只读事务，回滚即结束，不写任何数据
        ro_conn.close()

    # ---- 幂等性：重复导入最前面 1 万条（仅 --write-tests，且仅 *_test 库）----
    if args.write_tests:
        before = int(session_count())
        stats = import_metropt_csv(max_rows=10000, engine=engine)
        after = int(session_count())
        record("重复导入-新增行数", 0, stats.inserted_rows, stats.inserted_rows == 0)
        record("重复导入-冲突行数", 10000, stats.conflict_rows, stats.conflict_rows == 10000)
        record("重复导入-表行数不变", before, after, before == after)

    # ---- EXPLAIN ANALYZE ----
    if not args.skip_explain:
        explain_queries = {
            "时间范围计数": (
                "SELECT count(*) FROM sensor_readings"
                " WHERE equipment_id = :eid AND observed_at >= :start AND observed_at <= :end",
                {"eid": str(eid), "start": start, "end": end},
            ),
            "时间范围取数(limit 1000)": (
                "SELECT * FROM sensor_readings WHERE equipment_id = :eid"
                " AND observed_at >= :start AND observed_at <= :end"
                " ORDER BY observed_at LIMIT 1000",
                {"eid": str(eid), "start": start, "end": end},
            ),
            "冻结候选部分索引": (
                "SELECT count(*) FROM sensor_readings WHERE equipment_id = :eid"
                " AND quality_flags <> 0",
                {"eid": str(eid)},
            ),
            "覆盖度函数": (
                "SELECT * FROM sensor_coverage(:eid, :start, :end, 10)",
                {"eid": str(eid), "start": start, "end": end},
            ),
        }
        report["explain"] = {}
        for name, (sql, params) in explain_queries.items():
            plan = explain(engine, sql, params)
            report["explain"][name] = plan
            print(f"\n--- EXPLAIN ANALYZE: {name} ---")
            print(plan)
            if "Seq Scan on sensor_readings" in plan:
                failures.append(f"{name}: 出现全表 Seq Scan")
                print(f"[WARN] {name} 使用全表扫描，索引可能未被使用")

    report["checks"] = checks
    report["failed_checks"] = failures

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"\n结果已写入 {args.json}")

    print("\n" + "=" * 60)
    if failures:
        print(f"验证失败：{len(failures)} 项")
        for item in failures:
            print(f"  - {item}")
        return 1
    print(f"全部通过：{len(checks)} 项检查")
    return 0


def session_count() -> int:
    engine = get_engine()
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM sensor_readings")).scalar() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
