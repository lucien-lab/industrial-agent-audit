"""RAG 测试用的确定性替身与构造工具（只用于单元/集成测试，不进入生产路径）。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import fitz
from sqlalchemy.orm import Session

from app.models.knowledge import Document, DocumentChunk
from app.rag.config import RAGSettings, get_rag_settings
from app.rag.embedding.base import EmbeddingBatch
from app.rag.llm.base import LLMResponse
from app.rag.retrieval import RetrievalResult, RetrievedChunk

DIM = 1024


def fake_vector(seed: str, dim: int = DIM) -> list[float]:
    """由文本哈希生成的确定性向量（仅测试用；生产路径禁止）"""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    values = [((digest[i % len(digest)] / 255.0) - 0.5) for i in range(dim)]
    norm = sum(v * v for v in values) ** 0.5
    return [v / norm for v in values]


class FakeEmbedder:
    provider = "fake"
    model = "fake-embed"
    version = "fake-v1"

    def __init__(self, dim: int = DIM, query_vector: list[float] | None = None) -> None:
        self.dim = dim
        self.query_vector = query_vector or fake_vector("query", dim)
        self.calls: list[list[str]] = []

    def _batch(self, texts: list[str]) -> EmbeddingBatch:
        self.calls.append(list(texts))
        return EmbeddingBatch(
            vectors=[fake_vector(text, self.dim) for text in texts],
            model=self.model,
            dim=self.dim,
            texts=len(texts),
            elapsed_ms=1.0,
            provider=self.provider,
            attempts=1,
        )

    def embed_documents(self, texts: list[str]) -> EmbeddingBatch:
        return self._batch(texts)

    def embed_query(self, text: str) -> EmbeddingBatch:
        self.calls.append([text])
        return EmbeddingBatch(
            vectors=[self.query_vector],
            model=self.model,
            dim=self.dim,
            texts=1,
            elapsed_ms=1.0,
            provider=self.provider,
            attempts=1,
        )


class FakeChat:
    provider = "fake"
    model = "fake-chat"

    def __init__(self, payload: dict | str) -> None:
        self.payload = payload
        self.calls: list[list] = []
        self.last_messages: list = []

    def chat(self, messages, *, json_mode=False, temperature=None, max_tokens=None):
        self.calls.append(messages)
        self.last_messages = messages
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return LLMResponse(
            text=text, model=self.model, provider=self.provider, elapsed_ms=1.0, attempts=1
        )


def rag_settings(**overrides) -> RAGSettings:
    return replace(get_rag_settings(), **overrides)


def make_pdf(path, pages: list[str]) -> None:
    document = fitz.open()
    for text in pages:
        page = document.new_page()
        y = 72
        for line in text.split("\n"):
            page.insert_text((72, y), line, fontsize=11)
            y += 16
    document.save(path)
    document.close()


def add_document(
    session: Session,
    *,
    doc_key: str,
    title: str | None = None,
    scope: str = "metropt3_direct",
    doc_type: str = "technical_manual",
    chunking_version: str = "structural-v1",
    embedding_model: str = "fake-embed",
    embedding_version: str = "fake-v1",
) -> Document:
    document = Document(
        doc_key=doc_key,
        title=title or doc_key,
        file_path=f"knowledge/raw/{doc_key}.pdf",
        source_url="https://example.invalid/doc",
        file_sha256=hashlib.sha256(doc_key.encode()).hexdigest(),
        doc_type=doc_type,
        equipment_scope=scope,
        trust_level="authoritative_secondary",
        license="test-only",
        parse_status="parsed",
        content_revision=1,
    )
    session.add(document)
    session.flush()
    return document


def add_chunk(
    session: Session,
    document: Document,
    *,
    index: int,
    content: str,
    page: int = 1,
    section: str | None = "Test Section",
    vector: list[float] | None = None,
    chunking_version: str = "structural-v1",
    embedding_model: str | None = "fake-embed",
    embedding_version: str | None = "fake-v1",
) -> DocumentChunk:
    chunk = DocumentChunk(
        document_id=document.id,
        chunk_index=index,
        content=content,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        char_count=len(content),
        page_start=page,
        page_end=page,
        section_title=section,
        section_path=section,
        search_text=f"{section or ''} {content}".lower(),
        embedding=vector if vector is not None else fake_vector(content),
        embedding_model=embedding_model,
        embedding_version=embedding_version,
        embedding_dim=len(vector) if vector is not None else DIM,
        chunking_strategy="structural",
        chunking_version=chunking_version,
        meta={},
    )
    session.add(chunk)
    session.flush()
    return chunk


def make_chunk(chunk_id: int, *, content: str = "text", page: int = 1, doc_key: str = "doc") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=f"00000000-0000-0000-0000-{chunk_id:012d}",
        doc_key=doc_key,
        document_title=doc_key,
        chunk_index=chunk_id,
        content=content,
        page_start=page,
        page_end=page,
        section_title="Section",
        section_path="Section",
        equipment_scope="metropt3_direct",
        doc_type="technical_manual",
        trust_level="official_primary",
        source_url=None,
        document_version=None,
        license="test",
        chunking_version="structural-v1",
        embedding_model="fake-embed",
    )


class StubRetriever:
    """直接返回预设检索结果的检索器替身（不访问数据库/模型）。"""

    def __init__(self, chunks: list[RetrievedChunk]) -> None:
        self.chunks = chunks
        self.calls: list[str] = []
        self.query_terms: dict[str, list[str]] = {}

    def default_filters(self):
        from app.rag.retrieval import RetrievalFilters

        return RetrievalFilters()

    def search(self, query, *, method="hybrid", top_k=5, filters=None) -> RetrievalResult:
        self.calls.append(query)
        selected = self.chunks[:top_k]
        return RetrievalResult(
            method=method,
            query=query,
            results=selected,
            elapsed_ms=1.0,
            filters={"chunking_version": "structural-v1"},
            vector_candidates=len(selected),
            keyword_candidates=len(selected),
            warnings=[],
        )
