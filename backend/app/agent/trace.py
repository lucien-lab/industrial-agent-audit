"""结构化执行事件（最小 Trace 契约，供后续 Agent-Probe 消费）。

本阶段**只**建设最小事件流，不做风险评分、不做 LLM 审计、不落库（不改数据库结构）。

设计要点：
1. 事件是结构化 dict（`TraceEvent`），不是打印日志；日志只是附加的可观测手段；
2. 每条事件带 `trace_id` + 自增 `seq`，可完整重放一次 Agent 执行的顺序与耗时；
3. 工具输入/输出只保存**摘要**（截断 + 白名单字段），不包含连接串、密码、API Key；
4. 事件流同时写入进程内存储（供 `GET /api/v1/agent/traces/{id}` 查询）
   与可选的 JSONL 文件（供 Agent-Probe 离线消费）；
5. 未设置 `AGENT_TRACE_WRITE_FILES=true` 时只保留内存，测试不产生磁盘副作用。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 摘要长度上限：防止把整段 chunk / 大表写进事件流
SUMMARY_LIMIT = 480

EVENT_TYPES = (
    "run_start",
    "intent",
    "plan",
    "node_start",
    "node_end",
    "tool_call",
    "tool_result",
    "tool_retry",
    "decision",
    "answer",
    "run_end",
)

# 允许出现在事件中的摘要键（白名单；其它键在摘要化时直接丢弃）
_SAFE_SUMMARY_KEYS = (
    "tool",
    "query",
    "equipment_code",
    "start_time",
    "end_time",
    "sensors",
    "rows",
    "returned",
    "total",
    "count",
    "status",
    "method",
    "top_k",
    "labels",
    "intent",
    "reason",
    "rounds",
    "tool_calls",
    "step",
    "planned",
    "remaining",
    "answer_status",
    "mode",
    "warnings",
    "error_category",
    "error_code",
    "signal",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def summarize(payload: Any, *, limit: int = SUMMARY_LIMIT) -> Any:
    """把任意结构化对象压缩成安全摘要。

    - dict：只保留白名单键（其余键丢弃，避免把连接串/整段原文写进事件）；
    - list/tuple：截断到前 5 项，长度单独记录；
    - 字符串：截断到 `limit` 字符；
    - 其它标量：原样返回。
    """
    if payload is None:
        return None
    if isinstance(payload, str):
        text = payload.replace("\n", " ").strip()
        return text[:limit] + ("…" if len(text) > limit else "")
    if isinstance(payload, (int, float, bool)):
        return payload
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for key, value in payload.items():
            if key not in _SAFE_SUMMARY_KEYS:
                continue
            out[key] = summarize(value, limit=limit)
        return out
    if isinstance(payload, (list, tuple)):
        items = [summarize(item, limit=limit) for item in list(payload)[:5]]
        if len(payload) > 5:
            items.append(f"…(+{len(payload) - 5})")
        return items
    return summarize(str(payload), limit=limit)


@dataclass
class TraceEvent:
    """一条结构化执行事件。"""

    trace_id: str
    seq: int
    event_type: str
    node: str | None
    tool: str | None
    status: str | None
    started_at: str
    ended_at: str
    duration_ms: float
    input_summary: Any = None
    output_summary: Any = None
    error_category: str | None = None
    error_code: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TraceSummary:
    """一次运行的摘要（`GET /api/v1/agent/traces/{trace_id}` 的返回体）。"""

    trace_id: str
    question: str
    execution_status: str
    intent: str
    tools_used: list[str]
    tool_calls: int
    rounds: int
    total_duration_ms: float
    started_at: str
    ended_at: str
    events: list[dict[str, Any]]
    evidence_refs: list[str]
    errors: list[dict[str, Any]]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_summary_dict(self) -> dict[str, Any]:
        """不含完整事件列表的紧凑视图（HTTP 默认返回）。"""
        payload = self.to_dict()
        payload["event_count"] = len(self.events)
        payload.pop("events")
        return payload


class TraceStore:
    """进程内 Trace 存储（有界 LRU），线程安全。"""

    def __init__(self, capacity: int = 200) -> None:
        self.capacity = max(1, capacity)
        self._items: OrderedDict[str, TraceSummary] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, summary: TraceSummary) -> None:
        with self._lock:
            self._items[summary.trace_id] = summary
            self._items.move_to_end(summary.trace_id)
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)

    def get(self, trace_id: str) -> TraceSummary | None:
        with self._lock:
            summary = self._items.get(trace_id)
            if summary is not None:
                self._items.move_to_end(trace_id)
            return summary

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:  # pragma: no cover - 仅用于测试断言
        return len(self._items)


# 全局存储：API 进程内复用；测试可用 `get_trace_store().clear()` 隔离
_TRACE_STORE = TraceStore()


def get_trace_store() -> TraceStore:
    return _TRACE_STORE


class TraceRecorder:
    """一次 Agent 执行的事件记录器。"""

    def __init__(
        self,
        *,
        trace_id: str,
        trace_dir: Path | None = None,
        write_files: bool = False,
        store: TraceStore | None = None,
    ) -> None:
        self.trace_id = trace_id
        self.trace_dir = trace_dir
        self.write_files = write_files and trace_dir is not None
        self.store = store if store is not None else get_trace_store()
        self.started_at = _now()
        self._started_perf = time.perf_counter()
        self._seq = 0
        self.events: list[TraceEvent] = []
        self._lock = threading.Lock()
        self._file_path: Path | None = None
        if self.write_files:
            try:  # 目录不可写不应让 Agent 失败，但要在事件里留痕
                self.trace_dir.mkdir(parents=True, exist_ok=True)
                self._file_path = self.trace_dir / f"trace-{trace_id}.jsonl"
            except OSError:  # pragma: no cover - 依赖运行环境
                self._file_path = None

    # ------------------------------------------------------------------ #
    @staticmethod
    def new_trace_id() -> str:
        return uuid.uuid4().hex

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._started_perf) * 1000, 3)

    def record(
        self,
        event_type: str,
        *,
        node: str | None = None,
        tool: str | None = None,
        status: str | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        duration_ms: float | None = None,
        input_summary: Any = None,
        output_summary: Any = None,
        error_category: str | None = None,
        error_code: str | None = None,
        evidence_refs: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> TraceEvent:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"未知事件类型 {event_type!r}")
        with self._lock:
            self._seq += 1
            seq = self._seq
        end = ended_at or _now()
        start = started_at or end
        event = TraceEvent(
            trace_id=self.trace_id,
            seq=seq,
            event_type=event_type,
            node=node,
            tool=tool,
            status=status,
            started_at=_iso(start),
            ended_at=_iso(end),
            duration_ms=round(
                duration_ms
                if duration_ms is not None
                else (end - start).total_seconds() * 1000,
                3,
            ),
            input_summary=summarize(input_summary),
            output_summary=summarize(output_summary),
            error_category=error_category,
            error_code=error_code,
            evidence_refs=list(evidence_refs or []),
            meta=meta or {},
        )
        self.events.append(event)
        self._append_file(event)
        return event

    def _append_file(self, event: TraceEvent) -> None:
        if self._file_path is None:
            return
        try:
            with self._file_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        except OSError:  # pragma: no cover - 依赖运行环境
            self._file_path = None

    # ------------------------------------------------------------------ #
    def finish(
        self,
        *,
        question: str,
        execution_status: str,
        intent: str,
        tools_used: list[str],
        tool_calls: int,
        rounds: int,
        evidence_refs: list[str],
        errors: list[dict[str, Any]],
        warnings: list[str],
    ) -> TraceSummary:
        """写入结束事件并把摘要存入内存存储。"""
        self.record(
            "run_end",
            node="finalize",
            status=execution_status,
            output_summary={"status": execution_status, "tool_calls": tool_calls},
            evidence_refs=evidence_refs,
        )
        summary = TraceSummary(
            trace_id=self.trace_id,
            question=question,
            execution_status=execution_status,
            intent=intent,
            tools_used=list(tools_used),
            tool_calls=tool_calls,
            rounds=rounds,
            total_duration_ms=self.elapsed_ms(),
            started_at=_iso(self.started_at),
            ended_at=_iso(_now()),
            events=[event.to_dict() for event in self.events],
            evidence_refs=list(evidence_refs),
            errors=list(errors),
            warnings=list(warnings),
        )
        self.store.put(summary)
        return summary


def redact_url(value: str) -> str:
    """把连接串中的密码替换为 ***（日志/事件兜底用）。"""
    from app.core.config import mask_password

    return mask_password(value)


__all__ = [
    "EVENT_TYPES",
    "SUMMARY_LIMIT",
    "TraceEvent",
    "TraceRecorder",
    "TraceStore",
    "TraceSummary",
    "get_trace_store",
    "redact_url",
    "summarize",
]
