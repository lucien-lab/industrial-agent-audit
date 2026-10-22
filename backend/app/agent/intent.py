"""意图分析、槽位抽取与工具规划（确定性规则，不依赖 LLM 猜数值）。

为什么是规则而不是“让 LLM 决定调用哪些工具”：
1. 工具选择错误会直接导致数值错误或无关调用，7B 模型在中文工业问题上的稳定性不足；
2. 规则可复现、可单元测试、可在 Trace 中解释“为什么调用这个工具”；
3. 本项目的评估要求“不是同一个 LLM 的自我判断作为唯一正确性标准”。

同时保留 `llm` 规划器（`app.agent.planner_llm`）用于对比实验：它只能从注册表里的工具中选择，
参数仍要过 Pydantic 校验与预算裁剪，LLM 无法凭空构造 SQL 或新工具。

三类问题（验收要求）：
- `knowledge_only`：只检索知识库（如 TP2/TP3 的物理含义）；
- `data_only`：只查工业数据（如某日电流最大值）；
- `mixed_analysis`：数据 + 知识联合（如“结合运行数据与维护知识分析压力异常”）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.agent.config import AgentSettings
from app.agent.tools.registry import (
    TOOL_DATA_QUALITY,
    TOOL_EQUIPMENT_INFO,
    TOOL_FAULT_EVENTS,
    TOOL_KNOWLEDGE,
    TOOL_SENSOR_HISTORY,
    TOOL_SENSOR_STATISTICS,
)
from app.rag.ingest import detect_injection_flags

# --------------------------------------------------------------------------- #
# 时效/规模限制
# --------------------------------------------------------------------------- #
MAX_WINDOW_DAYS = 366

# --------------------------------------------------------------------------- #
# 时间表达
# --------------------------------------------------------------------------- #
_DATE_RE = re.compile(
    r"(?P<y>\d{4})\s*[-/.年]\s*(?P<m>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})\s*[日号]?"
)
_MONTH_DAY_RE = re.compile(r"(?<!\d)(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日号]")
_YEAR_MONTH_RE = re.compile(r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月(?!\s*\d)")
_TIME_RE = re.compile(r"(?<!\d)(?P<h>\d{1,2})\s*[:：]\s*(?P<mi>\d{2})(?!\d)")
_HOUR_RANGE_RE = re.compile(
    r"(?P<h1>\d{1,2})\s*(?:点|时)(?:\s*到|-|~|—|至)\s*(?P<h2>\d{1,2})\s*(?:点|时)"
)

# 明显越界 / 危险操作：直接拒绝，不调用任何工具
_DANGEROUS_RE = re.compile(
    r"(删|清空|清除|修改|更新|覆盖).{0,16}(数据|记录|表|库)|"
    r"(数据|记录|表|库|数据库).{0,16}(删除|删掉|清空|清除|改写|修改)|"
    r"\b(drop|delete|truncate|update|insert|alter|grant)\b|"
    r"执行.{0,12}(sql|语句|命令|select|insert|update|delete|drop)|"
    r"\bselect\b.{0,40}\bfrom\b|"
    r"(启动|停止|停机|关机|重启|打开|关闭).{0,6}(压缩机|设备|阀门|电磁阀|机器)|"
    r"(设置|调节|调整|修改).{0,6}(参数|阈值|压力|温度|转速)|"
    r"写入.{0,6}(数据库|表)",
    re.IGNORECASE,
)

# 数据类信号
_DATA_SIGNAL_RES: dict[str, re.Pattern[str]] = {
    "window": re.compile(
        r"\d{4}\s*[-/.年]\s*\d{1,2}|(?<!\d)\d{1,2}\s*月\s*\d{1,2}\s*[日号]|"
        r"今天|昨天|当天|当日|同期|之前|期间|时刻"
    ),
    "statistics": re.compile(
        r"最大值|最小值|平均|均值|标准差|中位|峰值|总和|累计|统计|分布|波动|趋势|"
        r"极值|最高|最低|占比|次数|时长|样本数"
    ),
    "anomaly": re.compile(
        r"是否异常|异常吗|有没有异常|是否正常|正常吗|异常|偏高|偏低|突增|突降|突变|"
        r"超限|超标|报警|异常波动|不寻常|下降|上升|升高|降低|增大|减小|波动|变化|偏移"
    ),
    "quality": re.compile(r"数据质量|覆盖率|缺口|缺失|采样|冻结|卡死|重复|质量位|断点"),
    "faults": re.compile(
        r"故障|失效|损坏|停机|维修|维护记录|报修|事件|事故|报警记录|故障事件|泄漏|leak"
    ),
    "history": re.compile(
        r"原始数据|原始记录|明细|逐条|每一条|时间序列|曲线|全部记录|所有采样值"
    ),
    "equipment_meta": re.compile(
        r"设备信息|设备档案|该设备|这台设备|设备编号|设备名称|观测时间|数据范围|有哪些传感器|"
        r"采集了哪些|测点有哪些|数据覆盖到|时间范围|观测范围|有效期|数据从"
    ),
}

# 知识类信号（技术资料、原理、排查思路）
_KNOWLEDGE_SIGNAL_RE = re.compile(
    r"含义|是什么|代表什么|表示什么|什么意思|物理含义|定义|原理|为什么|做什么|用来|"
    r"如何排查|如何判断|如何处理|如何预防|怎么排查|怎么判断|怎么处理|怎样排查|"
    r"排查|维护|保养|建议|指南|注意事项|规范|标准|资料|文档|依据|技术|作用|用途|影响|"
    r"会导致|泄漏|检测|查找|定位|动作|激活|触发|阈值|设定值|额定|量程|规格|"
    r"参考|介绍|说明|讲解|区别|分类|典型|常见|原因|导致|引起|通常|一般|哪些问题|关注哪些|"
    r"代表|什么状态|方法|措施|方案|步骤|三种|几种|哪几种|建议|策略"
)

# 传感器中英文映射（顺序稳定，便于断言）
_SENSOR_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("motor_current", ("电机电流", "电流", "motor_current", "motor current")),
    ("oil_temperature", ("油温", "润滑油温度", "oil_temperature", "oil temperature")),
    ("tp2", ("tp2",)),
    ("tp3", ("tp3",)),
    ("h1", ("h1",)),
    ("dv_pressure", ("卸荷压力", "dv_pressure", "dv pressure", "卸压")),
    ("reservoirs", ("储气罐压力", "储气压力", "储气罐", "储气", "reservoirs", "reservoir")),
    ("caudal_impulses", ("流量脉冲", "caudal_impulses", "caudal", "流量")),
    ("comp", ("压缩机状态", "comp状态", "comp ")),
    ("dv_eletric", ("电磁阀", "dv_eletric")),
    ("towers", ("冷却塔", "towers")),
    ("mpg", ("mpg",)),
    ("lps", ("低压开关", "lps")),
    ("pressure_switch", ("压力开关", "pressure_switch")),
    ("oil_level", ("油位", "oil_level")),
)

# 泛指“压力/温度”时默认取哪些测点（在工具返回中逐个列明，不做任何合并猜测）
_GENERIC_PRESSURE_SENSORS = ("tp2", "tp3", "dv_pressure", "reservoirs")
_GENERIC_VIBRATION_NOTE = "本数据集不含振动传感器，问题中的振动相关判断无法用现有数据回答"

SENSOR_LABELS_ZH: dict[str, str] = {
    "tp2": "TP2（压缩机压力测点）",
    "tp3": "TP3（压缩机出口压力测点）",
    "h1": "H1 测点",
    "dv_pressure": "DV_pressure（卸荷压力）",
    "reservoirs": "Reservoirs（储气罐压力）",
    "oil_temperature": "Oil_temperature（油温）",
    "motor_current": "Motor_current（电机电流）",
    "comp": "COMP（压缩机状态）",
    "dv_eletric": "DV_eletric（卸荷电磁阀状态）",
    "towers": "Towers（冷却塔状态）",
    "mpg": "MPG 状态位",
    "lps": "LPS（低压开关）",
    "pressure_switch": "Pressure_switch（压力开关）",
    "oil_level": "Oil_level（油位开关）",
    "caudal_impulses": "Caudal_impulses（流量脉冲）",
}


@dataclass(frozen=True)
class Slots:
    """从问题与请求中抽取的结构化槽位。"""

    equipment_code: str | None = None
    equipment_source: str = "none"  # request | question | default | none
    start_time: str | None = None
    end_time: str | None = None
    window_source: str = "none"  # request | question | none
    sensors: tuple[str, ...] | None = None
    sensor_source: str = "none"  # question | request | none
    asks_statistics: bool = False
    asks_anomaly: bool = False
    asks_faults: bool = False
    asks_quality: bool = False
    asks_history: bool = False
    asks_equipment_meta: bool = False
    notes: tuple[str, ...] = ()
    knowledge_query: str = ""
    data_signals: tuple[str, ...] = ()
    knowledge_signals: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Slots":
        """从（可序列化的）字典还原本对象；未知键忽略，保持向后兼容。"""
        known = {field_name for field_name in cls.__dataclass_fields__}
        payload: dict[str, Any] = {
            key: value for key, value in (data or {}).items() if key in known
        }
        if payload.get("sensors") is not None:
            payload["sensors"] = tuple(payload["sensors"])
        for key in ("notes", "data_signals", "knowledge_signals"):
            payload[key] = tuple(payload.get(key) or ())
        return cls(**payload)

    def as_dict(self) -> dict[str, Any]:
        return {
            "equipment_code": self.equipment_code,
            "equipment_source": self.equipment_source,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "window_source": self.window_source,
            "sensors": list(self.sensors) if self.sensors else None,
            "sensor_source": self.sensor_source,
            "asks_statistics": self.asks_statistics,
            "asks_anomaly": self.asks_anomaly,
            "asks_faults": self.asks_faults,
            "asks_quality": self.asks_quality,
            "asks_history": self.asks_history,
            "asks_equipment_meta": self.asks_equipment_meta,
            "notes": list(self.notes),
            "knowledge_query": self.knowledge_query,
            "data_signals": list(self.data_signals),
            "knowledge_signals": list(self.knowledge_signals),
        }


@dataclass(frozen=True)
class IntentResult:
    kind: str
    reason: str
    signals: tuple[str, ...] = ()
    needs_time_window: bool = False
    clarification: str | None = None
    security_flags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "signals": list(self.signals),
            "needs_time_window": self.needs_time_window,
            "clarification": self.clarification,
            "security_flags": list(self.security_flags),
        }


@dataclass(frozen=True)
class PlannedCall:
    """一条计划中的工具调用。"""

    tool: str
    params: dict[str, Any]
    reason: str
    depends_on_equipment: bool = True
    stage: str = "initial"  # initial | followup

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "params": self.params,
            "reason": self.reason,
            "depends_on_equipment": self.depends_on_equipment,
            "stage": self.stage,
        }


# --------------------------------------------------------------------------- #
# 时间窗解析
# --------------------------------------------------------------------------- #
def _fmt(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def _safe_date(year: int, month: int, day: int) -> datetime | None:
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def _month_end(year: int, month: int) -> datetime:
    if month == 12:
        return datetime(year + 1, 1, 1)
    return datetime(year, month + 1, 1)


@dataclass(frozen=True)
class _DateHit:
    date: datetime
    has_time: bool


def _find_dates(question: str, default_year: int, notes: list[str]) -> list[_DateHit]:
    hits: list[tuple[int, int, int, int, int]] = []  # (pos, y, m, d, len)
    consumed: list[tuple[int, int]] = []

    for match in _DATE_RE.finditer(question):
        y, m, d = (int(match.group("y")), int(match.group("m")), int(match.group("d")))
        value = _safe_date(y, m, d)
        if value is None:
            notes.append(f"问题中的日期 {match.group(0)!r} 不存在，已忽略")
            continue
        hits.append((match.start(), y, m, d, match.end() - match.start()))
        consumed.append((match.start(), match.end()))

    for match in _MONTH_DAY_RE.finditer(question):
        if any(start <= match.start() < end for start, end in consumed):
            continue
        m, d = int(match.group("m")), int(match.group("d"))
        value = _safe_date(default_year, m, d)
        if value is None:
            notes.append(f"问题中的日期 {match.group(0)!r} 不存在，已忽略")
            continue
        notes.append(f"问题未给出年份，按数据集年份 {default_year} 解释「{match.group(0).strip()}」")
        hits.append((match.start(), default_year, m, d, match.end() - match.start()))

    if hits:
        times = [(int(t.group("h")), int(t.group("mi"))) for t in _TIME_RE.finditer(question)]
        result: list[_DateHit] = []
        for index, (_pos, y, m, d, _length) in enumerate(sorted(hits)):
            date = datetime(y, m, d)
            has_time = False
            if index < len(times):
                hour, minute = times[index]
                if hour < 24 and minute < 60:
                    date = date.replace(hour=hour, minute=minute)
                    has_time = True
            result.append(_DateHit(date=date, has_time=has_time))
        return result

    # 只有“2020年4月”这类月份表达
    month_match = _YEAR_MONTH_RE.search(question)
    if month_match and 1 <= int(month_match.group("m")) <= 12:
        year, month = int(month_match.group("y")), int(month_match.group("m"))
        notes.append(f"问题只给出年月「{month_match.group(0).strip()}」，按整月 [月首, 次月首) 处理")
        return [
            _DateHit(date=datetime(year, month, 1), has_time=False),
            _DateHit(date=_month_end(year, month), has_time=True),
        ]
    return []


def _window_from_dates(hits: list[_DateHit], notes: list[str]) -> tuple[str, str] | None:
    if not hits:
        return None
    if len(hits) == 1:
        hit = hits[0]
        if hit.has_time:
            start = hit.date
            end = start + timedelta(days=1)
            notes.append("只给出一个时间点，窗口取该时间点起 24 小时")
        else:
            start = hit.date
            end = start + timedelta(days=1)
        return _fmt(start), _fmt(end)
    first, last = hits[0], hits[-1]
    start = first.date
    if last.has_time:
        end = last.date
    else:
        end = last.date + timedelta(days=1)
    if end <= start:
        notes.append("问题中的结束时间不晚于开始时间，已忽略问题内时间表达")
        return None
    return _fmt(start), _fmt(end)


def _hour_range_window(hits: list[_DateHit], question: str) -> tuple[str, str] | None:
    """支持“4 月 17 日 8 点到 12 点”这类小时区间。"""
    if len(hits) != 1:
        return None
    match = _HOUR_RANGE_RE.search(question)
    if not match:
        return None
    h1, h2 = int(match.group("h1")), int(match.group("h2"))
    if not (0 <= h1 < 24 and 0 <= h2 <= 24 and h2 > h1):
        return None
    base = hits[0].date.replace(hour=0, minute=0, second=0, microsecond=0)
    return _fmt(base + timedelta(hours=h1)), _fmt(base + timedelta(hours=h2))


def parse_window(
    question: str, request: dict[str, Any], settings: AgentSettings, notes: list[str]
) -> tuple[str | None, str | None, str]:
    """返回 (start, end, source)。问题内的时间表达优先于请求默认值。"""
    hits = _find_dates(question, settings.default_year, notes)
    if hits:
        hour_window = _hour_range_window(hits, question)
        if hour_window:
            return hour_window[0], hour_window[1], "question"
        window = _window_from_dates(hits, notes)
        if window:
            return window[0], window[1], "question"

    req_start = request.get("start_time")
    req_end = request.get("end_time")
    if req_start and req_end:
        return str(req_start), str(req_end), "request"
    if req_start or req_end:
        notes.append("请求只提供了 start_time / end_time 之一，需要同时提供才能定义时间窗")
    return None, None, "none"


def baseline_window(start: str, end: str) -> tuple[str, str] | None:
    """同长度的前一窗口（用于“是否异常”的确定性对照，不做任何趋势外推）。"""
    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError:
        return None
    span = end_dt - start_dt
    if span <= timedelta(0) or span > timedelta(days=MAX_WINDOW_DAYS):
        return None
    return _fmt(start_dt - span), _fmt(start_dt)


# --------------------------------------------------------------------------- #
# 传感器与设备抽取
# --------------------------------------------------------------------------- #
def parse_sensors(question: str, request: dict[str, Any], notes: list[str]) -> tuple[tuple[str, ...] | None, str]:
    explicit: list[str] = []
    lowered = question.lower()
    for sensor, hints in _SENSOR_HINTS:
        if any(hint in lowered for hint in hints):
            if sensor not in explicit:
                explicit.append(sensor)
    if "压力" in question and not any(
        sensor in explicit for sensor in ("tp2", "tp3", "dv_pressure", "reservoirs")
    ):
        for sensor in _GENERIC_PRESSURE_SENSORS:
            if sensor not in explicit:
                explicit.append(sensor)
        notes.append("问题泛指“压力”，按 TP2/TP3/DV_pressure/Reservoirs 四个测点分别统计")
    if "温度" in question and "oil_temperature" not in explicit:
        explicit.append("oil_temperature")
        notes.append("问题泛指“温度”，使用 Oil_temperature（油温）测点")
    if "振动" in question:
        notes.append(_GENERIC_VIBRATION_NOTE)

    requested = request.get("sensors")
    if requested:
        merged = list(dict.fromkeys(list(requested) + explicit))
        return tuple(merged), "request" if not explicit else "request+question"
    if explicit:
        return tuple(explicit), "question"
    return None, "none"


# --------------------------------------------------------------------------- #
# 意图分类
# --------------------------------------------------------------------------- #
def _matches(pattern_map: dict[str, re.Pattern[str]], text: str) -> list[str]:
    return [name for name, pattern in pattern_map.items() if pattern.search(text)]


def analyze(
    question: str,
    request: dict[str, Any],
    settings: AgentSettings,
) -> tuple[IntentResult, Slots]:
    """意图分析 + 槽位抽取（纯函数，便于单测）。"""
    notes: list[str] = []
    normalized = " ".join(str(question).split())
    lowered = normalized.lower()

    injection = detect_injection_flags(normalized)
    dangerous = _DANGEROUS_RE.search(normalized)
    if injection or dangerous:
        flags = list(injection)
        if dangerous:
            flags.append(dangerous.group(0)[:40])
        return (
            IntentResult(
                kind="out_of_scope",
                reason=(
                    "问题包含试图改变系统规则的内容或要求执行写操作/设备控制，"
                    "本系统只做只读的数据与知识查询"
                ),
                signals=tuple(flags),
                security_flags=tuple(flags),
            ),
            Slots(notes=("已拒绝并记录可疑输入；未调用任何工具",)),
        )

    # 纯越界（天气/编程/创作等）：复用 Phase 2 的确定性护栏
    from app.rag.answer import detect_out_of_scope

    out_of_scope = detect_out_of_scope(normalized)

    equipment_code = request.get("equipment_id") or None
    equipment_source = "request" if equipment_code else "none"
    if not equipment_code:
        match = re.search(r"metropt[\w-]*|apu[\w-]*", lowered)
        if match:
            equipment_source = "question"  # 交由工具校验真实存在性，不在此处伪造
        equipment_code = None
    if not equipment_code:
        equipment_code = settings.default_equipment_code
        equipment_source = "default"
        notes.append(
            f"请求未指定 equipment_id，使用默认设备 {settings.default_equipment_code}"
            "（数据集中唯一的逻辑设备编号）"
        )

    start, end, window_source = parse_window(normalized, request, settings, notes)
    sensors, sensor_source = parse_sensors(normalized, request, notes)

    data_signals = _matches(_DATA_SIGNAL_RES, normalized)
    knowledge_signals = ["knowledge_terms"] if _KNOWLEDGE_SIGNAL_RE.search(normalized) else []
    if window_source == "question" and "window" not in data_signals:
        data_signals.append("window")

    slots = Slots(
        equipment_code=equipment_code,
        equipment_source=equipment_source,
        start_time=start,
        end_time=end,
        window_source=window_source,
        sensors=sensors,
        sensor_source=sensor_source,
        asks_statistics="statistics" in data_signals,
        asks_anomaly="anomaly" in data_signals,
        asks_faults="faults" in data_signals,
        asks_quality="quality" in data_signals,
        asks_history="history" in data_signals,
        asks_equipment_meta="equipment_meta" in data_signals,
        notes=tuple(notes),
        knowledge_query=normalized[:300],
        data_signals=tuple(data_signals),
        knowledge_signals=tuple(knowledge_signals),
    )

    # 只有“故障/泄漏”信号但没有时间上下文：属于知识问题（如“泄漏如何检测”），
    # 不应在缺少时间范围时触发数据查询
    temporal = (
        window_source == "question"
        or slots.asks_statistics
        or slots.asks_anomaly
        or slots.asks_quality
        or slots.asks_history
    )
    demoted_faults = False
    if data_signals == ["faults"] and not temporal:
        data_signals = []
        demoted_faults = True

    if out_of_scope:
        # 越界问题（天气/编程/创作）不是安全事件：只记在 signals，不当作注入
        return (
            IntentResult(
                kind="out_of_scope",
                reason="问题不属于工业设备数据/知识范围",
                signals=tuple(out_of_scope),
            ),
            slots,
        )

    data_intent = bool(data_signals)
    knowledge_intent = bool(knowledge_signals)
    needs_window = (
        slots.asks_statistics
        or slots.asks_anomaly
        or slots.asks_faults
        or slots.asks_quality
        or slots.asks_history
    )

    if data_intent and knowledge_intent:
        kind, reason = (
            "mixed_analysis",
            "问题同时要求设备运行数据与技术知识，需要数据工具 + 知识检索联合回答",
        )
    elif data_intent:
        kind, reason = "data_only", "问题要求具体数据/统计结果，只需查询工业数据"
    elif knowledge_intent or demoted_faults:
        kind, reason = (
            "knowledge_only",
            "问题是技术知识类（或没有时间上下文的故障类问题），只需检索知识库文档",
        )
    elif start:
        kind, reason = "data_only", "问题提到具体时间，按数据查询处理"
    else:
        return (
            IntentResult(
                kind="unsupported",
                reason="既无法识别为数据查询，也无法识别为技术知识问题",
                signals=tuple(data_signals + knowledge_signals),
                clarification=(
                    "无法判断问题需要查询设备数据还是技术资料。请补充：设备编号、"
                    "关注的传感器（如电机电流/油温/储气压力）与时间范围，或用更具体的"
                    "技术问题描述（例如“TP2 的含义是什么”）。"
                ),
            ),
            slots,
        )

    # 兜底：数据/混合意图但没抽到具体动作 → 计划会按统计处理，因此同样需要时间窗
    if kind in ("data_only", "mixed_analysis") and not needs_window and not slots.asks_equipment_meta:
        needs_window = True
    if kind == "knowledge_only":
        needs_window = False  # 纯知识问题不依赖时间窗

    clarification = None
    if needs_window and window_source == "none":
        clarification = (
            "缺少时间范围，无法进行数据查询。请在问题中给出无时区的日期"
            "（如 2020年4月17日 或 2020-04-17），或通过 start_time/end_time 传入窗口。"
        )
    return (
        IntentResult(
            kind=kind,
            reason=reason,
            signals=tuple(data_signals + knowledge_signals),
            needs_time_window=needs_window and window_source == "none",
            clarification=clarification,
        ),
        slots,
    )


# --------------------------------------------------------------------------- #
# 工具规划
# --------------------------------------------------------------------------- #
def _stats_params(slots: Slots, start: str, end: str) -> dict[str, Any]:
    params: dict[str, Any] = {
        "equipment_code": slots.equipment_code,
        "start_time": start,
        "end_time": end,
    }
    if slots.sensors:
        params["sensors"] = list(slots.sensors)
    return params


def build_plan(
    intent: IntentResult,
    slots: Slots,
    settings: AgentSettings,
) -> list[PlannedCall]:
    """按意图生成有序工具调用计划（不做“全部工具都调一遍”）。"""
    if intent.kind in ("out_of_scope", "unsupported"):
        return []

    knowledge_call = PlannedCall(
        tool=TOOL_KNOWLEDGE,
        params={
            "query": slots.knowledge_query,
            "top_k": min(settings.max_knowledge_chunks, settings.max_knowledge_top_k),
        },
        reason="检索 Phase 2 知识库原始片段，作为技术知识依据",
        depends_on_equipment=False,
    )

    if intent.kind == "knowledge_only":
        return [knowledge_call]

    calls: list[PlannedCall] = []
    # 缺少时间窗：只查设备档案，用真实观测范围帮助用户补齐参数（不猜一个窗口）
    if intent.needs_time_window:
        calls.append(
            PlannedCall(
                tool=TOOL_EQUIPMENT_INFO,
                params={"equipment_code": slots.equipment_code},
                reason="缺少时间范围，先取设备观测跨度以便给出可用的时间提示",
            )
        )
        if intent.kind == "mixed_analysis":
            calls.append(knowledge_call)
        return calls

    wants_data_context = (
        intent.kind == "mixed_analysis"
        or slots.asks_equipment_meta
        or slots.asks_anomaly
    )
    if wants_data_context:
        calls.append(
            PlannedCall(
                tool=TOOL_EQUIPMENT_INFO,
                params={"equipment_code": slots.equipment_code},
                reason="确认设备存在并给出数据可用范围（异常判断需要基准时间跨度）",
            )
        )

    start, end = slots.start_time, slots.end_time
    if not (start and end):  # 防御性兜底：任何路径都不允许在缺窗口时构造数据查询
        calls.append(
            PlannedCall(
                tool=TOOL_EQUIPMENT_INFO,
                params={"equipment_code": slots.equipment_code},
                reason="无法确定时间窗，先取设备观测跨度以便给出可用时间提示",
            )
        )
        if intent.kind == "mixed_analysis":
            calls.append(knowledge_call)
        return calls
    if slots.asks_statistics or slots.asks_anomaly:
        calls.append(
            PlannedCall(
                tool=TOOL_SENSOR_STATISTICS,
                params=_stats_params(slots, start, end),
                reason="计算目标时间窗内的统计量（min/max/mean/std 与数字量状态）",
            )
        )
    if slots.asks_history:
        calls.append(
            PlannedCall(
                tool=TOOL_SENSOR_HISTORY,
                params={**_stats_params(slots, start, end), "limit": min(100, settings.max_history_rows)},
                reason="按用户要求返回原始观测记录（限行数）",
            )
        )
    if slots.asks_quality or slots.asks_anomaly:
        calls.append(
            PlannedCall(
                tool=TOOL_DATA_QUALITY,
                params={"equipment_code": slots.equipment_code, "start_time": start, "end_time": end},
                reason="异常判断必须先确认采样覆盖率、缺口与冻结候选，避免把缺数据当成停机",
            )
        )
    if slots.asks_faults or slots.asks_anomaly:
        calls.append(
            PlannedCall(
                tool=TOOL_FAULT_EVENTS,
                params={"equipment_code": slots.equipment_code, "start_time": start, "end_time": end,
                        "limit": min(10, settings.max_fault_events)},
                reason="查询与时间窗相交的官方故障事件（区间级事实）",
            )
        )
    if intent.kind == "mixed_analysis":
        calls.append(knowledge_call)

    if not calls:  # 兜底：数据意图但没抽到具体动作 → 做统计
        calls.append(
            PlannedCall(
                tool=TOOL_SENSOR_STATISTICS,
                params=_stats_params(slots, start, end),
                reason="问题指向数据但未指定具体动作，按该时间窗统计处理",
            )
        )
    return calls


def apply_budget(
    calls: list[PlannedCall], settings: AgentSettings
) -> tuple[list[PlannedCall], list[str]]:
    """按最大工具调用次数裁剪计划（保留顺序，明确记录被裁剪项）。"""
    warnings: list[str] = []
    if len(calls) > settings.max_tool_calls:
        dropped = calls[settings.max_tool_calls :]
        warnings.append(
            f"计划包含 {len(calls)} 次工具调用，超过上限 {settings.max_tool_calls}；"
            f"已裁剪：{[call.tool for call in dropped]}"
        )
        return calls[: settings.max_tool_calls], warnings
    return list(calls), warnings


__all__ = [
    "MAX_WINDOW_DAYS",
    "IntentResult",
    "PlannedCall",
    "SENSOR_LABELS_ZH",
    "Slots",
    "analyze",
    "apply_budget",
    "baseline_window",
    "build_plan",
    "parse_sensors",
    "parse_window",
]
