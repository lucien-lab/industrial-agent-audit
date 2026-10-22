"""MetroPT-3 字段与质量体检（只读）。

产出：
1. 控制台摘要：规模、类型、时间范围、缺失、重复、数字信号真实取值。
2. data/processed/metropt3/schema_profile.json：机器可读的完整体检结果。

设计原则：
- 统计全部基于完整 1,516,948 行，不抽样。
- 数字信号不预设为 0/1，实际取值分布原样输出。
- 物理越界只做"标记"，不修改、不删行、不插值。
"""

from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

from metropt_common import (
    ANALOG_COLUMNS,
    DIGITAL_COLUMNS,
    PROCESSED_DIR,
    UNITS,
    DataNotReady,
    frozen_mask,
    frozen_segments,
    load_raw,
)

# 物理上的粗略合理区间，仅用于"标记可疑值"，阈值依据官方 PDF 的量纲说明，
# 不代表设备合格的工程判据。
PLAUSIBLE_RANGES = {
    "TP2": (0.0, 12.0),
    "TP3": (0.0, 12.0),
    "H1": (0.0, 12.0),
    "DV_pressure": (-0.5, 12.0),
    "Reservoirs": (0.0, 12.0),
    "Oil_temperature": (-20.0, 120.0),
    "Motor_current": (0.0, 30.0),
}


def profile(df: pd.DataFrame) -> dict:
    result: dict = {}
    result["shape"] = {"rows": int(len(df)), "columns": int(df.shape[1])}
    result["columns"] = list(df.columns)
    result["dtypes"] = {c: str(t) for c, t in df.dtypes.items()}

    ts = df["timestamp"]
    result["timestamp"] = {
        "parse_failures": int(ts.isna().sum()),
        "min": None if ts.isna().all() else str(ts.min()),
        "max": None if ts.isna().all() else str(ts.max()),
        "is_monotonic_increasing": bool(ts.is_monotonic_increasing),
        "duplicate_values": int(ts.duplicated().sum()),
    }

    # 缺失值
    na = df.isna().sum()
    result["missing"] = {c: int(v) for c, v in na.items() if v > 0}
    result["missing_total"] = int(na.sum())

    # 重复行：整行重复（排除源文件索引列，它本身就是序列号）
    data_cols = [c for c in df.columns if c != "Unnamed: 0"]
    result["duplicate_rows_full"] = int(df.duplicated(subset=data_cols).sum())

    # 模拟量统计
    analog, digital = (
        [c for c in ANALOG_COLUMNS if c in df.columns],
        [c for c in DIGITAL_COLUMNS if c in df.columns],
    )
    result["analog_stats"] = {
        c: {
            "unit": UNITS.get(c),
            "min": float(df[c].min()),
            "max": float(df[c].max()),
            "mean": round(float(df[c].mean()), 6),
            "std": round(float(df[c].std()), 6),
            "q01": round(float(df[c].quantile(0.01)), 6),
            "median": round(float(df[c].median()), 6),
            "q99": round(float(df[c].quantile(0.99)), 6),
            "zeros": int((df[c] == 0).sum()),
            "negative": int((df[c] < 0).sum()),
        }
        for c in analog
    }

    # 数字信号：不假设是 0/1，输出真实取值集合
    digital_report = {}
    for c in digital:
        counts = df[c].value_counts(dropna=False).sort_index()
        values = [float(v) for v in counts.index if pd.notna(v)]
        digital_report[c] = {
            "unit": UNITS.get(c),
            "n_unique": int(df[c].nunique(dropna=True)),
            "values": values,
            "counts": {str(k): int(v) for k, v in counts.items()},
            "share_of_max_value": round(float((df[c] == df[c].max()).mean()), 6),
            "is_binary_0_1": bool(set(values) <= {0.0, 1.0}),
            "is_integer_valued": bool(
                df[c].dropna().mod(1).eq(0).all() if df[c].notna().any() else True
            ),
        }
    result["digital_stats"] = digital_report

    # 可疑值标记（只统计，不改数据）
    suspicious = {}
    for c, (lo, hi) in PLAUSIBLE_RANGES.items():
        if c not in df.columns:
            continue
        out = ((df[c] < lo) | (df[c] > hi)).sum()
        if out:
            suspicious[c] = {
                "outside_range": int(out),
                "declared_range": [lo, hi],
                "actual_min": float(df[c].min()),
                "actual_max": float(df[c].max()),
            }
    result["suspicious_analog"] = suspicious

    # 冻结段：全部模拟量同时与前一采样相同（记录端卡死，非真实工况）
    seg = frozen_segments(df, min_samples=1)
    mask = frozen_mask(df)
    result["frozen"] = {
        "criterion": "7 个模拟量同时与前一行完全相同，视为记录冻结",
        "segment_count": int(len(seg)),
        "segment_count_over_30min": int((seg["hours"] >= 0.5).sum()),
        "frozen_rows": int(mask.sum()),
        "frozen_share": round(float(mask.mean()), 4),
        "frozen_hours_total": round(float(seg["hours"].sum()), 2),
        "top_segments": [
            {
                "start": str(r.start),
                "end": str(r.end),
                "samples": int(r.samples),
                "hours": round(float(r.hours), 3),
            }
            for r in seg.head(10).itertuples()
        ],
    }

    # 常量列检查
    result["constant_columns"] = [
        c for c in df.columns if c != "Unnamed: 0" and df[c].nunique(dropna=False) <= 1
    ]

    result["extra_columns_not_in_official_list"] = [
        c
        for c in df.columns
        if c not in ("Unnamed: 0", "timestamp") and c not in ANALOG_COLUMNS + DIGITAL_COLUMNS
    ]
    result["official_columns_missing"] = [
        c for c in ANALOG_COLUMNS + DIGITAL_COLUMNS if c not in df.columns
    ]
    return result


