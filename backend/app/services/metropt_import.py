"""MetroPT-3 批量导入模块（Phase 1B）。

设计要点
--------
1. **分块读取**：pandas `chunksize` 流式读取，内存占用与文件大小无关。
2. **逐字保真**：CSV 以字符串读入，数字用 Decimal 定点解析，时间戳原文本单独保存；
   绝不把源时间当 UTC 处理。模拟量按 4 位小数定点入库，入库前强制校验
   `|raw − round(raw,4)| < 1e-6`（实测最大偏差 2.8e-14），超限直接中止而不是静默改写。
3. **批量写入**：psycopg3 `COPY` 写入 UNLOGGED 临时表，再用
   `INSERT ... SELECT ... ON CONFLICT DO NOTHING` 入正式表。
   选择理由：COPY 是 PG 最快的装载通道；ON CONFLICT 把幂等性交给数据库的
   唯一约束 `(source_id, source_row_number)`，不依赖 Python 侧“先查后插”的竞态逻辑。
4. **幂等**：重复导入同一文件时 `inserted_rows` 为 0、`conflict_rows` 等于有效行数，
   表中行数不变。
5. **质量标记**：行级冻结候选标记在导入时按 frozen-v1 计算；采样缺口写入
   `data_gaps`（窗口级），二者语义不同，互不覆盖。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator

import pandas as pd
import psycopg
from sqlalchemy import Engine

from app.db.session import get_engine
from app.models.quality import (
    FROZEN_RULE_VERSION,
    GAP_RULE_VERSION,
    GAP_THRESHOLD_SECONDS,
    NOMINAL_STEP_SECONDS,
    QUALITY_FLAG_FROZEN_CANDIDATE,
    estimate_missing_samples,
)
from app.services import dataset as ds

logger = logging.getLogger(__name__)

IMPORTER_VERSION = "metropt-importer/1.0.0"

CSV_HEADER = [
    "Unnamed: 0",
    "timestamp",
    "TP2",
    "TP3",
    "H1",
    "DV_pressure",
    "Reservoirs",
    "Oil_temperature",
    "Motor_current",
    "COMP",
    "DV_eletric",
    "Towers",
    "MPG",
    "LPS",
    "Pressure_switch",
    "Oil_level",
    "Caudal_impulses",
]

ANALOG_COLUMN_MAP = {
    "TP2": "tp2",
    "TP3": "tp3",
    "H1": "h1",
    "DV_pressure": "dv_pressure",
    "Reservoirs": "reservoirs",
    "Oil_temperature": "oil_temperature",
    "Motor_current": "motor_current",
}

DIGITAL_COLUMN_MAP = {
    "COMP": "comp",
    "DV_eletric": "dv_eletric",
    "Towers": "towers",
    "MPG": "mpg",
    "LPS": "lps",
    "Pressure_switch": "pressure_switch",
    "Oil_level": "oil_level",
    "Caudal_impulses": "caudal_impulses",
}

ANALOG_DB_COLUMNS = tuple(ANALOG_COLUMN_MAP.values())
DIGITAL_DB_COLUMNS = tuple(DIGITAL_COLUMN_MAP.values())

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
TIMESTAMP_MAX_CHARS = len("2020-02-01 00:00:00")
ANALOG_SCALE = Decimal("0.0001")
PRECISION_TOLERANCE = 1e-6

STAGING_TABLE = "stg_sensor_readings"

_INSERT_COLUMNS = (
    "equipment_id",
    "source_id",
    "source_row_number",
    "observed_at",
    "source_timestamp_raw",
    *ANALOG_DB_COLUMNS,
    *DIGITAL_DB_COLUMNS,
    "quality_flags",
    "quality_rule_version",
)

_CREATE_STAGING_SQL = f"""
CREATE TEMP TABLE IF NOT EXISTS {STAGING_TABLE} (
    equipment_id uuid NOT NULL,
    source_id uuid NOT NULL,
    source_row_number integer NOT NULL,
    observed_at timestamp without time zone NOT NULL,
    source_timestamp_raw varchar(19) NOT NULL,
    {", ".join(f"{c} numeric(8,4) NOT NULL" for c in ANALOG_DB_COLUMNS)},
    {", ".join(f"{c} smallint NOT NULL" for c in DIGITAL_DB_COLUMNS)},
    quality_flags smallint NOT NULL,
    quality_rule_version varchar(32) NOT NULL
) ON COMMIT DELETE ROWS
"""

_COPY_SQL = f"COPY {STAGING_TABLE} ({', '.join(_INSERT_COLUMNS)}) FROM STDIN"

_INSERT_SQL = f"""
INSERT INTO sensor_readings ({", ".join(_INSERT_COLUMNS)})
SELECT {", ".join(_INSERT_COLUMNS)}
FROM {STAGING_TABLE}
ORDER BY source_row_number
ON CONFLICT (source_id, source_row_number) DO NOTHING
"""

_VECTOR_EQUAL_SQL = " AND ".join(
    f"r.{c} = s.{c}" for c in (*ANALOG_DB_COLUMNS, *DIGITAL_DB_COLUMNS)
)

# 统计暂存块与既有观测的时间戳重叠情况：
#   identical_rows   同一设备同一时间戳且 15 路取值完全相同（重复观测，可跳过）
#   conflicting_rows 同一设备同一时间戳但取值不同（数据冲突，必须显式处理）
#   same_source_rows 同一来源同一源行号已存在（真正的幂等重导）
_OVERLAP_COUNT_SQL = f"""
SELECT
    count(*) FILTER (WHERE r.id IS NOT NULL AND {_VECTOR_EQUAL_SQL}) AS identical_rows,
    count(*) FILTER (WHERE r.id IS NOT NULL AND NOT ({_VECTOR_EQUAL_SQL})) AS conflicting_rows,
    count(*) FILTER (WHERE r.source_id = s.source_id) AS same_source_rows
