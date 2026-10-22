"""检索结果的数据结构与过滤条件。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.rag.config import DEFAULT_ALLOWED_SCOPES


@dataclass(frozen=True)
class RetrievalFilters:
    """检索过滤条件。默认只允许“直接关联 + 同类型通用”两类文档。"""

    equipment_scopes: tuple[str, ...] = DEFAULT_ALLOWED_SCOPES
    doc_types: tuple[str, ...] = ()
    doc_keys: tuple[str, ...] = ()
    chunking_version: str | None = None
    embedding_model: str | None = None
    embedding_version: str | None = None
    min_similarity: float = 0.0

    def describe(self) -> dict[str, object]:
        return {
            "equipment_scopes": list(self.equipment_scopes),
            "doc_types": list(self.doc_types),
            "doc_keys": list(self.doc_keys),
            "chunking_version": self.chunking_version,
            "embedding_model": self.embedding_model,
            "embedding_version": self.embedding_version,
            "min_similarity": self.min_similarity,
        }


@dataclass
class RetrievedChunk:
    chunk_id: int
    document_id: str
    doc_key: str
    document_title: str
    chunk_index: int
    content: str
    page_start: int
    page_end: int
    section_title: str | None
    section_path: str | None
    equipment_scope: str
    doc_type: str
    trust_level: str
    source_url: str | None
    document_version: str | None
    license: str | None
    chunking_version: str
    embedding_model: str | None
    score: float = 0.0
    vector_similarity: float | None = None
    vector_rank: int | None = None
    keyword_score: float | None = None
    keyword_rank: int | None = None
    rrf_score: float | None = None
    matched_terms: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def citation_label(self) -> str:
        return f"{self.doc_key}:p{self.page_start}" + (
            f"-{self.page_end}" if self.page_end != self.page_start else ""
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "doc_key": self.doc_key,
            "document_title": self.document_title,
            "chunk_index": self.chunk_index,
            "content": self.content,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "section_title": self.section_title,
            "section_path": self.section_path,
            "equipment_scope": self.equipment_scope,
            "doc_type": self.doc_type,
            "trust_level": self.trust_level,
            "source_url": self.source_url,
            "document_version": self.document_version,
            "license": self.license,
            "chunking_version": self.chunking_version,
            "embedding_model": self.embedding_model,
            "score": round(self.score, 6),
            "vector_similarity": (
                round(self.vector_similarity, 6)
                if self.vector_similarity is not None
                else None
            ),
            "vector_rank": self.vector_rank,
            "keyword_score": (
                round(self.keyword_score, 6) if self.keyword_score is not None else None
            ),
            "keyword_rank": self.keyword_rank,
            "rrf_score": round(self.rrf_score, 6) if self.rrf_score is not None else None,
            "matched_terms": self.matched_terms,
            "citation_label": self.citation_label,
        }


@dataclass
class RetrievalResult:
    method: str
    query: str
    results: list[RetrievedChunk]
    elapsed_ms: float
    filters: dict[str, object]
    vector_candidates: int = 0
    keyword_candidates: int = 0
    embedding_elapsed_ms: float = 0.0
    query_terms: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self, include_content: bool = True) -> dict[str, object]:
        items = []
        for item in self.results:
            payload = item.to_dict()
            if not include_content:
                payload.pop("content", None)
            items.append(payload)
        return {
            "method": self.method,
            "query": self.query,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "embedding_elapsed_ms": round(self.embedding_elapsed_ms, 2),
            "filters": self.filters,
            "query_terms": self.query_terms,
            "candidate_counts": {
                "vector": self.vector_candidates,
                "keyword": self.keyword_candidates,
                "returned": len(self.results),
            },
            "warnings": self.warnings,
            "results": items,
        }
