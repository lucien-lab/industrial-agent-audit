"""审计测试辅助：复用阶段 4 构集脚本中的最小 Trace，避免测试与实现漂移。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from build_audit_cases import (  # noqa: E402 - scripts/ 已由 conftest 加入 sys.path
    MUTATIONS,
    _handcrafted_events,
    _handcrafted_manifest,
)
from app.audit.collector import load_trace_from_events
from app.audit.config import AuditRuleConfig, AuditSettings
from app.audit.engine import AuditEngine
from app.audit.schemas import AuditReport, TraceSourceKind


def make_settings(tmp_path: Path, **overrides: Any) -> AuditSettings:
    payload = {
        "audit_database_url": None,
        "trace_dir": tmp_path,
        "verify_knowledge_offline": False,
        "judge_enabled": False,
        "rules": AuditRuleConfig(),
    }
    payload.update(overrides)
    return AuditSettings(**payload)


def base_manifest(**overrides: Any) -> dict[str, Any]:
    return _handcrafted_manifest(**overrides)


def make_trace(
    manifest: dict[str, Any] | None,
    settings: AuditSettings,
    *,
    events: list[dict[str, Any]] | None = None,
    trace_id: str = "handcrafted-test",
    with_manifest: bool = True,
):
    payload = events if events is not None else _handcrafted_events(
        manifest if manifest is not None else base_manifest(), with_manifest=with_manifest
    )
    for event in payload:
        event["trace_id"] = trace_id
    return load_trace_from_events(
        trace_id,
        payload,
        settings=settings,
        source=TraceSourceKind.CASE,
        source_ref="unit-test",
        question=(manifest or {}).get("question"),
        execution_status=(manifest or {}).get("execution_status"),
    )


def audit_manifest(
    manifest: dict[str, Any] | None,
    settings: AuditSettings,
    *,
    mode: str = "rule_only",
    mutation: str | None = None,
    judge: Any | None = None,
) -> AuditReport:
    events = _handcrafted_events(
        manifest if manifest is not None else base_manifest(),
        with_manifest=manifest is not None,
    )
    if mutation:
        result = MUTATIONS[mutation](events, {"execution_status": "completed"})
        assert result is not None, f"mutation {mutation} 不适用"
        events = result["events"]
    trace = make_trace(manifest, settings, events=events)
    return AuditEngine(settings, judge=judge).audit(trace, mode=mode)


def results_by_rule(report: AuditReport, rule_id: str):
    return [item for item in report.results if item.rule_id == rule_id]


def verdicts(report: AuditReport, rule_id: str) -> list[str]:
    return [str(item.verdict) for item in results_by_rule(report, rule_id)]


def write_trace_file(events: list[dict[str, Any]], trace_dir: Path, trace_id: str) -> Path:
    trace_dir.mkdir(parents=True, exist_ok=True)
    path = trace_dir / f"trace-{trace_id}.jsonl"
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    return path


def clone(value: Any) -> Any:
    return copy.deepcopy(value)
