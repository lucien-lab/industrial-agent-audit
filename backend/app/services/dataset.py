"""MetroPT-3 数据集登记常量（设备、来源、官方故障事件）。

这些常量只描述**已知事实**，与 Phase 1A 的分析报告和
`data/raw/metropt3/manifest.json` 保持一致：

- 逻辑设备编号 `metropt3-apu-01`：数据集内的稳定标识，非厂商序列号。
- 来源身份：文件名 + SHA-256。
- 时区：官方未声明 → `unknown`，不使用 UTC 解释源时间。
- 故障事件：4 条官方 Air leak 区间，来自 PDF 的 Failure Information 表；
  本文件只做字段转录，不新增、不推断任何事件。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = PROJECT_ROOT / "data" / "raw" / "metropt3"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed" / "metropt3"

CSV_FILENAME = "MetroPT3(AirCompressor).csv"
PDF_FILENAME = "Data Description_Metro.pdf"
MANIFEST_FILENAME = "manifest.json"
FAULT_INTERVALS_FILENAME = "fault_intervals.csv"

CSV_PATH = RAW_DIR / CSV_FILENAME
PDF_PATH = RAW_DIR / PDF_FILENAME
MANIFEST_PATH = RAW_DIR / MANIFEST_FILENAME
FAULT_INTERVALS_PATH = PROCESSED_DIR / FAULT_INTERVALS_FILENAME

DATASET_PAGE = "https://archive.ics.uci.edu/dataset/791/metropt+3+dataset"
DATASET_DOI = "10.24432/C5VW3R"
DATASET_LICENSE = "CC BY 4.0"

EQUIPMENT_CODE = "metropt3-apu-01"
EQUIPMENT_NAME = "MetroPT-3 空气生产单元（APU / 压缩机）"
EQUIPMENT_TYPE = "air_production_unit"
EQUIPMENT_DESCRIPTION = (
    "UCI MetroPT-3 数据集对应的地铁列车空气生产单元。"
    "该编号是数据集逻辑标识，不是厂商序列号，也不代表已核验的真实资产台账。"
)

SOURCE_NAME_CSV = "MetroPT-3 原始监测 CSV（UCI 791）"
SOURCE_NAME_PDF = "MetroPT-3 数据说明 PDF（UCI 791）"

TIMEZONE_NOTE = (
    "源时间戳为 'YYYY-MM-DD HH:MM:SS'，不含时区偏移，官方文档未声明时区。"
    "按 timestamp without time zone 存储并标记 unknown，禁止静默按 UTC 解释。"
)

# 官方 PDF Failure Information 表照录（与 Phase 1A 报告第 7.1 节一致）。
# CSV 统计字段（覆盖率、冻结行数等）不在此处复制，它们属于分析产物而非官方事实。
@dataclass(frozen=True)
class FaultEventSeed:
    event_code: str
    pdf_label: str
    start_time: str
    end_time: str
    failure_type: str
    severity: str
    maintenance_note: str
    verification_status: str
    verification_note: str


FAULT_EVENT_SEEDS: tuple[FaultEventSeed, ...] = (
    FaultEventSeed(
        event_code="E1",
        pdf_label="#1",
        start_time="2020-04-18 00:00:00",
        end_time="2020-04-18 23:59:00",
        failure_type="Air leak",
        severity="High stress",
        maintenance_note="",
        verification_status="verified",
        verification_note="PDF 未给出维护时间。",
    ),
    FaultEventSeed(
        event_code="E2",
        pdf_label="#1（PDF 中编号重复）",
        start_time="2020-05-29 23:30:00",
        end_time="2020-05-30 06:00:00",
        failure_type="Air leak",
        severity="High stress",
        maintenance_note="Maintenance on 30 Apr at 12:00",
        verification_status="typo_suspected",
        verification_note=(
            "PDF 原文写 30 Apr，与故障日期 5 月不符，疑为 30 May 笔误；"
            "按原文保留，不做静默纠正。"
        ),
    ),
    FaultEventSeed(
        event_code="E3",
        pdf_label="#3",
        start_time="2020-06-05 10:00:00",
        end_time="2020-06-07 14:30:00",
        failure_type="Air leak",
        severity="High stress",
        maintenance_note="Maintenance on 8 Jun at 16:00",
        verification_status="verified",
        verification_note="维护晚于故障结束约 25.5 h；区间内部存在采样缺口。",
    ),
    FaultEventSeed(
        event_code="E4",
        pdf_label="#4",
        start_time="2020-07-15 14:30:00",
        end_time="2020-07-15 19:00:00",
        failure_type="Air leak",
        severity="High stress",
        maintenance_note="Maintenance on 16 Jul at 00:00",
        verification_status="verified",
        verification_note="维护晚于故障结束约 5 h；DV_pressure 在区间内几乎恒为 0。",
    ),
)

FAULT_SOURCE_REFERENCE = (
    "Data Description_Metro.pdf → Failure Information 表（原文照录）；"
    "区间与字段经 Phase 1A 分析核对（data/processed/metropt3/fault_intervals.csv）。"
)
