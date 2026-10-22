"""Trace 规范化：把阶段 3 的异构事件转成稳定的审计事件契约。

只做“读取、排序、去重、脱敏、结构检查”，不做风险判断，也不重新执行工具。
任何缺失或歧义都记录为 `problems`，由规则决定是 UNKNOWN 还是 FAIL——
规范化阶段绝不把“没看到”当成“没问题”。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any

from app.audit.config import AuditSettings, SUPPORTED_TRACE_SCHEMA_VERSIONS
from app.audit.schemas import AuditEvent, NormalizedTrace, TraceSourceKind

_SECRET_KEY_RE = re.compile(
    r"(?i)(password|passwd|pwd|secret|api[_-]?key|apikey|token|authorization|cookie|"
    r"database_url|dsn|connection_string|private_key)"
)
_URL_WITH_CREDENTIALS_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^@\s]+)@")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|token)\s*[=:]\s*[^\s,;\"']+"
)


def redact(value: Any, *, depth: int = 0) -> Any:
    """递归脱敏：去掉密钥类键值、连接串凭据、Bearer Token。"""
    if depth > 8:
        return "<truncated-depth>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = _URL_WITH_CREDENTIALS_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}:***@", value)
        text = _BEARER_RE.sub(lambda m: f"{m.group(1)} ***", text)
        text = _SENSITIVE_VALUE_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
        return text
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:80]:
            key_text = str(key)
            if _SECRET_KEY_RE.search(key_text) and not key_text.endswith("_required"):
                out[key_text] = "<redacted>"
            else:
                out[key_text] = redact(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item, depth=depth + 1) for item in list(value)[:50]]
    return redact(str(value), depth=depth + 1)


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _digest(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _as_int(value: Any) -> int | None:
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_fingerprint(event: dict[str, Any]) -> str:
    payload = {
        "event_type": event.get("event_type"),
        "seq": event.get("seq"),
        "node": event.get("node"),
        "tool": event.get("tool"),
        "status": event.get("status"),
        "started_at": event.get("started_at"),
        "ended_at": event.get("ended_at"),
        "input_summary": event.get("input_summary"),
        "output_summary": event.get("output_summary"),
    }
    return _digest(payload)


def _extract_manifest(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    manifests: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") != "evidence":
            continue
        meta = event.get("meta") or {}
        manifest = meta.get("manifest") if isinstance(meta, dict) else None
        if isinstance(manifest, dict):
            manifests.append(manifest)
    return manifests[-1] if manifests else None


def normalize_trace(
    raw_events: list[dict[str, Any]],
    *,
    trace_id: str,
    settings: AuditSettings | None = None,
    question: str | None = None,
    execution_status: str | None = None,
    intent: str | None = None,
    agent_version: str | None = None,
    source: TraceSourceKind = TraceSourceKind.FILE,
    source_ref: str | None = None,
    raw_digest: str | None = None,
) -> NormalizedTrace:
    """规范化一条 Trace。输入事件可以缺失字段、乱序或重复。"""
    max_events = settings.max_trace_events if settings else 500
    problems: list[dict[str, Any]] = []
    warnings: list[str] = []

    events = [dict(item) for item in (raw_events or []) if isinstance(item, dict)]
    if not events:
        problems.append({"code": "empty_trace", "message": "Trace 不包含任何事件"})

    # 1) trace_id 一致性
    mismatched = [
        index
        for index, event in enumerate(events)
        if event.get("trace_id") not in (None, "", trace_id)
    ]
    if mismatched:
        problems.append(
            {
                "code": "trace_id_mismatch",
                "message": "存在 trace_id 不一致的事件，已保留但标记",
                "indices": mismatched[:20],
            }
        )

    # 2) 精确重复事件（同一行被写入两次）：去重并记录，避免影响预算统计
    seen: dict[str, int] = {}
    deduped: list[dict[str, Any]] = []
    duplicate_indices: list[int] = []
    for index, event in enumerate(events):
        fingerprint = _event_fingerprint(event)
        if fingerprint in seen:
            duplicate_indices.append(index)
            continue
        seen[fingerprint] = index
        deduped.append(event)
    if duplicate_indices:
        problems.append(
            {
                "code": "duplicate_events",
                "message": "检测到完全相同的事件被重复写入（已去重，仍记为结构问题）",
                "indices": duplicate_indices[:20],
            }
        )

    if len(deduped) > max_events:
        warnings.append(f"事件数 {len(deduped)} 超过审计上限 {max_events}，仅分析前 {max_events} 条")
        problems.append({"code": "event_limit_exceeded", "count": len(deduped)})
        deduped = deduped[:max_events]

    # 3) 序号与顺序
    seq_values = [_as_int(item.get("seq")) for item in deduped]
    missing_seq = [index for index, value in enumerate(seq_values) if value is None]
    if missing_seq:
        problems.append(
            {
                "code": "missing_sequence",
                "message": "部分事件缺少 seq，已按文件顺序恢复",
                "indices": missing_seq[:20],
            }
        )
    collision = [
        seq for seq, count in Counter(value for value in seq_values if value is not None).items()
        if count > 1
    ]
    if collision:
        problems.append(
            {
                "code": "sequence_collision",
                "message": "存在重复 seq 但内容不同的事件",
                "seq": sorted(collision)[:20],
            }
        )
    present = [value for value in seq_values if value is not None]
    if present != sorted(present):
        problems.append(
            {
                "code": "out_of_order_events",
                "message": "事件的记录顺序与 seq 不一致，已按 seq 排序",
            }
        )

    ordering = sorted(
        enumerate(deduped),
        key=lambda pair: (
            seq_values[pair[0]] if seq_values[pair[0]] is not None else 10**9,
            seq_values[pair[0]] is None,
            pair[0],
        ),
    )
    ordered = [item for _, item in ordering]
    source_indices = [index for index, _ in ordering]

    # 4) 构造稳定事件 ID（同 trace 内唯一）
    seen_ids: Counter[str] = Counter()
    audit_events: list[AuditEvent] = []
    for position, (event, source_index) in enumerate(zip(ordered, source_indices)):
        raw_seq = _as_int(event.get("seq"))
        base = f"E{raw_seq:04d}" if raw_seq is not None else f"E{position:04d}"
        if seen_ids[base]:
            base = f"{base}-{hashlib.sha1(_event_fingerprint(event).encode()).hexdigest()[:6]}"
        seen_ids[base] += 1
        status = event.get("status")
        audit_events.append(
            AuditEvent(
                event_id=base,
                trace_id=trace_id,
                sequence_number=position,
                event_type=str(event.get("event_type") or "unknown"),
                node_name=event.get("node"),
                tool_name=event.get("tool"),
                timestamp=event.get("ended_at") or event.get("started_at"),
                started_at=event.get("started_at"),
                duration_ms=(
                    float(event["duration_ms"])
                    if isinstance(event.get("duration_ms"), (int, float))
                    and not isinstance(event.get("duration_ms"), bool)
                    else None
                ),
                status=str(status) if status is not None else None,
                input_summary=redact(event.get("input_summary")),
                output_summary=redact(event.get("output_summary")),
                evidence_refs=[str(item)[:200] for item in (event.get("evidence_refs") or [])[:50]],
                error_type=event.get("error_category"),
                error_code=event.get("error_code"),
                source_index=source_index,
                raw_sequence_number=raw_seq,
                meta=redact(event.get("meta") or {}),
            )
        )

    # 5) 证据清单
    manifest = _extract_manifest(ordered)
    schema_version = "phase3-trace-v2" if manifest else "phase3-trace-v1"
    if schema_version not in SUPPORTED_TRACE_SCHEMA_VERSIONS:  # pragma: no cover - 防御式
        problems.append({"code": "unsupported_schema_version", "value": schema_version})
    if manifest:
        manifest_version = manifest.get("schema_version")
        if manifest_version != "agent-evidence-v1":
            problems.append(
                {
                    "code": "unknown_manifest_version",
                    "message": f"证据清单版本 {manifest_version!r} 未识别，相关规则将标记 UNKNOWN",
                }
            )
        if manifest.get("error"):
            problems.append(
                {"code": "manifest_build_error", "message": str(manifest.get("error"))[:200]}
            )

    run_start = next((event for event in audit_events if event.event_type == "run_start"), None)
    run_end = next(
        (event for event in reversed(audit_events) if event.event_type == "run_end"), None
    )
    intent_event = next((event for event in audit_events if event.event_type == "intent"), None)

    start_input = (run_start.input_summary if run_start and isinstance(run_start.input_summary, dict) else {}) or {}
    start_meta = (run_start.meta if run_start else {}) or {}
    end_status = run_end.status if run_end else None
    resolved_agent_version = (
        agent_version
        or start_meta.get("agent_version")
        or (manifest or {}).get("agent_version")
    )
    resolved_question = (
        question or (manifest or {}).get("question") or start_input.get("query") or ""
    )
    resolved_status = (
        execution_status
        or end_status
        or (manifest or {}).get("execution_status")
        or "unknown"
    )
    resolved_intent = intent or (intent_event.status if intent_event else None) or "unknown"

    if not run_start:
        problems.append({"code": "missing_run_start", "message": "Trace 缺少 run_start 事件"})
    if not run_end:
        problems.append({"code": "missing_run_end", "message": "Trace 缺少 run_end 事件"})
    if not resolved_question:
        problems.append({"code": "missing_question", "message": "无法从 Trace 恢复用户问题"})

    trace_digest = _digest(
        {
            "trace_id": trace_id,
            "events": [event.model_dump(mode="json") for event in audit_events],
            "manifest": manifest,
        }
    )

    return NormalizedTrace(
        trace_id=trace_id,
        question=str(resolved_question)[:2000],
        execution_status=str(resolved_status),
        intent=str(resolved_intent),
        agent_version=str(resolved_agent_version) if resolved_agent_version else None,
        trace_schema_version=schema_version,
        source=source,
        source_ref=source_ref,
        started_at=run_start.started_at if run_start else None,
        ended_at=run_end.timestamp if run_end else None,
        events=audit_events,
        evidence_manifest=manifest,
        trace_digest=trace_digest,
        raw_digest=raw_digest,
        event_count=len(audit_events),
        warnings=warnings,
        problems=problems,
    )


def pair_tool_events(trace: NormalizedTrace) -> dict[str, Any]:
    """按“同工具 + 出现顺序”配对 tool_call 与 tool_result。

    返回 `pairs`, `unpaired_calls`, `unpaired_results`。
    不猜测跨工具配对，也不把缺失结果当成成功。
    """
    pairs: list[dict[str, Any]] = []
    pending: dict[str, list[AuditEvent]] = {}
    unpaired_results: list[AuditEvent] = []
    for event in trace.events:
        if event.event_type == "tool_call":
            pending.setdefault(str(event.tool_name), []).append(event)
        elif event.event_type == "tool_result":
            queue = pending.get(str(event.tool_name)) or []
            if queue:
                call = queue.pop(0)
                pairs.append({"call": call, "result": event, "tool": event.tool_name})
            else:
                unpaired_results.append(event)
    unpaired_calls = [event for queue in pending.values() for event in queue]
    unpaired_calls.sort(key=lambda item: item.sequence_number)
    return {
        "pairs": pairs,
        "unpaired_calls": unpaired_calls,
        "unpaired_results": unpaired_results,
    }


def trace_to_event_dicts(trace: NormalizedTrace) -> list[dict[str, Any]]:
    """把规范化事件还原为阶段 3 风格的事件 dict（仅用于持久化索引）。"""
    payload: list[dict[str, Any]] = []
    for event in trace.events:
        payload.append(
            {
                "event_id": event.event_id,
                "trace_id": event.trace_id,
                "seq": event.raw_sequence_number,
                "event_type": event.event_type,
                "node": event.node_name,
                "tool": event.tool_name,
                "status": event.status,
                "started_at": event.started_at,
                "ended_at": event.timestamp,
                "duration_ms": event.duration_ms,
                "input_summary": event.input_summary,
                "output_summary": event.output_summary,
                "evidence_refs": event.evidence_refs,
                "error_category": event.error_type,
                "error_code": event.error_code,
                "meta": event.meta,
            }
        )
    return payload


__all__ = [
    "normalize_trace",
    "pair_tool_events",
    "redact",
    "trace_to_event_dicts",
]