FROM {STAGING_TABLE} s
LEFT JOIN sensor_readings r
       ON r.equipment_id = s.equipment_id
      AND r.observed_at = s.observed_at
"""

_CONFLICT_SAMPLE_SQL = f"""
SELECT s.observed_at, s.source_row_number, r.source_row_number AS existing_source_row
FROM {STAGING_TABLE} s
JOIN sensor_readings r
  ON r.equipment_id = s.equipment_id
 AND r.observed_at = s.observed_at
WHERE NOT ({_VECTOR_EQUAL_SQL})
ORDER BY s.observed_at
LIMIT 5
"""

# 受保护的插入：同一设备同一时间戳已存在时不再写入（跨来源去重）。
# 与 ON CONFLICT 组合使用：前者防止跨来源重复观测，后者保证同来源幂等。
_INSERT_GUARDED_SQL = f"""
INSERT INTO sensor_readings ({", ".join(_INSERT_COLUMNS)})
SELECT {", ".join("s." + c for c in _INSERT_COLUMNS)}
FROM {STAGING_TABLE} s
WHERE NOT EXISTS (
    SELECT 1 FROM sensor_readings r
    WHERE r.equipment_id = s.equipment_id
      AND r.observed_at = s.observed_at
)
ORDER BY s.source_row_number
ON CONFLICT (source_id, source_row_number) DO NOTHING
"""

_INSERT_GAP_SQL = """
INSERT INTO data_gaps (
    equipment_id, source_id, gap_start, gap_end, gap_seconds,
    missing_samples, threshold_seconds, rule_version
) VALUES (
    %(equipment_id)s, %(source_id)s, %(gap_start)s, %(gap_end)s, %(gap_seconds)s,
    %(missing_samples)s, %(threshold_seconds)s, %(rule_version)s
)
ON CONFLICT ON CONSTRAINT uq_data_gaps_source_window_rule DO NOTHING
"""


class ImportValidationError(RuntimeError):
    """输入不满足入库契约（表头、精度、取值等）。导入会中止且不写入半成品数据。"""


class ImportConflictError(RuntimeError):
    """同一设备同一时间戳存在**不同**观测值。

    这类冲突必须由人显式处理：不得覆盖已有记录，也不得静默跳过而不报告。
    默认策略下导入中止（已提交的 chunk 会保留，运行记录标记为 partial/failed）。
    """


@dataclass
class ImportStats:
    attempted_rows: int = 0
    inserted_rows: int = 0
    conflict_rows: int = 0
    invalid_rows: int = 0
    frozen_rows: int = 0
    gap_rows_written: int = 0
    duration_seconds: float = 0.0
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None
    first_row_number: int | None = None
    last_row_number: int | None = None
    invalid_reasons: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False
    run_id: int | None = None
    # --- P1-A：来源身份与跨来源重叠 ---
    source_reused: bool = False
    source_registered_file: str | None = None
    source_file_mismatch: bool | None = None
    db_hash_unique_enforced: bool = False
    overlap_skipped_rows: int = 0
    timestamp_conflict_rows: int = 0
    # --- P2-4：成功/部分成功状态 ---
    partial: bool = False

    @property
    def valid_rows(self) -> int:
        """通过校验、具备入库资格的行数。"""
        return self.attempted_rows - self.invalid_rows

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "attempted_rows": self.attempted_rows,
            "valid_rows": self.valid_rows,
            "inserted_rows": self.inserted_rows,
            "conflict_rows": self.conflict_rows,
            "invalid_rows": self.invalid_rows,
            "frozen_rows": self.frozen_rows,
            "gap_rows_written": self.gap_rows_written,
            "duration_seconds": round(self.duration_seconds, 3),
            "first_timestamp": _iso(self.first_timestamp),
            "last_timestamp": _iso(self.last_timestamp),
            "first_row_number": self.first_row_number,
            "last_row_number": self.last_row_number,
            "invalid_reasons": dict(self.invalid_reasons),
            "dry_run": self.dry_run,
            "run_id": self.run_id,
            "source_reused": self.source_reused,
            "source_registered_file": self.source_registered_file,
            "source_file_mismatch": self.source_file_mismatch,
            "db_hash_unique_enforced": self.db_hash_unique_enforced,
            "overlap_skipped_rows": self.overlap_skipped_rows,
            "timestamp_conflict_rows": self.timestamp_conflict_rows,
            "partial": self.partial,
            "committed_rows": self.inserted_rows,
        }
        return payload


def _iso(value: datetime | None) -> str | None:
    return value.strftime(TIMESTAMP_FORMAT) if value else None


# --------------------------------------------------------------------------- #
# 文件与来源
# --------------------------------------------------------------------------- #
def sha256_file(path: Path, chunk_bytes: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_bytes), b""):
            digest.update(block)
    return digest.hexdigest()


def read_manifest(path: Path | None = None) -> dict[str, Any]:
    manifest_path = path or ds.MANIFEST_PATH
    if not manifest_path.is_file():
        return {}
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def expected_sha256(csv_name: str, manifest: dict[str, Any]) -> str | None:
    entry = manifest.get("files", {}).get(csv_name)
    return entry.get("sha256") if entry else None


def validate_header(actual: Iterable[str]) -> None:
    """表头必须与官方 CSV 完全一致（顺序与名称），否则中止导入。"""
    actual_list = list(actual)
    if actual_list != CSV_HEADER:
        missing = [c for c in CSV_HEADER if c not in actual_list]
        unexpected = [c for c in actual_list if c not in CSV_HEADER]
        raise ImportValidationError(
            f"CSV 表头不符合预期。缺失列={missing}，多余列={unexpected}，"
            f"实际表头={actual_list}"
        )


# --------------------------------------------------------------------------- #
# 单块解析
# --------------------------------------------------------------------------- #
@dataclass
class ParsedChunk:
    """一个 CSV 块解析后的强类型结果。"""

    rows: list[tuple[Any, ...]]
    gaps: list[dict[str, Any]]
    invalid_rows: int
    invalid_reasons: dict[str, int]
    frozen_rows: int
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    first_row_number: int | None
    last_row_number: int | None


def _decimal_or_none(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def parse_chunk(
    chunk: pd.DataFrame,
    prev_analog: tuple[Decimal, ...] | None,
    prev_timestamp: datetime | None,
) -> ParsedChunk:
    """把一块字符串 DataFrame 解析成入库元组，并计算冻结标记与缺口。

    `prev_*` 由调用方在块之间传递，保证跨块的冻结/缺口判定连续。
    """
    invalid_reasons: dict[str, int] = {}

    def reject(reason: str, count: int) -> None:
        invalid_reasons[reason] = invalid_reasons.get(reason, 0) + count

    row_numbers_raw = chunk["Unnamed: 0"].astype(str).str.strip()
    timestamps_raw = chunk["timestamp"].astype(str).str.strip()

    row_number_numeric = pd.to_numeric(row_numbers_raw, errors="coerce")
    bad_rownum = row_number_numeric.isna()
    if bad_rownum.any():
        reject("source_row_number_not_integer", int(bad_rownum.sum()))
    row_number_numeric = row_number_numeric.fillna(-1)
    if (row_number_numeric % 1 != 0).any():
        reject("source_row_number_not_integer", int((row_number_numeric % 1 != 0).sum()))

    parsed_time = pd.to_datetime(timestamps_raw, format=TIMESTAMP_FORMAT, errors="coerce")
    bad_ts = parsed_time.isna()
    if bad_ts.any():
        reject("timestamp_unparsable", int(bad_ts.sum()))
    bad_len = timestamps_raw.str.len() != TIMESTAMP_MAX_CHARS
    if bad_len.any():
        reject("timestamp_text_not_canonical", int(bad_len.sum()))

    analog_frames: list[pd.Series] = []
    for csv_col in ANALOG_COLUMN_MAP:
        numeric = pd.to_numeric(chunk[csv_col].astype(str).str.strip(), errors="coerce")
        bad = numeric.isna()
        if bad.any():
            reject(f"analog_unparsable:{csv_col}", int(bad.sum()))
        deviation = (numeric - numeric.round(4)).abs()
        # 原始文本是 4 位小数值的 float64 序列化噪声；超过容差说明存在
        # NUMERIC(8,4) 无法无损表达的真实精度，属于契约失效，直接中止。
        overflow = deviation > PRECISION_TOLERANCE
        if overflow.any():
            raise ImportValidationError(
                f"列 {csv_col} 出现超出 NUMERIC(8,4) 可无损表达的精度："
                f"{int(overflow.sum())} 行，首个行号="
                f"{row_numbers_raw[overflow].iloc[0]}，原始值="
                f"{chunk[csv_col][overflow].iloc[0]!r}。"
                "请先调整数值类型设计，不要静默四舍五入。"
            )
        analog_frames.append(numeric.round(4))

    digital_frames: list[pd.Series] = []
    for csv_col in DIGITAL_COLUMN_MAP:
        numeric = pd.to_numeric(chunk[csv_col].astype(str).str.strip(), errors="coerce")
        bad = numeric.isna()
        if bad.any():
            reject(f"digital_unparsable:{csv_col}", int(bad.sum()))
        not_binary = ~numeric.isin([0, 1])
        if not_binary.any():
            reject(f"digital_not_binary:{csv_col}", int(not_binary.sum()))
        digital_frames.append(numeric)

    invalid_mask = bad_rownum | bad_ts | bad_len
    for series in analog_frames:
        invalid_mask = invalid_mask | series.isna()
    for series in digital_frames:
        invalid_mask = invalid_mask | series.isna()
        invalid_mask = invalid_mask | ~series.isin([0, 1])
    invalid_count = int(invalid_mask.sum())

    rows: list[tuple[Any, ...]] = []
    gaps: list[dict[str, Any]] = []
    frozen_rows = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    first_rn: int | None = None
    last_rn: int | None = None

    analog_matrix = [series.to_numpy() for series in analog_frames]
    digital_matrix = [series.to_numpy() for series in digital_frames]
    ts_values = parsed_time.to_numpy()
    rn_values = row_number_numeric.to_numpy()

    for idx in range(len(chunk)):
        if invalid_mask.iloc[idx]:
            continue
        ts_value = pd.Timestamp(ts_values[idx]).to_pydatetime()
        rn_value = int(rn_values[idx])
        raw_text = timestamps_raw.iloc[idx]

        analog_values = tuple(
            Decimal(repr(float(series[idx]))) for series in analog_matrix
        )
        digital_values = tuple(int(series[idx]) for series in digital_matrix)

        flags = 0
        if prev_analog is not None and analog_values == prev_analog:
            flags |= QUALITY_FLAG_FROZEN_CANDIDATE
            frozen_rows += 1

        if prev_timestamp is not None:
            delta_seconds = (ts_value - prev_timestamp).total_seconds()
            # 与 Phase 1A 分析脚本保持同一判据：间隔 ≥ 30 s 计为缺口
            # （标称 10 s，实测时钟抖动上限 13 s）。
            if delta_seconds >= GAP_THRESHOLD_SECONDS:
                # 统一口径：round_half_up(Δ/步长) - 1（与 PostgreSQL round 一致）
                missing = max(estimate_missing_samples(delta_seconds), 1)
                gaps.append(
                    {
                        "gap_start": prev_timestamp,
                        "gap_end": ts_value,
                        "gap_seconds": Decimal(f"{delta_seconds:.3f}"),
                        "missing_samples": missing,
                        "threshold_seconds": Decimal(str(GAP_THRESHOLD_SECONDS)),
                        "rule_version": GAP_RULE_VERSION,
                    }
                )

        prev_analog = analog_values
        prev_timestamp = ts_value

        if first_ts is None:
            first_ts = ts_value
            first_rn = rn_value
        last_ts = ts_value
        last_rn = rn_value

        rows.append(
            (
                rn_value,
                ts_value,
                raw_text,
                *analog_values,
                *digital_values,
                flags,
                FROZEN_RULE_VERSION,
            )
        )

    return ParsedChunk(
        rows=rows,
        gaps=gaps,
        invalid_rows=invalid_count,
        invalid_reasons=invalid_reasons,
        frozen_rows=frozen_rows,
        first_timestamp=first_ts,
        last_timestamp=last_ts,
        first_row_number=first_rn,
        last_row_number=last_rn,
    )


class _PrevState:
    """跨块传递的判定状态（冻结比较基准 + 缺口比较基准）。"""

    analog: tuple[Decimal, ...] | None = None
    timestamp: datetime | None = None

    def update(self, parsed: ParsedChunk) -> None:
        if parsed.rows:
            self.analog = tuple(parsed.rows[-1][3 : 3 + len(ANALOG_DB_COLUMNS)])
            self.timestamp = parsed.rows[-1][1]


def _chunk_iterator(
    csv_path: Path, chunk_size: int, max_rows: int | None
) -> Iterator[pd.DataFrame]:
    reader = pd.read_csv(
        csv_path,
        dtype=str,
        keep_default_na=False,
        na_filter=False,
        chunksize=chunk_size,
        usecols=CSV_HEADER,
        low_memory=False,
    )
    consumed = 0
    for chunk in reader:
        if max_rows is not None:
            remaining = max_rows - consumed
            if remaining <= 0:
                break
            if len(chunk) > remaining:
                chunk = chunk.iloc[:remaining]
        consumed += len(chunk)
        yield chunk
        if max_rows is not None and consumed >= max_rows:
            break


# --------------------------------------------------------------------------- #
# 数据库注册与导入
# --------------------------------------------------------------------------- #
def register_equipment(connection: psycopg.Connection) -> uuid.UUID:
    """幂等登记逻辑设备。"""
    connection.execute(
        """
        INSERT INTO equipment (equipment_code, equipment_name, equipment_type, description)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (equipment_code) DO NOTHING
        """,
        (
            ds.EQUIPMENT_CODE,
            ds.EQUIPMENT_NAME,
            ds.EQUIPMENT_TYPE,
            ds.EQUIPMENT_DESCRIPTION,
        ),
    )
    row = connection.execute(
        "SELECT id FROM equipment WHERE equipment_code = %s", (ds.EQUIPMENT_CODE,)
    ).fetchone()
    if row is None:  # pragma: no cover - 仅在并发删除时出现
        raise RuntimeError("设备登记失败")
    return row[0]


@dataclass(frozen=True)
class SourceRef:
    """数据来源解析结果。"""

    id: uuid.UUID
    reused: bool
    registered_file: str
    requested_file: str
    hash_unique_enforced: bool

    @property
    def file_mismatch(self) -> bool:
        """同一 SHA-256 但本次文件名与已登记文件名不同。"""
        return self.registered_file != self.requested_file


def has_source_hash_unique(connection: psycopg.Connection) -> bool:
    """数据库是否存在 file_sha256 唯一约束/索引。

    迁移 `0005` 之前（例如尚未升级的生产库）没有该约束，此时导入仍会按哈希复用
    既有来源（避免重复观测），但**防并发重复登记的硬保证**由 Python 侧查询提供，
    并会在 ingest_runs.notes 中记录 `db_hash_unique_enforced=false`。
    """
    row = connection.execute(
        """
        SELECT 1
        FROM pg_indexes
        WHERE schemaname = current_schema()
          AND tablename = 'data_sources'
          AND indexdef ILIKE 'CREATE UNIQUE INDEX%'
          AND indexdef LIKE '%(file_sha256)%'
        LIMIT 1
        """
    ).fetchone()
    return row is not None


def register_data_source(
    connection: psycopg.Connection,
    *,
    source_name: str,
    source_file: str,
    file_sha256: str,
    file_bytes: int | None,
    source_url: str | None = None,
    license: str | None = ds.DATASET_LICENSE,
    doi: str | None = ds.DATASET_DOI,
    timezone_note: str | None = ds.TIMEZONE_NOTE,
    nominal_sample_interval_s: float | None = NOMINAL_STEP_SECONDS,
    note: str | None = None,
) -> SourceRef:
    """按**文件内容哈希**幂等登记来源（P1-A）。

    身份规则：
    - 数据源身份以 SHA-256 为准；文件名只作为元数据保留；
    - 相同 SHA-256、不同文件名 → 复用已登记来源，不新建行；
    - 数据库层由 UNIQUE(file_sha256) 兜底（迁移 0005）；未升级的库仍按哈希复用
      （不会产生重复观测），但会记录 `db_hash_unique_enforced=false`。
    """
    hash_unique = has_source_hash_unique(connection)

    def _find() -> tuple[uuid.UUID, str] | None:
        row = connection.execute(
            """
            SELECT id, source_file
            FROM data_sources
            WHERE file_sha256 = %s
            ORDER BY recorded_at ASC, id ASC
            LIMIT 1
            """,
            (file_sha256,),
        ).fetchone()
        return (row[0], row[1]) if row else None

    found = _find()
    if found is not None:
        logger.info(
            "数据源已存在（SHA-256 复用）：registered=%s requested=%s",
            found[1],
            source_file,
        )
        return SourceRef(found[0], True, found[1], source_file, hash_unique)

    connection.execute(
        """
        INSERT INTO data_sources (
            source_name, source_url, source_file, file_sha256, file_bytes,
            license, doi, timezone_status, timezone_note,
            nominal_sample_interval_s, note
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'unknown', %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (
            source_name,
            source_url,
            source_file,
            file_sha256,
            file_bytes,
            license,
            doi,
            timezone_note,
            nominal_sample_interval_s,
            note,
        ),
    )
    # 并发情况下另一事务可能先插入：统一回查，保证拿到唯一来源 id
    found = _find()
    if found is None:  # pragma: no cover
        raise RuntimeError("来源登记失败")
    reused = found[1] != source_file or found[0] is None
    return SourceRef(found[0], reused, found[1], source_file, hash_unique)


