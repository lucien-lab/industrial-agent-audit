"""知识库 API 契约（阶段 2）。

校验要点：
- 问题/查询长度上限（防止超长文本拖垮 Embedding/LLM）；
- `top_k` 有硬上限（默认 5，最大 20），控制上下文长度与延迟；
- `equipment_scopes` / `doc_types` 使用枚举，未知值直接 422；
- 所有响应都带来源（doc_key、标题、页码、Chunk ID、许可证），可回溯。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.rag.config import DOC_TYPES, EQUIPMENT_SCOPES

EquipmentScope = Literal[
    "metropt3_direct", "generic_compressor", "model_specific_other", "unknown"
]
DocType = Literal[
    "dataset_description",
    "technical_manual",
    "maintenance_guide",
    "fault_case",
    "research_paper",
    "standard",
    "other",
]
SearchMethod = Literal["vector", "keyword", "hybrid"]
AnswerStatus = Literal[
    "answered",
    "insufficient_evidence",
    "no_evidence",
    "llm_unavailable",
    "llm_timeout",
    "llm_rate_limited",
    "llm_error",
]

__all__ = [
    "DOC_TYPES",
    "EQUIPMENT_SCOPES",
    "AskRequest",
    "AskResponse",
    "CitationOut",
    "DocumentsResponse",
    "DocumentOut",
    "EvidenceOut",
    "KnowledgeHealthResponse",
    "SearchRequest",
    "SearchResponse",
]


class SearchRequest(BaseModel):
    query: str = Field(
        min_length=1,
        max_length=500,
        description="检索查询（自然语言或标识符，如 TP2、DV_pressure、GA30）",
        examples=["TP2 和 TP3 分别代表什么？"],
    )
    method: SearchMethod = Field(
        default="hybrid", description="检索方法：vector（纯向量）/ keyword（纯关键词）/ hybrid（RRF 融合）"
    )
    top_k: int | None = Field(
        default=None, ge=1, le=20, description="返回条数，默认取服务配置（5），最大 20"
    )
    equipment_scopes: list[EquipmentScope] | None = Field(
        default=None,
        description=(
            "设备适用范围过滤。默认仅 metropt3_direct + generic_compressor；"
            "显式传空数组表示不限制（不推荐）"
        ),
    )
    doc_types: list[DocType] | None = Field(default=None, description="文档类型过滤")
    doc_keys: list[str] | None = Field(
        default=None, max_length=20, description="限定文档（doc_key，最多 20 个）"
    )
    chunking_version: str | None = Field(
        default=None, max_length=64, description="限定切分版本（对比实验用），默认取服务配置"
    )
    min_similarity: float | None = Field(
        default=None, ge=-1.0, le=1.0, description="向量相似度下限（仅对向量检索生效）"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "query": "储气压力异常可能涉及哪些系统？",
                "method": "hybrid",
                "top_k": 5,
                "equipment_scopes": ["metropt3_direct", "generic_compressor"],
            }
        }
    )


class EvidenceOut(BaseModel):
    chunk_id: int = Field(description="document_chunks.id，可用于复核原文")
    document_id: str
    doc_key: str = Field(description="文档逻辑键，例如 doe-compressed-air-sourcebook")
    document_title: str
    chunk_index: int
    content: str = Field(description="Chunk 原文（未改写）")
    page_start: int = Field(description="起始页码（从 1 开始）")
    page_end: int = Field(description="结束页码")
    section_title: str | None = None
    section_path: str | None = None
    equipment_scope: EquipmentScope = Field(
        description="适用性：metropt3_direct 为直接关联；generic_compressor 仅为通用参考"
    )
    doc_type: DocType
    trust_level: str
    source_url: str | None = None
    document_version: str | None = None
    license: str | None = None
    chunking_version: str
    embedding_model: str | None = None
    score: float = Field(description="排序分数（混合检索时为 RRF 分数，量纲与相似度不同）")
    vector_similarity: float | None = None
    vector_rank: int | None = None
    keyword_score: float | None = None
    keyword_rank: int | None = None
    rrf_score: float | None = None
    matched_terms: list[str] = Field(default_factory=list)
    citation_label: str = Field(description="人类可读引用，如 doe-tip-sheet-3:p1")

    model_config = ConfigDict(extra="ignore")


class RetrievalMetaOut(BaseModel):
    method: str
    query: str
    elapsed_ms: float
    embedding_elapsed_ms: float = 0.0
    filters: dict[str, object] = Field(default_factory=dict)
    query_terms: dict[str, list[str]] = Field(default_factory=dict)
    candidate_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    query: str
    method: str
    top_k: int
    results: list[EvidenceOut]
    retrieval: RetrievalMetaOut
    warnings: list[str] = Field(default_factory=list)


class CitationOut(BaseModel):
    label: str = Field(description="回答中的引用标签，如 C1")
    chunk_id: int
    doc_key: str
    document_title: str
    page_start: int
    page_end: int
    section_title: str | None = None
    equipment_scope: EquipmentScope
    source_url: str | None = None
    quote: str = Field(description="证据原文摘录（直接来自 Chunk，不改写）")


class AskRequest(BaseModel):
    question: str = Field(
        min_length=1,
        max_length=500,
        description="自然语言问题",
        examples=["TP2、TP3 等传感器在数据说明中代表什么？"],
    )
    method: SearchMethod = Field(default="hybrid", description="检索方法（默认混合检索）")
    top_k: int | None = Field(default=None, ge=1, le=20, description="证据条数上限，默认 5")
    equipment_scopes: list[EquipmentScope] | None = None
    doc_types: list[DocType] | None = None
    doc_keys: list[str] | None = Field(default=None, max_length=20)
    chunking_version: str | None = Field(default=None, max_length=64)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "question": "TP2、TP3 等传感器在数据说明中代表什么？",
                "method": "hybrid",
                "top_k": 5,
            }
        }
    )


class AskResponse(BaseModel):
    question: str
    status: AnswerStatus = Field(
        description=(
            "answered：有证据的回答；insufficient_evidence：证据不足/型号不匹配；"
            "no_evidence：检索为空；llm_*：模型调用失败（此时未生成结论）"
        )
    )
    answer: str = Field(description="回答文本；失败时明确说明未生成结论")
    citations: list[CitationOut] = Field(default_factory=list)
    evidence: list[EvidenceOut] = Field(default_factory=list)
    retrieval: RetrievalMetaOut
    model: dict[str, str | None] = Field(description="实际使用的 LLM provider/name")
    llm_status: str = Field(description="ok / skipped_* / failed")
    llm_error: str | None = None
    citation_validation: dict[str, object] = Field(default_factory=dict)
    timings_ms: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    prompt_chars: int = 0
    prompt_version: str


class DocumentOut(BaseModel):
    doc_key: str
    title: str
    source_url: str | None = None
    file_path: str
    file_sha256: str
    file_bytes: int | None = None
    page_count: int | None = None
    doc_type: DocType
    equipment_scope: EquipmentScope
    applicability_note: str | None = None
    manufacturer: str | None = None
    model_numbers: str | None = None
    language: str | None = None
    trust_level: str
    license: str | None = None
    version: str | None = None
    parse_status: str
    parse_quality_note: str | None = None
    content_revision: int
    ingested_at: str | None = None
    chunks: dict[str, int] = Field(
        default_factory=dict, description="按切分版本统计的 Chunk 数量"
    )

    model_config = ConfigDict(extra="ignore")


class DocumentsResponse(BaseModel):
    items: list[DocumentOut]
    total: int
    limit: int
    offset: int


class KnowledgeHealthResponse(BaseModel):
    status: str = Field(description="ok / degraded / unavailable")
    database: dict[str, object]
    schema_version: str | None = None
    schema_expected_version: str | None = None
    schema_ok: bool = False
    documents: int = 0
    chunks: int = 0
    indexes: list[dict[str, object]] = Field(default_factory=list)
    embedding: dict[str, object] = Field(default_factory=dict)
    llm: dict[str, object] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
