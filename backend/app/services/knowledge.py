"""知识库业务服务：检索、问答、文档列表、健康检查。

分层延续 Phase 1C：Router 只做 HTTP 装配，本模块承载业务逻辑，可直接被后续 Agent Tool 复用。
所有模型客户端都可注入（测试用 Fake），但生产路径必须真实调用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.knowledge import Document, DocumentChunk
from app.rag.answer import RAGAnswerer
from app.rag.config import DEFAULT_ALLOWED_SCOPES, RAGSettings
from app.rag.embedding import EmbeddingProvider, get_embedding_provider
from app.rag.errors import RAGError
from app.rag.llm import ChatMessage, ChatModel, get_chat_model
from app.rag.retrieval import KnowledgeRetriever, RetrievalFilters

logger = logging.getLogger("industrial_agent_audit.rag.service")

# 知识库结构版本（与迁移一致）。
# 0008 只新增审计表，不改变 RAG 表结构，因此 0007 与 0008 都满足知识库健康检查；
# 主库仍停留在 0004 时不会误报为“知识库可用”。
EXPECTED_KNOWLEDGE_SCHEMA = "0007_knowledge_rag"
COMPATIBLE_KNOWLEDGE_SCHEMAS = (EXPECTED_KNOWLEDGE_SCHEMA, "0008_agent_audit")


@dataclass(frozen=True)
class SearchSpec:
    query: str
    method: str = "hybrid"
    top_k: int | None = None
    equipment_scopes: tuple[str, ...] | None = None
    doc_types: tuple[str, ...] = ()
    doc_keys: tuple[str, ...] = ()
    chunking_version: str | None = None
    min_similarity: float | None = None


def build_filters(settings: RAGSettings, spec: SearchSpec) -> RetrievalFilters:
    scopes = (
        spec.equipment_scopes
        if spec.equipment_scopes is not None
        else DEFAULT_ALLOWED_SCOPES
    )
    return RetrievalFilters(
        equipment_scopes=tuple(scopes),
        doc_types=tuple(spec.doc_types or ()),
        doc_keys=tuple(spec.doc_keys or ()),
        chunking_version=spec.chunking_version or settings.chunking_version,
        embedding_model=settings.embedding_model,
        embedding_version=settings.embedding_version,
        min_similarity=(
            settings.min_similarity if spec.min_similarity is None else spec.min_similarity
        ),
    )


def search_knowledge(
    session: Session,
    settings: RAGSettings,
    spec: SearchSpec,
    *,
    embedder: EmbeddingProvider | None = None,
) -> dict[str, object]:
    embedder = embedder or get_embedding_provider(settings)
    retriever = KnowledgeRetriever(session, settings, embedder)
    filters = build_filters(settings, spec)
    top_k = spec.top_k or settings.default_top_k
    result = retriever.search(
        spec.query, method=spec.method, top_k=top_k, filters=filters
    )
    payload = result.to_dict()
    return {
        "query": spec.query,
        "method": spec.method,
        "top_k": top_k,
        "results": payload["results"],
        "retrieval": {
            "method": payload["method"],
            "query": payload["query"],
            "elapsed_ms": payload["elapsed_ms"],
            "embedding_elapsed_ms": payload["embedding_elapsed_ms"],
            "filters": payload["filters"],
            "query_terms": payload["query_terms"],
            "candidate_counts": payload["candidate_counts"],
            "warnings": payload["warnings"],
        },
        "warnings": payload["warnings"],
    }


def ask_knowledge(
    session: Session,
    settings: RAGSettings,
    spec: SearchSpec,
    *,
    embedder: EmbeddingProvider | None = None,
    chat_model: ChatModel | None = None,
) -> dict[str, object]:
    embedder = embedder or get_embedding_provider(settings)
    chat_model = chat_model or get_chat_model(settings)
    retriever = KnowledgeRetriever(session, settings, embedder)
    answerer = RAGAnswerer(session, settings, retriever, chat_model)
    filters = build_filters(settings, spec)
    result = answerer.answer(
        spec.query, top_k=spec.top_k or settings.default_top_k, method=spec.method, filters=filters
    )
    return result.to_dict()


def list_documents(
    session: Session,
    *,
    limit: int = 50,
    offset: int = 0,
    equipment_scope: str | None = None,
) -> tuple[list[dict[str, object]], int]:
    stmt = select(Document)
    count_stmt = select(func.count()).select_from(Document)
    if equipment_scope:
        stmt = stmt.where(Document.equipment_scope == equipment_scope)
        count_stmt = count_stmt.where(Document.equipment_scope == equipment_scope)
    total = session.execute(count_stmt).scalar_one()
    documents = (
        session.execute(
            stmt.order_by(Document.doc_key.asc()).limit(limit).offset(offset)
        )
        .scalars()
        .all()
    )
    counts: dict[str, dict[str, int]] = {}
    if documents:
        rows = session.execute(
            select(
                DocumentChunk.document_id,
                DocumentChunk.chunking_version,
                func.count(),
            )
            .where(DocumentChunk.document_id.in_([doc.id for doc in documents]))
            .group_by(DocumentChunk.document_id, DocumentChunk.chunking_version)
        ).all()
        for document_id, version, chunk_count in rows:
            counts.setdefault(str(document_id), {})[version] = int(chunk_count)

    items: list[dict[str, object]] = []
    for doc in documents:
        items.append(
            {
                "doc_key": doc.doc_key,
                "title": doc.title,
                "source_url": doc.source_url,
                "file_path": doc.file_path,
                "file_sha256": doc.file_sha256,
                "file_bytes": doc.file_bytes,
                "page_count": doc.page_count,
                "doc_type": doc.doc_type,
                "equipment_scope": doc.equipment_scope,
                "applicability_note": doc.applicability_note,
                "manufacturer": doc.manufacturer,
                "model_numbers": doc.model_numbers,
                "language": doc.language,
                "trust_level": doc.trust_level,
                "license": doc.license,
                "version": doc.version,
                "parse_status": doc.parse_status,
                "parse_quality_note": doc.parse_quality_note,
                "content_revision": doc.content_revision,
                "ingested_at": doc.ingested_at.isoformat() if doc.ingested_at else None,
                "chunks": counts.get(str(doc.id), {}),
            }
        )
    return items, total


def knowledge_health(
    session: Session,
    settings: RAGSettings,
    *,
    check_services: bool = False,
    embedder: EmbeddingProvider | None = None,
    chat_model: ChatModel | None = None,
) -> dict[str, object]:
    warnings: list[str] = []
    database: dict[str, object] = {"database": None, "schema": None}

    row = session.execute(
        text(
            "SELECT current_database() AS db, current_user AS usr,"
            " current_setting('server_version') AS version"
        )
    ).one()
    database = {"database": row.db, "user": row.usr, "server_version": row.version}

    schema_version = session.execute(
        text("SELECT version_num FROM alembic_version")
    ).scalar_one_or_none()
    schema_ok = schema_version in COMPATIBLE_KNOWLEDGE_SCHEMAS
    if not schema_ok:
        warnings.append(
            f"知识库迁移版本 {schema_version} 与代码期望 {EXPECTED_KNOWLEDGE_SCHEMA} 不一致"
        )

    documents = session.execute(select(func.count()).select_from(Document)).scalar_one()
    chunks = session.execute(select(func.count()).select_from(DocumentChunk)).scalar_one()
    index_rows = session.execute(
        select(
            DocumentChunk.chunking_version,
            DocumentChunk.embedding_model,
            DocumentChunk.embedding_version,
            DocumentChunk.embedding_dim,
            func.count(),
        )
        .group_by(
            DocumentChunk.chunking_version,
            DocumentChunk.embedding_model,
            DocumentChunk.embedding_version,
            DocumentChunk.embedding_dim,
        )
        .order_by(DocumentChunk.chunking_version)
    ).all()
    indexes = [
        {
            "chunking_version": row[0],
            "embedding_model": row[1],
            "embedding_version": row[2],
            "embedding_dim": row[3],
            "chunks": int(row[4]),
        }
        for row in index_rows
    ]

    embedding: dict[str, object] = {
        "provider": settings.embedding_provider,
        "model": settings.embedding_model,
        "configured_dim": settings.embedding_dim,
        "version": settings.embedding_version,
    }
    llm: dict[str, object] = {
        "provider": settings.llm_provider,
        "model": settings.llm_model,
    }
    indexed = {
        (row["embedding_model"], row["embedding_version"]): row["chunks"] for row in indexes
    }
    matched = indexed.get((settings.embedding_model, settings.embedding_version), 0)
    embedding["indexed_chunks"] = matched
    if chunks and not matched:
        warnings.append("索引中没有与当前 EMBEDDING_MODEL/EMBEDDING_VERSION 匹配的向量，需要重新入库")

    status = "ok" if schema_ok and matched else "degraded"
    if chunks == 0:
        status = "degraded"
        warnings.append("知识库为空：尚未入库任何文档")

    if check_services:
        embedder = embedder or get_embedding_provider(settings)
        chat_model = chat_model or get_chat_model(settings)
        try:
            batch = embedder.embed_query("health check")
            embedding["probe"] = {
                "ok": True,
                "elapsed_ms": batch.elapsed_ms,
                "dim": batch.dim,
            }
        except RAGError as exc:
            embedding["probe"] = {"ok": False, "error": f"{exc.code}: {exc.message}"}
            status = "degraded"
            warnings.append("Embedding 服务探测失败")
        try:
            response = chat_model.chat(
                [ChatMessage(role="user", content="ping")],
                json_mode=False,
                max_tokens=8,
            )
            llm["probe"] = {
                "ok": True,
                "elapsed_ms": response.elapsed_ms,
                "model": response.model,
            }
        except RAGError as exc:
            llm["probe"] = {"ok": False, "error": f"{exc.code}: {exc.message}"}
            status = "degraded"
            warnings.append("LLM 服务探测失败")

    return {
        "status": status,
        "database": database,
        "schema_version": schema_version,
        "schema_expected_version": EXPECTED_KNOWLEDGE_SCHEMA,
        "schema_ok": schema_ok,
        "documents": int(documents),
        "chunks": int(chunks),
        "indexes": indexes,
        "embedding": embedding,
        "llm": llm,
        "warnings": warnings,
    }