def _supported_status(connection: psycopg.Connection, desired: str) -> str:
    """`partial` 需要迁移 0005 的 CHECK 扩展；未升级的库回退为 failed 并在 notes 标注。"""
    if desired != "partial":
        return desired
    row = connection.execute(
        """
        SELECT pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = 'ingest_runs'::regclass
          AND contype = 'c'
          AND conname LIKE '%status%'
        LIMIT 1
        """
    ).fetchone()
    if row and "partial" in row[0]:
        return "partial"
    logger.warning("目标库 ingest_runs.status 尚不支持 partial（待迁移 0005），回退为 failed")
    return "failed"


def _stats_notes(stats: "ImportStats", error: str | None = None) -> str:
    """把导入的幂等/重叠/部分提交信息写入 ingest_runs.notes（结构化 key=value）。"""
    parts = [
        f"source_reused={str(stats.source_reused).lower()}",
        f"overlap_skipped={stats.overlap_skipped_rows}",
        f"timestamp_conflicts={stats.timestamp_conflict_rows}",
        f"db_hash_unique_enforced={str(stats.db_hash_unique_enforced).lower()}",
        f"committed_rows={stats.inserted_rows}",
    ]
    if stats.source_file_mismatch:
        parts.append(f"source_file_mismatch={stats.source_registered_file}")
    if stats.partial:
        parts.append("partial_commit=true")
    if error:
        parts.append("error=" + " ".join(error.split())[:1500])
    return "; ".join(parts)


