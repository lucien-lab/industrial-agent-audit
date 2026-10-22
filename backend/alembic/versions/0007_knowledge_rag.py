"""阶段 2：RAG 知识库结构（documents / document_chunks / knowledge_ingest_runs）。

Revision ID: 0007_knowledge_rag
Revises: 0006_check_constraint_names
Create Date: 2026-10-22

本迁移**只新增对象**：
- 扩展 `pg_trgm`（中文没有可靠分词时，关键词检索用 trigram/ILIKE 做精确标识符匹配；
  `vector` 扩展已由 0001 创建）；
- 表 `documents`、`document_chunks`、`knowledge_ingest_runs`；
- 索引：HNSW（`vector_cosine_ops`）、GIN（tsvector）、GIN（trgm）、模型/版本过滤索引。

不触碰任何已有表，也不删除任何工业传感器数据。

维度约定：`document_chunks.embedding` 固定为 `vector(1024)`，与实测的
`bge-m3`（Ollama）输出维度一致。更换维度必须新增迁移，不允许在运行时改列。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0007_knowledge_rag"
down_revision: str | None = "0006_check_constraint_names"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 1024

SCOPES = ("metropt3_direct", "generic_compressor", "model_specific_other", "unknown")
DOC_TYPES = (
    "dataset_description",
    "technical_manual",
    "maintenance_guide",
    "fault_case",
    "research_paper",
    "standard",
    "other",
)
TRUST_LEVELS = (
    "official_primary",
    "authoritative_secondary",
    "general_reference",
    "unverified",
)
PARSE_STATUSES = ("pending", "parsed", "partial", "failed")


def _in_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "documents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("doc_key", sa.String(128), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("file_bytes", sa.BigInteger()),
        sa.Column("page_count", sa.Integer()),
        sa.Column("doc_type", sa.String(32), nullable=False),
        sa.Column("equipment_scope", sa.String(32), nullable=False),
        sa.Column("applicability_note", sa.Text()),
        sa.Column("manufacturer", sa.String(128)),
        sa.Column("model_numbers", sa.String(256)),
        sa.Column("language", sa.String(16)),
        sa.Column("trust_level", sa.String(32), nullable=False),
        sa.Column("license", sa.String(128)),
        sa.Column("license_note", sa.Text()),
        sa.Column("version", sa.String(64)),
        sa.Column("content_revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("parse_status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("parse_quality_note", sa.Text()),
        sa.Column("parser_version", sa.String(32)),
        sa.Column("extra_metadata", postgresql.JSONB()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("ingested_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("doc_key", name="uq_documents_doc_key"),
        sa.CheckConstraint(
            f"equipment_scope IN ({_in_list(SCOPES)})", name="equipment_scope_valid"
        ),
        sa.CheckConstraint(
            f"doc_type IN ({_in_list(DOC_TYPES)})", name="doc_type_valid"
        ),
        sa.CheckConstraint(
            f"trust_level IN ({_in_list(TRUST_LEVELS)})", name="trust_level_valid"
        ),
        sa.CheckConstraint(
            f"parse_status IN ({_in_list(PARSE_STATUSES)})",
            name="parse_status_valid",
        ),
        sa.CheckConstraint(
            "file_sha256 ~ '^[0-9a-f]{64}$'", name="file_sha256_hex"
        ),
        sa.CheckConstraint("content_revision >= 1", name="content_revision_positive"),
        comment="知识库文档元数据（来源、许可、设备适用范围、解析状态）",
    )
    op.create_index("ix_documents_equipment_scope", "documents", ["equipment_scope"])
    op.create_index("ix_documents_doc_type", "documents", ["doc_type"])
    op.create_index("ix_documents_file_sha256", "documents", ["file_sha256"])

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("section_title", sa.Text()),
        sa.Column("section_path", sa.Text()),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple', coalesce(section_title, '') || ' ' || content)",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column("embedding", Vector(EMBEDDING_DIM)),
        sa.Column("embedding_model", sa.String(128)),
        sa.Column("embedding_version", sa.String(64)),
        sa.Column("embedding_dim", sa.SmallInteger()),
        sa.Column("chunking_strategy", sa.String(32), nullable=False),
        sa.Column("chunking_version", sa.String(64), nullable=False),
        sa.Column("metadata", postgresql.JSONB()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "document_id", "chunking_version", "chunk_index", name="uq_chunk_version_index"
        ),
        sa.CheckConstraint("page_start >= 1", name="page_start_positive"),
        sa.CheckConstraint("page_end >= page_start", name="page_range_valid"),
        sa.CheckConstraint(
            "char_count = length(content)", name="char_count_matches"
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'", name="content_sha256_hex"
        ),
        comment="知识库检索片段：页码/章节/向量/切分版本，可回溯到源文档",
    )
    op.create_index("ix_document_chunks_document_id", "document_chunks", ["document_id"])
    op.create_index(
        "ix_document_chunks_model_version",
        "document_chunks",
        ["embedding_model", "embedding_version"],
    )
    op.create_index(
        "ix_document_chunks_search_tsv",
        "document_chunks",
        ["search_tsv"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_document_chunks_search_text_trgm",
        "document_chunks",
        ["search_text"],
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_document_chunks_embedding_hnsw",
        "document_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "knowledge_ingest_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'running'")),
        sa.Column("chunking_strategy", sa.String(32)),
        sa.Column("chunking_version", sa.String(64)),
        sa.Column("embedding_model", sa.String(128)),
        sa.Column("embedding_version", sa.String(64)),
        sa.Column("documents_total", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("documents_imported", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("documents_skipped", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("chunks_created", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("embeddings_created", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("embedding_failures", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("parse_elapsed_ms", sa.Integer()),
        sa.Column("chunk_elapsed_ms", sa.Integer()),
        sa.Column("embedding_elapsed_ms", sa.Integer()),
        sa.Column("write_elapsed_ms", sa.Integer()),
        sa.Column("total_elapsed_ms", sa.Integer()),
        sa.Column("error_message", sa.Text()),
        sa.Column("report", postgresql.JSONB()),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'partial', 'dry_run')",
            name="status_valid",
        ),
        comment="知识库入库任务记录（只记录计数与耗时，不记录文档正文）",
    )
    op.create_index(
        "ix_knowledge_ingest_runs_started_at", "knowledge_ingest_runs", ["started_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_ingest_runs_started_at", table_name="knowledge_ingest_runs")
    op.drop_table("knowledge_ingest_runs")

    op.drop_index("ix_document_chunks_embedding_hnsw", table_name="document_chunks")
    op.drop_index("ix_document_chunks_search_text_trgm", table_name="document_chunks")
    op.drop_index("ix_document_chunks_search_tsv", table_name="document_chunks")
    op.drop_index("ix_document_chunks_model_version", table_name="document_chunks")
    op.drop_index("ix_document_chunks_document_id", table_name="document_chunks")
    op.drop_table("document_chunks")

    op.drop_index("ix_documents_file_sha256", table_name="documents")
    op.drop_index("ix_documents_doc_type", table_name="documents")
    op.drop_index("ix_documents_equipment_scope", table_name="documents")
    op.drop_table("documents")

    # 说明：不回滚 pg_trgm —— 扩展为集群级共享对象，删除可能影响其它库。
