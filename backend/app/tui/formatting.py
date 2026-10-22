"""展示层辅助：状态配色、数值格式、Sparkline、事件详情渲染。

原则：
- 颜色只用于区分状态，不改变语义；`UNKNOWN` 永远显示为 UNKNOWN（黄），
  绝不与 `PASS`（绿）共用颜色或文案；
- 数值一律来自后端原始值，不做客户端重算或插值；
- 缺少证据时明确显示“缺少 evidence manifest”，不猜测。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------------- #
# 状态配色
# --------------------------------------------------------------------------- #
VERDICT_STYLE: dict[str, str] = {
    "pass": "bold green",
    "fail": "bold red",
    "unknown": "bold yellow",
    "not_applicable": "dim",
    "partial": "bold yellow",
    "error": "bold red",
}

VERDICT_LABEL: dict[str, str] = {
    "pass": "PASS",
    "fail": "FAIL",
    "unknown": "UNKNOWN",
    "not_applicable": "NOT_APPLICABLE",
    "partial": "PARTIAL",
    "error": "ERROR",
}

RISK_STYLE: dict[str, str] = {
    "none": "green",
    "low": "cyan",
    "medium": "yellow",
    "high": "bold red",
    "critical": "bold white on red",
    "unknown": "yellow",
}

SEVERITY_STYLE: dict[str, str] = {
    "info": "dim",
    "low": "cyan",
    "medium": "yellow",
    "high": "bold red",
    "critical": "bold white on red",
}

STATE_STYLE: dict[str, str] = {
    "ready": "bold green",
    "degraded": "bold yellow",
    "unavailable": "bold red",
    "unknown": "dim",
}

STATUS_STYLE: dict[str, str] = {
    "completed": "green",
    "answered": "green",
    "ok": "green",
    "partial": "yellow",
    "insufficient_evidence": "yellow",
    "budget_exhausted": "yellow",
    "refused": "yellow",
    "timeout": "red",
    "failed": "red",
    "error": "red",
    "empty": "yellow",
    "running": "cyan",
}

SCOPE_LABEL: dict[str, str] = {
    "metropt3_direct": "MetroPT-3 直接资料",
    "generic_compressor": "通用压缩机资料（非本设备专属）",
    "model_specific_other": "其他型号专属资料",
    "unknown": "来源适用性未知",
}


def verdict_label(verdict: str | None) -> str:
    return VERDICT_LABEL.get((verdict or "unknown").lower(), (verdict or "UNKNOWN").upper())


def verdict_style(verdict: str | None) -> str:
    return VERDICT_STYLE.get((verdict or "unknown").lower(), "yellow")


def risk_style(risk: str | None) -> str:
    return RISK_STYLE.get((risk or "unknown").lower(), "yellow")


def severity_style(severity: str | None) -> str:
    return SEVERITY_STYLE.get((severity or "info").lower(), "dim")


def state_style(state: str | None) -> str:
    return STATE_STYLE.get((state or "unknown").lower(), "dim")


def status_style(status: str | None) -> str:
    return STATUS_STYLE.get((status or "").lower(), "white")


def scope_label(scope: str | None) -> str:
    return SCOPE_LABEL.get(scope or "", scope or "未知")


# --------------------------------------------------------------------------- #
# 数值格式
# --------------------------------------------------------------------------- #
def fmt_number(value: float | int | None, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, int):
        return f"{value}"
    if value != value:  # NaN
        return "—"
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def fmt_ms(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    return f"{value:.0f}ms"


def fmt_ratio(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def fmt_seconds(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 3600:
        return f"{value / 3600:.2f}h"
    if value >= 60:
        return f"{value / 60:.1f}min"
    return f"{value:.0f}s"


def fmt_ts(value: datetime | str | None) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text


def truncate(text: str | None, limit: int = 80) -> str:
    if not text:
        return ""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def json_block(payload: Any, *, limit: int = 4000) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if len(text) > limit:
        text = text[:limit] + "\n…（已截断）"
    return text


# --------------------------------------------------------------------------- #
# Sparkline（真实采样值，不做插值）
# --------------------------------------------------------------------------- #
_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: Sequence[float | int | None], *, width: int | None = None) -> str:
    """把数值序列画成 Unicode 块状图。

    - `None` 表示该位置没有观测值，用空格占位（明确表示缺失，不做插值）；
    - 全部相同或只有一个点时退化为一层块，不虚构波动。
    """
    if not values:
        return ""
    data = list(values)
    if width and width > 0 and len(data) > width:
        # 等比抽样：保留首尾
        step = len(data) / width
        data = [data[min(len(data) - 1, int(i * step))] for i in range(width)]
    numeric = [float(v) for v in data if v is not None]
    if not numeric:
        return " " * len(data)
    low, high = min(numeric), max(numeric)
    span = high - low
    chars: list[str] = []
    for value in data:
        if value is None:
            chars.append(" ")
            continue
        if span == 0:
            chars.append(_BLOCKS[len(_BLOCKS) // 2])
            continue
        index = int((float(value) - low) / span * (len(_BLOCKS) - 1))
        chars.append(_BLOCKS[max(0, min(len(_BLOCKS) - 1, index))])
    return "".join(chars)


def coverage_note(sample_coverage: float | None, time_coverage: float | None) -> str:
    """根据覆盖率给出提示文案（不修改任何数值）。"""
    notes: list[str] = []
    if sample_coverage is not None and sample_coverage < 0.95:
        notes.append(
            f"样本覆盖率 {sample_coverage:.3f} < 0.95：窗口内存在采样缺口，"
            "统计仅覆盖实际存在的观测行（未插值）"
        )
    if time_coverage is not None and time_coverage < 0.95:
        notes.append(f"时间覆盖率 {time_coverage:.3f}：窗口内有缺口，缺失时长为估计值")
    if sample_coverage is not None and sample_coverage > 1.0:
        notes.append(f"样本覆盖率 {sample_coverage:.3f} > 1：记录比标称 10 s 间隔更密")
    return "；".join(notes)


def tool_call_lines(tool_calls: Iterable[Any]) -> list[str]:
    lines: list[str] = []
    for call in tool_calls:
        name = getattr(call, "tool", "?")
        status = getattr(call, "status", "?")
        elapsed = fmt_ms(getattr(call, "elapsed_ms", None))
        stage = getattr(call, "stage", "initial")
        reason = getattr(call, "reason", None)
        line = f"- `{name}` [{status}] {elapsed} · {stage}"
        if reason:
            line += f" · {truncate(reason, 60)}"
        lines.append(line)
    return lines


def event_detail_markdown(event: Any) -> str:
    """渲染单条 Trace 事件为 Markdown（字段均为后端白名单字段）。"""
    lines = [
        f"### {getattr(event, 'event_id', '?')} · {getattr(event, 'event_type', '?')}",
        "",
        f"- 节点：`{getattr(event, 'node_name', None) or '—'}`",
        f"- 工具：`{getattr(event, 'tool_name', None) or '—'}`",
        f"- 时间：{fmt_ts(getattr(event, 'timestamp', None))}",
        f"- 耗时：{fmt_ms(getattr(event, 'duration_ms', None))}",
        f"- 状态：{getattr(event, 'status', None) or '—'}",
    ]
    refs = getattr(event, "evidence_refs", None) or []
    if refs:
        lines.append(f"- 证据引用：{', '.join(str(r) for r in refs)}")
    error_type = getattr(event, "error_type", None)
    if error_type:
        lines.append(f"- 错误：{error_type} / {getattr(event, 'error_code', None) or '—'}")
    input_summary = getattr(event, "input_summary", None)
    output_summary = getattr(event, "output_summary", None)
    if input_summary:
        lines += ["", "**入参 / 摘要**", "```json", json_block(input_summary, limit=1600), "```"]
    if output_summary:
        lines += ["", "**输出 / 摘要**", "```json", json_block(output_summary, limit=2400), "```"]
    if not input_summary and not output_summary:
        lines += ["", "_该事件没有 input/output 摘要。_"]
    return "\n".join(lines)


__all__ = [
    "SCOPE_LABEL",
    "coverage_note",
    "event_detail_markdown",
    "fmt_ms",
    "fmt_number",
    "fmt_ratio",
    "fmt_seconds",
    "fmt_ts",
    "json_block",
    "risk_style",
    "scope_label",
    "severity_style",
    "sparkline",
    "state_style",
    "status_style",
    "tool_call_lines",
    "truncate",
    "verdict_label",
    "verdict_style",
]
