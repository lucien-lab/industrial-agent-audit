"""历史执行回放：离线审计与审计历史查询。

语义边界：
- **纯离线回放**只使用冻结 Trace（JSONL/内存/审计事件索引），不重新执行工业数据工具，
  也不重新运行业务 Agent；
- 如果显式启用知识库原文核对，`report.external_reads` 会记录外部读取与内容哈希，
  此时不能称为“完全基于冻结 Trace 的纯离线回放”；
- 审计结果不能修改原始业务回答与 Trace。
"""

from __future__ import annotations

from typing import Any

from app.audit.collector import load_trace
from app.audit.config import AUDIT_VERSION, AuditSettings, get_audit_settings
from app.audit.engine import AuditEngine
from app.audit.evidence import build_evidence_index, build_tool_timeline
from app.audit.repository import get_repository


def replay_trace(
    trace_id: str,
    *,
    settings: AuditSettings | None = None,
    include_audit_history: bool = True,
) -> dict[str, Any]:
    """回放一次执行链（事件顺序、工具调用、证据、审计历史）。"""
    settings = settings or get_audit_settings()
    repository = get_repository(settings)
    trace = load_trace(
        trace_id,
        settings=settings,
        db_loader=repository.load_trace_events if repository else None,
    )
    evidence = build_evidence_index(trace)
    audits: list[dict[str, Any]] = []
    if include_audit_history and repository is not None:
        audits = repository.audits_for_trace(trace_id)
    return {
        "trace": trace.model_dump(mode="json"),
        "tool_calls": build_tool_timeline(trace),
        "evidence": {
            "has_manifest": evidence.has_manifest,
            "manifest_schema_version": (evidence.manifest or {}).get("schema_version"),
            "requested": evidence.requested,
            "evidence_window": (evidence.manifest or {}).get("evidence_window"),
            "statistics": evidence.statistics,
            "quality": evidence.quality,
            "knowledge": [
                {
                    "chunk_id": item.get("chunk_id"),
                    "doc_key": item.get("doc_key"),
                    "page_start": item.get("page_start"),
                    "page_end": item.get("page_end"),
                    "equipment_scope": item.get("equipment_scope"),
                    "quote_sha256": item.get("quote_sha256"),
                }
                for item in evidence.knowledge
            ],
            "fault_events": evidence.faults,
            "injection_flags": evidence.injection_flags,
            "external_reads": evidence.external_reads,
            "knowledge_verification": evidence.knowledge_verification,
        },
        "audits": audits,
        "external_reads": evidence.external_reads,
        "offline_only": not evidence.external_reads,
    }


def replay_audit(
    trace_id: str,
    *,
    mode: str = "hybrid",
    audit_version: str | None = None,
    settings: AuditSettings | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    """对历史 Trace 重新审计（不同模式/版本），返回报告与回放视图。"""
    settings = settings or get_audit_settings()
    engine = AuditEngine(settings, use_repository=persist)
    report = engine.audit_trace_id(
        trace_id,
        mode=mode,
        audit_version=audit_version or AUDIT_VERSION,
        persist=persist,
    )
    replay = replay_trace(trace_id, settings=settings)
    return {"audit": report.model_dump(mode="json"), "replay": replay}


__all__ = ["replay_audit", "replay_trace"]
