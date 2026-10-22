"""阶段 4 最小扩展：为离线审计生成**有界、脱敏、可复现**的证据清单。

为什么需要它：
阶段 3 的 Trace 事件只保存摘要（白名单键 + 480 字符上限），足以支持
`GET /agent/traces/{id}`，但不足以让独立审计器判断“回答里的数值是否来自工具证据”、
“统计口径是否写错”、“引用 Chunk 是否真实存在”。如果审计器为了核对这些信息重新
执行工具，就不再是基于冻结 Trace 的离线审计。

因此这里在 `finalize` 节点追加**一个** `evidence` 事件（不改动任何已有事件与字段）：
- 只包含工具已经返回的结构化证据（不重新查询、不重新生成）；
- 明确长度上限，文档片段只保留有限摘录 + 哈希；
- 不包含连接串、密码、API Key（构建时只读白名单字段）；
- 旧 Trace 没有该事件时，审计器必须把依赖它的规则标为 UNKNOWN，而不是 PASS。

该模块**不属于**审计器：审计器不修改业务 Trace，只消费它。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

EVIDENCE_MANIFEST_VERSION = "agent-evidence-v1"

MAX_ANSWER_CHARS = 8000
MAX_QUOTE_CHARS = 600
MAX_FACT_ITEMS = 60
MAX_REFERENCE_ITEMS = 20
MAX_TEXT_CHARS = 800

_URL_RE = re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|redis|amqp|https?)://[^\s\"']+")
_SECRET_RE = re.compile(
    r"(?i)(password|passwd|pwd|secret|api[_-]?key|token|authorization|cookie)\s*[=:]\s*\S+"
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_text(value: Any, *, limit: int = MAX_TEXT_CHARS) -> str:
    """截断 + 去除连接串/密钥样式文本（兜底，不依赖调用方）。"""
    text = " ".join(str(value or "").split())
    text = _URL_RE.sub("<redacted-url>", text)
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _scalar(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, str)):
        return value if not isinstance(value, str) else value[:200]
    if isinstance(value, (list, tuple)):
        return [_scalar(item) for item in list(value)[:8]]
    if isinstance(value, dict):
        return {str(key)[:40]: _scalar(item) for key, item in list(value.items())[:12]}
    return safe_text(value, limit=120)


def _window(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    payload = {
        key: value.get(key)
        for key in ("start", "end", "duration_seconds", "boundary")
        if value.get(key) is not None
    }
    return payload or None


def _tool_call_record(observation: dict[str, Any]) -> dict[str, Any]:
    """一次工具调用的结构化输入/状态（不含原始大对象）。"""
    data = observation.get("data") or {}
    record: dict[str, Any] = {
        "tool": observation.get("tool"),
        "input": _scalar(observation.get("input") or {}),
        "status": observation.get("status"),
        "attempts": observation.get("attempts"),
        "elapsed_ms": observation.get("elapsed_ms"),
        "window": _window(data.get("window")),
        "row_count": data.get("row_count"),
        "returned": data.get("returned"),
        "total": data.get("total"),
        "error": _scalar(observation.get("error") or None),
    }
    equipment = data.get("equipment") or {}
    if isinstance(equipment, dict) and equipment.get("equipment_code"):
        record["equipment_code"] = equipment.get("equipment_code")
    return {key: value for key, value in record.items() if value is not None}


def _stats_records(observation: dict[str, Any]) -> list[dict[str, Any]]:
    data = observation.get("data") or {}
    window = _window(data.get("window"))
    equipment = (data.get("equipment") or {}).get("equipment_code")
    records: list[dict[str, Any]] = []
    for kind, key in (("analog", "analog"), ("digital", "digital")):
        for item in data.get(key) or []:
            record = {
                "kind": kind,
                "sensor": item.get("sensor"),
                "count": item.get("count"),
                "min": item.get("min"),
                "max": item.get("max"),
                "mean": item.get("mean"),
                "std": item.get("std"),
                "count_0": item.get("count_0"),
                "count_1": item.get("count_1"),
                "ratio_1": item.get("ratio_1"),
                "transitions": item.get("transitions"),
                "first_timestamp": item.get("first_timestamp"),
                "last_timestamp": item.get("last_timestamp"),
                "window": window,
                "equipment_code": equipment,
                "stage": "baseline" if observation.get("_baseline") else "target",
            }
            records.append({k: v for k, v in record.items() if v is not None})
    return records


def _quality_record(observation: dict[str, Any]) -> dict[str, Any] | None:
    data = observation.get("data") or {}
    if not data:
        return None
    keys = (
        "actual_samples",
        "expected_samples",
        "sample_coverage",
        "time_coverage",
        "gap_count",
        "gap_duration_seconds",
        "max_gap_seconds",
        "missing_samples_estimate",
        "frozen_candidate_count",
        "frozen_candidate_ratio",
        "records_with_quality_flags",
        "window_inside_data_range",
        "data_first_observed_at",
        "data_last_observed_at",
        "nominal_step_seconds",
    )
    record = {key: data.get(key) for key in keys if data.get(key) is not None}
    record["window"] = _window(data.get("window"))
    record["warnings"] = [
        {
            "code": str(item.get("code")),
            "severity": str(item.get("severity")),
            "message": safe_text(item.get("message"), limit=300),
        }
        for item in (data.get("warnings") or [])[:10]
        if isinstance(item, dict)
    ]
    return record


def _knowledge_records(observation: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    data = observation.get("data") or {}
    records: list[dict[str, Any]] = []
    for chunk in (data.get("chunks") or [])[:limit]:
        excerpt = str(chunk.get("excerpt") or "")
        quote = safe_text(excerpt, limit=MAX_QUOTE_CHARS)
        records.append(
            {
                "chunk_id": chunk.get("chunk_id"),
                "document_id": str(chunk.get("document_id") or ""),
                "doc_key": chunk.get("doc_key"),
                "document_title": chunk.get("document_title"),
                "page_start": chunk.get("page_start"),
                "page_end": chunk.get("page_end"),
                "section_title": chunk.get("section_title"),
                "equipment_scope": chunk.get("equipment_scope"),
                "trust_level": chunk.get("trust_level"),
                "citation_label": chunk.get("citation_label"),
                "score": chunk.get("score"),
                "method": data.get("method"),
                "excerpt": quote,
                "quote_sha256": _sha256(quote),
                "excerpt_full_chars": len(excerpt),
            }
        )
    return records


def _fault_records(observation: dict[str, Any]) -> list[dict[str, Any]]:
    data = observation.get("data") or {}
    records: list[dict[str, Any]] = []
    for event in (data.get("events") or [])[:MAX_REFERENCE_ITEMS]:
        records.append(
            {
                key: _scalar(event.get(key))
                for key in (
                    "event_code",
                    "pdf_label",
                    "start_time",
                    "end_time",
                    "duration_seconds",
                    "overlap_seconds",
                    "failure_type",
                    "severity",
                    "verification_status",
                )
                if event.get(key) is not None
            }
        )
    return records


def _sheet_value(sheet: Any, key: str, default: Any = None) -> Any:
    """兼容 FactSheet 对象与 State 中的 dict 序列化形式。"""
    if sheet is None:
        return default
    if isinstance(sheet, dict):
        aliases = {"baseline": "baseline_window"}
        return sheet.get(key, sheet.get(aliases.get(key, key), default))
    return getattr(sheet, key, default)


def _answer_sections(answer: dict[str, Any]) -> dict[str, Any]:
    sections = answer.get("sections") or {}
    return {
        "conclusion": safe_text(sections.get("conclusion"), limit=2000),
        "inference": [safe_text(item, limit=500) for item in (sections.get("inference") or [])[:8]],
        "uncertainties": [
            safe_text(item, limit=500) for item in (sections.get("uncertainties") or [])[:8]
        ],
        "knowledge": [
            {
                "text": safe_text(item.get("text"), limit=600),
                "labels": list(item.get("labels") or [])[:5],
            }
            for item in (sections.get("knowledge") or [])[:8]
            if isinstance(item, dict)
        ],
        "mode": sections.get("mode"),
    }


def _citations(answer: dict[str, Any]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in (answer.get("citations") or [])[:MAX_FACT_ITEMS]:
        if item.get("kind") == "knowledge":
            quote = str(item.get("quote") or "")
            payload.append(
                {
                    "label": item.get("label"),
                    "kind": "knowledge",
                    "chunk_id": item.get("chunk_id"),
                    "doc_key": item.get("doc_key"),
                    "document_id": str(item.get("document_id") or ""),
                    "page_start": item.get("page_start"),
                    "page_end": item.get("page_end"),
                    "equipment_scope": item.get("equipment_scope"),
                    "quote": safe_text(quote, limit=MAX_QUOTE_CHARS),
                    "quote_sha256": _sha256(safe_text(quote, limit=MAX_QUOTE_CHARS)),
                    "citation_label": item.get("citation_label"),
                }
            )
        else:
            payload.append(
                {
                    "label": item.get("label"),
                    "kind": "data",
                    "fact_type": item.get("fact_type"),
                    "text": safe_text(item.get("text"), limit=600),
                    "source": _scalar(item.get("source") or {}),
                }
            )
    return payload


def _statistics_records(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按执行顺序标记目标窗口与对照窗口（与 `build_fact_sheet` 口径一致）。"""
    seen_statistics = False
    records: list[dict[str, Any]] = []
    for observation in observations:
        if observation.get("tool") != "get_sensor_statistics":
            continue
        if observation.get("status") not in ("ok", "empty"):
            continue
        enriched = dict(observation)
        enriched["_baseline"] = seen_statistics
        seen_statistics = True
        records.extend(_stats_records(enriched))
    return records[:MAX_FACT_ITEMS]


