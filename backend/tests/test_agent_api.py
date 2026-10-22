"""Agent HTTP 接口测试（TestClient + 隔离测试库 + 确定性回答模式）。

说明：回答生成使用 `AGENT_ANSWER_MODE=deterministic`，避免在自动化测试中调用真实模型；
真实模型端到端验证见 `test_agent_e2e_real.py`。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from tests.helpers_rag import add_chunk, add_document, fake_vector

CHUNK_TEXT = "TP2 (bar) – the measure of the pressure on the compressor."


@pytest.fixture()
def agent_api(api_dataset, monkeypatch):
    """真实数据样本 + 一条知识片段；Agent 使用确定性渲染。"""
    monkeypatch.setenv("AGENT_ANSWER_MODE", "deterministic")
    monkeypatch.setenv("AGENT_TRACE_WRITE_FILES", "false")
    monkeypatch.delenv("AGENT_PLANNER", raising=False)
    from app.agent.trace import get_trace_store

    get_trace_store().clear()
    with Session(api_dataset if hasattr(api_dataset, "begin") else None) as session:  # type: ignore[arg-type]
        pass
    return api_dataset


@pytest.fixture()
def seeded_knowledge(clean_engine, agent_api, monkeypatch):
    from tests.helpers_rag import FakeEmbedder

    monkeypatch.setenv("EMBEDDING_MODEL", "fake-embed")
    monkeypatch.setenv("EMBEDDING_VERSION", "fake-v1")
    # HTTP 层无法注入 embedder：这里替换 Service 内部的 Provider 工厂（仅测试）
    monkeypatch.setattr(
        "app.services.knowledge.get_embedding_provider", lambda settings: FakeEmbedder()
    )
    with Session(clean_engine) as session:
        document = add_document(session, doc_key="metropt3-data-description")
        add_chunk(
            session, document, index=0, content=CHUNK_TEXT, page=2, vector=fake_vector("tp2 pressure")
        )
        session.commit()
    return clean_engine


def test_chat_returns_evidence_and_trace_id(api_client, agent_api) -> None:
    response = api_client.post(
        "/api/v1/agent/chat",
        json={"question": "2020年2月1日 00:00 到 00:30 电机电流的最大值是多少？"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["trace_id"]
    assert response.headers["X-Agent-Trace-Id"] == body["trace_id"]
    assert body["intent"] == "data_only"
    assert body["execution_status"] == "completed"
    assert body["tools_used"] == ["get_sensor_statistics"]
    assert body["answer"].startswith("## 结论")
    assert body["evidence"]
    assert body["total_duration_ms"] >= 0
    assert body["agent_version"]
    assert body["trace"] is None  # 默认不返回 Trace


def test_chat_out_of_scope_returns_refused(api_client, agent_api) -> None:
    response = api_client.post("/api/v1/agent/chat", json={"question": "帮我写一首诗"})
    assert response.status_code == 200
    body = response.json()
    assert body["execution_status"] == "refused"
    assert body["tools_used"] == []
    assert body["answer_status"] == "refused"


def test_chat_include_trace(api_client, agent_api) -> None:
    response = api_client.post(
        "/api/v1/agent/chat",
        json={"question": "2020年2月1日 00:00 到 00:30 电机电流的最大值", "include_trace": True},
    )
    body = response.json()
    assert body["trace"] is not None
    assert body["trace"]["trace_id"] == body["trace_id"]
    assert body["trace"]["event_count"] > 0


def test_trace_endpoint(api_client, agent_api) -> None:
    chat = api_client.post(
        "/api/v1/agent/chat",
        json={"question": "2020年2月1日 00:00 到 00:30 电机电流的最大值"},
    ).json()
    trace_id = chat["trace_id"]

    summary = api_client.get(f"/api/v1/agent/traces/{trace_id}")
    assert summary.status_code == 200
    payload = summary.json()
    assert payload["trace_id"] == trace_id
    assert payload["tools_used"] == ["get_sensor_statistics"]
    assert payload["event_count"] > 0
    assert "events" not in payload or payload["events"] is None

    with_events = api_client.get(f"/api/v1/agent/traces/{trace_id}?include_events=true")
    assert with_events.status_code == 200
    events = with_events.json()["events"]
    assert events and {"event_type", "seq", "started_at", "duration_ms"} <= set(events[0])


def test_trace_not_found_is_standard_error(api_client, agent_api) -> None:
    response = api_client.get("/api/v1/agent/traces/does-not-exist")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "trace_not_found"
    assert "does-not-exist" in error["details"]["trace_id"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"question": ""},
        {"question": "   "},
        {"question": "x" * 501},
        {"question": "电流多少", "equipment_id": "bad id!"},
        {"question": "电流多少", "sensors": ["not_a_sensor"]},
        {"question": "电流多少", "start_time": "2020-04-17T00:00:00+08:00", "end_time": "2020-04-18"},
        {"question": "电流多少", "start_time": "2020-04-17"},
        {"question": "电流多少", "tools": ["run_sql"]},
        {"question": "电流多少", "sql": "select * from equipment"},
        {"question": "电流多少", "planner": "gpt"},
    ],
)
def test_chat_request_validation(api_client, agent_api, payload) -> None:
    response = api_client.post("/api/v1/agent/chat", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_parameter"


def test_chat_does_not_leak_internal_details(api_client, agent_api) -> None:
    body = api_client.post(
        "/api/v1/agent/chat",
        json={"question": "2020年2月1日 00:00 到 00:30 电机电流的最大值", "include_trace": True},
    ).json()
    payload = json.dumps(body, ensure_ascii=False).lower()
    for forbidden in ("postgresql+psycopg", "password", "app_user", "traceback"):
        assert forbidden not in payload


def test_chat_unknown_equipment_is_200_with_insufficient_evidence(api_client, agent_api) -> None:
    response = api_client.post(
        "/api/v1/agent/chat",
        json={
            "question": "2020年2月1日 00:00 到 00:30 电机电流的最大值",
            "equipment_id": "unknown-device",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["execution_status"] == "insufficient_evidence"
    assert body["errors"][0]["category"] == "not_found"


def test_chat_empty_window_is_insufficient_evidence(api_client, agent_api) -> None:
    body = api_client.post(
        "/api/v1/agent/chat",
        json={"question": "2019年1月1日 电机电流的最大值是多少？"},
    ).json()
    assert body["execution_status"] == "insufficient_evidence"
    assert "没有查到任何观测记录" in body["answer"]


def test_chat_knowledge_question_uses_citations(api_client, seeded_knowledge) -> None:
    body = api_client.post(
        "/api/v1/agent/chat", json={"question": "TP2 的物理含义是什么？"}
    ).json()
    assert body["intent"] == "knowledge_only"
    assert body["tools_used"] == ["search_technical_knowledge"]
    knowledge = [item for item in body["evidence"] if item["kind"] == "knowledge"]
    assert knowledge
    assert knowledge[0]["chunk_id"] and knowledge[0]["page_start"] == 2
    assert knowledge[0]["quote"].startswith("TP2 (bar)")


def test_openapi_documents_agent_endpoints(api_client, agent_api) -> None:
    spec = api_client.get("/openapi.json").json()
    assert "/api/v1/agent/chat" in spec["paths"]
    assert "/api/v1/agent/traces/{trace_id}" in spec["paths"]
    post = spec["paths"]["/api/v1/agent/chat"]["post"]
    assert post["tags"] == ["Agent"]
