"""MetroPT-3 时间序列质量与故障区间分析，并生成探索性图表。

全部统计基于完整 CSV（1,516,948 行）；仅在绘图时按小时/按日降采样。
产出：
  data/processed/metropt3/time_quality.json    采样间隔、缺口、月度/逐日覆盖
  data/processed/metropt3/fault_intervals.csv  官方故障报告区间及其覆盖数据量
  data/processed/metropt3/daily_summary.csv    按日聚合摘要（供后续建模参考）
  data/processed/metropt3/gaps.csv             采样中断清单（≥30 s）
  docs/figures/*.png                           探索性图表
"""

from __future__ import annotations

import json
import sys

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from metropt_common import (
    ANALOG_COLUMNS,
    FIGURES_DIR,
    PROCESSED_DIR,
    DataNotReady,
    frozen_mask,
    frozen_segments,
    load_raw,
)

plt.rcParams["font.sans-serif"] = ["PingFang SC", "Arial Unicode MS", "Hiragino Sans GB"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["figure.dpi"] = 130
plt.rcParams["savefig.bbox"] = "tight"

STEP_S = 10.0           # 标称采样步长（由时间戳众数测得，非预设）
GAP_THRESHOLD_S = 30.0  # 超过时钟量化抖动上限（实测 ≤13 s）才计为缺口
NORMAL_MIN_DAYS = 7
NORMAL_MIN_DAILY_COVERAGE = 0.90

# ---- 官方故障报告（来源：Data Description_Metro.pdf → Failure Information 表） ----
# 只转录官方内容；maintenance 保留 PDF 原文日期，不做静默纠正。
FAULTS = [
    {
        "id": "E1",
        "pdf_label": "#1",
        "start": "2020-04-18 00:00:00",
        "end": "2020-04-18 23:59:00",
        "failure": "Air leak",
        "severity": "High stress",
        "maintenance": "",
        "note": "PDF 未给出维护时间。",
    },
    {
        "id": "E2",
        "pdf_label": "#1（PDF 中编号重复）",
        "start": "2020-05-29 23:30:00",
        "end": "2020-05-30 06:00:00",
        "failure": "Air leak",
        "severity": "High stress",
        "maintenance": "Maintenance on 30 Apr at 12:00",
        "note": "PDF 原文写 30 Apr，与故障日期 5 月不符，疑为 30 May 笔误；待核验。",
    },
    {
        "id": "E3",
        "pdf_label": "#3",
        "start": "2020-06-05 10:00:00",
        "end": "2020-06-07 14:30:00",
        "failure": "Air leak",
        "severity": "High stress",
        "maintenance": "Maintenance on 8 Jun at 16:00",
        "note": "维护晚于故障结束约 25.5 h。",
    },
    {
        "id": "E4",
        "pdf_label": "#4",
        "start": "2020-07-15 14:30:00",
        "end": "2020-07-15 19:00:00",
        "failure": "Air leak",
        "severity": "High stress",
        "maintenance": "Maintenance on 16 Jul at 00:00",
        "note": "维护晚于故障结束约 5 h。",
    },
]


def fault_frame() -> pd.DataFrame:
    df = pd.DataFrame(FAULTS)
    df["start"] = pd.to_datetime(df["start"])
    df["end"] = pd.to_datetime(df["end"])
    df["duration_h"] = (df["end"] - df["start"]).dt.total_seconds() / 3600
    return df


def time_quality(df: pd.DataFrame) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    ts = df["timestamp"]
    dt = ts.diff().dt.total_seconds()
    valid = dt.dropna()

    counts = valid.value_counts().sort_index()
    exact = {f"{k:g}": int(v) for k, v in counts.head(15).items()}
    jitter = float(((valid - STEP_S).abs() <= 1).mean())

    gap_mask = valid >= GAP_THRESHOLD_S
    gaps = pd.DataFrame(
        {
            "gap_start": ts.shift(1)[dt >= GAP_THRESHOLD_S],
            "gap_end": ts[dt >= GAP_THRESHOLD_S],
            "gap_seconds": valid[gap_mask],
        }
    ).reset_index(drop=True)
    gaps["gap_hours"] = (gaps["gap_seconds"] / 3600).round(3)
    gaps["missing_samples"] = (gaps["gap_seconds"] / STEP_S).round().astype("int64") - 1
    gaps = gaps.sort_values("gap_seconds", ascending=False).reset_index(drop=True)

    missing_seconds = float((valid[gap_mask] - STEP_S).sum())
    span_seconds = (ts.iloc[-1] - ts.iloc[0]).total_seconds()

    # 月度覆盖：首尾月份按实际数据边界裁剪期望行数，避免把半月/半日算成缺失
    month = ts.dt.to_period("M")
    monthly = []
    for period, grp in ts.groupby(month):
        lo = max(period.start_time, ts.min())
        hi = min(period.end_time, ts.max())
        expected = (hi - lo).total_seconds() / STEP_S + 1
        monthly.append(
            {
                "month": str(period),
                "rows": int(len(grp)),
                "expected_rows_in_range": int(round(expected)),
                "coverage": round(len(grp) / expected, 4),
                "partial_month": bool(period == month.iloc[0] or period == month.iloc[-1]),
            }
        )

    daily = pd.DataFrame(
        {"date": ts.dt.normalize(), "rows": 1}
    ).groupby("date", as_index=False)["rows"].sum()
    daily["expected_rows"] = int(round(86400 / STEP_S))
    daily["coverage"] = (daily["rows"] / daily["expected_rows"]).clip(upper=1).round(4)

    report = {
        "step_seconds_mode": STEP_S,
        "effective_mean_rate_hz": round(1 / STEP_S, 4),
        "declared_rate_hz_in_pdf": 1.0,
        "median_interval_seconds": float(valid.median()),
        "exact_interval_counts": exact,
        "jitter_share_within_1s": round(jitter, 5),
        "interval_below_step": int((valid < STEP_S).sum()),
        "interval_1_to_threshold": int(((valid > STEP_S) & (valid < GAP_THRESHOLD_S)).sum()),
        "gap_threshold_seconds": GAP_THRESHOLD_S,
        "gap_count": int(len(gaps)),
        "gap_count_over_1h": int((gaps["gap_seconds"] >= 3600).sum()),
        "missing_seconds_in_gaps": missing_seconds,
        "missing_days_in_gaps": round(missing_seconds / 86400, 3),
        "missing_share_of_span": round(missing_seconds / span_seconds, 4),
        "max_interval_seconds": float(valid.max()),
        "max_interval_hours": round(float(valid.max()) / 3600, 2),
        "span_days": round(span_seconds / 86400, 3),
        "expected_rows_at_step": int(round(span_seconds / STEP_S)) + 1,
        "actual_rows": int(len(ts)),
        "row_coverage": round(len(ts) / (span_seconds / STEP_S + 1), 4),
        "daily_coverage": {
            "days": int(len(daily)),
            "min": float(daily["coverage"].min()),
            "median": float(daily["coverage"].median()),
            "mean": round(float(daily["coverage"].mean()), 4),
            "days_below_0_90": int((daily["coverage"] < 0.90).sum()),
            "days_below_0_50": int((daily["coverage"] < 0.50).sum()),
        },
        "monthly": monthly,
        "longest_gaps": [
            {
                "gap_start": str(r.gap_start),
                "gap_end": str(r.gap_end),
                "gap_hours": float(r.gap_hours),
                "missing_samples": int(r.missing_samples),
            }
            for r in gaps.head(10).itertuples()
        ],
    }
    return report, gaps, daily


def daily_summary(df: pd.DataFrame) -> pd.DataFrame:
    g = df.set_index("timestamp").resample("D")
    out = pd.DataFrame(
        {
            "rows": g.size(),
            "TP2_mean": g["TP2"].mean(),
            "TP3_mean": g["TP3"].mean(),
            "H1_mean": g["H1"].mean(),
            "Reservoirs_mean": g["Reservoirs"].mean(),
            "DV_pressure_mean": g["DV_pressure"].mean(),
            "Oil_temperature_mean": g["Oil_temperature"].mean(),
            "Motor_current_mean": g["Motor_current"].mean(),
            "Motor_current_max": g["Motor_current"].max(),
            "COMP_active_share": g["COMP"].mean(),
            "LPS_active_share": g["LPS"].mean(),
        }
    ).reset_index()
    out["expected_rows"] = int(round(86400 / STEP_S))
    out["coverage"] = (out["rows"] / out["expected_rows"]).clip(upper=1).round(4)
    return out


def window_stats(df: pd.DataFrame, start, end) -> dict:
    sub = df[(df["timestamp"] >= start) & (df["timestamp"] <= end)]
    out = {}
    for c in ANALOG_COLUMNS:
        s = sub[c]
        out[c] = {
            "mean": round(float(s.mean()), 4),
            "std": round(float(s.std()), 4),
            "min": round(float(s.min()), 4),
            "max": round(float(s.max()), 4),
        }
    return {"rows": int(len(sub)), "analog": out}


def fault_coverage(df: pd.DataFrame, faults: pd.DataFrame) -> pd.DataFrame:
    """统计每个官方故障区间内部的数据量与采样中断（不使用可能 >1 的比值）。"""
    recs = []
    for row in faults.itertuples():
        sub = df[(df["timestamp"] >= row.start) & (df["timestamp"] <= row.end)]
        dt = sub["timestamp"].diff().dt.total_seconds()
        lost = float((dt[dt >= GAP_THRESHOLD_S] - STEP_S).sum())
        fz = int(frozen_mask(sub).sum())
        duration = (row.end - row.start).total_seconds()
        st = window_stats(df, row.start, row.end)
        recs.append(
            {
                "id": row.id,
                "pdf_label": row.pdf_label,
                "start": str(row.start),
                "end": str(row.end),
                "duration_h": round(row.duration_h, 2),
                "failure": row.failure,
                "severity": row.severity,
                "maintenance": row.maintenance,
                "rows_in_interval": int(len(sub)),
                "expected_rows": int(round(duration / STEP_S)) + 1,
                "missing_samples": int(round(lost / STEP_S)),
                "coverage": round(1 - lost / duration, 4),
                "mean_interval_s": round(float(dt.mean()), 3),
                "frozen_rows": fz,
                "frozen_share": round(fz / max(1, len(sub)), 4),
                "TP2_mean": st["analog"]["TP2"]["mean"],
                "Reservoirs_mean": st["analog"]["Reservoirs"]["mean"],
                "Motor_current_mean": st["analog"]["Motor_current"]["mean"],
                "Oil_temperature_mean": st["analog"]["Oil_temperature"]["mean"],
                "note": row.note,
            }
        )
    return pd.DataFrame(recs)


def choose_normal_window(
    df: pd.DataFrame, daily: pd.DataFrame, faults: pd.DataFrame
) -> dict | None:
    """在官方故障日期之外，选 7 天连续、逐日覆盖率最高的窗口作为"正常候选"。"""
    bad = set()
    for row in faults.itertuples():
        bad |= set(pd.date_range(row.start.normalize(), row.end.normalize(), freq="D").date)

    cov = dict(zip(daily["date"].dt.date, daily["coverage"]))
    dates = list(daily["date"].dt.date)
    best = None
    n = NORMAL_MIN_DAYS
    for i in range(len(dates) - n + 1):
        window = dates[i : i + n]
        if any(d in bad for d in window):
            continue
        worst = min(cov[d] for d in window)
        if worst < NORMAL_MIN_DAILY_COVERAGE:
            continue
        if best is None or worst > best[1]:
            best = (window[0], worst)

    if best is None:
        return None
    start = pd.Timestamp(best[0])
    end = start + pd.Timedelta(days=NORMAL_MIN_DAYS) - pd.Timedelta(seconds=STEP_S)
    return {
        "start": str(start),
        "end": str(end),
        "days": NORMAL_MIN_DAYS,
        "rows": int(((df["timestamp"] >= start) & (df["timestamp"] <= end)).sum()),
        "min_daily_coverage": round(best[1], 4),
        "criteria": (
            f"连续 {NORMAL_MIN_DAYS} 天且不含官方故障日期，"
            f"逐日覆盖率均 ≥ {NORMAL_MIN_DAILY_COVERAGE:.0%}，取其中最低日覆盖率最大者"
        ),
    }


def regime_stats(df: pd.DataFrame, faults: pd.DataFrame, normal: dict | None) -> dict:
    """量化"持续加载"工况与官方故障区间的重合度。

    只用可观察量（TP2 高值占比、电流>3A 占比）做描述统计，
    不下因果结论，也不把工况差异当作故障判别规则。
    冻结段是记录端卡死，会伪造"持续高负荷"，因此单独剔除后再统计。
    """

    def shares(frame: pd.DataFrame) -> dict:
        frozen = frozen_mask(frame)
        sub = frame[~frozen]
        return {
            "rows": int(len(sub)),
            "rows_frozen_excluded": int(frozen.sum()),
            "TP2_gt_6bar": round(float((sub["TP2"] > 6).mean()), 4),
            "Motor_current_gt_3A": round(float((sub["Motor_current"] > 3).mean()), 4),
            "DV_pressure_gt_0_5bar": round(float((sub["DV_pressure"] > 0.5).mean()), 4),
            "Motor_current_mean": round(float(sub["Motor_current"].mean()), 4),
            "TP2_mean": round(float(sub["TP2"].mean()), 4),
        }

    out = {"dataset_reference": shares(df)}
    out["definition"] = (
        "占比在剔除冻结行后计算；冻结段判据见 metropt_common.frozen_mask"
    )
    for row in faults.itertuples():
        sub = df[(df["timestamp"] >= row.start) & (df["timestamp"] <= row.end)]
        out[f"fault_{row.id}"] = shares(sub)
    if normal:
        sub = df[
            (df["timestamp"] >= pd.Timestamp(normal["start"]))
            & (df["timestamp"] <= pd.Timestamp(normal["end"]))
        ]
        out["normal_window"] = shares(sub)

    clean = df[~frozen_mask(df)]
    d = clean.set_index("timestamp").resample("D").agg(
        share_TP2_gt6=("TP2", lambda s: float((s > 6).mean())),
        share_I_gt3=("Motor_current", lambda s: float((s > 3).mean())),
        rows=("TP2", "size"),
    )
    d["coverage"] = (d["rows"] / (86400 / STEP_S)).clip(upper=1)
    high = d[(d["share_TP2_gt6"] > 0.8) & (d["coverage"] > 0.8)]
    fault_days = set()
    for row in faults.itertuples():
        fault_days |= set(
            pd.date_range(row.start.normalize(), row.end.normalize(), freq="D").date
        )
    high_days = [str(x.date()) for x in high.index]
    out["high_load_days"] = {
        "definition": (
            "剔除冻结行后，当日 TP2>6 bar 的时间占比 >80% 且当日有效覆盖率 >80%"
        ),
        "count": len(high_days),
        "days": high_days,
        "inside_reported_fault_days": sum(1 for x in high.index if x.date() in fault_days),
        "outside_reported_fault_days": [
            str(x.date()) for x in high.index if x.date() not in fault_days
        ],
        "reported_fault_days": len(fault_days),
    }
    return out


def apply_fault_shading(ax, faults: pd.DataFrame, label: bool = True) -> None:
    for i, row in enumerate(faults.itertuples()):
        ax.axvspan(row.start, row.end, color="#d62728", alpha=0.25, linewidth=0,
                   label="官方故障区间" if (label and i == 0) else None)


def fig_analog_overview(df: pd.DataFrame, faults: pd.DataFrame) -> None:
    hourly = df.set_index("timestamp").resample("h").mean(numeric_only=True)
    cols = [c for c in ANALOG_COLUMNS if c in hourly.columns]
    fig, axes = plt.subplots(len(cols), 1, figsize=(12, 1.7 * len(cols)), sharex=True)
    for ax, c in zip(axes, cols):
        ax.plot(hourly.index, hourly[c], lw=0.6, color="#1f77b4")
        apply_fault_shading(ax, faults, label=(c == cols[0]))
        ax.set_ylabel(c, fontsize=9)
        ax.grid(alpha=0.25, lw=0.4)
    axes[0].legend(loc="upper right", fontsize=8)
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    fig.suptitle("MetroPT-3 全部模拟量逐小时均值（2020-02-01 至 2020-09-01，红色为官方故障区间）",
                 fontsize=11)
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "fig01_analog_overview.png")
    plt.close(fig)


