"""RAG 知识检索工具（复用 Phase 2 混合检索，不重建索引、不改知识库）。

关键边界：
- 只做**检索**，不调用 `ask_knowledge`：本阶段不接受另一条未经核验的 RAG 生成回答作为事实输入；
- 不新建向量库、不重新生成 Embedding、不写 `documents` / `document_chunks`；
- 返回必须包含文档名、文档 ID、Chunk ID、页码、片段、相关性分数与设备适用范围；
- 通用资料（`generic_compressor`）在回答阶段必须显式标注，不能当作本设备专属参数。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.tools.base import AgentToolContext, EvidenceItem, ToolResult
from app.rag.config import EQUIPMENT_SCOPES, get_rag_settings
from app.services import knowledge as knowledge_service

MAX_KNOWLEDGE_TOP_K = 8
MAX_EXCERPT_CHARS = 1200
_SEARCH_METHODS = ("hybrid", "vector", "keyword")


class KnowledgeSearchInput(BaseModel):
    """知识检索工具输入。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2, max_length=300, description="检索查询（自然语言或标识符）")
    top_k: int = Field(default=5, ge=1, le=MAX_KNOWLEDGE_TOP_K, description="返回片段数上限")
    method: Literal["hybrid", "vector", "keyword"] = Field(
        default="hybrid", description="检索方法：hybrid（默认）/ vector / keyword"
    )
    doc_keys: list[str] | None = Field(
        default=None, max_length=10, description="限定文档 doc_key（可选）"
    )
    equipment_scopes: list[str] | None = Field(
        default=None, max_length=len(EQUIPMENT_SCOPES), description="设备适用范围过滤（可选）"
    )
    min_similarity: float | None = Field(
        default=None, ge=0.0, le=1.0, description="向量相似度下限（仅对向量检索生效）"
    )

    @field_validator("query")
    @classmethod
    def _clean_query(cls, value: str) -> str:
        text = " ".join(value.split())
        if not text:
            raise ValueError("query 不能为空")
        return text

    @field_validator("equipment_scopes")
    @classmethod
    def _check_scopes(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unknown = [scope for scope in value if scope not in EQUIPMENT_SCOPES]
        if unknown:
            raise ValueError(f"未知的设备适用范围：{unknown}；可选值：{list(EQUIPMENT_SCOPES)}")
        return value

    @field_validator("doc_keys")
    @classmethod
    def _check_doc_keys(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        unknown = [key for key in value if not key.replace("-", "").replace("_", "").isalnum()]
        if unknown:
            raise ValueError(f"doc_key 只能包含字母、数字、连字符与下划线：{unknown}")
        return value


def search_technical_knowledge(ctx: AgentToolContext, payload: BaseModel) -> ToolResult:
    params = KnowledgeSearchInput.model_validate(payload)
    settings = get_rag_settings()
    spec = knowledge_service.SearchSpec(
        query=params.query,
        method=params.method,
        top_k=params.top_k,
        equipment_scopes=tuple(params.equipment_scopes) if params.equipment_scopes else None,
        doc_keys=tuple(params.doc_keys or ()),
        min_similarity=params.min_similarity,
    )
    with ctx.open_knowledge_session() as session:
        result = knowledge_service.search_knowledge(
            session, settings, spec, embedder=getattr(ctx, "embedder", None)
        )

    chunks: list[dict[str, Any]] = []
    evidence: list[EvidenceItem] = []
    for item in result.get("results", []):
        content = str(item.get("content") or "")
        excerpt = " ".join(content.split())[:MAX_EXCERPT_CHARS]
        chunk = {
            "chunk_id": item.get("chunk_id"),
            "document_id": item.get("document_id"),
            "doc_key": item.get("doc_key"),
            "document_title": item.get("document_title"),
            "page_start": item.get("page_start"),
            "page_end": item.get("page_end"),
            "section_title": item.get("section_title"),
            "section_path": item.get("section_path"),
            "equipment_scope": item.get("equipment_scope"),
            "doc_type": item.get("doc_type"),
            "trust_level": item.get("trust_level"),
            "source_url": item.get("source_url"),
            "license": item.get("license"),
            "chunking_version": item.get("chunking_version"),
            "embedding_model": item.get("embedding_model"),
            "citation_label": item.get("citation_label"),
            "score": item.get("score"),
            "vector_similarity": item.get("vector_similarity"),
            "keyword_score": item.get("keyword_score"),
            "rrf_score": item.get("rrf_score"),
            "matched_terms": item.get("matched_terms") or [],
            "content_chars": len(content),
            "excerpt": excerpt,
        }
        chunks.append(chunk)
        evidence.append(
            EvidenceItem(
                kind="knowledge",
                origin_tool="search_technical_knowledge",
                source={
                    "kind": "document",
                    "doc_key": chunk["doc_key"],
                    "document_id": chunk["document_id"],
                    "document_title": chunk["document_title"],
                    "page_start": chunk["page_start"],
                    "page_end": chunk["page_end"],
                    "section_title": chunk["section_title"],
                    "equipment_scope": chunk["equipment_scope"],
                    "trust_level": chunk["trust_level"],
                    "source_url": chunk["source_url"],
                    "chunk_id": chunk["chunk_id"],
                    "chunking_version": chunk["chunking_version"],
                    "retrieval_method": result.get("method"),
                },
                detail={
                    "score": chunk["score"],
                    "vector_similarity": chunk["vector_similarity"],
                    "keyword_score": chunk["keyword_score"],
                    "rrf_score": chunk["rrf_score"],
                    "matched_terms": chunk["matched_terms"],
                },
                quote=excerpt,
            )
        )

    retrieval = result.get("retrieval", {})
    data = {
        "query": result.get("query", params.query),
        "method": result.get("method", params.method),
        "top_k": result.get("top_k", params.top_k),
        "returned": len(chunks),
        "chunks": chunks,
        "retrieval": {
            "elapsed_ms": retrieval.get("elapsed_ms"),
            "embedding_elapsed_ms": retrieval.get("embedding_elapsed_ms"),
            "candidate_counts": retrieval.get("candidate_counts"),
            "filters": retrieval.get("filters"),
            "warnings": retrieval.get("warnings", []),
        },
        "warnings": result.get("warnings", []),
        "source": {
            "service": "services.knowledge.search_knowledge",
            "engine": "phase2 hybrid retrieval (RRF)",
            "embedding_model": settings.embedding_model,
            "embedding_version": settings.embedding_version,
            "chunking_version": settings.chunking_version,
        },
    }
    return ToolResult(status="ok" if chunks else "empty", data=data, evidence=evidence)


__all__ = ["MAX_KNOWLEDGE_TOP_K", "KnowledgeSearchInput", "search_technical_knowledge"]