def build_evidence_manifest(
    *,
    agent_version: str,
    question: str,
    execution_status: str,
    answer: dict[str, Any] | None,
    observations: list[dict[str, Any]],
    slots: Any = None,
    fact_sheet: Any = None,
    settings: Any = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建有界证据清单（只读取工具已返回的数据，不发起任何查询）。"""
    answer = answer or {}
    observations = [item for item in (observations or []) if isinstance(item, dict)]
    max_chunks = int(getattr(settings, "max_knowledge_chunks", 5) or 5)

    knowledge: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    quality: dict[str, Any] | None = None
    faults: list[dict[str, Any]] = []
    for observation in observations[:MAX_REFERENCE_ITEMS]:
        tool_calls.append(_tool_call_record(observation))
        tool = observation.get("tool")
        if tool == "search_technical_knowledge":
            knowledge.extend(_knowledge_records(observation, max(1, max_chunks - len(knowledge))))
        elif tool == "get_data_quality" and quality is None:
            quality = _quality_record(observation)
        elif tool == "get_fault_events":
            faults.extend(_fault_records(observation))

    requested = None
    if slots is not None:
        requested = {
            "equipment_code": getattr(slots, "equipment_code", None),
            "equipment_source": getattr(slots, "equipment_source", None),
            "start_time": getattr(slots, "start_time", None),
            "end_time": getattr(slots, "end_time", None),
            "window_source": getattr(slots, "window_source", None),
            "sensors": list(getattr(slots, "sensors", None) or []) or None,
            "asks_anomaly": bool(getattr(slots, "asks_anomaly", False)),
            "asks_faults": bool(getattr(slots, "asks_faults", False)),
        }
        requested = {key: value for key, value in requested.items() if value is not None}

    manifest = {
        "schema_version": EVIDENCE_MANIFEST_VERSION,
        "agent_version": agent_version,
        "question": safe_text(question, limit=500),
        "execution_status": execution_status,
        "requested": requested,
        "metrics": _scalar(metrics or {}),
        "answer_status": answer.get("status"),
        "answer_mode": answer.get("answer_mode"),
        "answer_text": safe_text(answer.get("answer"), limit=MAX_ANSWER_CHARS),
        "answer_sections": _answer_sections(answer),
        "grounding": _scalar(answer.get("grounding") or {}),
        "citations": _citations(answer),
        "tool_calls": tool_calls,
        "facts": [
            {
                "label": item.get("label"),
                "kind": item.get("kind"),
                "text": safe_text(item.get("text"), limit=800),
            }
            for item in (_sheet_value(fact_sheet, "facts", []) or [])[:MAX_FACT_ITEMS]
            if isinstance(item, dict)
        ],
        "statistics": _statistics_records(observations),
        "knowledge": knowledge[:max_chunks],
        "quality": quality,
        "fault_events": faults[:MAX_REFERENCE_ITEMS],
        "injection_flags": list(_sheet_value(fact_sheet, "injection_flags", []) or [])[:10],
        "warnings": [safe_text(item, limit=400) for item in (answer.get("warnings") or [])[:10]],
        "evidence_window": _window(_sheet_value(fact_sheet, "window")),
        "baseline_window": _window(_sheet_value(fact_sheet, "baseline")),
    }
    return manifest


__all__ = [
    "EVIDENCE_MANIFEST_VERSION",
    "MAX_ANSWER_CHARS",
    "build_evidence_manifest",
    "safe_text",
]