def fig_pressure(df: pd.DataFrame, faults: pd.DataFrame) -> None:
    hourly = df.set_index("timestamp").resample("h")
    fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    for ax, col, unit in ((axes[0], "TP2", "bar"), (axes[1], "Reservoirs", "bar")):
        ax.fill_between(hourly[col].min().index, hourly[col].min(), hourly[col].max(),
                        color="#9ecae1", alpha=0.55, label="逐小时最小—最大范围")
        ax.plot(hourly[col].mean().index, hourly[col].mean(), lw=0.7,
                color="#08519c", label="逐小时均值")
        apply_fault_shading(ax, faults, label=(col == "TP2"))
        ax.set_ylabel(f"{col} [{unit}]", fontsize=9)
        ax.grid(alpha=0.25, lw=0.4)
    axes[0].set_title("压缩机出口压力 TP2 与储气罐压力 Reservoirs（含官方故障区间）", fontsize=11)
    axes[0].legend(loc="upper right", fontsize=8)
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "fig02_pressure_faults.png")
    plt.close(fig)


def fig_motor_current(df: pd.DataFrame, faults: pd.DataFrame) -> None:
    hourly = df.set_index("timestamp").resample("h")["Motor_current"]
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.2), gridspec_kw={"width_ratios": [2.4, 1]})
    ax = axes[0]
    ax.plot(hourly.mean().index, hourly.mean(), lw=0.6, color="#2ca02c", label="逐小时均值")
    ax.plot(hourly.max().index, hourly.max(), lw=0.5, color="#d62728", alpha=0.6,
            label="逐小时最大值")
    apply_fault_shading(ax, faults)
    ax.set_ylabel("Motor_current [A]")
    ax.set_title("电机电流：逐小时均值与最大值", fontsize=10)
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.25, lw=0.4)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))

    ax = axes[1]
    ax.hist(df["Motor_current"], bins=120, color="#2ca02c", log=True)
    ax.set_xlabel("Motor_current [A]")
    ax.set_ylabel("记录数（对数刻度）")
    ax.set_title("电机电流全量取值分布", fontsize=10)
    ax.grid(alpha=0.25, lw=0.4)
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "fig03_motor_current.png")
    plt.close(fig)


