"""MetroPT-3 分析脚本的公共常量与加载工具。

只做三件事：定义路径、定义官方字段分组、提供只读加载函数。
不包含任何统计或绘图逻辑，避免与具体分析脚本职责重叠。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

# ---- 路径约定（相对项目根目录） ----
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = PROJECT_ROOT / "data" / "raw" / "metropt3"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed" / "metropt3"
FIGURES_DIR = PROJECT_ROOT / "docs" / "figures"

CSV_NAME = "MetroPT3(AirCompressor).csv"
PDF_NAME = "Data Description_Metro.pdf"
ZIP_NAME = "metropt+3+dataset.zip"

CSV_PATH = RAW_DIR / CSV_NAME
PDF_PATH = RAW_DIR / PDF_NAME

DATASET_PAGE = "https://archive.ics.uci.edu/dataset/791/metropt+3+dataset"
DATASET_URL = "https://archive.ics.uci.edu/static/public/791/metropt+3+dataset.zip"
LICENSE = "CC BY 4.0"
DOI = "10.24432/C5VW3R"

# 官方 PDF 的字段顺序与物理含义（名称以实际 CSV 列名为准，此处用于校验）
ANALOG_COLUMNS = [
    "TP2",
    "TP3",
    "H1",
    "DV_pressure",
    "Reservoirs",
    "Oil_temperature",
    "Motor_current",
]

DIGITAL_COLUMNS = [
    "COMP",
    "DV_eletric",
    "Towers",
    "MPG",
    "LPS",
    "Pressure_switch",
    "Oil_level",
    "Caudal_impulses",
]

UNITS = {
    "TP2": "bar",
    "TP3": "bar",
    "H1": "bar",
    "DV_pressure": "bar",
    "Reservoirs": "bar",
    "Oil_temperature": "°C",
    "Motor_current": "A",
    "COMP": "digital",
    "DV_eletric": "digital",
    "Towers": "digital",
    "MPG": "digital",
    "LPS": "digital",
    "Pressure_switch": "digital",
    "Oil_level": "digital",
    "Caudal_impulses": "digital",
}

# 官方 PDF 记录的申明采样频率（1 Hz）与文件实际步长需分别对待：
# 后者由时间戳实测得到，不在此预设。
DECLARED_SAMPLE_RATE_HZ = 1.0


class DataNotReady(RuntimeError):
    """原始文件缺失或不可读时抛出，供脚本给出明确提示。"""


def load_raw(usecols: list[str] | None = None, nrows: int | None = None) -> pd.DataFrame:
    """只读加载原始 CSV，解析 timestamp，并做最小完整性校验。"""
    if not CSV_PATH.exists():
        raise DataNotReady(
            f"未找到原始数据：{CSV_PATH}\n"
            "请先运行：python scripts/fetch_metropt.py"
        )

    df = pd.read_csv(CSV_PATH, usecols=usecols, nrows=nrows)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    return df


def sensor_columns(df: pd.DataFrame) -> tuple[list[str], list[str]]:
    """按官方分组返回数据中实际存在的模拟量/数字量列名。"""
    analog = [c for c in ANALOG_COLUMNS if c in df.columns]
    digital = [c for c in DIGITAL_COLUMNS if c in df.columns]
    return analog, digital


# ---- 冻结段（传感器数值卡死）检测 ----
# 判据：7 个模拟量同时与该行前一采样完全相同。
# 物理上各通道不可能长期保持位级相同，因此视为记录端冻结而非真实工况。
def frozen_mask(df: pd.DataFrame) -> pd.Series:
    analog = [c for c in ANALOG_COLUMNS if c in df.columns]
    return (df[analog].diff() == 0).all(axis=1)


def frozen_segments(df: pd.DataFrame, min_samples: int = 1) -> pd.DataFrame:
    """返回冻结段清单。

    samples 表示与前一采样完全相同的连续行数；因此实际冻结区间为
    [start（上一行时间）, end]，起点已回退一行以避免少算首个冻结时刻。
    """
    same = frozen_mask(df)
    run = same.ne(same.shift()).cumsum()
    frame = pd.DataFrame(
        {
            "run": run.values,
            "same": same.fillna(False).values,
            "prev_ts": df["timestamp"].shift(1).values,
            "end_ts": df["timestamp"].values,
        }
    )
    agg = frame.groupby("run").agg(
        is_frozen=("same", "first"),
        samples=("same", "size"),
        start=("prev_ts", "first"),
        end=("end_ts", "last"),
    )
    seg = agg[agg["is_frozen"]].copy()
    seg = seg[seg["samples"] >= min_samples]
    seg["hours"] = (
        seg["end"] - pd.to_datetime(seg["start"])
    ).dt.total_seconds().div(3600)
    return (
        seg[["start", "end", "samples", "hours"]]
        .sort_values(["hours", "samples"], ascending=False)
        .reset_index(drop=True)
    )
