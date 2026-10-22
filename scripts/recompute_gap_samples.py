#!/usr/bin/env python
"""安全修订 data_gaps.missing_samples（P2-1 取整口径统一后的可选数据修订）。

背景：
    旧实现用 Python 内置 `round()`（银行家舍入，x.5 时取偶）计算
    `missing_samples = round(Δ/10) - 1`；文档公式在 PostgreSQL 中按 half-up
    计算。332 个缺口中有 23 个因 Δ 恰为 x.5 个步长而相差 1 个样本。

    新口径统一为 `round_half_up(Δ/步长) - 1`（见 app/models/quality.py）。
    新导入的数据自动使用新口径；**历史数据需要显式修订**。

安全设计：
    - 默认 `--dry-run`：只统计差异，不写库；
    - `--apply` 才执行 UPDATE，且默认只允许 *_test 库；
    - 对生产库必须同时给出 `--allow-production --apply`，并建议先备份/在副本验证；
    - 只更新 `missing_samples` 这一派生字段，不触碰原始观测、不删除任何行；
    - 输出中的连接串一律脱敏；设置 `--no-password` 时完全省略。

用法：
    python scripts/recompute_gap_samples.py                # 只读，统计差异
    python scripts/recompute_gap_samples.py --apply         # 仅 *_test 库
    python scripts/recompute_gap_samples.py --apply --allow-production
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine, text  # noqa: E402

from app.core.config import get_settings, mask_password  # noqa: E402
from app.models.quality import NOMINAL_STEP_SECONDS  # noqa: E402

# 与 estimate_missing_samples 完全一致的 SQL 表达：
# PostgreSQL 的 `round(numeric)` 是 half-up（半值远离零），gap_seconds 恒为正。
# 注意：必须全程停留在 numeric 上——`round(double precision)` 使用
# round-half-to-even（银行家舍入），在 Δ 恰为 x.5 个步长时会少算 1，
# 与 half-up 口径不一致（曾导致 --apply 更新 0 行）。
CALC_SQL = f"""
SELECT id,
       missing_samples AS old_samples,
       GREATEST(round(gap_seconds / {NOMINAL_STEP_SECONDS:g})::int - 1, 1) AS new_samples
FROM data_gaps
"""

VERIFY_SQL = f"""
SELECT count(*) FROM data_gaps
WHERE missing_samples <> GREATEST(round(gap_seconds / {NOMINAL_STEP_SECONDS:g})::int - 1, 1)
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按统一口径修订 data_gaps.missing_samples")
    parser.add_argument("--database-url", default=None, help="覆盖 DATABASE_URL")
    parser.add_argument("--apply", action="store_true", help="实际执行 UPDATE（默认只读）")
    parser.add_argument(
        "--allow-production",
        action="store_true",
        help="允许对非 *_test 库执行 UPDATE（默认拒绝）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    url = args.database_url or get_settings().database_url
    engine = create_engine(url, pool_pre_ping=True, future=True)

    with engine.connect() as conn:
        database = conn.execute(text("SELECT current_database()")).scalar()
        rows = conn.execute(text(CALC_SQL)).fetchall()

    changed = [r for r in rows if r.old_samples != r.new_samples]
    old_sum = sum(r.old_samples for r in rows)
    new_sum = sum(r.new_samples for r in rows)

    summary = {
        "database": database,
        "url": mask_password(url),
        "gaps_total": len(rows),
        "gaps_changed": len(changed),
        "missing_samples_old_sum": old_sum,
        "missing_samples_new_sum": new_sum,
        "applied": False,
        "mode": "apply" if args.apply else "dry-run",
    }

    if args.apply:
        if not str(database).endswith("_test") and not args.allow_production:
            print(
                f"[拒绝] 目标库 {database!r} 不是 *_test；如确需在其它库执行，"
                "请显式加 --allow-production（建议先在副本上验证）。",
                file=sys.stderr,
            )
            return 5
        with engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE data_gaps AS g
                    SET missing_samples = calc.new_samples
                    FROM (
                        SELECT id,
                               GREATEST(round(gap_seconds / CAST(:step AS numeric))::int - 1, 1)
                                   AS new_samples
                        FROM data_gaps
                    ) AS calc
                    WHERE g.id = calc.id
                      AND g.missing_samples <> calc.new_samples
                    """
                ),
                {"step": NOMINAL_STEP_SECONDS},
            )
            summary["updated_rows"] = int(result.rowcount or 0)
            summary["applied"] = True
            # 自校验：更新后必须不再有差异行，且更新行数等于预检差异行数。
            summary["remaining_diff_rows"] = int(conn.execute(text(VERIFY_SQL)).scalar() or 0)
            summary["missing_samples_new_sum_after"] = int(
                conn.execute(text("SELECT coalesce(sum(missing_samples), 0) FROM data_gaps")).scalar()
                or 0
            )
            if summary["updated_rows"] != len(changed):
                raise RuntimeError(
                    "修订行数与预检差异不一致："
                    f"预检 {len(changed)} 行，实际更新 {summary['updated_rows']} 行（已回滚）"
                )
            if summary["remaining_diff_rows"] != 0:
                raise RuntimeError(
                    f"修订后仍存在 {summary['remaining_diff_rows']} 行差异（已回滚）"
                )
            if summary["missing_samples_new_sum_after"] != new_sum:
                raise RuntimeError(
                    "修订后合计与预检不一致："
                    f"预检 {new_sum}，实际 {summary['missing_samples_new_sum_after']}（已回滚）"
                )

    if args.json:
        import json

        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"目标数据库：{database}  ({mask_password(url)})")
        print(f"模式：{summary['mode']}")
        print(f"缺口总数：{summary['gaps_total']}")
        print(f"需要修订：{summary['gaps_changed']}")
        print(f"missing_samples 合计：{summary['missing_samples_old_sum']} -> {summary['missing_samples_new_sum']}")
        if args.apply:
            print(f"已更新行数：{summary.get('updated_rows', 0)}")
            print(f"修订后剩余差异行：{summary.get('remaining_diff_rows')}")
            print(f"修订后合计：{summary.get('missing_samples_new_sum_after')}")
        else:
            print("（只读模式，未修改任何数据；如需写入请加 --apply）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
