"""Trace Collector / Normalizer 测试（阶段 4）。

覆盖：
- 事件顺序恢复、稳定 event_id、重复事件去重、缺失 seq、乱序；
- Schema 版本（有/无 evidence 事件）；
- 脱敏（密码、连接串、Bearer Token）；
- tool_call/tool_result 配对；
- 文件读取的路径穿越与大小限制；
- Trace 摘要对内容变化敏感。
"""

from __future__ import annotations

import json

import pytest

from app.audit.collector import (
    TraceLoadError,
    TraceNotFoundError,
    load_trace_from_file,
    validate_trace_id,
)
from app.audit.normalizer import normalize_trace, pair_tool_events, redact
from tests.audit_helpers import base_manifest, clone, make_settings, make_trace, write_trace_file
from build_audit_cases import _handcrafted_events


def _events() -> list[dict]:
    return clone(_handcrafted_events(base_manifest()))


def test_normalize_preserves_order_and_assigns_unique_ids(tmp_path) -> None:
    settings = make_settings(tmp_path)
    events = _events()
    events[3]["seq"] = 99
    events[4]["seq"] = 4
    trace = normalize_trace(
        events, trace_id="handcrafted-test", settings=settings, question="q"
    )
    assert trace.event_count == len(events)
    assert [event.raw_sequence_number for event in trace.events] == sorted(
        event.raw_sequence_number for event in trace.events
    )
    ids = [event.event_id for event in trace.events]
    assert len(ids) == len(set(ids))
    assert any(problem["code"] == "out_of_order_events" for problem in trace.problems)


def test_duplicate_events_are_deduplicated_and_recorded(tmp_path) -> None:
    settings = make_settings(tmp_path)
    events = _events()
    events.insert(5, clone(events[4]))
    trace = normalize_trace(events, trace_id="handcrafted-test", settings=settings)
    assert trace.event_count == len(_events())
    assert any(problem["code"] == "duplicate_events" for problem in trace.problems)


def test_missing_sequence_is_recovered(tmp_path) -> None:
    settings = make_settings(tmp_path)
    events = _events()
    for event in events:
        event.pop("seq", None)
    trace = normalize_trace(events, trace_id="handcrafted-test", settings=settings)
    assert [event.sequence_number for event in trace.events] == list(range(len(events)))
    assert all(event.raw_sequence_number is None for event in trace.events)
    assert any(problem["code"] == "missing_sequence" for problem in trace.problems)


def test_evidence_manifest_switches_schema_version(tmp_path) -> None:
    settings = make_settings(tmp_path)
    with_manifest = make_trace(base_manifest(), settings, trace_id="t-v2")
    assert with_manifest.trace_schema_version == "phase3-trace-v2"
    assert with_manifest.evidence_manifest is not None

    legacy = make_trace(base_manifest(), settings, trace_id="t-v1", with_manifest=False)
    assert legacy.trace_schema_version == "phase3-trace-v1"
    assert legacy.evidence_manifest is None


def test_redaction_removes_secrets() -> None:
    payload = {
        "password": "supersecret",
        "database_url": "postgresql+psycopg://app_user:supersecret@127.0.0.1:5432/db",
        "note": "Bearer abcdefghijklmnop",
        "nested": {"api_key": "sk-123456", "connection_string": "postgres://u:p@h/db"},
        "safe": "2020-04-17",
    }
    redacted = redact(payload)
    blob = json.dumps(redacted, ensure_ascii=False)
    assert "supersecret" not in blob
    assert "sk-123456" not in blob
    assert "abcdefghijklmnop" not in blob
    assert redacted["safe"] == "2020-04-17"
    assert redacted["nested"]["api_key"] == "<redacted>"


def test_tool_call_result_pairing_detects_missing_result(tmp_path) -> None:
    settings = make_settings(tmp_path)
    events = _events()
    result_index = next(
        index for index, event in enumerate(events) if event["event_type"] == "tool_result"
    )
    del events[result_index]
    trace = normalize_trace(events, trace_id="handcrafted-test", settings=settings)
    pairing = pair_tool_events(trace)
    assert len(pairing["pairs"]) == 0
    assert len(pairing["unpaired_calls"]) == 1


def test_trace_digest_is_stable_and_content_sensitive(tmp_path) -> None:
    settings = make_settings(tmp_path)
    manifest = base_manifest()
    first = make_trace(manifest, settings, trace_id="t-digest")
    second = make_trace(clone(manifest), settings, trace_id="t-digest")
    assert first.trace_digest == second.trace_digest

    changed_manifest = clone(manifest)
    changed_manifest["answer_text"] = "被修改的回答"
    changed = make_trace(changed_manifest, settings, trace_id="t-digest")
    assert changed.trace_digest != first.trace_digest


def test_load_trace_from_file_and_not_found(tmp_path) -> None:
    settings = make_settings(tmp_path)
    events = _events()
    write_trace_file(events, tmp_path, "file-trace")
    trace = load_trace_from_file("file-trace", settings=settings)
    assert trace.source.value == "file"
    assert trace.event_count == len(events)
    assert trace.raw_digest

    with pytest.raises(TraceNotFoundError):
        load_trace_from_file("missing-trace", settings=settings)


def test_file_size_limit(tmp_path) -> None:
    settings = make_settings(tmp_path, max_trace_bytes=50)
    write_trace_file(_events(), tmp_path, "big-trace")
    with pytest.raises(TraceLoadError):
        load_trace_from_file("big-trace", settings=settings)


@pytest.mark.parametrize("trace_id", ["../etc/passwd", "a b", "", "x" * 65, "a/b"])
def test_invalid_trace_id_rejected(trace_id: str) -> None:
    with pytest.raises(TraceLoadError):
        validate_trace_id(trace_id)


def test_known_trace_id_accepted() -> None:
    assert validate_trace_id("06becd168a5143e99efd2a4279d8344d")
