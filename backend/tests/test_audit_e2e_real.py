"""真实端到端验收（默认跳过）：真实 Trace + 真实证据 + 真实 Ollama Judge。

启用：

    IAA_AUDIT_E2E=1 pytest tests/test_audit_e2e_real.py -q -m slow

前置：
- `.env` 中有真实 `DATABASE_URL` / `RAG_DATABASE_URL`（只读使用）；
- `data/processed/agent/traces/` 中有真实 Phase 3 Trace；
- 本机 Ollama 已加载 `qwen2.5:7b-instruct`；
- `AUDIT_DATABASE_URL` 指向隔离审计库（不会写主库）。

Mock 测试通过 ≠ 真实审计能力验证通过；本文件才是真实链路入口。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.audit.collector import discover_trace_ids, load_trace
from app.audit.config import get_audit_settings
from app.audit.engine import AuditEngine
from app.core.config import PROJECT_ROOT

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("IAA_AUDIT_E2E") != "1",
        reason="未设置 IAA_AUDIT_E2E=1，跳过真实审计端到端验证",
    ),
]

REAL_INDEX = PROJECT_ROOT / "data" / "processed" / "audit" / "real_traces.json"


def _trace_id() -> str:
    if REAL_INDEX.is_file():
        payload = json.loads(REAL_INDEX.read_text(encoding="utf-8"))
        run = next(
            (item for item in payload.get("runs", []) if item.get("execution_status") == "completed"),
            None,
        )
        if run:
            return str(run["trace_id"])
    traces = discover_trace_ids(get_audit_settings())
    if not traces:
        pytest.skip("没有可用的真实 Trace")
    return traces[-1]


def test_real_trace_has_evidence_manifest() -> None:
    settings = get_audit_settings()
    trace = load_trace(_trace_id(), settings=settings)
    assert trace.trace_schema_version == "phase3-trace-v2"
    assert trace.evidence_manifest is not None
    assert trace.evidence_manifest.get("schema_version") == "agent-evidence-v1"


def test_real_rule_audit_and_replay() -> None:
    settings = get_audit_settings()
    engine = AuditEngine(settings)
    trace_id = _trace_id()
    replay = engine.replay(trace_id, mode="rule_only")
    assert replay["trace"]["trace_id"] == trace_id
    assert replay["trace"]["events"]
    assert replay["tool_calls"] is not None
    report = replay["audit"]
    assert report["trace_digest"]
    assert report["status"] in ("completed", "partial")
    # 离线回放不得读取外部知识（除非显式开启，并在报告中记录 external_reads）
    if not settings.verify_knowledge_offline:
        assert report["external_reads"] == []


def test_real_hybrid_audit_with_ollama() -> None:
    settings = get_audit_settings()
    if not settings.judge_enabled:
        pytest.skip("AUDIT_JUDGE_ENABLED=false")
    trace = load_trace(_trace_id(), settings=settings)
    report = AuditEngine(settings).audit(trace, mode="hybrid", persist=False)
    assert report.judge.status in ("ok", "error", "unavailable")
    if report.judge.status == "ok":
        assert report.judge.model
        assert report.prompt_version == settings.judge_prompt_version
        assert report.judge.input_digest
    else:
        # 模型不可用时必须是 UNKNOWN/partial，不允许伪装成通过
        assert report.status.value == "partial"
        assert report.overall_risk.value in ("unknown", "low", "medium", "high", "critical")
        assert any(item.verdict.value == "unknown" for item in report.findings)
