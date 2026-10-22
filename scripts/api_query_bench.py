"""API 查询性能基准（只读）。

对 API 实际使用的 SQL 逐条执行 `EXPLAIN (ANALYZE, BUFFERS)`，打印真实耗时与执行计划要点。
不写库、不改数据，可直接对全量数据副本运行：

    IAA_FULLDATA_DATABASE_URL=postgresql+psycopg://... \
      python scripts/api_query_bench.py --window-hours 24

输出中的 `execution_ms` 来自 PostgreSQL 自身的计时，`plan` 列显示是否命中索引与扫描行数。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import load_env_file  # noqa: E402
from app.services import queries  # noqa: E402

EQUIPMENT_CODE = "metropt3-apu-01"


def _database_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    load_env_file()
    url = os.environ.get("IAA_FULLDATA_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("需要 --database-url 或 IAA_FULLDATA_DATABASE_URL / DATABASE_URL")
    return url


def explain(session: Session, label: str, sql: str, params: dict[str, object]) -> None:
    plan_rows = session.execute(
        text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {sql}"), params
    ).all()
    plan = "\n".join(row[0] for row in plan_rows)
    execution_ms = None
    rows_returned = None
    for line in plan.splitlines():
        if "Execution Time:" in line:
            execution_ms = float(line.split(":", 1)[1].strip().split()[0])
        if line.strip().startswith("rows=") and rows_returned is None:
            try:
                rows_returned = int(line.strip().split("rows=")[1].split()[0])
            except (IndexError, ValueError):  # pragma: no cover - 计划文本差异
                rows_returned = None
    scans = [seg.strip() for seg in plan.splitlines() if "Scan" in seg or "Index" in seg]
    print(f"\n### {label}")
    print(f"execution_ms={execution_ms}  first_node_rows={rows_returned}")
    for segment in scans[:4]:
        print("  " + segment.strip()[:160])
    first_line = plan.splitlines()[0].strip()
    print(f"  plan_root: {first_line[:160]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="API 查询 EXPLAIN ANALYZE 基准（只读）")
    parser.add_argument("--database-url", default=None)
    parser.add_argument(
        "--window-hours", type=int, default=24, help="窗口小时数（默认 24，取官方故障 E1 当天）"
    )
    parser.add_argument(
        "--start", default="2020-04-18 00:00:00", help="窗口起点（无时区，默认故障 E1 当天）"
    )
    args = parser.parse_args()

    url = _database_url(args.database_url)
    engine = create_engine(url, pool_pre_ping=True, future=True)
    with Session(engine) as session:
        equipment = queries.get_equipment(session, EQUIPMENT_CODE)
        if equipment is None:
            raise SystemExit("数据库中没有设备 metropt3-apu-01")
        eid = equipment.id
        start = session.execute(text("SELECT CAST(:start AS timestamp)"), {"start": args.start}).scalar()
        end = start.replace(hour=0) + __import__("datetime").timedelta(hours=args.window_hours)
        closed_end = end - __import__("datetime").timedelta(microseconds=1)
        print(f"database={url.rsplit('/', 1)[-1]}  equipment={EQUIPMENT_CODE}")
        print(f"window=[{start}, {end})  hours={args.window_hours}")

        total_start = time.perf_counter()
        rows = queries.count_readings(session, eid, start, closed_end)
        print(f"\n窗口内总行数={rows}（count 查询客户端耗时 {(time.perf_counter()-total_start)*1000:.1f} ms）")

        explain(
            session,
            "count_readings（分页 total）",
            "SELECT count(*) FROM sensor_readings WHERE equipment_id = :eid"
            " AND observed_at >= :start AND observed_at <= :end",
            {"eid": eid, "start": start, "end": closed_end},
        )
        explain(
            session,
            "readings_between（列表页，limit 100）",
            "SELECT id FROM sensor_readings WHERE equipment_id = :eid"
            " AND observed_at >= :start AND observed_at <= :end"
            " ORDER BY observed_at, id LIMIT 100",
            {"eid": eid, "start": start, "end": closed_end},
        )
        analog_sql = ", ".join(
            f"min({c}), max({c}), avg({c}), count({c}), stddev_samp({c})"
            for c in __import__("app.models", fromlist=["ANALOG_COLUMNS"]).ANALOG_COLUMNS
        )
        explain(
            session,
            "analog_statistics（7 路模拟量单次聚合）",
            f"SELECT {analog_sql} FROM sensor_readings WHERE equipment_id = :eid"
            " AND observed_at >= :start AND observed_at <= :end",
            {"eid": eid, "start": start, "end": closed_end},
        )
        digital_sql = ", ".join(
            f"count(*) FILTER (WHERE {c} = 0), count(*) FILTER (WHERE {c} = 1)"
            for c in __import__("app.models", fromlist=["DIGITAL_COLUMNS"]).DIGITAL_COLUMNS
        )
        lag_sql = ", ".join(
            f"lag({c}) OVER (ORDER BY observed_at, id) AS {c}_prev"
            for c in __import__("app.models", fromlist=["DIGITAL_COLUMNS"]).DIGITAL_COLUMNS
        )
        explain(
            session,
            "digital_statistics（窗口函数统计状态切换次数）",
            f"SELECT {digital_sql} FROM (SELECT {lag_sql}, * FROM sensor_readings"
            " WHERE equipment_id = :eid AND observed_at >= :start AND observed_at <= :end) t",
            {"eid": eid, "start": start, "end": closed_end},
        )
        explain(
            session,
            "coverage（数据库函数 sensor_coverage）",
            "SELECT * FROM sensor_coverage(:eid, :start, :end, 10)",
            {"eid": eid, "start": start, "end": closed_end},
        )
        explain(
            session,
            "gap_overlap_stats（缺口时长按窗口裁剪）",
            """
            WITH overlapping AS (
                SELECT g.gap_start, g.gap_end, g.missing_samples,
                       GREATEST(g.gap_start, CAST(:start AS timestamp)) AS clip_start,
                       LEAST(g.gap_end, CAST(:end AS timestamp)) AS clip_end
                FROM data_gaps g
                WHERE g.equipment_id = :eid
                  AND g.gap_start <= CAST(:end AS timestamp)
                  AND g.gap_end > CAST(:start AS timestamp)
            )
            SELECT count(*), sum(EXTRACT(epoch FROM (clip_end - clip_start))) FROM overlapping
            """,
            {"eid": eid, "start": start, "end": end},
        )
        explain(
            session,
            "count_frozen_candidates（部分索引 quality_flags <> 0）",
            "SELECT count(*) FROM sensor_readings WHERE equipment_id = :eid"
            " AND observed_at >= :start AND observed_at <= :end AND (quality_flags & 1) <> 0",
            {"eid": eid, "start": start, "end": closed_end},
        )
        explain(
            session,
            "list_fault_events（半开区间重叠）",
            "SELECT id FROM fault_events WHERE equipment_id = :eid"
            " AND end_time >= :start AND start_time <= :end ORDER BY start_time, id LIMIT 100",
            {"eid": eid, "start": start, "end": closed_end},
        )
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
