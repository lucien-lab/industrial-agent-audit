"""Trace 收集：从阶段 3 的 JSONL 文件 / 进程内存储 / 内联事件读取执行链。

设计要点：
- **只读**：不调用 Agent、不执行工具、不查询业务数据库；
- 文件是权威来源（`trace-<trace_id>.jsonl`），内存仅作为同一进程内的回退；
- trace_id 做字符集校验，拒绝路径穿越；
- 文件大小、事件条数、坏行都有上限与显式记录；
- 收集器不判断风险，只把原始事件交给规范化器。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable

from app.audit.config import AuditSettings, get_audit_settings
from app.audit.normalizer import normalize_trace
from app.audit.schemas import NormalizedTrace, TraceSourceKind

TRACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class TraceNotFoundError(LookupError):
    """指定 trace_id 在文件、内存与索引中都不存在。"""

    code = "audit_trace_not_found"


class TraceLoadError(RuntimeError):
    """Trace 存在但无法读取（过大、格式非法等）。"""

    code = "audit_trace_unreadable"


def validate_trace_id(trace_id: str) -> str:
    value = (trace_id or "").strip()
    if not TRACE_ID_RE.match(value):
        raise TraceLoadError("trace_id 只能包含字母、数字、点、下划线、连字符（最长 64）")
    return value


def trace_file_path(trace_id: str, settings: AuditSettings) -> Path:
    return settings.trace_dir / f"trace-{trace_id}.jsonl"


def _read_jsonl(path: Path, *, max_bytes: int) -> tuple[list[dict[str, Any]], bytes, list[dict[str, Any]]]:
    size = path.stat().st_size
    if size > max_bytes:
        raise TraceLoadError(f"Trace 文件 {size} 字节超过审计上限 {max_bytes}")
    raw = path.read_bytes()
    events: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            problems.append(
                {"code": "invalid_json_line", "line": line_number, "message": str(exc)[:200]}
            )
            continue
        if not isinstance(payload, dict):
            problems.append({"code": "non_object_event", "line": line_number})
            continue
        events.append(payload)
    return events, raw, problems


def load_trace_from_file(
    trace_id: str, *, settings: AuditSettings | None = None
) -> NormalizedTrace:
    settings = settings or get_audit_settings()
    trace_id = validate_trace_id(trace_id)
    path = trace_file_path(trace_id, settings)
    if not path.is_file():
        raise TraceNotFoundError(f"trace_id {trace_id!r} 的 JSONL 文件不存在：{path.name}")
    events, raw, problems = _read_jsonl(path, max_bytes=settings.max_trace_bytes)
    trace = normalize_trace(
        events,
        trace_id=trace_id,
        settings=settings,
        source=TraceSourceKind.FILE,
        source_ref=str(path.relative_to(settings.trace_dir.parent))
        if settings.trace_dir.parent in path.parents
        else path.name,
        raw_digest=hashlib.sha256(raw).hexdigest(),
    )
    trace.problems.extend(problems)
    return trace


def load_trace_from_memory(
    trace_id: str, *, settings: AuditSettings | None = None
) -> NormalizedTrace | None:
    """从阶段 3 的进程内 TraceStore 读取（同一进程内可用）。"""
    from app.agent.trace import get_trace_store

    summary = get_trace_store().get(validate_trace_id(trace_id))
    if summary is None:
        return None
    payload = summary.to_dict()
    trace = normalize_trace(
        payload.get("events") or [],
        trace_id=trace_id,
        settings=settings or get_audit_settings(),
        source=TraceSourceKind.MEMORY,
        source_ref="agent-trace-store",
        question=payload.get("question"),
        execution_status=payload.get("execution_status"),
        intent=payload.get("intent"),
    )
    return trace


def load_trace_from_events(
    trace_id: str,
    events: list[dict[str, Any]],
    *,
    settings: AuditSettings | None = None,
    source: TraceSourceKind = TraceSourceKind.INLINE,
    source_ref: str | None = None,
    question: str | None = None,
    execution_status: str | None = None,
    intent: str | None = None,
) -> NormalizedTrace:
    return normalize_trace(
        events,
        trace_id=trace_id,
        settings=settings or get_audit_settings(),
        source=source,
        source_ref=source_ref,
        question=question,
        execution_status=execution_status,
        intent=intent,
    )


def load_trace(
    trace_id: str,
    *,
    settings: AuditSettings | None = None,
    prefer_memory: bool = False,
    db_loader: Callable[[str], list[dict[str, Any]] | None] | None = None,
) -> NormalizedTrace:
    """按优先级读取 Trace：文件（权威）→ 内存 → 数据库索引（如提供）。

    数据库索引只作为**已有规范化事件的副本**，不是第二套真值来源；
    返回值中的 `source` 会明确标注实际来源。
    """
    settings = settings or get_audit_settings()
    trace_id = validate_trace_id(trace_id)
    errors: list[str] = []

    loaders: list[Callable[[], NormalizedTrace | None]] = []
    if prefer_memory:
        loaders.append(lambda: load_trace_from_memory(trace_id, settings=settings))
        loaders.append(lambda: load_trace_from_file(trace_id, settings=settings))
    else:
        loaders.append(lambda: load_trace_from_file(trace_id, settings=settings))
        loaders.append(lambda: load_trace_from_memory(trace_id, settings=settings))

    if db_loader is not None:
        def _from_db() -> NormalizedTrace | None:
            events = db_loader(trace_id)
            if not events:
                return None
            return load_trace_from_events(
                trace_id,
                events,
                settings=settings,
                source=TraceSourceKind.DATABASE,
                source_ref="audit_trace_events",
            )

        loaders.append(_from_db)

    for loader in loaders:
        try:
            trace = loader()
        except TraceNotFoundError as exc:
            errors.append(str(exc))
            continue
        if trace is not None:
            return trace
    raise TraceNotFoundError(
        f"trace_id {trace_id!r} 不存在（已尝试文件、内存与数据库索引）"
        + (f"；{errors[0]}" if errors else "")
    )


def discover_trace_ids(settings: AuditSettings) -> list[str]:
    """列出 Trace 目录中的 trace_id（按文件名排序，不做内容判断）。"""
    if not settings.trace_dir.is_dir():
        return []
    found: list[str] = []
    for path in sorted(settings.trace_dir.glob("trace-*.jsonl")):
        name = path.name
        if not name.startswith("trace-") or not name.endswith(".jsonl"):
            continue
        trace_id = name[len("trace-") : -len(".jsonl")]
        if TRACE_ID_RE.match(trace_id):
            found.append(trace_id)
    return found


def iter_trace_files(settings: AuditSettings) -> Iterable[Path]:
    if not settings.trace_dir.is_dir():
        return []
    return sorted(settings.trace_dir.glob("trace-*.jsonl"))


__all__ = [
    "TRACE_ID_RE",
    "TraceLoadError",
    "TraceNotFoundError",
    "discover_trace_ids",
    "iter_trace_files",
    "load_trace",
    "load_trace_from_events",
    "load_trace_from_file",
    "load_trace_from_memory",
    "trace_file_path",
    "validate_trace_id",
]
