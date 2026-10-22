#!/usr/bin/env python
"""迁移 0005/0006 与缺口修订的核验脚本（Phase 1B 收口）。

默认**只读**：只查询系统目录与数据聚合，不写任何业务表。
写入探针（重复 SHA-256 必须被拒绝、status=partial 必须被接受）需要显式
`--probe-writes`；为避免误伤生产库，还要求目标库名以 `_test` 结尾，
或显式追加 `--allow-production-probes`。所有探针都在事务内执行并**一律 ROLLBACK**，
但生产环境仍建议只在副本上开启。

检查项：
  1. Alembic 版本 = 期望值
  2. 行数/内容指纹与基线快照一致（--baseline 传入 migration_audit.py 的快照）
  3. `uq_data_sources_content_sha256` 存在，且物理索引存在
  4. `ingest_runs` 状态 CHECK 定义包含 partial、且名称规范
  5. 9 条 CHECK 约束名称与 ORM 元数据一致；不存在任何双前缀 `ck_表名_ck_` 名称
  6. 所有 CHECK 约束的**定义**与基线语义一致（只允许改名，不允许改判定）
  7. 外键数量与基线一致；覆盖度函数与冻结段视图可执行
  8. `data_gaps.missing_samples` 与 half-up 独立复算一致（修订后应为 0 行差异）
  9. （可选）重复 SHA-256 / 非法 status 被数据库拒绝，partial 被接受

用法：
    python scripts/verify_migration_0005_0006.py \
        --database-url postgresql+psycopg://... --baseline /tmp/prod_0004_baseline.json
    python scripts/verify_migration_0005_0006.py --database-url ... --probe-writes
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import get_settings, mask_password  # noqa: E402

EXPECTED_VERSION = "0006_check_constraint_names"
UNIQUE_CONSTRAINT = "uq_data_sources_content_sha256"
STATUS_CONSTRAINT = "ck_ingest_runs_status_valid"
EXPECTED_CHECK_NAMES = {
    "data_gaps": {
        "ck_data_gaps_gap_end_after_start",
        "ck_data_gaps_missing_samples_positive",
    },
    "data_sources": {
        "ck_data_sources_file_sha256_hex",
        "ck_data_sources_timezone_status_valid",
    },
    "fault_events": {
        "ck_fault_events_end_after_start",
        "ck_fault_events_verification_status_valid",
    },
    "ingest_runs": {STATUS_CONSTRAINT},
    "sensor_readings": {
        "ck_sensor_readings_digital_signals_binary",
        "ck_sensor_readings_observed_at_matches_source_text",
        "ck_sensor_readings_quality_flags_non_negative",
    },
}

checks: list[dict[str, Any]] = []

_CAST_NOISE = re.compile(r"::(?:text\[\]|text|character varying)")


def check_signature(definition: str) -> tuple:
    """把 CHECK 定义归一化为“允许值集合”或“去噪表达式”，以便只比语义。

    原因：pg_dump/pg_restore 会把 `IN (...)` 反解为 `ANY (ARRAY[...])` 的另一种写法，
    且 0005 重建的 status 约束与基线形式不同。名称与反解形式都不是本检查的对象，
    本检查只确认允许值集合与判定表达式未被修改。
    """
    literals = tuple(sorted(re.findall(r"'([^']*)'", definition)))
    if literals:
        return ("values", literals)
    return ("expr", re.sub(r"[\s()\[\]]", "", _CAST_NOISE.sub("", definition)))


def record(name: str, ok: bool, detail: str = "") -> None:
    checks.append({"check": name, "ok": bool(ok), "detail": detail})
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="核验迁移 0005/0006 与缺口修订结果")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--baseline", default=None, help="迁移前 migration_audit.py 快照")
    parser.add_argument("--expected-version", default=EXPECTED_VERSION)
    parser.add_argument("--probe-writes", action="store_true", help="运行写探针（一律 ROLLBACK）")
    parser.add_argument("--allow-production-probes", action="store_true")
    parser.add_argument(
        "--expect-gap-diff",
        type=int,
        default=0,
        help="期望的 missing_samples 差异行数（迁移前为 23，修订后为 0）",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    url = args.database_url or get_settings().database_url
    engine = create_engine(url, pool_pre_ping=True, future=True)
    baseline = (
        json.loads(Path(args.baseline).read_text(encoding="utf-8")) if args.baseline else None
    )

    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))

        def scalar(sql: str) -> Any:
            return conn.execute(text(sql)).scalar()

        def rows(sql: str) -> list[Any]:
            return conn.execute(text(sql)).fetchall()

        database = scalar("SELECT current_database()")
        print(f"目标库：{database}  ({mask_password(url)})")

        # 1. 版本
        version = scalar("SELECT version_num FROM alembic_version")
        record("Alembic 版本", version == args.expected_version,
               f"{version}（期望 {args.expected_version}）")

        # 2. 行数与指纹
        counts = {
            t: scalar(f"SELECT count(*) FROM {t}")
            for t in ("equipment", "data_sources", "sensor_readings",
                      "fault_events", "data_gaps", "ingest_runs")
        }
        record("业务表行数", True, "  ".join(f"{k}={v}" for k, v in counts.items()))
        if baseline:
            same_counts = counts == baseline["counts"]
            record("行数与基线一致", same_counts,
                   "" if same_counts else f"base={baseline['counts']}")
            fp = scalar(
                "SELECT md5(string_agg(concat_ws('|', r.observed_at, r.tp2, r.tp3, r.h1,"
                " r.dv_pressure, r.reservoirs, r.oil_temperature, r.motor_current, r.comp,"
                " r.dv_eletric, r.towers, r.mpg, r.lps, r.pressure_switch, r.oil_level,"
                " r.caudal_impulses, r.quality_flags), '||' ORDER BY r.id))"
                " FROM sensor_readings r"
            )
            record("内容指纹与基线一致", fp == baseline["fingerprint"]["value"], f"{fp}")
            record("数据源与基线一致",
                   [dict(zip(("id", "source_file", "file_sha256", "timezone_status"), r))
                    for r in rows("SELECT id::text, source_file, file_sha256, timezone_status"
                                  " FROM data_sources ORDER BY source_file")]
                   == baseline["data_sources"], f"{counts['data_sources']} 个来源")
            record("故障事件与基线一致",
                   sorted(r[0] for r in rows("SELECT event_code FROM fault_events"))
                   == sorted(baseline["fault_events"]["codes"]),
                   f"{counts['fault_events']} 条")
            base_fk = sum(1 for c in baseline["constraints"] if c["contype"] == "f")
            now_fk = scalar(
                "SELECT count(*) FROM pg_constraint WHERE contype='f'"
                " AND connamespace='public'::regnamespace"
            )
            record("外键数量与基线一致", base_fk == now_fk, f"{now_fk} 个（基线 {base_fk}）")

        # 3. 唯一约束
        uq = scalar(
            "SELECT count(*) FROM pg_constraint"
            f" WHERE conname = '{UNIQUE_CONSTRAINT}' AND contype = 'u'"
        )
        record("UNIQUE(file_sha256) 约束存在", uq == 1, UNIQUE_CONSTRAINT)
        uq_idx = scalar(
            f"SELECT count(*) FROM pg_indexes WHERE indexname = '{UNIQUE_CONSTRAINT}'"
        )
        record("唯一约束物理索引存在", uq_idx == 1, UNIQUE_CONSTRAINT)
        record("历史重复 SHA-256 数量为 0",
               scalar("SELECT count(*) FROM (SELECT file_sha256 FROM data_sources"
                      " GROUP BY 1 HAVING count(*) > 1) d") == 0)

        # 4. 状态约束
        status_def = scalar(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            f" WHERE conname = '{STATUS_CONSTRAINT}' AND contype = 'c'"
        )
        record("status 约束存在且包含 partial",
               bool(status_def) and "partial" in (status_def or ""),
               (status_def or "缺失")[:120])
        record("status 约束名称规范", bool(status_def))

        # 5. CHECK 命名规范
        db_checks: dict[str, set[str]] = {}
        for table, name in rows(
            "SELECT conrelid::regclass::text, conname FROM pg_constraint"
            " WHERE contype='c' AND connamespace='public'::regnamespace"
        ):
            db_checks.setdefault(table, set()).add(name)
        record("CHECK 约束集合与 ORM 元数据一致",
               db_checks == EXPECTED_CHECK_NAMES,
               f"数据库 {sum(len(v) for v in db_checks.values())} 条 / 期望 "
               f"{sum(len(v) for v in EXPECTED_CHECK_NAMES.values())} 条")
        double_prefix = [
            f"{t}.{n}" for t, names in db_checks.items() for n in names
            if n.startswith(f"ck_{t}_ck_{t}_")
        ]
        record("无双重前缀 CHECK 名称", not double_prefix, ", ".join(double_prefix) or "无")

        # 6. CHECK 定义语义未变（只允许改名；0005 额外允许 status 增加 partial）
        if baseline:
            base_sigs: dict[str, list[tuple]] = {}
            for c in baseline["constraints"]:
                if c["contype"] != "c":
                    continue
                base_sigs.setdefault(c["table_name"], []).append(
                    check_signature(c["definition"])
                )
            now_sigs: dict[str, list[tuple]] = {}
            for table, definition in rows(
                "SELECT conrelid::regclass::text, pg_get_constraintdef(oid)"
                " FROM pg_constraint WHERE contype='c'"
                " AND connamespace='public'::regnamespace"
            ):
                sig = check_signature(definition)
                if table == "ingest_runs" and sig[0] == "values" and "partial" in sig[1]:
                    sig = ("values", tuple(v for v in sig[1] if v != "partial"))
                now_sigs.setdefault(table, []).append(sig)
            same = {t: sorted(v) for t, v in base_sigs.items()} == {
                t: sorted(v) for t, v in now_sigs.items()
            }
            record("CHECK 定义语义未变（仅 0005 的 partial 放宽）", same,
                   "逐条归一化比对允许值集合/判定表达式")

        # 7. 派生对象可用
        record("覆盖度函数可调用",
               scalar("SELECT count(*) FROM sensor_coverage("
                      "(SELECT id FROM equipment LIMIT 1),"
                      " '2020-02-01'::timestamp, '2020-02-02'::timestamp, 10)") == 1)
        record("冻结段视图可查询",
               (scalar("SELECT count(*) FROM sensor_frozen_segments") or 0) > 0,
               f"{scalar('SELECT count(*) FROM sensor_frozen_segments')} 段")

        # 8. 缺口 half-up 一致性
        diff = scalar(
            """
            SELECT count(*) FROM (
                SELECT missing_samples,
                       GREATEST(round(extract(epoch FROM (gap_end - gap_start)) / 10)::int - 1, 1)
                           AS expected
                FROM data_gaps
            ) d WHERE missing_samples <> expected
            """
        )
        total = scalar("SELECT count(*) FROM data_gaps")
        gap_sum = scalar("SELECT sum(missing_samples) FROM data_gaps")
        record("missing_samples 与 half-up 复算一致", diff == args.expect_gap_diff,
               f"差异 {diff} 行 / 共 {total} 行，合计 {gap_sum}（期望差异 {args.expect_gap_diff} 行）")

    engine.dispose()

    # 9. 写探针：必须使用**可写**的独立连接（上面的会话是 READ ONLY），
    #    所有探针在同一事务内执行，最后无条件 ROLLBACK。
    if args.probe_writes:
        if not database.endswith("_test") and not args.allow_production_probes:
            record("写探针已跳过", False, "目标库非 *_test，需 --allow-production-probes")
        else:
            probe_engine = create_engine(url, pool_pre_ping=True, future=True)
            try:
                with probe_engine.connect() as pconn:
                    trans = pconn.begin()
                    sha = pconn.execute(
                        text("SELECT file_sha256 FROM data_sources LIMIT 1")
                    ).scalar()

                    def must_fail(sql: str, expect: str, **params: Any) -> tuple[bool, str]:
                        """执行一条应被拒绝的 SQL，并确认是**指定约束**拒绝的。"""
                        try:
                            with pconn.begin_nested():
                                pconn.execute(text(sql), params)
                        except Exception as exc:  # noqa: BLE001
                            first = str(exc).splitlines()[0][:90]
                            return expect in str(exc), f"{type(exc).__name__}: {first}"
                        return False, "未被拒绝"

                    # 复制现有来源（保留必填列），只改文件名，哈希相同 → 必须被唯一约束拒绝
                    rejected, detail = must_fail(
                        "INSERT INTO data_sources (source_name, source_file, file_sha256,"
                        " timezone_status)"
                        " SELECT source_name, 'probe-duplicate.csv', file_sha256, timezone_status"
                        " FROM data_sources LIMIT 1",
                        UNIQUE_CONSTRAINT,
                        sha=sha,
                    )
                    record("重复 SHA-256 被唯一约束拒绝", rejected, detail)

                    rejected, detail = must_fail(
                        "INSERT INTO ingest_runs (source_id, status, started_at)"
                        " SELECT id, 'bogus', now() FROM data_sources LIMIT 1",
                        STATUS_CONSTRAINT,
                    )
                    record("非法 status 被 CHECK 拒绝", rejected, detail)

                    partial_error = None
                    try:
                        with pconn.begin_nested():
                            pconn.execute(
                                text(
                                    "INSERT INTO ingest_runs (source_id, status, started_at)"
                                    " SELECT id, 'partial', now() FROM data_sources LIMIT 1"
                                )
                            )
                    except Exception as exc:  # noqa: BLE001
                        partial_error = str(exc).splitlines()[0][:90]
                    record("status=partial 被接受", partial_error is None, partial_error or "")

                    trans.rollback()
                    record("写探针已回滚", True,
                           f"ROLLBACK 后 data_sources={pconn.execute(text('SELECT count(*) FROM data_sources')).scalar()}"
                           f" ingest_runs={pconn.execute(text('SELECT count(*) FROM ingest_runs')).scalar()}")
            finally:
                probe_engine.dispose()
    failed = [c for c in checks if not c["ok"]]
    if args.json:
        print(json.dumps({"database": database, "checks": checks,
                          "failed": len(failed)}, ensure_ascii=False, indent=2))
    print(f"\n{'全部通过' if not failed else '存在失败'}：{len(checks) - len(failed)}/{len(checks)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
