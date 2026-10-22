"""知识库入库流水线：解析 → 切分 → Embedding → 写库（可重复执行）。

幂等与版本规则：
- 文档身份是 `doc_key`（逻辑文档），内容身份是 `file_sha256`；
- 同一 `doc_key` + 同一 `file_sha256` + 同一 `chunking_version` 且已有 Chunk → 跳过；
- 内容变了（sha 变化）→ 更新文档行、`content_revision + 1`、重建该 chunking_version 的 Chunk；
- 切分版本不同 → 两套 Chunk **并存**，用于对比实验（不互相覆盖）；
- 换 Embedding 模型/版本 → 必须显式 `--reindex`，并在写库前校验维度。

失败处理：单文档失败只标记该文档并在报告中记录，不写入半成品 Chunk
（每个文档的写库在独立事务中完成）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.models.knowledge import EMBEDDING_DIM, Document, DocumentChunk, KnowledgeIngestRun
from app.rag.chunking import get_chunker
from app.rag.config import RAGSettings
from app.rag.embedding import EmbeddingProvider, vector_literal
from app.rag.errors import DocumentParseError, IndexModelMismatch
from app.rag.parsing.pdf import ParsedDocument, PdfParser

logger = logging.getLogger("industrial_agent_audit.rag.ingest")

PARSER_VERSION = "pdf-parser-v1"

# 提示注入/危险指令的粗筛（只做标记与提示，不做内容改写）
_INJECTION_PATTERNS = (
    r"ignore (all )?(previous|prior|above) instructions",
    r"disregard (the )?(system|previous) prompt",
    r"忽略(以上|之前|前面)(的)?(所有)?(指令|提示)",
    r"system prompt",
    r"you are now (a|an|the) ",
    r"drop\s+table",
    r"delete\s+from\s+\w+",
    r"```\s*sql",
    r"call (the )?tool",
    r"执行(以下)?(代码|sql|命令)",
)
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


def detect_injection_flags(content: str) -> list[str]:
    """返回命中的可疑片段（截断到 80 字符），用于标记而不是删改内容。"""
    return [match.group(0)[:80] for match in _INJECTION_RE.finditer(content)]


@dataclass(frozen=True)
class SourceSpec:
    doc_key: str
    title: str
    file_path: str
    source_url: str | None
    doc_type: str
    equipment_scope: str
    applicability_note: str | None
    manufacturer: str | None
    model_numbers: str | None
    version: str | None
    language: str | None
    trust_level: str
    license: str | None
    license_note: str | None
    parser_version: str = PARSER_VERSION

    @classmethod
    def from_dict(cls, data: dict) -> "SourceSpec":
        required = (
            "doc_key",
            "title",
            "file_path",
            "doc_type",
            "equipment_scope",
            "trust_level",
        )
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise ValueError(f"来源条目缺少字段：{missing}")
        return cls(
            doc_key=data["doc_key"],
            title=data["title"],
            file_path=data["file_path"],
            source_url=data.get("source_url"),
            doc_type=data["doc_type"],
            equipment_scope=data["equipment_scope"],
            applicability_note=data.get("applicability_note"),
            manufacturer=data.get("manufacturer"),
            model_numbers=data.get("model_numbers"),
            version=data.get("version"),
            language=data.get("language"),
            trust_level=data["trust_level"],
            license=data.get("license"),
            license_note=data.get("license_note"),
            parser_version=data.get("parser_version") or PARSER_VERSION,
        )


@dataclass
class DocumentIngestResult:
    doc_key: str
    status: str  # imported / skipped_unchanged / failed / dry_run
    title: str
    page_count: int | None = None
    parse_status: str | None = None
    chunks: int = 0
    embeddings: int = 0
    parse_elapsed_ms: int = 0
    chunk_elapsed_ms: int = 0
    embedding_elapsed_ms: int = 0
    write_elapsed_ms: int = 0
    content_revision: int | None = None
    parse_warnings: list[str] = field(default_factory=list)
    quality_notes: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class IngestReport:
    strategy: str
    chunking_version: str
    embedding_model: str
    embedding_version: str
    documents_total: int
    documents_imported: int
    documents_skipped: int
    documents_failed: int
    chunks_created: int
    embeddings_created: int
    parse_elapsed_ms: int
    chunk_elapsed_ms: int
    embedding_elapsed_ms: int
    write_elapsed_ms: int
    total_elapsed_ms: int
    results: list[DocumentIngestResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "chunking_version": self.chunking_version,
            "embedding_model": self.embedding_model,
            "embedding_version": self.embedding_version,
            "documents_total": self.documents_total,
            "documents_imported": self.documents_imported,
            "documents_skipped": self.documents_skipped,
            "documents_failed": self.documents_failed,
            "chunks_created": self.chunks_created,
            "embeddings_created": self.embeddings_created,
            "timings_ms": {
                "parse": self.parse_elapsed_ms,
                "chunk": self.chunk_elapsed_ms,
                "embedding": self.embedding_elapsed_ms,
                "write": self.write_elapsed_ms,
                "total": self.total_elapsed_ms,
            },
            "documents": [result.as_dict() for result in self.results],
        }


def load_sources(path: Path) -> list[SourceSpec]:
    data = json.loads(path.read_text(encoding="utf-8"))
    documents = data.get("documents") or []
    if not documents:
        raise ValueError(f"{path} 中没有 documents 条目")
    return [SourceSpec.from_dict(item) for item in documents]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_vector_dim(session: Session, expected_dim: int) -> None:
    """校验数据库 `document_chunks.embedding` 的真实维度。

    维度不一致时**拒绝写入**：pgvector 会静默接受长度不同的字面量再报错，
    与其在插入时得到晦涩异常，不如提前给出可操作的信息。
    """
    actual = session.execute(
        text(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = 'document_chunks'::regclass AND attname = 'embedding'"
        )
    ).scalar_one_or_none()
    if actual is None:
        raise IndexModelMismatch(
            "数据库缺少 document_chunks.embedding 列，请先执行迁移 0007_knowledge_rag"
        )
    if int(actual) != expected_dim:
        raise IndexModelMismatch(
            f"数据库 vector 维度为 {actual}，配置 EMBEDDING_DIM={expected_dim}；"
            "换模型需要新增迁移并全量重建索引",
            database_dim=int(actual),
            configured_dim=expected_dim,
        )


def _searchable_text(section_title: str | None, content: str) -> str:
    """关键词检索用小写文本；不做去标点/去数字等破坏标识符的清洗。"""
    return f"{section_title or ''} {content}".lower()


def ingest_document(
    session: Session,
    spec: SourceSpec,
    *,
    project_root: Path,
    chunker,
    embedder: EmbeddingProvider | None,
    settings: RAGSettings,
    dry_run: bool = False,
    reindex: bool = False,
) -> DocumentIngestResult:
    """入库单个文档。调用方负责 commit/rollback。"""
    path = project_root / spec.file_path
    result = DocumentIngestResult(
        doc_key=spec.doc_key, status="imported", title=spec.title
    )
    if not path.is_file():
        result.status = "failed"
        result.error = f"文件不存在：{spec.file_path}"
        return result

    file_sha = sha256_file(path)
    file_bytes = path.stat().st_size

    existing = session.execute(
        select(Document).where(Document.doc_key == spec.doc_key)
    ).scalar_one_or_none()
    existing_chunks = 0
    if existing is not None:
        existing_chunks = session.execute(
            select(func.count())
            .select_from(DocumentChunk)
            .where(
                DocumentChunk.document_id == existing.id,
                DocumentChunk.chunking_version == chunker.chunking_version,
            )
        ).scalar_one()

    unchanged = (
        existing is not None
        and existing.file_sha256 == file_sha
        and existing_chunks > 0
        and not reindex
    )
    if unchanged:
        result.status = "skipped_unchanged"
        result.page_count = existing.page_count
        result.parse_status = existing.parse_status
        result.chunks = existing_chunks
        result.content_revision = existing.content_revision
        return result

    # 1) 解析
    parse_started = time.perf_counter()
    try:
        parsed: ParsedDocument = PdfParser().parse(path)
    except DocumentParseError as exc:
        result.status = "failed"
        result.error = exc.message
        return result
    result.parse_elapsed_ms = int((time.perf_counter() - parse_started) * 1000)
    result.page_count = parsed.page_count
    result.parse_status = parsed.parse_status
    result.parse_warnings = list(parsed.warnings)
    result.quality_notes = list(parsed.quality_notes)

    if parsed.parse_status == "failed":
        result.status = "failed"
        result.error = "文档未能提取有效文本（可能是扫描件），未写入任何 Chunk"
        if not dry_run:
            _upsert_document(
                session, spec, parsed, file_sha, file_bytes,
                content_revision=(existing.content_revision if existing else 1),
                parse_quality_note="; ".join(parsed.quality_notes),
            )
        return result

    # 2) 切分
    chunk_started = time.perf_counter()
    chunks = chunker.chunk(parsed)
    result.chunk_elapsed_ms = int((time.perf_counter() - chunk_started) * 1000)
    result.chunks = len(chunks)
    if not chunks:
        result.status = "failed"
        result.error = "切分结果为空"
        return result

    # 3) Embedding（真实调用；失败直接抛给上层，绝不写假向量）
    vectors: list[list[float]] = []
    if embedder is not None:
        embed_started = time.perf_counter()
        batch = embedder.embed_documents([chunk.content for chunk in chunks])
        result.embedding_elapsed_ms = int((time.perf_counter() - embed_started) * 1000)
        if len(batch.vectors) != len(chunks):
            raise IndexModelMismatch(
                f"Embedding 数量 {len(batch.vectors)} 与 Chunk 数量 {len(chunks)} 不一致"
            )
        vectors = batch.vectors
        result.embeddings = len(vectors)
        if batch.dim != EMBEDDING_DIM:
            raise IndexModelMismatch(
                f"模型输出维度 {batch.dim} 与数据库 vector({EMBEDDING_DIM}) 不一致",
                model_dim=batch.dim,
                database_dim=EMBEDDING_DIM,
            )

    if dry_run:
        result.status = "dry_run"
        return result

    # 4) 写库（同一文档一个事务）
    write_started = time.perf_counter()
    document = _upsert_document(
        session,
        spec,
        parsed,
        file_sha,
        file_bytes,
        content_revision=_next_revision(existing, file_sha),
        parse_quality_note="; ".join(parsed.quality_notes),
        unchanged_content=(existing is not None and existing.file_sha256 == file_sha),
    )
    session.execute(
        delete(DocumentChunk).where(
            DocumentChunk.document_id == document.id,
            DocumentChunk.chunking_version == chunker.chunking_version,
        )
    )
    model_name = embedder.model if embedder is not None else None
    model_version = embedder.version if embedder is not None else None
    for chunk, vector in zip(chunks, vectors or [None] * len(chunks)):
        flags = detect_injection_flags(chunk.content)
        meta = {**chunk.metadata}
        if flags:
            meta["suspected_prompt_injection"] = flags
        session.add(
            DocumentChunk(
                document_id=document.id,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                content_sha256=chunk.content_sha256,
                char_count=chunk.char_count,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                section_title=chunk.section_title,
                section_path=chunk.section_path,
                search_text=_searchable_text(chunk.section_title, chunk.content),
                embedding=vector,
                embedding_model=model_name,
                embedding_version=model_version,
                embedding_dim=(len(vector) if vector else None),
                chunking_strategy=chunk.chunking_strategy,
                chunking_version=chunker.chunking_version,
                meta=meta,
            )
        )
    session.flush()
    result.write_elapsed_ms = int((time.perf_counter() - write_started) * 1000)
    result.content_revision = document.content_revision
    return result


def _next_revision(existing: Document | None, file_sha: str) -> int:
    """内容变化才递增修订号；同一内容换切分版本不增加 revision。"""
    if existing is None:
        return 1
    if existing.file_sha256 == file_sha:
        return existing.content_revision
    return existing.content_revision + 1


def _upsert_document(
    session: Session,
    spec: SourceSpec,
    parsed: ParsedDocument,
    file_sha: str,
    file_bytes: int,
    *,
    content_revision: int,
    parse_quality_note: str,
    unchanged_content: bool = False,
) -> Document:
    document = session.execute(
        select(Document).where(Document.doc_key == spec.doc_key)
    ).scalar_one_or_none()
    if document is None:
        document = Document(doc_key=spec.doc_key)
        session.add(document)
    document.title = spec.title
    document.file_path = spec.file_path
    document.source_url = spec.source_url
    document.file_sha256 = file_sha
    document.file_bytes = file_bytes
    document.page_count = parsed.page_count
    document.doc_type = spec.doc_type
    document.equipment_scope = spec.equipment_scope
    document.applicability_note = spec.applicability_note
    document.manufacturer = spec.manufacturer
    document.model_numbers = spec.model_numbers
    document.language = spec.language
    document.trust_level = spec.trust_level
    document.license = spec.license
    document.license_note = spec.license_note
    document.version = spec.version
    document.parse_status = parsed.parse_status
    document.parse_quality_note = parse_quality_note
    document.parser_version = spec.parser_version
    document.content_revision = max(1, content_revision)
    if not unchanged_content or document.ingested_at is None:
        document.ingested_at = func.now()
    document.extra_metadata = {
        "warnings": list(parsed.warnings),
        "sections": len(parsed.sections),
        "blocks": len(parsed.blocks),
        "removed_repeated_lines": list(parsed.removed_repeated_lines),
        "pdf_metadata": parsed.metadata,
        "parse_elapsed_ms": parsed.elapsed_ms,
    }
    session.flush()
    return document


def run_ingest(
    session: Session,
    specs: list[SourceSpec],
    *,
    project_root: Path,
    settings: RAGSettings,
    strategy: str,
    chunking_version: str,
    chunk_size: int,
    chunk_overlap: int,
    embedder: EmbeddingProvider | None,
    dry_run: bool = False,
    reindex: bool = False,
) -> IngestReport:
    started = time.perf_counter()
    chunker = get_chunker(
        strategy,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        chunking_version=chunking_version,
    )
    if embedder is not None:
        assert_vector_dim(session, settings.embedding_dim)

    report = IngestReport(
        strategy=strategy,
        chunking_version=chunking_version,
        embedding_model=(embedder.model if embedder else "none"),
        embedding_version=(embedder.version if embedder else "none"),
        documents_total=len(specs),
        documents_imported=0,
        documents_skipped=0,
        documents_failed=0,
        chunks_created=0,
        embeddings_created=0,
        parse_elapsed_ms=0,
        chunk_elapsed_ms=0,
        embedding_elapsed_ms=0,
        write_elapsed_ms=0,
        total_elapsed_ms=0,
    )

    for spec in specs:
        try:
            result = ingest_document(
                session,
                spec,
                project_root=project_root,
                chunker=chunker,
                embedder=embedder,
                settings=settings,
                dry_run=dry_run,
                reindex=reindex,
            )
        except Exception as exc:  # 单文档失败不影响其它文档，但必须记录
            session.rollback()
            logger.exception("文档入库失败 doc_key=%s", spec.doc_key)
            result = DocumentIngestResult(
                doc_key=spec.doc_key,
                status="failed",
                title=spec.title,
                error=f"{type(exc).__name__}: {exc}",
            )
        else:
            if result.status == "imported":
                session.commit()
            else:
                session.rollback()

        report.results.append(result)
        if result.status in ("imported", "dry_run"):
            report.documents_imported += 1
            report.chunks_created += result.chunks
            report.embeddings_created += result.embeddings
        elif result.status == "skipped_unchanged":
            report.documents_skipped += 1
        else:
            report.documents_failed += 1
        report.parse_elapsed_ms += result.parse_elapsed_ms
        report.chunk_elapsed_ms += result.chunk_elapsed_ms
        report.embedding_elapsed_ms += result.embedding_elapsed_ms
        report.write_elapsed_ms += result.write_elapsed_ms

    report.total_elapsed_ms = int((time.perf_counter() - started) * 1000)

    if not dry_run:
        status = "succeeded"
        if report.documents_failed and report.documents_imported:
            status = "partial"
        elif report.documents_failed:
            status = "failed"
        run = KnowledgeIngestRun(
            status=status,
            chunking_strategy=strategy,
            chunking_version=chunking_version,
            embedding_model=report.embedding_model,
            embedding_version=report.embedding_version,
            documents_total=report.documents_total,
            documents_imported=report.documents_imported,
            documents_skipped=report.documents_skipped,
            chunks_created=report.chunks_created,
            embeddings_created=report.embeddings_created,
            embedding_failures=report.documents_failed,
            parse_elapsed_ms=report.parse_elapsed_ms,
            chunk_elapsed_ms=report.chunk_elapsed_ms,
            embedding_elapsed_ms=report.embedding_elapsed_ms,
            write_elapsed_ms=report.write_elapsed_ms,
            total_elapsed_ms=report.total_elapsed_ms,
            error_message="; ".join(
                f"{r.doc_key}: {r.error}" for r in report.results if r.error
            )[:2000]
            or None,
            report=report.as_dict(),
        )
        run.finished_at = func.now()
        session.add(run)
        session.commit()
    return report


def index_stats(session: Session) -> dict[str, object]:
    """索引概览：文档数、Chunk 数、按切分版本/模型分组的计数。"""
    documents = session.execute(select(func.count()).select_from(Document)).scalar_one()
    chunks = session.execute(select(func.count()).select_from(DocumentChunk)).scalar_one()
    by_version = session.execute(
        select(
            DocumentChunk.chunking_version,
            DocumentChunk.embedding_model,
            DocumentChunk.embedding_version,
            func.count(),
        )
        .group_by(
            DocumentChunk.chunking_version,
            DocumentChunk.embedding_model,
            DocumentChunk.embedding_version,
        )
        .order_by(DocumentChunk.chunking_version)
    ).all()
    return {
        "documents": documents,
        "chunks": chunks,
        "by_version": [
            {
                "chunking_version": row[0],
                "embedding_model": row[1],
                "embedding_version": row[2],
                "chunks": row[3],
            }
            for row in by_version
        ],
    }


__all__ = [
    "DocumentIngestResult",
    "IngestReport",
    "SourceSpec",
    "assert_vector_dim",
    "detect_injection_flags",
    "index_stats",
    "ingest_document",
    "load_sources",
    "run_ingest",
    "sha256_file",
    "vector_literal",
]
