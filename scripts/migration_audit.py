#!/usr/bin/env python
"""只读迁移审计：生成/比对数据库快照（Schema + 数据指纹 + 行数）。

用途（Phase 1B 收口）：
    1) 迁移前对目标库拍摄基线快照；
    2) 迁移/修订后再次快照，与基线逐项比对；
    3) 也可用于比对“生产库”与“备份恢复副本”，验证备份可恢复且内容一致。

安全设计：
    - 整个会话 `SET TRANSACTION READ ONLY`：任何写入都会被数据库拒绝；
    - 不做 DDL、不建临时表、不改数据；
    - 输出中的连接串一律脱敏（mask_password）；
    - 不输出任何业务明细（只有聚合与指纹），可安全写入文档/日志。

内容指纹（canonical，17 字段，本文件唯一定义）：
    md5(string_agg(concat_ws('|',
        observed_at, tp2, tp3, h1, dv_pressure, reservoirs, oil_temperature,
        motor_current, comp, dv_eletric, towers, mpg, lps, pressure_switch,
        oil_level, caudal_impulses, quality_flags), '||' order by id))
    覆盖 15 路传感器值 + 观测时间 + 质量标记；不含主键/来源/摄入时间，
    因此迁移（只改约束）与缺口修订（只改 data_gaps）都不会改变该指纹。

用法：
    python scripts/migration_audit.py --out /tmp/baseline.json
    python scripts/migration_audit.py --compare /tmp/baseline.json
    python scripts/migration_audit.py --database-url postgresql+psycopg://... --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import get_settings, mask_password  # noqa: E402

ANALOG_COLUMNS = (
    "tp2",
    "tp3",
    "h1",
    "dv_pressure",
    "reservoirs",
    "oil_temperature",
    "motor_current",
)
DIGITAL_COLUMNS = (
    "comp",
    "dv_eletric",
    "towers",
    "mpg",
    "lps",
    "pressure_switch",
    "oil_level",
    "caudal_impulses",
)
FINGERPRINT_FIELDS = (
    "observed_at",
    *ANALOG_COLUMNS,
    *DIGITAL_COLUMNS,
    "quality_flags",
)

BUSINESS_TABLES = (
    "equipment",
    "data_sources",
    "sensor_readings",
    "fault_events",
    "data_gaps",
    "ingest_runs",
)


def fingerprint_sql() -> str:
    expr = ", ".join(f"r.{c}" for c in FINGERPRINT_FIELDS)
    return (
        "SELECT md5(string_agg(concat_ws('|', " + expr + "), '||' ORDER BY r.id))"
        " FROM sensor_readings r"
    )


def snapshot(conn) -> dict[str, Any]:
    def scalar(sql: str, **params: Any) -> Any:
        return conn.execute(text(sql), params).scalar()

    def rows(sql: str, **params: Any) -> list[dict[str, Any]]:
        return [dict(r._mapping) for r in conn.execute(text(sql), params)]

    snap: dict[str, Any] = {
        "database": scalar("SELECT current_database()"),
        "user": scalar("SELECT current_user"),
        "server_version": scalar("SELECT current_setting('server_version')"),
        "alembic_version": scalar("SELECT version_num FROM alembic_version"),
        "fingerprint": {
            "definition": f"md5(string_agg(concat_ws('|', {', '.join(FINGERPRINT_FIELDS)}), '||' ORDER BY id))",
            "field_count": len(FINGERPRINT_FIELDS),
            "value": scalar(fingerprint_sql()),
        },
        "counts": {t: scalar(f"SELECT count(*) FROM {t}") for t in BUSINESS_TABLES},
        "sensor_readings": {
            "min_observed_at": scalar("SELECT min(observed_at)::text FROM sensor_readings"),
            "max_observed_at": scalar("SELECT max(observed_at)::text FROM sensor_readings"),
            "distinct_observed_at": scalar("SELECT count(DISTINCT observed_at) FROM sensor_readings"),
            "frozen_candidate": scalar(
                "SELECT count(*) FROM sensor_readings WHERE quality_flags <> 0"
            ),
            "orphan_equipment": scalar(
                "SELECT count(*) FROM sensor_readings r"
                " LEFT JOIN equipment e ON e.id = r.equipment_id WHERE e.id IS NULL"
            ),
            "orphan_source": scalar(
                "SELECT count(*) FROM sensor_readings r"
                " LEFT JOIN data_sources s ON s.id = r.source_id WHERE s.id IS NULL"
            ),
        },
        "data_gaps": {
            "missing_samples_sum": scalar("SELECT coalesce(sum(missing_samples), 0) FROM data_gaps"),
            "min_gap_start": scalar("SELECT min(gap_start)::text FROM data_gaps"),
            "max_gap_end": scalar("SELECT max(gap_end)::text FROM data_gaps"),
            "orphan_source": scalar(
                "SELECT count(*) FROM data_gaps g"
                " LEFT JOIN data_sources s ON s.id = g.source_id WHERE s.id IS NULL"
            ),
        },
        "fault_events": {
            "codes": [
                r["event_code"]
                for r in rows("SELECT event_code FROM fault_events ORDER BY event_code")
            ],
            "orphan_equipment": scalar(
                "SELECT count(*) FROM fault_events f"
                " LEFT JOIN equipment e ON e.id = f.equipment_id WHERE e.id IS NULL"
            ),
        },
        "data_sources": rows(
            "SELECT id::text AS id, source_file, file_sha256, timezone_status"
            " FROM data_sources ORDER BY source_file"
        ),
        "data_source_sha256_duplicates": rows(
            "SELECT file_sha256, count(*) AS n FROM data_sources"
            " GROUP BY file_sha256 HAVING count(*) > 1"
        ),
        "ingest_runs_status": rows(
            "SELECT status, count(*) AS n FROM ingest_runs GROUP BY status ORDER BY status"
        ),
        "constraints": rows(
            """
            SELECT conrelid::regclass::text AS table_name,
                   conname,
                   contype,
                   pg_get_constraintdef(oid) AS definition
            FROM pg_constraint
            WHERE connamespace = 'public'::regnamespace
              AND conrelid::regclass::text <> 'alembic_version'
            ORDER BY 1, 3 DESC, 2
            """
        ),
        "indexes": rows(
            """
            SELECT tablename AS table_name, indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'public' AND tablename <> 'alembic_version'
            ORDER BY 1, 2
            """
        ),
        "extensions": rows("SELECT extname, extversion FROM pg_extension ORDER BY 1"),
        "derived_objects": {
            "functions": [
                r["routine_name"]
                for r in rows(
                    "SELECT routine_name FROM information_schema.routines"
                    " WHERE routine_schema = 'public' ORDER BY 1"
                )
            ],
            "views": [
                r["table_name"]
                for r in rows(
                    "SELECT table_name FROM information_schema.views"
                    " WHERE table_schema = 'public' ORDER BY 1"
                )
            ],
        },
    }
    snap["gap_rounding_diff"] = gap_rounding_diff(conn)
    return snap


def gap_rounding_diff(conn) -> dict[str, Any]:
    """按 half-up 口径重算 missing_samples，返回与库内值的差异清单。"""
    rows = [
        dict(r._mapping)
        for r in conn.execute(
            text(
                """
                SELECT id,
                       gap_start::text AS gap_start,
                       gap_end::text AS gap_end,
                       round(extract(epoch FROM (gap_end - gap_start)))::int AS gap_seconds,
                       missing_samples AS old_samples,
                       GREATEST(round(extract(epoch FROM (gap_end - gap_start)) / 10)::int - 1, 1)
                           AS new_samples
                FROM data_gaps
                ORDER BY id
                """
            )
        )
    ]
    changed = [r for r in rows if r["old_samples"] != r["new_samples"]]
    return {
        "rule": "missing_samples = max(round_half_up(gap_seconds / 10) - 1, 1)",
        "rows_total": len(rows),
        "rows_changed": len(changed),
        "old_sum": sum(r["old_samples"] for r in rows),
        "new_sum": sum(r["new_samples"] for r in rows),
        "diff": changed,
    }


STRUCTURAL_KEYS = ("constraints", "indexes", "extensions", "derived_objects")


def _keys(items: Any) -> set[str]:
    if not isinstance(items, list):
        return set()
    out: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        if "indexname" in item:
            out.add(f"{item.get('table_name')}.{item['indexname']}")
        elif "conname" in item:
            out.add(f"{item.get('table_name')}.{item['conname']}")
        else:
            out.add(json.dumps(item, sort_keys=True, ensure_ascii=False))
    return out


def _added(before: Any, after: Any) -> list[str]:
    return sorted(_keys(after) - _keys(before))


def _removed(before: Any, after: Any) -> list[str]:
    return sorted(_keys(before) - _keys(after))


def compare(
    baseline: dict[str, Any],
    current: dict[str, Any],
    allow_missing_samples_change: bool = False,
) -> dict[str, Any]:
    """比对两次快照。

    - 数据类（行数、指纹、来源、缺口值、故障事件）必须完全一致；
    - 结构类（约束/索引）允许变化，但逐项列出差异，供人工确认是否为预期变更。
    """
    data_failures: list[str] = []
    # alembic_version 是“本次操作的目标”，不是数据一致性指标：单独记录，不算失败。
    version_change = {
        "before": baseline.get("alembic_version"),
        "after": current.get("alembic_version"),
    }
    for key in ("fingerprint", "counts", "data_sources",
                "data_source_sha256_duplicates", "ingest_runs_status"):
        if baseline.get(key) != current.get(key):
            data_failures.append(key)

    # sensor_readings/data_gaps/fault_events 的聚合字段逐项比较。
    # data_gaps.missing_samples_sum 是本次**预期变更**（23 行 half-up 修订），
    # 仅当调用方显式声明时才允许变化，并单独记录，不混入失败项。
    expected_changes: list[dict[str, Any]] = []
    allowed = {"data_gaps.missing_samples_sum"} if allow_missing_samples_change else set()
    for section in ("sensor_readings", "data_gaps", "fault_events"):
        for field, before in baseline.get(section, {}).items():
            after = current.get(section, {}).get(field)
            if before == after:
                continue
            key = f"{section}.{field}"
            if key in allowed:
                expected_changes.append({"field": key, "before": before, "after": after})
            else:
                data_failures.append(key)

    structural: dict[str, Any] = {}
    for key in STRUCTURAL_KEYS:
        before, after = baseline.get(key), current.get(key)
        if before != after:
            structural[key] = {
                "added": _added(before, after),
                "removed": _removed(before, after),
            }

    before_gap = baseline.get("gap_rounding_diff", {})
    after_gap = current.get("gap_rounding_diff", {})
    return {
        "database_baseline": baseline.get("database"),
        "database_current": current.get("database"),
        "version_change": version_change,
        "data_failures": data_failures,
        "expected_changes": expected_changes,
        "data_ok": not data_failures,
        "structural_changes": structural,
        "gap_rounding_before": {
            k: before_gap.get(k) for k in ("rows_changed", "old_sum", "new_sum")
        },
        "gap_rounding_after": {
            k: after_gap.get(k) for k in ("rows_changed", "old_sum", "new_sum")
        },
    }


def load_snapshot(url: str) -> dict[str, Any]:
    engine = create_engine(url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY"))
            return snapshot(conn)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读迁移审计快照/比对")
    parser.add_argument("--database-url", default=None, help="覆盖 DATABASE_URL")
    parser.add_argument("--out", default=None, help="把快照写入该 JSON 文件")
    parser.add_argument("--compare", default=None, help="与该 JSON 基线比对")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    parser.add_argument("--quiet", action="store_true", help="只输出结论（供脚本使用）")
    parser.add_argument(
        "--allow-missing-samples-change",
        action="store_true",
        help="允许 data_gaps.missing_samples 合计发生变化（half-up 修订后使用）",
    )
    args = parser.parse_args(argv)

    url = args.database_url or get_settings().database_url
    current = load_snapshot(url)

    if args.compare:
        baseline = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        result = compare(baseline, current, args.allow_missing_samples_change)
        result["url"] = mask_password(url)
        if args.out:
            Path(args.out).write_text(
                json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif not args.quiet:
            print(f"目标库：{current['database']}  ({mask_password(url)})")
            print(f"Alembic：{baseline.get('alembic_version')} -> {current.get('alembic_version')}")
            print(f"指纹：{current['fingerprint']['value']}")
            print("行数：" + "  ".join(f"{k}={v}" for k, v in current["counts"].items()))
            print(f"数据一致性：{'PASS' if result['data_ok'] else 'FAIL'}")
            for item in result["data_failures"]:
                print(f"  [数据差异] {item}")
            for item in result["expected_changes"]:
                print(f"  [预期变更] {item['field']}: {item['before']} -> {item['after']}")
            if result["structural_changes"]:
                print(f"结构变更：{len(result['structural_changes'])} 类（需人工确认为预期）")
                for key, diff in result["structural_changes"].items():
                    print(f"  [{key}] 新增={diff['added']}")
                    print(f"  [{key}] 移除={diff['removed']}")
            else:
                print("结构变更：无")
            print(f"缺口修订前需改行数：{result['gap_rounding_before']}")
            print(f"缺口修订后需改行数：{result['gap_rounding_after']}")
        if args.quiet:
            print("PASS" if result["data_ok"] else "FAIL")
        return 0 if result["data_ok"] else 1

    if args.out:
        Path(args.out).write_text(
            json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if args.json:
        print(json.dumps(current, ensure_ascii=False, indent=2))
    elif not args.quiet:
        print(f"数据库：{current['database']}  用户：{current['user']}"
              f"  ({mask_password(url)})")
        print(f"服务端：{current['server_version']}   Alembic：{current['alembic_version']}")
        print("行数：" + "  ".join(f"{k}={v}" for k, v in current["counts"].items()))
        print(f"指纹（{current['fingerprint']['field_count']} 字段）：{current['fingerprint']['value']}")
        print(
            "时间范围："
            f"{current['sensor_readings']['min_observed_at']} ~ "
            f"{current['sensor_readings']['max_observed_at']}"
        )
        print(
            "冻结候选："
            f"{current['sensor_readings']['frozen_candidate']}   缺口："
            f"{current['data_gaps']['missing_samples_sum']} 个样本 / "
            f"{current['counts']['data_gaps']} 条"
        )
        gap = current["gap_rounding_diff"]
        print(
            f"缺口取整差异：{gap['rows_changed']} 行  "
            f"{gap['old_sum']} -> {gap['new_sum']}（half-up 口径）"
        )
        print(f"重复 SHA-256 来源：{len(current['data_source_sha256_duplicates'])} 组")
        print(f"约束 {len(current['constraints'])} 条，索引 {len(current['indexes'])} 条")
        if args.out:
            print(f"快照已写入：{args.out}")
    else:
        print(current["fingerprint"]["value"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
