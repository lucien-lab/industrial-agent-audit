"""知识库 API 测试：参数校验、检索、问答、文档列表与健康检查。

使用隔离测试库 + Fake Embedding/LLM（真实 HTTP 模型路径由端到端评估脚本覆盖）。
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from tests.helpers_rag import DIM, FakeChat, FakeEmbedder, add_chunk, add_document

MODEL = "bge-m3"
VERSION = "bge-m3-1024-v1"


def _unit(index: int) -> list[float]:
    vector = [0.0] * DIM
    vector[index] = 1.0
    return vector


def _seed(engine):
    with Session(engine) as session:
        doc = add_document(
            session, doc_key="metropt3-data-description", doc_type="dataset_description"
        )
        add_chunk(
            session,
            doc,
            index=0,
            content="TP2 (bar) – the measure of the pressure on the compressor.",
            page=2,
            vector=_unit(0),
            embedding_model=MODEL,
            embedding_version=VERSION,
        )
        add_chunk(
            session,
            doc,
            index=1,
            content="TP3 (bar) – the measure of the pressure generated at the pneumatic panel.",
            page=2,
            vector=_unit(1),
            embedding_model=MODEL,
            embedding_version=VERSION,
        )
        session.commit()


def _patch_embedding(monkeypatch, query_vector=None):
    embedder = FakeEmbedder(query_vector=query_vector or _unit(0))
    embedder.model = MODEL
    embedder.version = VERSION
    monkeypatch.setattr(
        "app.services.knowledge.get_embedding_provider", lambda settings: embedder
    )
    return embedder


def test_search_returns_evidence_with_pages(api_client, clean_engine, monkeypatch):
    _seed(clean_engine)
    _patch_embedding(monkeypatch)
    response = api_client.post(
        "/api/v1/knowledge/search",
        json={"query": "TP2 是什么？", "top_k": 2, "method": "hybrid"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["method"] == "hybrid"
    assert body["results"]
    first = body["results"][0]
    assert first["doc_key"] == "metropt3-data-description"
    assert first["page_start"] == 2
    assert first["chunk_id"] > 0
    assert first["equipment_scope"] == "metropt3_direct"
    assert body["retrieval"]["candidate_counts"]["returned"] == len(body["results"])


def test_search_validates_top_k_and_scope(api_client, clean_engine):
    _seed(clean_engine)
    assert api_client.post(
        "/api/v1/knowledge/search", json={"query": "TP2", "top_k": 21}
    ).status_code == 422
    assert api_client.post(
        "/api/v1/knowledge/search", json={"query": "TP2", "top_k": 0}
    ).status_code == 422
    assert api_client.post(
        "/api/v1/knowledge/search", json={"query": ""}
    ).status_code == 422
    invalid = api_client.post(
        "/api/v1/knowledge/search",
        json={"query": "TP2", "equipment_scopes": ["not-a-scope"]},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_parameter"


def test_search_no_results_with_high_similarity_threshold(api_client, clean_engine, monkeypatch):
    _seed(clean_engine)
    _patch_embedding(monkeypatch, query_vector=_unit(5))
    response = api_client.post(
        "/api/v1/knowledge/search",
        json={"query": "zzz 无匹配", "method": "hybrid", "min_similarity": 0.99},
    )
    assert response.status_code == 200
    assert response.json()["results"] == []


def test_search_on_empty_index_returns_503(api_client, clean_engine, monkeypatch):
    _patch_embedding(monkeypatch)
    response = api_client.post("/api/v1/knowledge/search", json={"query": "TP2"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "index_model_mismatch"


def test_ask_returns_answer_and_verified_citation(api_client, clean_engine, monkeypatch):
    _seed(clean_engine)
    _patch_embedding(monkeypatch)
    chat = FakeChat(
        {
            "answer": "TP2 测量压缩机压力 [C1]。",
            "insufficient_evidence": False,
            "used_labels": ["C1"],
            "support_quotes": {
                "C1": "TP2 (bar) – the measure of the pressure on the compressor."
            },
        }
    )
    chat.model = "fake-chat"
    monkeypatch.setattr("app.services.knowledge.get_chat_model", lambda settings: chat)

    response = api_client.post(
        "/api/v1/knowledge/ask", json={"question": "TP2 测量什么？", "top_k": 2}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered"
    assert body["llm_status"] == "ok"
    assert body["citations"][0]["page_start"] == 2
    assert body["citation_validation"]["quote_supported"] == "all"
    assert body["timings_ms"]["total"] >= 0
    assert body["model"]["name"] == "fake-chat"


def test_ask_validates_question_length(api_client, clean_engine):
    assert api_client.post(
        "/api/v1/knowledge/ask", json={"question": ""}
    ).status_code == 422
    assert api_client.post(
        "/api/v1/knowledge/ask", json={"question": "x" * 501}
    ).status_code == 422


def test_documents_endpoint_lists_ingested_docs(api_client, clean_engine):
    _seed(clean_engine)
    response = api_client.get("/api/v1/knowledge/documents")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["doc_key"] == "metropt3-data-description"
    assert item["chunks"] == {"structural-v1": 2}
    assert item["file_sha256"]


def test_documents_endpoint_pagination_validation(api_client, clean_engine):
    assert api_client.get("/api/v1/knowledge/documents?limit=0").status_code == 422
    assert api_client.get("/api/v1/knowledge/documents?limit=500").status_code == 422
    assert api_client.get("/api/v1/knowledge/documents?offset=-1").status_code == 422


def test_knowledge_health_reports_schema_and_index(api_client, clean_engine):
    _seed(clean_engine)
    response = api_client.get("/api/v1/knowledge/health")
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "0008_agent_audit"
    assert body["schema_ok"] is True
    assert body["documents"] == 1
    assert body["chunks"] == 2
    assert body["indexes"][0]["embedding_model"] == MODEL
    assert body["embedding"]["indexed_chunks"] == 2


def test_phase1c_endpoints_still_work(api_client):
    """Phase 1C 回归：知识库接口加入后原健康检查仍正常。"""
    assert api_client.get("/health").status_code == 200
    assert api_client.get("/openapi.json").status_code == 200
    paths = api_client.get("/openapi.json").json()["paths"]
    assert "/api/v1/knowledge/search" in paths
    assert "/api/v1/knowledge/ask" in paths
    assert "/api/v1/equipment" in paths
