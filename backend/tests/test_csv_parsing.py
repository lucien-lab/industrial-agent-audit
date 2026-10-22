"""CSV 字段解析、时间戳保真与数值精度测试（不依赖数据库）。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from app.services import dataset as ds
from app.services.metropt_import import (
    CSV_HEADER,
    ImportValidationError,
    parse_chunk,
    sha256_file,
    validate_header,
)


def _row(**overrides: str) -> dict[str, str]:
    """构造一行原始文本，字段名与真实 CSV 一致。"""
    base = {
        "Unnamed: 0": "0",
        "timestamp": "2020-02-01 00:00:00",
        "TP2": "-0.012",
        "TP3": "9.358",
        "H1": "9.34",
        "DV_pressure": "-0.024",
        "Reservoirs": "9.358",
        "Oil_temperature": "53.6",
        "Motor_current": "0.04",
        "COMP": "1.0",
        "DV_eletric": "0.0",
        "Towers": "1.0",
        "MPG": "1.0",
        "LPS": "0.0",
        "Pressure_switch": "1.0",
        "Oil_level": "1.0",
        "Caudal_impulses": "1.0",
    }
    base.update(overrides)
    return base


def _frame(rows: list[dict[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=CSV_HEADER)


def test_header_accepts_official_columns() -> None:
    validate_header(CSV_HEADER)


def test_header_rejects_reordered_columns() -> None:
    with pytest.raises(ImportValidationError):
        validate_header(list(reversed(CSV_HEADER)))


def test_header_rejects_missing_column() -> None:
    with pytest.raises(ImportValidationError):
        validate_header(CSV_HEADER[:-1])


def test_timestamp_fidelity_no_timezone_conversion() -> None:
    """源时间按字面值入库，不做任何时区换算。"""
    parsed = parse_chunk(_frame([_row()]), None, None)
    assert parsed.invalid_rows == 0
    row = parsed.rows[0]
    assert row[1] == datetime(2020, 2, 1, 0, 0, 0)
    assert row[1].tzinfo is None
    assert row[2] == "2020-02-01 00:00:00"


def test_float_serialization_noise_is_quantized_exactly() -> None:
    """原始文本是 4 位小数值的 float 噪声，定点解析必须还原为 0.0425。"""
    parsed = parse_chunk(
        _frame([_row(**{"Motor_current": "0.0425000000000005"})]), None, None
    )
    assert parsed.rows[0][3 + 6] == Decimal("0.0425")


def test_precision_overflow_aborts_instead_of_rounding() -> None:
    with pytest.raises(ImportValidationError):
        parse_chunk(_frame([_row(**{"TP2": "1.23456"})]), None, None)


def test_non_canonical_timestamp_is_rejected() -> None:
    parsed = parse_chunk(_frame([_row(timestamp="2020-02-01T00:00:00")]), None, None)
    assert parsed.invalid_rows == 1
    assert "timestamp_unparsable" in parsed.invalid_reasons
    assert parsed.rows == []


def test_digital_value_must_be_binary() -> None:
    parsed = parse_chunk(_frame([_row(LPS="2.0")]), None, None)
    assert parsed.invalid_rows == 1
    assert any(k.startswith("digital_not_binary") for k in parsed.invalid_reasons)


def test_source_row_number_must_be_integer() -> None:
    parsed = parse_chunk(_frame([_row(**{"Unnamed: 0": "abc"})]), None, None)
    assert parsed.invalid_rows == 1
    assert "source_row_number_not_integer" in parsed.invalid_reasons


def test_row_numbers_keep_step_of_ten() -> None:
    frame = _frame(
        [
            _row(**{"Unnamed: 0": "0", "timestamp": "2020-02-01 00:00:00"}),
            _row(**{"Unnamed: 0": "10", "timestamp": "2020-02-01 00:00:10"}),
            _row(**{"Unnamed: 0": "20", "timestamp": "2020-02-01 00:00:20"}),
        ]
    )
    parsed = parse_chunk(frame, None, None)
    assert [r[0] for r in parsed.rows] == [0, 10, 20]


def test_frozen_rule_flags_only_repeated_vectors() -> None:
    frame = _frame(
        [
            _row(**{"Unnamed: 0": "0", "timestamp": "2020-02-01 00:00:00"}),
            _row(**{"Unnamed: 0": "10", "timestamp": "2020-02-01 00:00:10"}),
            _row(**{"Unnamed: 0": "20", "timestamp": "2020-02-01 00:00:20"}),
            _row(
                **{
                    "Unnamed: 0": "30",
                    "timestamp": "2020-02-01 00:00:30",
                    "TP2": "0.5",
                }
            ),
        ]
    )
    parsed = parse_chunk(frame, None, None)
    # 第 2、3 行与上一行完全相同 -> 冻结候选；第 4 行 TP2 变化 -> 不是
    assert [r[-2] for r in parsed.rows] == [0, 1, 1, 0]
    assert parsed.frozen_rows == 2


def test_gap_detection_uses_thirty_seconds_threshold() -> None:
    frame = _frame(
        [
            _row(**{"Unnamed: 0": "0", "timestamp": "2020-02-01 00:00:00"}),
            _row(**{"Unnamed: 0": "10", "timestamp": "2020-02-01 00:00:20"}),
            _row(**{"Unnamed: 0": "20", "timestamp": "2020-02-01 00:01:20"}),
        ]
    )
    parsed = parse_chunk(frame, None, None)
    assert len(parsed.gaps) == 1
    gap = parsed.gaps[0]
    assert gap["gap_seconds"] == Decimal("60.000")
    assert gap["missing_samples"] == 5
    assert gap["gap_start"] == datetime(2020, 2, 1, 0, 0, 20)
    assert gap["gap_end"] == datetime(2020, 2, 1, 0, 1, 20)


def test_real_sample_parses_without_invalid_rows(real_head_csv: Path) -> None:
    frame = pd.read_csv(real_head_csv, dtype=str, keep_default_na=False)
    parsed = parse_chunk(frame, None, None)
    assert len(frame) == 400
    assert parsed.invalid_rows == 0
    assert len(parsed.rows) == 400
    assert parsed.rows[0][0] == 0
    assert parsed.rows[1][0] == 10


def test_manifest_hash_matches_real_file(real_csv_path: Path) -> None:
    from app.services.metropt_import import expected_sha256, read_manifest

    expected = expected_sha256(ds.CSV_FILENAME, read_manifest())
    assert expected is not None
    assert sha256_file(real_csv_path) == expected
