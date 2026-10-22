"""向量检索：问题 → Embedding → pgvector 余弦相似度 Top-K。

要点：
- 查询向量必须与索引使用**同一模型 + 同一版本**，否则会得到不可比的分数；
  发现索引里只有其它模型/版本的向量时直接抛 `IndexModelMismatch`，而不是返回空结果；
- 相似度 = 1 - 余弦距离，范围理论上 [-1, 1]，只用于排序与展示；
- 过滤条件（设备适用范围/文档类型/文档键）在 SQL 中执行，支持“型号不匹配不参与检索”。
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.rag.embedding import EmbeddingProvider, vector_literal
from app.rag.errors import IndexModelMismatch
from app.rag.retrieval.base import RetrievalFilters, RetrievedChunk
from app.rag.retrieval.keyword import _COLUMNS, _build_filter_sql, _row_to_chunk

logger = logging.getLogger("industrial_agent_audit.rag.retrieval.vector")

_DISTANCE = "c.embedding <=> CAST(:query_vector AS vector)"


class VectorSearch:
    method = "vector"

    def __init__(self, embedder: EmbeddingProvider) -> None:
        self.embedder = embedder

    def search(
        self,
        session: Session,
        query: str,
        *,
        filters: RetrievalFilters,
        top_k: int,
        candidate_k: int | None = None,
    ) -> tuple[list[RetrievedChunk], float]:
        """返回 (结果, 查询向量化耗时毫秒)。"""
        limit = max(top_k, candidate_k or top_k)
        self._assert_index_compatible(session, filters)

        batch = self.embedder.embed_query(query)
        vector = batch.vectors[0]
        filter_sql, filter_params = _build_filter_sql(filters)

        sql = f"""
            SELECT {_COLUMNS},
                   1 - ({_DISTANCE}) AS similarity
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.embedding IS NOT NULL
              AND c.embedding_model = :embedding_model
              AND c.embedding_version = :embedding_version
              {filter_sql}
            ORDER BY {_DISTANCE} ASC, c.id ASC
            LIMIT :limit
        """
        # 检索按过滤条件指定的模型/版本执行（默认由 KnowledgeRetriever 填成当前配置）；
        # 未显式指定时才回退到 provider 自身，避免“配置与索引混用”却静默返回空结果。
        model = filters.embedding_model or self.embedder.model
        version = filters.embedding_version or self.embedder.version
        params = {
            "query_vector": vector_literal(vector),
            "embedding_model": model,
            "embedding_version": version,
            "limit": limit,
            **filter_params,
        }
        rows = session.execute(text(sql), params).all()
        results: list[RetrievedChunk] = []
        for rank, row in enumerate(rows, start=1):
            similarity = float(row.similarity)
            if similarity < filters.min_similarity:
                continue
            chunk = _row_to_chunk(row, score=similarity, matched_terms=[])
            chunk.vector_similarity = similarity
            chunk.vector_rank = rank
            results.append(chunk)
        return results, batch.elapsed_ms

    def _assert_index_compatible(
        self, session: Session, filters: RetrievalFilters
    ) -> None:
        """确认索引中确实存在当前模型/版本的向量，避免静默空结果。"""
        model = filters.embedding_model or self.embedder.model
        version = filters.embedding_version or self.embedder.version
        row = session.execute(
            text(
                """
                SELECT
                    count(*) FILTER (
                        WHERE c.embedding_model = :embedding_model
                          AND c.embedding_version = :embedding_version
                    ) AS matching,
                    count(*) AS total,
                    array_agg(DISTINCT coalesce(c.embedding_model || '@' || c.embedding_version, 'null'))
                        AS models
                FROM document_chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE c.chunking_version = :chunking_version
                """
                + (
                    " AND d.equipment_scope = ANY(:equipment_scopes)"
                    if filters.equipment_scopes
                    else ""
                )
            ),
            {
                "embedding_model": model,
                "embedding_version": version,
                "chunking_version": filters.chunking_version,
                "equipment_scopes": list(filters.equipment_scopes),
            },
        ).one()
        if int(row.total) == 0:
            raise IndexModelMismatch(
                f"切分版本 {filters.chunking_version!r} 没有已向量化的 Chunk，请先执行入库",
                chunking_version=filters.chunking_version,
            )
        if int(row.matching) == 0:
            raise IndexModelMismatch(
                "索引中的向量模型与当前配置不一致："
                f"查询使用 {model}@{version}，"
                f"索引中为 {sorted(row.models or [])}；请重新向量化或修改配置",
                configured_model=model,
                configured_version=version,
                indexed=sorted(row.models or []),
            )


def vector_search(
    session: Session,
    query: str,
    *,
    embedder: EmbeddingProvider,
    filters: RetrievalFilters,
    top_k: int = 10,
    candidate_k: int | None = None,
) -> list[RetrievedChunk]:
    results, _ = VectorSearch(embedder).search(
        session, query, filters=filters, top_k=top_k, candidate_k=candidate_k
    )
    return results
