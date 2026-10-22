"""传感器名称登记表与时间窗参数校验（Service 层共用，不依赖 FastAPI）。

时间语义（与 `docs/database_design.md` 一致，是本项目的硬约束）：

- 源数据 `observed_at` 是 `timestamp without time zone`，官方未声明时区；
- 因此本服务**只接受无时区的 ISO 8601 字符串**，不做任何 UTC/本地时间换算；
- 时间区间统一为左闭右开 `[start, end)`：包含 `start`，不包含 `end`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.models import ANALOG_COLUMNS, DIGITAL_COLUMNS, NOMINAL_STEP_SECONDS
from app.services.errors import (
    InvalidParameter,
    InvalidTimeRange,
    TimezoneNotSupported,
    UnsupportedSensor,
    WindowTooLarge,
)

# --------------------------------------------------------------------------- #
# 传感器登记表：键是数据库列名（也是 API 的合法取值），值区分模拟量/数字量。
# 客户端不允许传入任意列名——只能命中这里的白名单。
# --------------------------------------------------------------------------- #
SENSOR_KINDS: dict[str, str] = {
    **{name: "analog" for name in ANALOG_COLUMNS},
    **{name: "digital" for name in DIGITAL_COLUMNS},
}
ALL_SENSORS: tuple[str, ...] = tuple(SENSOR_KINDS)
ANALOG_SENSORS: tuple[str, ...] = ANALOG_COLUMNS
DIGITAL_SENSORS: tuple[str, ...] = DIGITAL_COLUMNS

SENSOR_LABELS: dict[str, str] = {
    "tp2": "TP2 温度/压力测点",
    "tp3": "TP3 温度/压力测点",
    "h1": "H1 测点",
    "dv_pressure": "DV_pressure 卸荷压力",
    "reservoirs": "Reservoirs 储气罐压力",
    "oil_temperature": "Oil_temperature 油温",
    "motor_current": "Motor_current 电机电流",
    "comp": "COMP 压缩机状态",
    "dv_eletric": "DV_eletric 卸荷电磁阀状态",
    "towers": "Towers 冷却塔状态",
    "mpg": "MPG 状态位",
    "lps": "LPS 低压开关",
    "pressure_switch": "Pressure_switch 压力开关",
    "oil_level": "Oil_level 油位开关",
    "caudal_impulses": "Caudal_impulses 流量脉冲",
}

# --------------------------------------------------------------------------- #
# 分页与窗口上限（硬限制，避免无界查询）
# --------------------------------------------------------------------------- #
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000
# 数据集本身跨度 213 天；上限设为 366 天，任何真实窗口都可用，同时挡住无界查询。
MAX_WINDOW_DAYS = 366
MAX_WINDOW = timedelta(days=MAX_WINDOW_DAYS)
NOMINAL_STEP = NOMINAL_STEP_SECONDS

# 只允许这几种形状：日期、日期+时间（T 或空格分隔）、可带秒/小数秒
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?$"
)
# 只有“带偏移的 ISO 8601”才判定为时区问题；其它非法输入统一按格式错误处理
_ISO_WITH_OFFSET = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:?\d{2})$"
)


def parse_naive_datetime(value: str, *, field: str) -> datetime:
    """把查询参数解析成 **naive** datetime。

    明确拒绝带时区偏移的输入（`Z`、`+08:00`、`-0500`），而不是静默转换成 UTC：
    源时间戳的时区未知，任何自动换算都会制造不存在的事实。
    """
    if not isinstance(value, str) or not value.strip():
        raise InvalidParameter(f"{field} 不能为空，需为 ISO 8601 无时区时间", field=field)
    text = value.strip()
    if not (_DATE_ONLY.match(text) or _DATETIME.match(text)):
        if _ISO_WITH_OFFSET.match(text):
            raise TimezoneNotSupported(
                f"{field}={value!r} 带有时间偏移；本服务只接受无时区时间（如 2020-04-18T00:00:00），"
                "源数据时间戳无时区信息，不做任何自动换算",
                field=field,
            )
        raise InvalidParameter(
            f"{field}={value!r} 不是合法的无时区 ISO 8601 时间，"
            "期望格式 2020-04-18 / 2020-04-18T00:00:00 / 2020-04-18 00:00:00",
            field=field,
        )
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError as exc:  # pragma: no cover - 正则已挡住绝大多数情况
        raise InvalidParameter(f"{field}={value!r} 不是合法时间：{exc}", field=field) from exc
    if parsed.tzinfo is not None:  # pragma: no cover - 双保险
        raise TimezoneNotSupported(f"{field}={value!r} 带有时区信息", field=field)
    return parsed


@dataclass(frozen=True)
class TimeWindow:
    """左闭右开时间窗 `[start, end)`。"""

    start: datetime
    end: datetime

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def inclusive_end(self) -> datetime:
        """等价闭区间上界（`end - 1µs`），用于复用 Phase 1B 的闭区间查询。"""
        return self.end - timedelta(microseconds=1)

    def as_dict(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "duration_seconds": self.duration_seconds,
            "boundary": "[start, end)",
            "timezone": "naive (source timestamps carry no offset)",
        }


def make_time_window(
    start: str | datetime, end: str | datetime, *, max_window: timedelta = MAX_WINDOW
) -> TimeWindow:
    """解析并校验时间窗：end 必须大于 start，跨度不得超过上限。"""
    start_dt = (
        start if isinstance(start, datetime) else parse_naive_datetime(start, field="start_time")
    )
    end_dt = end if isinstance(end, datetime) else parse_naive_datetime(end, field="end_time")
    for name, value in (("start_time", start_dt), ("end_time", end_dt)):
        if value.tzinfo is not None:
            raise TimezoneNotSupported(f"{name} 带有时区信息，本服务只接受无时区时间", field=name)
    if end_dt <= start_dt:
        raise InvalidTimeRange(
            f"end_time ({end_dt.isoformat()}) 必须晚于 start_time ({start_dt.isoformat()})；"
            "区间为左闭右开 [start_time, end_time)",
            start_time=start_dt.isoformat(),
            end_time=end_dt.isoformat(),
        )
    if end_dt - start_dt > max_window:
        raise WindowTooLarge(
            f"时间窗跨度 {(end_dt - start_dt).days} 天，超过上限 {max_window.days} 天；"
            "请缩小时间范围后重试",
            max_window_days=max_window.days,
        )
    return TimeWindow(start=start_dt, end=end_dt)


def normalize_sensors(sensors: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """校验并去重传感器名称（保持登记表顺序，结果稳定可缓存）。"""
    if sensors is None:
        return ALL_SENSORS
    if isinstance(sensors, str):
        requested = [part.strip() for part in sensors.split(",") if part.strip()]
    else:
        requested = [str(part).strip() for part in sensors if str(part).strip()]
    if not requested:
        return ALL_SENSORS
    unknown = [name for name in requested if name not in SENSOR_KINDS]
    if unknown:
        raise UnsupportedSensor(
            f"不支持的传感器：{unknown}；可选值：{list(ALL_SENSORS)}",
            unsupported=unknown,
            supported=list(ALL_SENSORS),
        )
    wanted = set(requested)
    return tuple(name for name in ALL_SENSORS if name in wanted)


def split_sensors(sensors: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """拆成（模拟量, 数字量），保持输入顺序。"""
    analog = tuple(name for name in sensors if SENSOR_KINDS[name] == "analog")
    digital = tuple(name for name in sensors if SENSOR_KINDS[name] == "digital")
    return analog, digital


def validate_page(limit: int, offset: int) -> tuple[int, int]:
    """API 层分页参数校验：limit ∈ [1, 1000]，offset ≥ 0。"""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise InvalidParameter(f"limit 必须是整数，收到 {limit!r}", field="limit")
    if limit < 1:
        raise InvalidParameter(f"limit 必须 >= 1，收到 {limit}", field="limit")
    if limit > MAX_PAGE_SIZE:
        raise InvalidParameter(
            f"limit 不能超过 {MAX_PAGE_SIZE}，收到 {limit}；请使用 offset 翻页",
            field="limit",
            max_limit=MAX_PAGE_SIZE,
        )
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise InvalidParameter(f"offset 必须是整数，收到 {offset!r}", field="offset")
    if offset < 0:
        raise InvalidParameter(f"offset 必须 >= 0，收到 {offset}", field="offset")
    return limit, offset