def fig_oil_temperature(df: pd.DataFrame, faults: pd.DataFrame) -> None:
    s = df.set_index("timestamp")["Oil_temperature"]
    hourly, daily = s.resample("h").mean(), s.resample("D").mean()
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(hourly.index, hourly, lw=0.5, color="#ff7f0e", alpha=0.85, label="逐小时均值")
    ax.plot(daily.index, daily, lw=1.4, color="#8c2d04", label="逐日均值")
    apply_fault_shading(ax, faults)
    ax.set_ylabel("Oil_temperature [°C]")
    ax.set_title("润滑油温度（红色为官方故障区间）", fontsize=11)
    ax.legend(loc="lower right", fontsize=8)
    ax.grid(alpha=0.25, lw=0.4)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "fig04_oil_temperature.png")
    plt.close(fig)


def fig_sampling(df: pd.DataFrame, tq: dict, daily: pd.DataFrame, gaps: pd.DataFrame) -> None:
    ts = df["timestamp"]
    dt = ts.diff().dt.total_seconds().dropna()
    fig, axes = plt.subplots(3, 1, figsize=(12, 10))

    ax = axes[0]
    small = dt[dt <= 20].value_counts().sort_index()
    ax.bar(small.index, small.values, width=0.7, color="#6a51a3")
    ax.set_yscale("log")
    ax.set_xlabel("相邻时间戳间隔 [秒]（仅 ≤20 s，用于显示时钟量化抖动）")
    ax.set_ylabel("记录数（对数刻度）")
    ax.set_title(f"间隔取值：{tq['exact_interval_counts']}；"
                 f"|间隔−10 s| ≤ 1 s 占 {tq['jitter_share_within_1s']:.2%}", fontsize=9)
    ax.grid(alpha=0.25, lw=0.4)

    ax = axes[1]
    if len(gaps):
        ax.scatter(gaps["gap_seconds"] / 60, gaps["missing_samples"], s=8,
                   color="#d62728", alpha=0.6)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("缺口时长 [分钟，对数刻度]")
    ax.set_ylabel("缺失样本数（对数刻度）")
    ax.set_title(f"≥{GAP_THRESHOLD_S:g} s 的采样中断共 {tq['gap_count']:,} 处，"
                 f"累计缺失约 {tq['missing_days_in_gaps']:.2f} 天，"
                 f"最长 {tq['max_interval_hours']:.2f} h", fontsize=9)
    ax.grid(alpha=0.25, lw=0.4)

    ax = axes[2]
    ax.bar(daily["date"], daily["coverage"], width=0.9, color="#3182bd")
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("逐日覆盖率\n(实际行/8640)")
    ax.set_xlabel("日期（2020 年）")
    ax.set_title(f"逐日覆盖率：中位数 {tq['daily_coverage']['median']:.3f}，"
                 f"最低 {tq['daily_coverage']['min']:.3f}", fontsize=9)
    ax.grid(alpha=0.25, lw=0.4, axis="y")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "fig05_sampling_intervals.png")
    plt.close(fig)


