"""Service 层单元测试（不经过 HTTP，验证未来 Agent Tool 可用的契约）。"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.services.catalog import (
    ALL_SENSORS,
    MAX_PAGE_SIZE,
    make_time_window,
    normalize_sensors,
    parse_naive_datetime,
    split_sensors,
    validate_page,
)
from app.services.errors import (
    InvalidParameter,
    InvalidTimeRange,
    TimezoneNotSupported,
    UnsupportedSensor,
    WindowTooLarge,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2020-04-18", datetime(2020, 4, 18)),
        ("2020-04-18T12:34:56", datetime(2020, 4, 18, 12, 34, 56)),
        ("2020-04-18 12:34:56", datetime(2020, 4, 18, 12, 34, 56)),
        ("2020-04-18T12:34", datetime(2020, 4, 18, 12, 34)),
        ("2020-04-18T12:34:56.123456", datetime(2020, 4, 18, 12, 34, 56, 123456)),
    ],
)
def test_parse_naive_datetime_accepts_iso_without_offset(text: str, expected: datetime) -> None:
    parsed = parse_naive_datetime(text, field="start_time")
    assert parsed == expected
    assert parsed.tzinfo is None


@pytest.mark.parametrize(
    "text",
    ["2020-04-18T12:34:56Z", "2020-04-18T12:34:56+08:00", "2020-04-18T12:34:56-0500"],
)
def test_parse_naive_datetime_rejects_timezone(text: str) -> None:
    with pytest.raises(TimezoneNotSupported):
        parse_naive_datetime(text, field="start_time")


@pytest.mark.parametrize("text", ["", "yesterday", "2020/04/18", "2020-13-01", "18-04-2020"])
def test_parse_naive_datetime_rejects_garbage(text: str) -> None:
    with pytest.raises(InvalidParameter):
        parse_naive_datetime(text, field="start_time")


def test_make_time_window_is_half_open() -> None:
    window = make_time_window("2020-04-18T00:00:00", "2020-04-18T01:00:00")
    assert window.duration_seconds == 3600
    # end 的闭区间等价上界比 end 小 1 微秒（源时间戳精确到秒）
    assert window.inclusive_end == datetime(2020, 4, 18, 0, 59, 59, 999999)
    assert window.as_dict()["boundary"] == "[start, end)"


def test_make_time_window_rejects_empty_or_inverted() -> None:
    with pytest.raises(InvalidTimeRange):
        make_time_window("2020-04-18T00:00:00", "2020-04-18T00:00:00")
    with pytest.raises(InvalidTimeRange):
        make_time_window("2020-04-18T02:00:00", "2020-04-18T01:00:00")


def test_make_time_window_rejects_too_large() -> None:
    with pytest.raises(WindowTooLarge):
        make_time_window("2020-01-01", "2021-06-01")


def test_normalize_sensors() -> None:
    assert normalize_sensors(None) == ALL_SENSORS
    assert normalize_sensors("tp2") == ("tp2",)
    assert normalize_sensors("tp2,comp") == ("tp2", "comp")
    assert normalize_sensors(["comp", "tp2"]) == ("tp2", "comp")  # 结果稳定排序
    assert len(normalize_sensors([])) == len(ALL_SENSORS)
    with pytest.raises(UnsupportedSensor):
        normalize_sensors("tp2;drop")


def test_split_sensors_by_kind() -> None:
    analog, digital = split_sensors(("tp2", "comp", "lps"))
    assert analog == ("tp2",)
    assert digital == ("comp", "lps")


def test_validate_page_bounds() -> None:
    assert validate_page(1, 0) == (1, 0)
    assert validate_page(MAX_PAGE_SIZE, 0) == (MAX_PAGE_SIZE, 0)
    for limit, offset in ((0, 0), (MAX_PAGE_SIZE + 1, 0), (-1, 0), (10, -1)):
        with pytest.raises(InvalidParameter):
            validate_page(limit, offset)