def _create_ingest_run(connection: psycopg.Connection, payload: dict[str, Any]) -> int:
    row = connection.execute(
        """
        INSERT INTO ingest_runs (
            source_id, status, csv_path, chunk_size, max_rows, dry_run, importer_version
        ) VALUES (
            %(source_id)s, 'running', %(csv_path)s, %(chunk_size)s, %(max_rows)s,
            %(dry_run)s, %(importer_version)s
        ) RETURNING id
        """,
        payload,
    ).fetchone()
    return int(row[0])


def _finish_ingest_run(
    connection: psycopg.Connection,
    run_id: int,
    stats: ImportStats,
    status: str,
    notes: str | None = None,
) -> None:
    effective = _supported_status(connection, status)
    if effective != status:
        notes = f"status_requested={status}; " + (notes or "")
    status = effective
    connection.execute(
        """
        UPDATE ingest_runs SET
            status = %(status)s,
            finished_at = now(),
            duration_seconds = %(duration)s,
            attempted_rows = %(attempted)s,
            inserted_rows = %(inserted)s,
            conflict_rows = %(conflict)s,
            invalid_rows = %(invalid)s,
            gap_rows_written = %(gaps)s,
            frozen_rows = %(frozen)s,
            notes = %(notes)s
        WHERE id = %(run_id)s
        """,
        {
            "status": status,
            "duration": Decimal(f"{stats.duration_seconds:.3f}"),
            "attempted": stats.attempted_rows,
            "inserted": stats.inserted_rows,
            "conflict": stats.conflict_rows,
            "invalid": stats.invalid_rows,
            "gaps": stats.gap_rows_written,
            "frozen": stats.frozen_rows,
            "notes": notes,
            "run_id": run_id,
        },
    )