def fig_fault_windows(df: pd.DataFrame, faults: pd.DataFrame) -> None:
    cols = ["TP2", "Reservoirs", "Motor_current"]
    fig, axes = plt.subplots(len(faults), 1, figsize=(12, 2.9 * len(faults)))
    for ax, row in zip(axes, faults.itertuples()):
        pad = pd.Timedelta(hours=12)
        win = df[(df["timestamp"] >= row.start - pad) & (df["timestamp"] <= row.end + pad)]
        for c, color in zip(cols, ("#1f77b4", "#2ca02c", "#9467bd")):
            series = win[c]
            lo, hi = float(series.min()), float(series.max())
            norm = (series - lo) / (hi - lo) if hi > lo else series * 0
            ax.plot(win["timestamp"], norm, lw=0.45, color=color,
                    label=f"{c}（实测 {lo:.2f}–{hi:.2f}，归一化）")
        ax.axvspan(row.start, row.end, color="#d62728", alpha=0.2, linewidth=0)
        ax.set_title(f"{row.id} 官方区间 {row.start:%m-%d %H:%M} → {row.end:%m-%d %H:%M}"
                     f"（{row.duration_h:.1f} h，{row.failure}）", fontsize=9)
        ax.set_ylabel("归一化幅值", fontsize=8)
        ax.legend(loc="upper right", fontsize=7, ncol=3)
        ax.grid(alpha=0.25, lw=0.4)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
        ax.tick_params(axis="x", labelsize=7, rotation=20)
    fig.suptitle("官方故障区间前后各 12 小时的三条代表性信号（原始 10 s 采样，幅值各自归一化）",
                 fontsize=11)
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "fig06_fault_windows.png")
    plt.close(fig)


