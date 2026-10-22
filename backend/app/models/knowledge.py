"""RAG 知识库 ORM 模型（阶段 2 新增，迁移 `0007_knowledge_rag`）。

三张表：
- `documents`：文档级元数据（来源、许可、适用范围、解析状态）；
- `document_chunks`：可检索片段（页码、章节、向量、切分版本）；
- `knowledge_ingest_runs`：入库任务记录（耗时、数量、失败原因）。

关键设计：
1. **适用性是一等字段**：`equipment_scope` 区分“MetroPT-3 直接关联文档”“同类型通用资料”
   “其它型号专属资料”“未知”，检索与回答都按它过滤/提示，避免把通用参数冒充设备配置。
2. **切分版本并存**：`UNIQUE(document_id, chunking_version, chunk_index)`，
   同一文档可以同时存在 fixed / structural 两套切分，用于对比实验。
3. **向量模型版本显式存储**：`embedding_model + embedding_version + embedding_dim`，
   检索按模型/版本过滤，禁止在同一索引里混用不同模型的向量。
4. `search_text` 由程序写入小写文本（供 pg_trgm / ILIKE 关键词匹配），
   `search_tsv` 是数据库生成的 tsvector（`simple` 分词，不做中英文假设）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.rag.config import (
    DOC_TYPES,
    EQUIPMENT_SCOPES,
    PARSE_STATUSES,
    TRUST_LEVELS,
)

# 数据库 vector 维度：与迁移 0007 中 `vector(1024)` 一致。
# 更换 Embedding 模型（维度变化）时必须新增迁移修改该列并全量重建索引。
EMBEDDING_DIM = 1024

SCOPE_LIST_SQL = ", ".join(f"'{value}'" for value in EQUIPMENT_SCOPES)
DOC_TYPE_LIST_SQL = ", ".join(f"'{value}'" for value in DOC_TYPES)
TRUST_LIST_SQL = ", ".join(f"'{value}'" for value in TRUST_LEVELS)
PARSE_LIST_SQL = ", ".join(f"'{value}'" for value in PARSE_STATUSES)

INGEST_STATUSES = ("running", "succeeded", "failed", "partial", "dry_run")


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(f"equipment_scope IN ({SCOPE_LIST_SQL})", name="equipment_scope_valid"),
        CheckConstraint(f"doc_type IN ({DOC_TYPE_LIST_SQL})", name="doc_type_valid"),
        CheckConstraint(f"trust_level IN ({TRUST_LIST_SQL})", name="trust_level_valid"),
        CheckConstraint(f"parse_status IN ({PARSE_LIST_SQL})", name="parse_status_valid"),
        CheckConstraint("file_sha256 ~ '^[0-9a-f]{64}$'", name="file_sha256_hex"),
        CheckConstraint("content_revision >= 1", name="content_revision_positive"),
        Index("ix_documents_equipment_scope", "equipment_scope"),
        Index("ix_documents_doc_type", "doc_type"),
        Index("ix_documents_file_sha256", "file_sha256"),
        {"comment": "知识库文档元数据（来源、许可、设备适用范围、解析状态）"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # 逻辑文档键：同一文档换版本时复用该键，只是 sha/revision 变化
    doc_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_bytes: Mapped[int | None] = mapped_column(BigInteger)
    page_count: Mapped[int | None] = mapped_column(Integer)

    doc_type: Mapped[str] = mapped_column(String(32), nullable=False)
    equipment_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    applicability_note: Mapped[str | None] = mapped_column(Text)
    manufacturer: Mapped[str | None] = mapped_column(String(128))
    model_numbers: Mapped[str | None] = mapped_column(String(256))
    language: Mapped[str | None] = mapped_column(String(16))
    trust_level: Mapped[str] = mapped_column(String(32), nullable=False)
    license: Mapped[str | None] = mapped_column(String(128))
    license_note: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(String(64))

    content_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    parse_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    parse_quality_note: Mapped[str | None] = mapped_column(Text)
    parser_version: Mapped[str | None] = mapped_column(String(32))
    extra_metadata: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "chunking_version", "chunk_index", name="uq_chunk_version_index"
        ),
        CheckConstraint("page_start >= 1", name="page_start_positive"),
        CheckConstraint("page_end >= page_start", name="page_range_valid"),
        CheckConstraint("char_count = length(content)", name="char_count_matches"),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="content_sha256_hex"),
        Index("ix_document_chunks_document_id", "document_id"),
        Index(
            "ix_document_chunks_model_version",
            "embedding_model",
            "embedding_version",
        ),
        Index(
            "ix_document_chunks_search_tsv",
            "search_tsv",
            postgresql_using="gin",
        ),
        Index(
            "ix_document_chunks_search_text_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        {"comment": "知识库检索片段：页码/章节/向量/切分版本，可回溯到源文档"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)

    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    section_title: Mapped[str | None] = mapped_column(Text)
    section_path: Mapped[str | None] = mapped_column(Text)

    # 关键词检索：由程序写入的小写文本 + 数据库生成的 tsvector（simple 分词）
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    search_tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', coalesce(section_title, '') || ' ' || content)",
            persisted=True,
        ),
        nullable=False,
    )

    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    embedding_version: Mapped[str | None] = mapped_column(String(64))
    embedding_dim: Mapped[int | None] = mapped_column(SmallInteger)

    chunking_strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    chunking_version: Mapped[str] = mapped_column(String(64), nullable=False)
    meta: Mapped[dict[str, object] | None] = mapped_column("metadata", JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="chunks")


class KnowledgeIngestRun(Base):
    __tablename__ = "knowledge_ingest_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'partial', 'dry_run')",
            name="status_valid",
        ),
        Index("ix_knowledge_ingest_runs_started_at", "started_at"),
        {"comment": "知识库入库任务记录（只记录计数与耗时，不记录文档正文）"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'running'")
    )
    chunking_strategy: Mapped[str | None] = mapped_column(String(32))
    chunking_version: Mapped[str | None] = mapped_column(String(64))
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    embedding_version: Mapped[str | None] = mapped_column(String(64))

    documents_total: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    documents_imported: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    documents_skipped: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    chunks_created: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    embeddings_created: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    embedding_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    parse_elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    chunk_elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    embedding_elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    write_elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    total_elapsed_ms: Mapped[int | None] = mapped_column(Integer)

    error_message: Mapped[str | None] = mapped_column(Text)
    report: Mapped[dict[str, object] | None] = mapped_column(JSONB)