def import_metropt_csv(
    csv_path: Path | str | None = None,
    *,
    engine: Engine | None = None,
    chunk_size: int = 100_000,
    max_rows: int | None = None,
    dry_run: bool = False,
    verify_hash: bool = True,
    overlap_policy: str = "abort",
) -> ImportStats:
    """导入 MetroPT-3 CSV。

    `dry_run=True` 时不建立任何数据库连接，只做解析、校验与统计。

    `overlap_policy` 控制“同一设备同一时间戳已存在、但取值不同”时的行为：
    - `abort`（默认）：抛出 ImportConflictError，冲突行不写入，绝不覆盖既有观测；
    - `skip`：跳过冲突行并继续，运行记录标记为 partial 并统计冲突数。
    无论哪种策略，取值相同的重复观测都会被跳过而不是重复插入。
    """
    path = Path(csv_path) if csv_path is not None else ds.CSV_PATH
    if not path.is_file():
        raise FileNotFoundError(f"找不到 CSV：{path}")
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须为正整数")
    if overlap_policy not in {"abort", "skip"}:
        raise ValueError("overlap_policy 只支持 abort / skip")

    started = time.perf_counter()
    stats = ImportStats(dry_run=dry_run)

    file_hash = sha256_file(path)
    manifest = read_manifest()
    # 只对登记在 manifest 中的同名原始文件做哈希校验；
    # 测试或抽样文件不在 manifest 中，因此不会因为名称不同而误报。
    expected = expected_sha256(path.name, manifest)
    if verify_hash and expected and file_hash != expected:
        raise ImportValidationError(
            f"文件 SHA-256 与 manifest 不一致：实际={file_hash} 期望={expected}。"
            "原始数据可能被改动，拒绝导入。"
        )

    header = pd.read_csv(path, nrows=0).columns.tolist()
    validate_header(header)

    state = _PrevState()
    conn_cm: Any = None
    connection: psycopg.Connection | None = None
    equipment_id: uuid.UUID | None = None
    source_id: uuid.UUID | None = None
    run_id: int | None = None
    overlap_guard = False

    if not dry_run:
        active_engine = engine or get_engine()
        conn_cm = active_engine.raw_connection()
        raw_connection = conn_cm
        connection = raw_connection.driver_connection
        with connection.transaction():
            equipment_id = register_equipment(connection)
            source_ref = register_data_source(
                connection,
                source_name=ds.SOURCE_NAME_CSV,
                source_url=ds.DATASET_PAGE,
                source_file=path.name,
                file_sha256=file_hash,
                file_bytes=path.stat().st_size,
                note="原始 CSV，只读；导入遵循 docs/database_design.md 的幂等策略。",
            )
            source_id = source_ref.id
            stats.source_reused = source_ref.reused
            stats.source_registered_file = source_ref.registered_file
            stats.source_file_mismatch = source_ref.file_mismatch
            stats.db_hash_unique_enforced = source_ref.hash_unique_enforced
            # 已有观测时才启用跨来源重叠保护（首次全量导入走快速路径）
            existing_readings = int(
                connection.execute(
                    "SELECT count(*) FROM sensor_readings WHERE equipment_id = %s",
                    (equipment_id,),
                ).fetchone()[0]
            )
            overlap_guard = existing_readings > 0
            logger.info(
                "source: id=%s reused=%s registered_file=%s requested_file=%s"
                " hash_unique_enforced=%s overlap_guard=%s",
                source_id,
                stats.source_reused,
                source_ref.registered_file,
                source_ref.requested_file,
                stats.db_hash_unique_enforced,
                overlap_guard,
            )
            run_id = _create_ingest_run(
                connection,
                {
                    "source_id": source_id,
                    "csv_path": str(path),
                    "chunk_size": chunk_size,
                    "max_rows": max_rows,
                    "dry_run": dry_run,
                    "importer_version": IMPORTER_VERSION,
                },
            )
            stats.run_id = run_id

    try:
        for chunk in _chunk_iterator(path, chunk_size, max_rows):
            parsed = parse_chunk(chunk, state.analog, state.timestamp)
            state.update(parsed)

            stats.attempted_rows += len(chunk)
            stats.invalid_rows += parsed.invalid_rows
            stats.frozen_rows += parsed.frozen_rows
            for reason, count in parsed.invalid_reasons.items():
                stats.invalid_reasons[reason] = stats.invalid_reasons.get(reason, 0) + count
            if parsed.first_timestamp is not None and stats.first_timestamp is None:
                stats.first_timestamp = parsed.first_timestamp
                stats.first_row_number = parsed.first_row_number
            if parsed.last_timestamp is not None:
                stats.last_timestamp = parsed.last_timestamp
                stats.last_row_number = parsed.last_row_number

            if dry_run or connection is None or equipment_id is None or source_id is None:
                # 试运行不写库：只统计“通过校验、可入库”的行与缺口数量。
                stats.gap_rows_written += len(parsed.gaps)
                continue

            with connection.transaction():
                connection.execute(_CREATE_STAGING_SQL)
                connection.execute(f"TRUNCATE {STAGING_TABLE}")
                identical = conflicting = same_source = 0
                with connection.cursor() as cursor:
                    with cursor.copy(_COPY_SQL) as copy:
                        for row in parsed.rows:
                            copy.write_row((equipment_id, source_id, *row))
                    if overlap_guard:
                        cursor.execute(_OVERLAP_COUNT_SQL)
                        identical, conflicting, same_source = cursor.fetchone()
                        # 先记账再决定是否中止：冲突计数必须能追溯到 ingest_runs.notes
                        stats.conflict_rows += int(same_source)
                        stats.overlap_skipped_rows += max(0, int(identical) - int(same_source))
                        stats.timestamp_conflict_rows += int(conflicting)
                        if conflicting and overlap_policy == "abort":
                            samples = cursor.execute(_CONFLICT_SAMPLE_SQL).fetchall()
                            detail = ", ".join(
                                f"{s[0]} (新行 {s[1]} / 已存行 {s[2]})" for s in samples
                            )
                            raise ImportConflictError(
                                f"检测到 {conflicting} 条时间戳冲突（同一设备同一时刻取值不同），"
                                f"已中止导入且未覆盖任何既有记录。示例：{detail}。"
                                "如需跳过冲突继续，请显式使用 overlap_policy='skip'。"
                            )
                        cursor.execute(_INSERT_GUARDED_SQL)
                    else:
                        cursor.execute(_INSERT_SQL)
                    inserted = cursor.rowcount
                    gap_written = 0
                    if parsed.gaps:
                        for gap in parsed.gaps:
                            gap_with_ids = {
                                **gap,
                                "equipment_id": equipment_id,
                                "source_id": source_id,
                            }
                            cursor.execute(_INSERT_GAP_SQL, gap_with_ids)
                            gap_written += cursor.rowcount
                stats.inserted_rows += inserted
                if not overlap_guard:
                    # 无重叠保护时（设备此前没有任何观测），跳过只可能来自同源冲突
                    stats.conflict_rows += len(parsed.rows) - inserted
                stats.gap_rows_written += gap_written
                logger.info(
                    "chunk done: valid=%s inserted=%s same_source=%s overlap_identical=%s"
                    " conflicts=%s invalid=%s",
                    len(parsed.rows),
                    inserted,
                    same_source,
                    max(0, int(identical) - int(same_source)),
                    conflicting,
                    parsed.invalid_rows,
                )

        stats.duration_seconds = time.perf_counter() - started
        if stats.timestamp_conflict_rows > 0:
            # policy=skip 时冲突行被跳过：数据不完整，不能记作完全成功
            stats.partial = True
        final_status = "partial" if stats.partial else "succeeded"
        if connection is not None and run_id is not None:
            with connection.transaction():
                _finish_ingest_run(connection, run_id, stats, final_status, _stats_notes(stats))
        if stats.partial:
            logger.warning(
                "导入完成但存在被跳过的冲突行（%s 条），运行记录标记为 partial",
                stats.timestamp_conflict_rows,
            )
        return stats
    except Exception as exc:
        stats.duration_seconds = time.perf_counter() - started
        # 已经提交过 chunk 或已检出冲突 -> 部分成功；否则为失败
        stats.partial = stats.inserted_rows > 0
        if connection is not None and run_id is not None:
            try:
                with connection.transaction():
                    _finish_ingest_run(
                        connection,
                        run_id,
                        stats,
                        "partial" if stats.partial else "failed",
                        _stats_notes(stats, error=str(exc)),
                    )
            except Exception:  # pragma: no cover - 记录失败不应覆盖原始异常
                logger.exception("写入 ingest_runs 失败状态时出错")
        raise
    finally:
        if conn_cm is not None:
            conn_cm.close()


