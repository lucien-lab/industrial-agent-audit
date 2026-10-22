"""检索测试（隔离测试库）：向量、关键词、RRF 融合、过滤、模型版本一致性。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.knowledge import DocumentChunk
from app.rag.errors import IndexModelMismatch
from app.rag.retrieval import (
    KnowledgeRetriever,
    RetrievalFilters,
    RetrievedChunk,
    reciprocal_rank_fusion,
)
from app.rag.retrieval.keyword import extract_query_terms, keyword_search
from app.rag.retrieval.vector import VectorSearch
from tests.helpers_rag import (
    DIM,
    FakeEmbedder,
    add_chunk,
    add_document,
    fake_vector,
    make_chunk,
    rag_settings,
)


def _unit_vector(index: int, dim: int = DIM) -> list[float]:
    vector = [0.0] * dim
    vector[index] = 1.0
    return vector


def _seed(session):
    doc = add_document(session, doc_key="metropt3-data-description")
    other = add_document(
        session,
        doc_key="other-model-manual",
        scope="model_specific_other",
        doc_type="technical_manual",
    )
    a = add_chunk(
        session, doc, index=0, content="TP2 measures compressor pressure.", page=2,
        vector=_unit_vector(0),
    )
    b = add_chunk(
        session, doc, index=1, content="TP2 and TP3 definitions with 8.2 bar limit.", page=2,
        vector=[0.9, 0.1] + [0.0] * (DIM - 2),
    )
    c = add_chunk(
        session, doc, index=2, content="TP3 measures pneumatic panel pressure.", page=3,
        vector=_unit_vector(1),
    )
    excluded = add_chunk(
        session, other, index=0, content="TP2 of another model is configured at 10 bar.",
        page=1, vector=_unit_vector(0),
    )
    session.commit()
    return {"a": a, "b": b, "c": c, "excluded": excluded, "doc": doc, "other": other}


def test_vector_search_orders_by_cosine_similarity(session):
    ids = _seed(session)
    embedder = FakeEmbedder(query_vector=_unit_vector(0))
    results, embedding_ms = VectorSearch(embedder).search(
        session,
        "TP2",
        filters=RetrievalFilters(
            chunking_version="structural-v1",
            embedding_model=embedder.model,
            embedding_version=embedder.version,
        ),
        top_k=3,
    )
    assert [chunk.chunk_id for chunk in results] == [
        ids["a"].id,
        ids["b"].id,
        ids["c"].id,
    ]
    assert results[0].vector_similarity == pytest.approx(1.0, abs=1e-6)
    assert embedding_ms >= 0


def test_vector_search_respects_equipment_scope(session):
    ids = _seed(session)
    embedder = FakeEmbedder(query_vector=_unit_vector(0))
    results = VectorSearch(embedder).search(
        session,
        "TP2",
        filters=RetrievalFilters(
            equipment_scopes=("metropt3_direct",),
            chunking_version="structural-v1",
            embedding_model=embedder.model,
            embedding_version=embedder.version,
        ),
        top_k=10,
    )[0]
    returned = {chunk.chunk_id for chunk in results}
    assert ids["excluded"].id not in returned
    assert all(chunk.equipment_scope == "metropt3_direct" for chunk in results)


def test_vector_search_model_version_mismatch_raises(session):
    _seed(session)
    with pytest.raises(IndexModelMismatch) as excinfo:
        VectorSearch(FakeEmbedder()).search(
            session,
            "TP2",
            filters=RetrievalFilters(
                chunking_version="structural-v1",
                embedding_model="fake-embed",
                embedding_version="new-version",
            ),
            top_k=3,
        )
    assert "不一致" in excinfo.value.message


def test_vector_search_missing_chunking_version_raises(session):
    _seed(session)
    with pytest.raises(IndexModelMismatch):
        VectorSearch(FakeEmbedder()).search(
            session,
            "TP2",
            filters=RetrievalFilters(
                chunking_version="does-not-exist", embedding_model="fake-embed",
                embedding_version="fake-v1",
            ),
            top_k=3,
        )


def test_vector_min_similarity_filters_low_scores(session):
    _seed(session)
    embedder = FakeEmbedder(query_vector=_unit_vector(0))
    results = VectorSearch(embedder).search(
        session,
        "TP2",
        filters=RetrievalFilters(
            chunking_version="structural-v1", embedding_model=embedder.model,
            embedding_version=embedder.version, min_similarity=0.99,
        ),
        top_k=10,
    )[0]
    assert all(chunk.vector_similarity >= 0.99 for chunk in results)


def test_keyword_search_matches_ascii_identifier(session):
    ids = _seed(session)
    results = keyword_search(
        session, "TP2 的含义", filters=RetrievalFilters(), top_k=5
    )
    assert results
    assert ids["a"].id in {chunk.chunk_id for chunk in results}
    assert any("tp2" in chunk.matched_terms for chunk in results)


def test_keyword_search_matches_chinese_bigrams(session):
    doc = add_document(session, doc_key="zh-doc")
    add_chunk(
        session, doc, index=0,
        content="储气压力异常时应检查排水阀与压力开关。",
    )
    session.commit()
    results = keyword_search(
        session, "储气压力异常", filters=RetrievalFilters(), top_k=5
    )
    assert results
    assert results[0].keyword_score > 0
    assert "储气" in results[0].matched_terms


def test_keyword_search_returns_empty_without_terms(session):
    _seed(session)
    assert keyword_search(session, "???", filters=RetrievalFilters(), top_k=5) == []


def test_extract_query_terms_keeps_identifiers_and_units():
    terms = extract_query_terms("TP2 与 DV_pressure 在 8.2 bar 下的关系")
    assert "tp2" in terms.ascii_terms
    assert "dv_pressure" in terms.ascii_terms
    assert "8.2" in terms.ascii_terms
    assert any("关系" in bigram for bigram in terms.cjk_bigrams)


def test_hybrid_fusion_ranks_chunk_hit_by_both_signals_first(session):
    ids = _seed(session)
    settings = rag_settings(
        chunking_version="structural-v1",
        embedding_model="fake-embed",
        embedding_version="fake-v1",
    )
    retriever = KnowledgeRetriever(
        session, settings, FakeEmbedder(query_vector=_unit_vector(0))
    )
    result = retriever.search("TP3", method="hybrid", top_k=5)
    ordered = [chunk.chunk_id for chunk in result.results]
    assert ordered[0] == ids["b"].id  # 唯一同时被向量与关键词命中的 Chunk（B 含 TP3）
    assert len(ordered) == len(set(ordered))  # 去重
    assert result.results[0].vector_rank == 2
    assert result.results[0].keyword_rank == 1
    assert result.results[0].rrf_score and result.results[0].rrf_score > 0
    assert result.vector_candidates and result.keyword_candidates


def test_rrf_dedupes_and_combines_ranks():
    a = make_chunk(1)
    b = make_chunk(2)
    c = make_chunk(3)
    fused = reciprocal_rank_fusion(
        {"vector": [a, b], "keyword": [b, c]}, rrf_k=60, top_k=10
    )
    assert [chunk.chunk_id for chunk in fused] == [2, 1, 3]
    assert fused[0].rrf_score == pytest.approx(1 / 62 + 1 / 61)
    assert fused[0].vector_rank == 2 and fused[0].keyword_rank == 1


def test_hybrid_retrieval_filters_model_specific_docs(session):
    _seed(session)
    settings = rag_settings(
        chunking_version="structural-v1",
        embedding_model="fake-embed",
        embedding_version="fake-v1",
    )
    retriever = KnowledgeRetriever(session, settings, FakeEmbedder())
    result = retriever.search("TP2", method="hybrid", top_k=10)
    assert all(chunk.equipment_scope == "metropt3_direct" for chunk in result.results)


def test_retrieval_filters_by_doc_type_and_doc_key(session):
    _seed(session)
    settings = rag_settings(
        chunking_version="structural-v1",
        embedding_model="fake-embed",
        embedding_version="fake-v1",
    )
    retriever = KnowledgeRetriever(session, settings, FakeEmbedder())
    result = retriever.search(
        "TP2",
        method="hybrid",
        top_k=10,
        filters=RetrievalFilters(
            equipment_scopes=("metropt3_direct",),
            doc_keys=("metropt3-data-description",),
            chunking_version="structural-v1",
            embedding_model="fake-embed",
            embedding_version="fake-v1",
        ),
    )
    assert result.results
    assert all(chunk.doc_key == "metropt3-data-description" for chunk in result.results)


def test_chunking_version_filter_isolates_indexes(session):
    doc = add_document(session, doc_key="doc")
    add_chunk(session, doc, index=0, content="TP2 pressure", chunking_version="structural-v1")
    add_chunk(session, doc, index=0, content="TP2 pressure", chunking_version="fixed-v1")
    session.commit()
    results = keyword_search(
        session, "TP2", filters=RetrievalFilters(chunking_version="fixed-v1"), top_k=5
    )
    assert results
    versions = set(
        session.execute(
            select(DocumentChunk.chunking_version).where(
                DocumentChunk.id.in_([chunk.chunk_id for chunk in results])
            )
        ).scalars()
    )
    assert versions == {"fixed-v1"}
