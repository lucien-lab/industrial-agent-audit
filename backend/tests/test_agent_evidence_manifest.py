"""阶段 3 Trace 扩展：有界证据清单（evidence 事件）。

只测试新增的 `evidence` 事件与清单构建，不改变原有 Trace 契约：
- 清单有长度上限、文档片段只保留有限摘录 + 哈希；
- 连接串/密钥样式文本被去除；
- `TraceRecorder` 接受 evidence 事件且不影响已有事件；
- 旧 Trace 缺少 evidence 时规范化版本为 v1（见 test_audit_normalizer）。
"""

from __future__ import annotations

import json

from app.agent.evidence_manifest import (
    MAX_QUOTE_CHARS,
    build_evidence_manifest,
    safe_text,
)
from app.agent.intent import Slots
from app.agent.trace import TraceRecorder


def _observation(**overrides):
    payload = {
        "tool": "get_sensor_statistics",
        "input": {
            "equipment_code": "metropt3-apu-01",
            "start_time": "2020-04-17T00:00:00",
            "end_time": "2020-04-18T00:00:00",
            "sensors": ["motor_current"],
        },
        "status": "ok",
        "attempts": 1,
        "elapsed_ms": 12.3,
        "data": {
            "equipment": {"equipment_code": "metropt3-apu-01"},
            "window": {
                "start": "2020-04-17T00:00:00",
                "end": "2020-04-18T00:00:00",
                "duration_seconds": 86400.0,
                "boundary": "[start, end)",
            },
            "analog": [
                {
                    "sensor": "motor_current",
                    "count": 6301,
                    "min": 0.035,
                    "max": 6.36,
                    "mean": 0.4512,
                    "std": 1.302,
                    "first_timestamp": "2020-04-17T00:00:05",
                    "last_timestamp": "2020-04-17T23:59:49",
                }
            ],
            "digital": [],
        },
        "evidence": [],
        "error": None,
    }
    payload.update(overrides)
    return payload


def test_manifest_is_bounded_and_structured() -> None:
    manifest = build_evidence_manifest(
        agent_version="phase3-agent-v1",
        question="2020年4月17日电机电流最大值是多少？",
        execution_status="completed",
        answer={"answer": "最大 6.36", "status": "answered", "citations": []},
        observations=[_observation()],
        slots=Slots(
            equipment_code="metropt3-apu-01",
            start_time="2020-04-17T00:00:00",
            end_time="2020-04-18T00:00:00",
            sensors=("motor_current",),
        ),
        fact_sheet={"window": {"start": "2020-04-17T00:00:00", "end": "2020-04-18T00:00:00"}},
        metrics={"steps": 7, "rounds": 0, "tool_call_count": 1},
    )
    assert manifest["schema_version"] == "agent-evidence-v1"
    assert manifest["statistics"][0]["sensor"] == "motor_current"
    assert manifest["statistics"][0]["max"] == 6.36
    assert manifest["statistics"][0]["first_timestamp"] == "2020-04-17T00:00:05"
    assert manifest["requested"]["equipment_code"] == "metropt3-apu-01"
    assert manifest["metrics"]["steps"] == 7
    assert manifest["evidence_window"]["start"] == "2020-04-17T00:00:00"


def test_manifest_knowledge_quotes_are_truncated_and_hashed() -> None:
    long_excerpt = "A" * 5000
    observation = {
        "tool": "search_technical_knowledge",
        "input": {"query": "TP2", "top_k": 5},
        "status": "ok",
        "attempts": 1,
        "data": {
            "method": "hybrid",
            "chunks": [
                {
                    "chunk_id": 12,
                    "document_id": "00000000-0000-0000-0000-000000000001",
                    "doc_key": "metropt3-data-description",
                    "document_title": "MetroPT-3 Data Description",
                    "page_start": 2,
                    "page_end": 2,
                    "equipment_scope": "metropt3_direct",
                    "excerpt": long_excerpt,
                }
            ],
        },
    }
    manifest = build_evidence_manifest(
        agent_version="phase3-agent-v1",
        question="TP2 是什么？",
        execution_status="completed",
        answer={"answer": "pressure", "citations": []},
        observations=[observation],
        fact_sheet={},
    )
    chunk = manifest["knowledge"][0]
    assert len(chunk["excerpt"]) <= MAX_QUOTE_CHARS
    assert len(chunk["quote_sha256"]) == 64
    assert chunk["chunk_id"] == 12
    assert chunk["excerpt_full_chars"] == 5000


def test_safe_text_removes_credentials() -> None:
    text = "连接 postgresql://user:secret@host:5432/db 并设置 password=hunter2"
    sanitized = safe_text(text)
    assert "secret" not in sanitized
    assert "hunter2" not in sanitized


def test_trace_recorder_accepts_evidence_event() -> None:
    recorder = TraceRecorder(trace_id="evidence-test", write_files=False)
    event = recorder.record(
        "evidence",
        node="finalize",
        status="ok",
        meta={"manifest": {"schema_version": "agent-evidence-v1"}},
    )
    assert event.event_type == "evidence"
    assert event.meta["manifest"]["schema_version"] == "agent-evidence-v1"
    # 序列化后仍是合法 JSON（meta 不经过摘要化）
    decoded = json.loads(json.dumps(event.to_dict(), ensure_ascii=False))
    assert decoded["meta"]["manifest"]["schema_version"] == "agent-evidence-v1"