def fig_frozen_segment(df: pd.DataFrame) -> None:
    """用最长冻结段证明数据本身存在记录端卡死。"""
    seg = frozen_segments(df, min_samples=1)
    if not len(seg):
        return
    top = seg.iloc[0]
    start = pd.Timestamp(top["start"]) - pd.Timedelta(hours=3)
    end = pd.Timestamp(top["end"]) + pd.Timedelta(hours=3)
    win = df[(df["timestamp"] >= start) & (df["timestamp"] <= end)]
    cols = ["TP2", "Oil_temperature", "Motor_current"]
    fig, axes = plt.subplots(len(cols), 1, figsize=(12, 6.4), sharex=True)
    for ax, c in zip(axes, cols):
        ax.plot(win["timestamp"], win[c], lw=0.5, color="#1f77b4")
        ax.axvspan(top["start"], top["end"], color="#fdae6b", alpha=0.35, linewidth=0)
        ax.set_ylabel(c, fontsize=8)
        ax.grid(alpha=0.25, lw=0.4)
    axes[0].text(
        0.01, 0.92,
        f"橙色区间为冻结段 {top['hours']:.1f} h（{top['samples']:,} 行逐位不变）",
        transform=axes[0].transAxes, fontsize=8, color="#a63603",
    )
    axes[0].set_title("最长冻结段前后各 3 小时的原始采样：数值卡死、时间戳仍在推进", fontsize=10)
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "fig08_frozen_segment.png")
    plt.close(fig)