def print_summary(rep: dict) -> None:
    print("=" * 78)
    print(f"规模：{rep['shape']['rows']:,} 行 × {rep['shape']['columns']} 列")
    t = rep["timestamp"]
    print(f"时间范围：{t['min']} → {t['max']}")
    print(f"时间戳解析失败：{t['parse_failures']}；重复时间戳：{t['duplicate_values']}；"
          f"单调递增：{t['is_monotonic_increasing']}")
    print(f"缺失值合计：{rep['missing_total']}；整行重复：{rep['duplicate_rows_full']:,}")
    print(f"非常量列：{len(rep['columns']) - len(rep['constant_columns'])}；"
          f"常量列：{rep['constant_columns'] or '无'}")
    if rep["extra_columns_not_in_official_list"]:
        print(f"官方清单之外的多余列：{rep['extra_columns_not_in_official_list']}")

    print("-" * 78)
    print(f"{'信号':<16}{'单位':<9}{'最小':>10}{'中位':>10}{'最大':>10}{'标准差':>10}")
    for c, s in rep["analog_stats"].items():
        print(f"{c:<16}{s['unit'] or '-':<9}{s['min']:>10.4f}{s['median']:>10.4f}"
              f"{s['max']:>10.4f}{s['std']:>10.4f}")

    print("-" * 78)
    print("数字信号真实取值（不预设 0/1）：")
    for c, s in rep["digital_stats"].items():
        print(f"  {c:<16} 唯一值={s['n_unique']:<2} 取值={s['values']} "
              f"0/1布尔={s['is_binary_0_1']} 取1占比={s['share_of_max_value']:.4f}")

    fz = rep["frozen"]
    print("-" * 78)
    print(f"冻结段：{fz['segment_count']} 段（≥30 min 的 {fz['segment_count_over_30min']} 段），"
          f"冻结行 {fz['frozen_rows']:,}（{fz['frozen_share']:.2%}），"
          f"累计 {fz['frozen_hours_total']:.1f} h")
    for seg in fz["top_segments"][:5]:
        print(f"  {seg['start']} → {seg['end']}  {seg['hours']:7.2f} h  {seg['samples']:,} 行")

    if rep["suspicious_analog"]:
        print("-" * 78)
        print("超出粗设合理区间的记录数（仅标记，未修改数据）：")
        for c, s in rep["suspicious_analog"].items():
            print(f"  {c:<16} {s['outside_range']:>8,} 条  实际[{s['actual_min']:.4f}, "
                  f"{s['actual_max']:.4f}] 粗设区间{s['declared_range']}")


def main() -> int:
    try:
        df = load_raw()
    except DataNotReady as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    rep = profile(df)
    print_summary(rep)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out = PROCESSED_DIR / "schema_profile.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("-" * 78)
    print(f"已写出 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
