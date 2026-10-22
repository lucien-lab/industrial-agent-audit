"""证据索引与交叉校验（审计侧，只读）。

职责：
- 从规范化 Trace 的 `evidence` 清单与事件中整理出可核对的证据；
- 提供数值、统计量、时间窗、设备、质量与引用查找；
- 可选地**显式**读取知识库核对 Chunk 原文（记录外部读取与数据版本）。

不做：
- 不重新执行工业数据工具（时间窗/设备/数值全部来自冻结 Trace）；
- 不把“证据清单缺失”当成“证据一致”；
- 不把 Judge 结论混进原始证据。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.agent.evidence import extract_numbers
from app.agent.intent import SENSOR_LABELS_ZH
from app.audit.config import AuditSettings
from app.audit.schemas import NormalizedTrace
from app.services.catalog import SENSOR_LABELS

_NAIVE_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?$")
_TZ_SUFFIX_RE = re.compile(r"(?i)(Z|[+-]\d{2}:?\d{2})$")
_DIGIT_RUN_RE = re.compile(r"\d+(?:\.\d+)?")


def has_timezone(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if _TZ_SUFFIX_RE.search(text):
        # 纯日期（2020-04-17）不会被匹配；`2020-04-17T00:00:00+08:00` 会
        return "T" in text or " " in text
    return False


def parse_naive_time(value: Any) -> datetime | None:
    """只解析无时区时间；带时区/非法值返回 None（调用方据此报错）。"""
    text = str(value or "").strip()
    if not text or not _NAIVE_TIME_RE.match(text):
        return None
    normalized = text.replace(" ", "T")
    try:
        if len(normalized) == 10:
            return datetime.fromisoformat(normalized)
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def parse_iso_timestamp(value: Any) -> datetime | None:
    """解析审计 Trace 的时间戳（允许带时区；这是审计元数据，不是工业数据时间）。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return parse_naive_time(text)


def canonical_params(payload: Any) -> str:
    import json

    return json.dumps(payload or {}, sort_keys=True, ensure_ascii=False, default=str)