def fig_regime_daily(df: pd.DataFrame, faults: pd.DataFrame, regime: dict) -> None:
    d = df.set_index("timestamp").resample("D").agg(
        share_TP2_gt6=("TP2", lambda s: float((s > 6).mean())), rows=("TP2", "size")
    )
    d["coverage"] = (d["rows"] / (86400 / STEP_S)).clip(upper=1)
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.bar(d.index, d["share_TP2_gt6"], width=0.9, color="#756bb1", label="当日 TP2>6 bar 占比")
    ax.plot(d.index, d["coverage"], lw=0.7, color="#31a354", alpha=0.8, label="当日覆盖率")
    apply_fault_shading(ax, faults)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("占比")
    ax.set_title(
        "逐日高负荷占比与数据覆盖率的对照：高负荷日共 "
        f"{regime['high_load_days']['count']} 天，其中 "
        f"{regime['high_load_days']['inside_reported_fault_days']} 天落在官方故障区间内",
        fontsize=10,
    )
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.25, lw=0.4, axis="y")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    fig.tight_layout()
    fig.savefig(FIGURES_DIR / "fig07_regime_daily.png")
    plt.close(fig)


def main() -> int:
    try:
        df = load_raw()
    except DataNotReady as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)

    faults = fault_frame()
    tq, gaps, daily_cov = time_quality(df)
    daily = daily_summary(df)
    fault_cov = fault_coverage(df, faults)
    normal = choose_normal_window(df, daily_cov, faults)

    seg = frozen_segments(df, min_samples=1)
    mask = frozen_mask(df)
    tq["frozen"] = {
        "criterion": "7 个模拟量同时与前一行完全相同，视为记录端冻结",
        "segment_count": int(len(seg)),
        "segment_count_over_30min": int((seg["hours"] >= 0.5).sum()),
        "frozen_rows": int(mask.sum()),
        "frozen_share": round(float(mask.mean()), 4),
        "frozen_hours_total": round(float(seg["hours"].sum()), 2),
        "segments": [
            {
                "start": str(r.start),
                "end": str(r.end),
                "samples": int(r.samples),
                "hours": round(float(r.hours), 3),
            }
            for r in seg.itertuples()
        ],
    }
    tq["faults"] = {
        "count": int(len(faults)),
        "source": "Data Description_Metro.pdf → Failure Information 表",
        "all_within_csv_range": bool(
            (faults["start"] >= df["timestamp"].min()).all()
            and (faults["end"] <= df["timestamp"].max()).all()
        ),
        "labeling": "区间级报告，不是逐样本标签；区间内每行不得视为独立故障样本",
    }
    regime = regime_stats(df, faults, normal)
    tq["regime"] = regime
    tq["normal_candidate_window"] = normal
    if normal:
        tq["normal_window_stats"] = window_stats(
            df, pd.Timestamp(normal["start"]), pd.Timestamp(normal["end"])
        )
    tq["fault_window_stats"] = {
        row.id: window_stats(df, row.start, row.end) for row in faults.itertuples()
    }

    (PROCESSED_DIR / "time_quality.json").write_text(
        json.dumps(tq, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    fault_cov.to_csv(PROCESSED_DIR / "fault_intervals.csv", index=False)
    daily.to_csv(PROCESSED_DIR / "daily_summary.csv", index=False)
    gaps.head(200).to_csv(PROCESSED_DIR / "gaps.csv", index=False)

    fig_analog_overview(df, faults)
    fig_pressure(df, faults)
    fig_motor_current(df, faults)
    fig_oil_temperature(df, faults)
    fig_sampling(df, tq, daily_cov, gaps)
    fig_fault_windows(df, faults)
    fig_regime_daily(df, faults, regime)
    fig_frozen_segment(df)

    print("=" * 78)
    print(f"采样间隔众数 {STEP_S:g} s（{tq['effective_mean_rate_hz']:.2f} Hz），PDF 声明 1 Hz")
    print(f"间隔取值分布：{tq['exact_interval_counts']}")
    print(f"|间隔−10s|≤1s 占比 {tq['jitter_share_within_1s']:.4f}；"
          f"小于 10 s：{tq['interval_below_step']:,}；10~30 s：{tq['interval_1_to_threshold']:,}")
    print(f"时间跨度 {tq['span_days']} 天；按 10 s 应有 {tq['expected_rows_at_step']:,} 行，"
          f"实际 {tq['actual_rows']:,}，行覆盖率 {tq['row_coverage']:.4f}")
    print(f"≥{GAP_THRESHOLD_S:g}s 缺口 {tq['gap_count']:,} 处（≥1 h 的 {tq['gap_count_over_1h']} 处），"
          f"累计缺失 {tq['missing_days_in_gaps']:.2f} 天，占时间跨度 {tq['missing_share_of_span']:.2%}")
    print(f"逐日覆盖率：中位 {tq['daily_coverage']['median']:.3f}，最低 {tq['daily_coverage']['min']:.3f}，"
          f"<0.90 的天数 {tq['daily_coverage']['days_below_0_90']}")
    print("月度覆盖：")
    for m in tq["monthly"]:
        tag = "（首/末月，按实际范围裁剪）" if m["partial_month"] else ""
        print(f"  {m['month']}  行数 {m['rows']:>8,}  覆盖 {m['coverage']:.4f}{tag}")
    print("最长 5 处中断：")
    for g in tq["longest_gaps"][:5]:
        print(f"  {g['gap_start']} → {g['gap_end']}  {g['gap_hours']:8.2f} h")
    print("-" * 78)
    print("官方故障区间（PDF）与库内覆盖：")
    print(fault_cov[["id", "start", "end", "duration_h", "rows_in_interval",
                     "missing_samples", "coverage", "TP2_mean",
                     "Motor_current_mean"]].to_string(index=False))
    print(f"全部落在 CSV 时间范围内：{tq['faults']['all_within_csv_range']}")
    print("-" * 78)
    print(f"正常候选区间：{normal}")
    if normal:
        print("该区间模拟量均值：",
              {k: v["mean"] for k, v in tq["normal_window_stats"]["analog"].items()})
        print("各故障区间模拟量均值（对比用）：")
        for fid, st in tq["fault_window_stats"].items():
            print(f"  {fid}:", {k: v["mean"] for k, v in st["analog"].items()})
    print("-" * 78)
    fz = tq["frozen"]
    print(f"冻结段（记录端卡死）：{fz['segment_count']} 段，{fz['frozen_rows']:,} 行"
          f"（{fz['frozen_share']:.2%}），累计 {fz['frozen_hours_total']:.1f} h；"
          f"其中 ≥30 min 的 {fz['segment_count_over_30min']} 段")
    for g in fz["segments"][:3]:
        print(f"  {g['start']} → {g['end']}  {g['hours']:7.2f} h")
    print("-" * 78)
    print("工况关联观察（描述性，不作因果结论；占比已剔除冻结行）：")
    for key in ("dataset_reference", "normal_window", "fault_E1", "fault_E2", "fault_E3", "fault_E4"):
        if key in regime:
            r = regime[key]
            print(f"  {key:<18} TP2>6bar {r['TP2_gt_6bar']:.3f}  "
                  f"I>3A {r['Motor_current_gt_3A']:.3f}  "
                  f"DV>0.5bar {r['DV_pressure_gt_0_5bar']:.3f}  "
                  f"I均值 {r['Motor_current_mean']:.3f} A")
    hd = regime["high_load_days"]
    print(f"  高负荷日 {hd['count']} 天 / 共 {len(daily)} 天；"
          f"其中落在官方故障日的 {hd['inside_reported_fault_days']} 天"
          f"（官方故障共 {hd['reported_fault_days']} 天）")
    print(f"  非故障高负荷日：{hd['outside_reported_fault_days']}")
    print("-" * 78)
    for p in sorted(FIGURES_DIR.glob("fig0*.png")):
        print(f"图 {p.relative_to(p.parents[2])}  {p.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
