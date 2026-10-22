#!/usr/bin/env python
"""MetroPT-3 数据导入命令行入口（Phase 1B）。

用法示例：

    # 1) 试运行：只解析与校验，不连数据库
    python scripts/import_metropt.py --max-rows 10000 --dry-run

    # 2) 先导入 1 万条真实记录
    python scripts/import_metropt.py --max-rows 10000

    # 3) 校验通过后导入全量
    python scripts/import_metropt.py

    # 4) 只导入官方故障事件
    python scripts/import_metropt.py --faults-only
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import ConfigError, get_settings  # noqa: E402
from app.services import dataset as ds  # noqa: E402
from app.services.metropt_import import (  # noqa: E402
    ImportConflictError,
    ImportValidationError,
    import_fault_events,
    import_metropt_csv,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导入 MetroPT-3 原始 CSV 与官方故障事件")
    parser.add_argument("--csv", type=Path, default=ds.CSV_PATH, help="原始 CSV 路径")
    parser.add_argument("--chunk-size", type=int, default=100_000, help="分块大小（行）")
    parser.add_argument("--max-rows", type=int, default=None, help="最多导入多少行（试导入用）")
    parser.add_argument("--dry-run", action="store_true", help="只校验不写库")
    parser.add_argument("--faults-only", action="store_true", help="只导入官方故障事件")
    parser.add_argument("--no-faults", action="store_true", help="只导入传感器数据，不导入故障事件")
    parser.add_argument("--no-hash-check", action="store_true", help="跳过与 manifest 的哈希比对")
    parser.add_argument(
        "--overlap-policy",
        choices=("abort", "skip"),
        default="abort",
        help="同一设备同一时间戳已存在且取值不同时的策略：abort（默认，报冲突并中止）/ skip（跳过并标记 partial）",
    )
    parser.add_argument("--database-url", default=None, help="覆盖 DATABASE_URL（测试库用）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    engine = None
    if not args.dry_run:
        try:
            settings = get_settings()
        except ConfigError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            return 2
        url = args.database_url or settings.database_url
        from sqlalchemy import create_engine

        engine = create_engine(url, pool_pre_ping=True, future=True)

    result: dict[str, object] = {}
    exit_code = 0

    if not args.faults_only:
        try:
            stats = import_metropt_csv(
                args.csv,
                engine=engine,
                chunk_size=args.chunk_size,
                max_rows=args.max_rows,
                dry_run=args.dry_run,
                verify_hash=not args.no_hash_check,
                overlap_policy=args.overlap_policy,
            )
        except ImportConflictError as exc:
            print(f"[数据冲突，已中止且未覆盖任何记录] {exc}", file=sys.stderr)
            return 3
        except (ImportValidationError, FileNotFoundError) as exc:
            print(f"[导入失败] {exc}", file=sys.stderr)
            return 1
        result["sensor_import"] = stats.as_dict()
        if not args.json:
            payload = stats.as_dict()
            print("=" * 64)
            print("传感器数据导入结果" + ("（试运行，未写库）" if args.dry_run else ""))
            print("=" * 64)
            for key in (
                "attempted_rows",
                "valid_rows",
                "inserted_rows",
                "conflict_rows",
                "invalid_rows",
                "frozen_rows",
                "gap_rows_written",
                "overlap_skipped_rows",
                "timestamp_conflict_rows",
                "duration_seconds",
                "first_timestamp",
                "last_timestamp",
                "first_row_number",
                "last_row_number",
                "source_reused",
                "source_file_mismatch",
                "db_hash_unique_enforced",
                "partial",
                "run_id",
            ):
                print(f"  {key:<22} {payload[key]}")
            if payload["invalid_reasons"]:
                print(f"  invalid_reasons        {payload['invalid_reasons']}")
            if payload["source_reused"]:
                note = "" if not payload["source_file_mismatch"] else (
                    f"（已登记文件名 {payload['source_registered_file']}，本次 {args.csv.name}）"
                )
                print(f"  [来源复用] SHA-256 相同，复用既有数据源{note}")
            if payload["partial"]:
                print("  [部分成功] 本次运行未完整导入，详见 ingest_runs.notes")

    if not args.dry_run and not args.no_faults:
        try:
            faults = import_fault_events(engine=engine)
        except (ImportValidationError, FileNotFoundError) as exc:
            print(f"[故障事件导入失败] {exc}", file=sys.stderr)
            return 1
        result["fault_import"] = faults
        if not args.json:
            print("-" * 64)
            print(
                "官方故障事件：文件 {events_in_file} 条，新增 {inserted} 条，"
                "已存在跳过 {skipped_existing} 条".format(**faults)
            )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