def _walk_numbers(value: Any, out: set[float], *, depth: int = 0) -> None:
    if depth > 8 or value is None:
        return
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        out.add(float(value))
        return
    if isinstance(value, str):
        out.update(extract_numbers(value))
        _walk_digit_fragments(value, out)
        return
    if isinstance(value, dict):
        for item in value.values():
            _walk_numbers(item, out, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _walk_numbers(item, out, depth=depth + 1)


def _walk_digit_fragments(text: str, out: set[float]) -> None:
    """时间戳/页码/ChunkID 的每个数字片段都可以被如实引用。"""
    for match in _DIGIT_RUN_RE.finditer(text):
        try:
            out.add(float(match.group(0)))
        except ValueError:  # pragma: no cover
            continue


@dataclass
class EvidenceIndex:
    """一次审计可用的全部证据视图。"""

    trace: NormalizedTrace
    manifest: dict[str, Any] | None = None
    requested: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    statistics: list[dict[str, Any]] = field(default_factory=list)
    quality: dict[str, Any] | None = None
    knowledge: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    faults: list[dict[str, Any]] = field(default_factory=list)
    facts: list[dict[str, Any]] = field(default_factory=list)
    answer_text: str = ""
    answer_sections: dict[str, Any] = field(default_factory=dict)
    grounding: dict[str, Any] = field(default_factory=dict)
    injection_flags: list[str] = field(default_factory=list)
    evidence_numbers: set[float] = field(default_factory=set)
    external_reads: list[dict[str, Any]] = field(default_factory=list)
    knowledge_verification: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @property
    def has_manifest(self) -> bool:
        return bool(self.manifest) and not self.manifest.get("error")

    @property
    def requires_manifest_for_evidence_rules(self) -> bool:
        return True

    @property
    def requested_equipment(self) -> str | None:
        return self.requested.get("equipment_code")

    @property
    def requested_window(self) -> tuple[str | None, str | None]:
        return self.requested.get("start_time"), self.requested.get("end_time")

    @property
    def numeric_text(self) -> str:
        """用于数值核对的回答文本：剔除“执行说明/参考来源”这类系统说明。"""
        text = self.answer_text or ""
        for marker in ("## 执行说明", "## 参考来源"):
            index = text.find(marker)
            if index != -1:
                text = text[:index]
        return text

    @property
    def answer_status(self) -> str | None:
        return (self.manifest or {}).get("answer_status") or None

    @property
    def answer_mode(self) -> str | None:
        return (self.manifest or {}).get("answer_mode") or None

    def event_by_id(self, event_id: str):
        return next((event for event in self.trace.events if event.event_id == event_id), None)

    # ------------------------------------------------------------------ #
    def tool_inputs(self) -> list[dict[str, Any]]:
        """优先使用证据清单中的完整入参；缺失时回退到事件摘要（并标注来源）。"""
        if self.tool_calls:
            return [
                {
                    "tool": item.get("tool"),
                    "input": item.get("input") or {},
                    "status": item.get("status"),
                    "attempts": item.get("attempts"),
                    "window": item.get("window"),
                    "equipment_code": item.get("equipment_code"),
                    "event_id": item.get("event_id"),
                    "source": "manifest",
                }
                for item in self.tool_calls
            ]
        return [
            {
                "tool": event.tool_name,
                "input": event.input_summary or {},
                "status": event.status,
                "attempts": None,
                "window": (event.output_summary or {}).get("window")
                if isinstance(event.output_summary, dict)
                else None,
                "equipment_code": (event.input_summary or {}).get("equipment_code")
                if isinstance(event.input_summary, dict)
                else None,
                "source": "event",
                "event_id": event.event_id,
            }
            for event in self.trace.tool_calls()
        ]

    def statistics_for(self, sensor: str) -> list[dict[str, Any]]:
        return [item for item in self.statistics if item.get("sensor") == sensor]

    def stat_value(self, sensor: str, stat: str, *, stage: str = "target") -> float | None:
        for item in self.statistics:
            if item.get("sensor") != sensor:
                continue
            if item.get("stage", "target") != stage:
                continue
            value = item.get(stat)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        return None

    def answer_numbers(self, *, include_question: bool = True) -> list[str]:
        tokens = [_token for _token in extract_number_tokens(self.answer_text)]
        if include_question:
            tokens.extend(extract_number_tokens(self.trace.question))
        return tokens

    def is_number_supported(self, token: str, *, rel: float = 1e-4, abs_: float = 1e-6) -> bool:
        value = float(token)
        for candidate in self.evidence_numbers:
            if abs(candidate - value) <= abs_:
                return True
            scale = max(abs(candidate), abs(value), 1.0)
            if abs(candidate - value) / scale <= rel:
                return True
        return False

    def quality_warnings(self) -> list[dict[str, Any]]:
        return list((self.quality or {}).get("warnings") or [])


def extract_number_tokens(text: str) -> list[str]:
    """回答中的数值 token（与阶段 3 的严格切分一致，剔除标签/URL）。"""
    from app.agent.evidence import _NUMBER_RE, remove_labels  # noqa: PLC2701 - 复用同一口径

    cleaned = re.sub(r"https?://\S+", " ", remove_labels(str(text or "")))
    return [match.group(1) for match in _NUMBER_RE.finditer(cleaned)]


def build_evidence_index(trace: NormalizedTrace) -> EvidenceIndex:
    manifest = trace.evidence_manifest if trace.evidence_manifest else None
    manifest = manifest if isinstance(manifest, dict) else None
    usable = manifest if manifest and not manifest.get("error") else None

    index = EvidenceIndex(trace=trace, manifest=manifest)
    if not usable:
        # 没有证据清单：只保留能从事件里确定的信息；依赖清单的规则必须 UNKNOWN。
        # run_start 中的请求槽位是原始事件，可以作为时间窗/设备一致性的事实来源。
        run_start = next(
            (event for event in trace.events if event.event_type == "run_start"), None
        )
        start_input = (
            run_start.input_summary
            if run_start is not None and isinstance(run_start.input_summary, dict)
            else {}
        )
        requested = {
            "equipment_code": start_input.get("equipment_code"),
            "start_time": start_input.get("start_time"),
            "end_time": start_input.get("end_time"),
            "equipment_source": "run_start",
            "window_source": "run_start",
        }
        index.requested = {key: value for key, value in requested.items() if value is not None}
        index.answer_text = ""
        index.tool_calls = []
        return index

    index.requested = dict(usable.get("requested") or {})
    index.tool_calls = list(usable.get("tool_calls") or [])
    # 把证据清单中的调用映射回 Trace 事件 ID（用于定位风险）
    pending_events: dict[str, list[str]] = {}
    for event in trace.tool_calls():
        pending_events.setdefault(str(event.tool_name), []).append(event.event_id)
    for item in index.tool_calls:
        queue = pending_events.get(str(item.get("tool"))) or []
        if queue:
            item["event_id"] = queue.pop(0)
    index.statistics = list(usable.get("statistics") or [])
    index.quality = usable.get("quality") if isinstance(usable.get("quality"), dict) else None
    index.knowledge = list(usable.get("knowledge") or [])
    index.citations = list(usable.get("citations") or [])
    index.faults = list(usable.get("fault_events") or [])
    index.facts = list(usable.get("facts") or [])
    index.answer_text = str(usable.get("answer_text") or "")
    index.answer_sections = (
        usable.get("answer_sections") if isinstance(usable.get("answer_sections"), dict) else {}
    )
    index.grounding = usable.get("grounding") if isinstance(usable.get("grounding"), dict) else {}
    index.injection_flags = [str(item) for item in (usable.get("injection_flags") or [])]

    numbers: set[float] = set()
    _walk_numbers(usable.get("statistics"), numbers)
    _walk_numbers(usable.get("facts"), numbers)
    _walk_numbers(usable.get("quality"), numbers)
    _walk_numbers(usable.get("fault_events"), numbers)
    _walk_numbers(usable.get("tool_calls"), numbers)
    _walk_numbers(usable.get("evidence_window"), numbers)
    _walk_numbers(usable.get("baseline_window"), numbers)
    _walk_numbers(usable.get("requested"), numbers)
    for item in index.knowledge:
        for key in (
            "chunk_id",
            "page_start",
            "page_end",
            "excerpt",
            "document_title",
            "citation_label",
        ):
            _walk_numbers(item.get(key), numbers)
    for item in index.citations:
        _walk_numbers(item.get("text"), numbers)
        for key in (
            "chunk_id",
            "page_start",
            "page_end",
            "quote",
            "document_title",
            "citation_label",
        ):
            _walk_numbers(item.get(key), numbers)
    # 问题中的数字（用户给出的日期/时间）不算编造
    _walk_numbers(trace.question, numbers)
    index.evidence_numbers = numbers
    return index


def verify_knowledge_chunks(
    index: EvidenceIndex,
    *,
    settings: AuditSettings,
    session: Any | None = None,
) -> None:
    """可选：只读核对知识库 Chunk 原文是否存在、摘录是否逐字出现。

    - 使用 `RAG_DATABASE_URL` 的只读会话（若传入 session 则直接使用）；
    - 每次外部读取都写入 `external_reads`（表名、库名、时间、内容哈希）；
    - 读取失败不会让审计“通过”，只会让 R8 变为 UNKNOWN。
    """
    if not index.knowledge:
        return
    from sqlalchemy import text

    from app.core.config import load_env_file, mask_password
    from app.rag.config import get_rag_settings

    load_env_file()
    owned = False
    chunk_ids = [
        int(item["chunk_id"])
        for item in index.knowledge
        if isinstance(item.get("chunk_id"), int)
    ]
    if not chunk_ids:
        index.knowledge_verification.append(
            {"ok": False, "reason": "no_chunk_ids", "message": "证据清单中没有可核对的 chunk_id"}
        )
        return
    try:
        if session is None:
            from app.db.session import get_session_factory

            rag_url = get_rag_settings().database_url
            session = get_session_factory(rag_url)()
            owned = True
            index.external_reads.append(
                {
                    "source": "rag_document_chunks",
                    "database": rag_url.rsplit("/", 1)[-1],
                    "url": mask_password(rag_url),
                    "chunk_ids": chunk_ids,
                    "read_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    "read_only": True,
                }
            )
        rows = session.execute(
            text(
                "SELECT c.id, c.document_id, c.content, d.doc_key "
                "FROM document_chunks c JOIN documents d ON d.id = c.document_id "
                "WHERE c.id = ANY(:ids)"
            ),
            {"ids": chunk_ids},
        ).all()
    except Exception as exc:  # noqa: BLE001 - 外部读取失败必须显式降级
        index.knowledge_verification.append(
            {"ok": False, "reason": "external_read_failed", "message": type(exc).__name__}
        )
        return
    finally:
        if owned and session is not None:
            try:
                session.rollback()
            finally:
                session.close()

    found = {row[0]: row for row in rows}
    for item in index.knowledge:
        chunk_id = item.get("chunk_id")
        row = found.get(chunk_id)
        if row is None:
            index.knowledge_verification.append(
                {"ok": False, "reason": "chunk_not_found", "chunk_id": chunk_id}
            )
            continue
        from app.rag.answer import normalize_for_quote_match

        content = str(row[2] or "")
        excerpt = str(item.get("excerpt") or "").rstrip("…")
        normalized_content = normalize_for_quote_match(content)
        normalized_excerpt = normalize_for_quote_match(excerpt)
        index.knowledge_verification.append(
            {
                "ok": True,
                "chunk_id": chunk_id,
                "doc_key_matches": bool(item.get("doc_key") in (None, "", row[3])),
                "excerpt_verbatim": bool(
                    normalized_excerpt and normalized_excerpt in normalized_content
                ),
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "content_chars": len(content),
            }
        )


def build_tool_timeline(trace: NormalizedTrace) -> list[dict[str, Any]]:
    """回放视图：工具调用 + 返回（顺序、状态、耗时、证据引用）。"""
    from app.audit.normalizer import pair_tool_events

    pairing = pair_tool_events(trace)
    timeline: list[dict[str, Any]] = []
    for pair in pairing["pairs"]:
        call, result = pair["call"], pair["result"]
        timeline.append(
            {
                "tool": pair["tool"],
                "call_event_id": call.event_id,
                "result_event_id": result.event_id,
                "call_sequence": call.sequence_number,
                "result_sequence": result.sequence_number,
                "input_summary": call.input_summary,
                "status": result.status,
                "duration_ms": result.duration_ms,
                "evidence_refs": result.evidence_refs,
                "error_type": result.error_type,
                "error_code": result.error_code,
            }
        )
    return timeline


__all__ = [
    "EvidenceIndex",
    "build_evidence_index",
    "build_tool_timeline",
    "canonical_params",
    "extract_number_tokens",
    "has_timezone",
    "parse_iso_timestamp",
    "parse_naive_time",
    "verify_knowledge_chunks",
]
