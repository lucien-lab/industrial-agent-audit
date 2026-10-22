"""审计 FastAPI 接口测试（隔离测试库 + 临时 Trace 目录）。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from build_audit_cases import MUTATIONS, _handcrafted_events
from tests.audit_helpers import base_manifest, clone, write_trace_file

TRACE_ID = "api-trace-0001"
FAIL_TRACE_ID = "api-trace-fail-0001"


def _events_for(trace_id: str, *, mutation: str | None = None) -> list[dict]:
    events = clone(_handcrafted_events(base_manifest()))
    if mutation:
        result = MUTATIONS[mutation](events, {"execution_status": "completed"})
        assert result is not None
        events = result["events"]
    for event in events:
        event["trace_id"] = trace_id
    # 验证 API 不会回显敏感信息（事件摘要层也会脱敏）
    events[0].setdefault("input_summary", {})["note"] = "password=supersecret"
    events[0]["input_summary"]["api_key"] = "sk-should-not-leak"
    return events


@pytest.fixture()
def audit_api(api_client, clean_engine, tmp_path, monkeypatch):  # noqa: ANN001
    trace_dir = tmp_path / "traces"
    monkeypatch.setenv("AGENT_TRACE_DIR", str(trace_dir))
    monkeypatch.setenv("AUDIT_DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("AUDIT_ALLOW_MAIN_DATABASE", "true")
    monkeypatch.setenv("AUDIT_VERIFY_KNOWLEDGE_OFFLINE", "false")
    monkeypatch.setenv("AUDIT_JUDGE_ENABLED", "false")
    monkeypatch.delenv("AUDIT_API_TOKEN", raising=False)
    write_trace_file(_events_for(TRACE_ID), trace_dir, TRACE_ID)
    write_trace_file(_events_for(FAIL_TRACE_ID, mutation="numeric_tamper"), trace_dir, FAIL_TRACE_ID)
    return api_client


def test_list_rules(audit_api) -> None:
    response = audit_api.get("/api/v1/audit/rules")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload) >= 10
    assert {"rule_id", "category", "severity", "applicability"} <= set(payload[0])


def test_get_trace_detail_and_redaction(audit_api) -> None:
    response = audit_api.get(f"/api/v1/audit/traces/{TRACE_ID}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["trace"]["trace_id"] == TRACE_ID
    assert body["trace"]["event_count"] == 9
    assert body["tool_calls"][0]["tool"] == "get_sensor_statistics"
    assert body["offline_only"] is True
    assert "supersecret" not in response.text
    assert "sk-should-not-leak" not in response.text


def test_get_trace_not_found(audit_api) -> None:
    assert audit_api.get("/api/v1/audit/traces/no-such-trace").status_code == 404


def test_get_trace_invalid_id(audit_api) -> None:
    assert audit_api.get("/api/v1/audit/traces/a%20b").status_code == 422


def test_evaluate_is_idempotent(audit_api) -> None:
    first = audit_api.post(
        f"/api/v1/audit/traces/{TRACE_ID}/evaluate", json={"mode": "rule_only"}
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["audit_mode"] == "rule_only"
    assert body["overall_risk"] in {"none", "low", "medium", "high", "critical", "unknown"}
    assert body["findings"] == []
    audit_id = body["audit_id"]

    second = audit_api.post(
        f"/api/v1/audit/traces/{TRACE_ID}/evaluate", json={"mode": "rule_only"}
    )
    assert second.status_code == 200
    assert second.json()["audit_id"] == audit_id
    assert any(
        item.get("code") == "idempotent_reuse" for item in second.json()["diagnostics"]
    )

    run = audit_api.get(f"/api/v1/audit/runs/{audit_id}")
    assert run.status_code == 200
    assert run.json()["trace_id"] == TRACE_ID


def test_evaluate_creates_findings_and_filters(audit_api) -> None:
    response = audit_api.post(
        f"/api/v1/audit/traces/{FAIL_TRACE_ID}/evaluate", json={"mode": "rule_only"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["findings"], "数值篡改应产生至少一条发现"
    finding = body["findings"][0]
    assert finding["related_event_ids"] or finding["evidence_refs"] is not None

    filtered = audit_api.get(
        "/api/v1/audit/findings",
        params={"trace_id": FAIL_TRACE_ID, "risk_category": "numeric_evidence_mismatch"},
    )
    assert filtered.status_code == 200
    payload = filtered.json()
    assert payload["total"] >= 1
    assert all(item["risk_category"] == "numeric_evidence_mismatch" for item in payload["items"])

    review = audit_api.patch(
        f"/api/v1/audit/findings/{finding['finding_id']}/review",
        json={"review_status": "confirmed", "note": "人工确认"},
    )
    assert review.status_code == 200
    assert review.json()["review_status"] == "confirmed"


def test_summary_endpoint(audit_api) -> None:
    audit_api.post(f"/api/v1/audit/traces/{TRACE_ID}/evaluate", json={"mode": "rule_only"})
    response = audit_api.get("/api/v1/audit/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["total_audits"] >= 1
    assert "audits_by_mode" in body
    assert "avg_duration_ms" in body


def test_pagination_and_parameter_validation(audit_api) -> None:
    assert audit_api.get("/api/v1/audit/findings", params={"limit": 201}).status_code == 422
    assert audit_api.get("/api/v1/audit/findings", params={"offset": -1}).status_code == 422
    assert (
        audit_api.get("/api/v1/audit/findings", params={"severity": "bogus"}).status_code == 422
    )
    assert (
        audit_api.get(
            "/api/v1/audit/findings", params={"started_after": "not-a-date"}
        ).status_code
        == 422
    )
    assert (
        audit_api.post(f"/api/v1/audit/traces/{TRACE_ID}/evaluate", json={"mode": "bogus"}).status_code
        == 422
    )
    assert (
        audit_api.post(
            f"/api/v1/audit/traces/{TRACE_ID}/evaluate", json={"mode": "rule_only", "sql": "drop"}
        ).status_code
        == 422
    )


def test_unknown_run_and_finding(audit_api) -> None:
    assert audit_api.get("/api/v1/audit/runs/aud-doesnotexist").status_code == 404
    assert (
        audit_api.patch(
            "/api/v1/audit/findings/Fdoesnotexist/review", json={"review_status": "dismissed"}
        ).status_code
        == 404
    )


def test_token_auth(api_client, audit_api, monkeypatch) -> None:
    monkeypatch.setenv("AUDIT_API_TOKEN", "test-token")
    assert audit_api.get("/api/v1/audit/summary").status_code == 401
    assert (
        audit_api.get("/api/v1/audit/summary", headers={"X-Audit-Token": "wrong"}).status_code
        == 401
    )
    assert (
        audit_api.get(
            "/api/v1/audit/summary", headers={"X-Audit-Token": "test-token"}
        ).status_code
        == 200
    )


def test_judge_disabled_returns_partial_not_pass(audit_api, monkeypatch) -> None:
    monkeypatch.setenv("AUDIT_JUDGE_ENABLED", "false")
    response = audit_api.post(
        f"/api/v1/audit/traces/{TRACE_ID}/evaluate", json={"mode": "llm_only"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "partial"
    assert body["overall_risk"] == "unknown"
