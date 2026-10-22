"""知识库接口（阶段 2）。

- `POST /api/v1/knowledge/search`：混合/向量/关键词检索，返回 Chunk 与来源页码；
- `POST /api/v1/knowledge/ask`：有证据的 RAG 回答（含引用、模型状态与耗时）；
- `GET  /api/v1/knowledge/documents`：已入库文档与状态；
- `GET  /api/v1/knowledge/health`：知识库结构、索引与（可选）模型服务探测。

安全边界：只读；不提供文件上传与写接口；文档入库通过离线 CLI
（`scripts/ingest_knowledge.py`）完成，避免在 HTTP 层引入任意文件写入。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import KnowledgeSessionDep
from app.rag.config import get_rag_settings
from app.schemas.common import ErrorResponse
from app.schemas.knowledge import (
    AskRequest,
    AskResponse,
    DocumentsResponse,
    KnowledgeHealthResponse,
    SearchRequest,
    SearchResponse,
)
from app.services import knowledge as service

router = APIRouter(prefix="/api/v1/knowledge", tags=["知识库"])

_RAG_ERRORS = {
    422: {"model": ErrorResponse, "description": "请求参数不合法"},
    503: {
        "model": ErrorResponse,
        "description": "知识库/Embedding/LLM 不可用，或索引模型与配置不一致",
    },
}


@router.post(
    "/search",
    response_model=SearchResponse,
    summary="知识检索",
    description=(
        "按问题检索知识库片段，支持三种方法：\n\n"
        "- `vector`：pgvector 余弦相似度 Top-K；\n"
        "- `keyword`：ASCII 标识符精确包含 + 中文 bigram 覆盖 + tsvector 辅助；\n"
        "- `hybrid`（默认）：两路候选按 Reciprocal Rank Fusion 融合。\n\n"
        "默认只检索 `metropt3_direct`（直接关联）与 `generic_compressor`（通用资料）两类文档；"
        "每条结果都带 `page_start/page_end`、`chunk_id`、`equipment_scope`，可回溯原文。"
    ),
    responses=_RAG_ERRORS,
)
def search(payload: SearchRequest, session: KnowledgeSessionDep) -> SearchResponse:
    settings = get_rag_settings()
    result = service.search_knowledge(
        session,
        settings,
        service.SearchSpec(
            query=payload.query,
            method=payload.method,
            top_k=payload.top_k,
            equipment_scopes=(
                tuple(payload.equipment_scopes) if payload.equipment_scopes is not None else None
            ),
            doc_types=tuple(payload.doc_types or ()),
            doc_keys=tuple(payload.doc_keys or ()),
            chunking_version=payload.chunking_version,
            min_similarity=payload.min_similarity,
        ),
    )
    return SearchResponse.model_validate(result)


@router.post(
    "/ask",
    response_model=AskResponse,
    summary="知识问答（RAG）",
    description=(
        "执行完整 RAG 流程：混合检索 → 证据选择 → Prompt → LLM → 引用校验。\n\n"
        "回答约束：\n"
        "- 只依据检索证据；证据不足时 `status=insufficient_evidence`，不编造结论；\n"
        "- 引用可定位到文档、页码与 Chunk ID；模型编造的引用标签会被标记；\n"
        "- 问题中的型号/编号若在证据中完全不存在，则直接拒答，不调用模型；\n"
        "- LLM 超时/限流/不可用时 `status=llm_*`，仅返回原文证据，不生成假答案。"
    ),
    responses=_RAG_ERRORS,
)
def ask(payload: AskRequest, session: KnowledgeSessionDep) -> AskResponse:
    settings = get_rag_settings()
    result = service.ask_knowledge(
        session,
        settings,
        service.SearchSpec(
            query=payload.question,
            method=payload.method,
            top_k=payload.top_k,
            equipment_scopes=(
                tuple(payload.equipment_scopes) if payload.equipment_scopes is not None else None
            ),
            doc_types=tuple(payload.doc_types or ()),
            doc_keys=tuple(payload.doc_keys or ()),
            chunking_version=payload.chunking_version,
        ),
    )
    return AskResponse.model_validate(result)


@router.get(
    "/documents",
    response_model=DocumentsResponse,
    summary="已入库文档",
    description=(
        "返回文档元数据与解析状态，并给出每个切分版本（chunking_version）的 Chunk 数量。\n\n"
        "`equipment_scope` 说明适用性：`metropt3_direct` 为直接关联，"
        "`generic_compressor` 仅为同类型设备的通用资料。"
    ),
    responses={422: {"model": ErrorResponse, "description": "分页参数不合法"}},
)
def list_documents(
    session: KnowledgeSessionDep,
    limit: int = Query(50, ge=1, le=200, description="返回条数上限（1–200）"),
    offset: int = Query(0, ge=0, description="偏移量"),
    equipment_scope: str | None = Query(
        None,
        description="按适用性过滤：metropt3_direct / generic_compressor / model_specific_other / unknown",
    ),
) -> DocumentsResponse:
    items, total = service.list_documents(
        session, limit=limit, offset=offset, equipment_scope=equipment_scope
    )
    return DocumentsResponse(items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/health",
    response_model=KnowledgeHealthResponse,
    summary="知识库健康检查",
    description=(
        "检查知识库数据库、迁移版本、文档/Chunk 数量与索引使用的向量模型/版本。\n\n"
        "`check_services=true` 时会真实调用一次 Embedding 与 LLM 探测（会增加 1–15 秒延迟），"
        "默认关闭以免拖慢监控探针。"
    ),
)
def health(
    session: KnowledgeSessionDep,
    check_services: bool = Query(
        False, description="是否真实探测 Embedding / LLM 服务（默认 false）"
    ),
) -> KnowledgeHealthResponse:
    settings = get_rag_settings()
    result = service.knowledge_health(
        session, settings, check_services=check_services
    )
    return KnowledgeHealthResponse.model_validate(result)