# --------------------------------------------------------------------------- #
# 官方故障事件
# --------------------------------------------------------------------------- #
def import_fault_events(
    *,
    engine: Engine | None = None,
    fault_file: Path | None = None,
) -> dict[str, Any]:
    """从 Phase 1A 已验证的 `fault_intervals.csv` 导入官方故障区间。

    与 `app.services.dataset.FAULT_EVENT_SEEDS`（PDF 原文转录）逐条交叉核对，
    不一致即中止，避免把派生文件的错误带进数据库。
    """
    path = fault_file or ds.FAULT_INTERVALS_PATH
    if not path.is_file():
        raise FileNotFoundError(f"找不到故障事件文件：{path}")

    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    required = {"id", "pdf_label", "start", "end", "failure", "severity", "maintenance", "note"}
    missing = required - set(frame.columns)
    if missing:
        raise ImportValidationError(f"故障事件文件缺少列：{sorted(missing)}")
    if len(frame) != len(ds.FAULT_EVENT_SEEDS):
        raise ImportValidationError(
            f"故障事件数量不符：文件 {len(frame)} 条，官方转录 {len(ds.FAULT_EVENT_SEEDS)} 条。"
            "不得凭空增减官方事件。"
        )

    seeds = {seed.event_code: seed for seed in ds.FAULT_EVENT_SEEDS}
    records = []
    for row in frame.to_dict(orient="records"):
        code = row["id"].strip()
        seed = seeds.get(code)
        if seed is None:
            raise ImportValidationError(f"故障事件 {code} 不在官方转录清单中")
        if (row["start"].strip(), row["end"].strip()) != (seed.start_time, seed.end_time):
            raise ImportValidationError(
                f"故障事件 {code} 区间与官方转录不一致：文件={row['start']}~{row['end']}，"
                f"官方={seed.start_time}~{seed.end_time}"
            )
        records.append(
            {
                "event_code": code,
                "pdf_label": row["pdf_label"].strip() or seed.pdf_label,
                "start_time": seed.start_time,
                "end_time": seed.end_time,
                "failure_type": seed.failure_type,
                "severity": seed.severity,
                "maintenance_note": row["maintenance"].strip(),
                "verification_status": seed.verification_status,
                "verification_note": (row["note"].strip() or seed.verification_note),
            }
        )

    active_engine = engine or get_engine()
    conn_cm = active_engine.raw_connection()
    raw_connection = conn_cm
    connection = raw_connection.driver_connection
    inserted = 0
    try:
        with connection.transaction():
            equipment_id = register_equipment(connection)
            pdf_hash = expected_sha256(ds.PDF_FILENAME, read_manifest()) or (
                sha256_file(ds.PDF_PATH) if ds.PDF_PATH.is_file() else ""
            )
            if not pdf_hash:
                raise ImportValidationError("无法确定 PDF 校验和，拒绝登记故障来源")
            pdf_source = register_data_source(
                connection,
                source_name=ds.SOURCE_NAME_PDF,
                source_url=ds.DATASET_PAGE,
                source_file=ds.PDF_FILENAME,
                file_sha256=pdf_hash,
                file_bytes=ds.PDF_PATH.stat().st_size if ds.PDF_PATH.is_file() else None,
                nominal_sample_interval_s=None,
                note="故障区间官方说明文件（Failure Information 表）。",
            )
            source_id = pdf_source.id
            for record in records:
                cursor = connection.execute(
                    """
                    INSERT INTO fault_events (
                        equipment_id, source_id, event_code, pdf_label,
                        start_time, end_time, failure_type, severity,
                        fault_description, maintenance_note, source_reference,
                        verification_status, verification_note
                    ) VALUES (
                        %(equipment_id)s, %(source_id)s, %(event_code)s, %(pdf_label)s,
                        %(start_time)s, %(end_time)s, %(failure_type)s, %(severity)s,
                        %(description)s, %(maintenance_note)s, %(source_reference)s,
                        %(verification_status)s, %(verification_note)s
                    )
                    ON CONFLICT (event_code) DO NOTHING
                    """,
                    {
                        **record,
                        "equipment_id": equipment_id,
                        "source_id": source_id,
                        "description": f"{record['failure_type']} / {record['severity']}（官方报告）",
                        "source_reference": ds.FAULT_SOURCE_REFERENCE,
                    },
                )
                inserted += cursor.rowcount
    finally:
        conn_cm.close()

    return {
        "file": str(path),
        "events_in_file": len(records),
        "inserted": inserted,
        "skipped_existing": len(records) - inserted,
    }
