"""混合检索：Reciprocal Rank Fusion（RRF）。

为什么用 RRF 而不是“余弦相似度 + 关键词分数”直接相加：
- 余弦相似度（约 0.3–0.9）与关键词命中比例（0–1）量纲不同，未归一化相加会让
  某个信号压倒另一个；
- RRF 只使用**名次**：score = Σ 1 / (k + rank)，对分数量纲不敏感，参数只有一个 `k`。

同时保留两路原始分数与名次，便于解释“为什么这条被排上来”。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.rag.config import RAGSettings
from app.rag.embedding import EmbeddingProvider
from app.rag.retrieval.base import RetrievedChunk, RetrievalFilters, RetrievalResult
from app.rag.retrieval.keyword import KeywordSearch, extract_query_terms
from app.rag.retrieval.vector import VectorSearch


@dataclass
class HybridConfig:
    rrf_k: int = 60
    vector_candidates: int = 30
    keyword_candidates: int = 30


def reciprocal_rank_fusion(
    ranked_lists: dict[str, list[RetrievedChunk]],
    *,
    rrf_k: int = 60,
    top_k: int = 10,
) -> list[RetrievedChunk]:
    """按 chunk_id 去重并融合多路排名。`rank` 从 1 开始。"""
    if rrf_k < 1:
        raise ValueError("rrf_k 必须 >= 1")
    merged: dict[int, RetrievedChunk] = {}
    scores: dict[int, float] = {}
    for method, items in ranked_lists.items():
        for rank, item in enumerate(items, start=1):
            current = merged.get(item.chunk_id)
            if current is None:
                current = item
                merged[item.chunk_id] = current
                scores[item.chunk_id] = 0.0
            # 合并两路的可解释字段
            if method == "vector":
                current.vector_similarity = item.vector_similarity
                current.vector_rank = item.vector_rank or rank
            elif method == "keyword":
                current.keyword_score = item.keyword_score
                current.keyword_rank = item.keyword_rank or rank
                if item.matched_terms:
                    current.matched_terms = item.matched_terms
            scores[item.chunk_id] += 1.0 / (rrf_k + rank)

    for chunk_id, chunk in merged.items():
        chunk.rrf_score = scores[chunk_id]
        chunk.score = scores[chunk_id]

    ordered = sorted(
        merged.values(),
        key=lambda c: (-(c.rrf_score or 0.0), c.chunk_id),
    )
    return ordered[:top_k]


class KnowledgeRetriever:
    """检索门面：向量 / 关键词 / 混合三种方法，统一过滤与计时。"""

    def __init__(
        self,
        session: Session,
        settings: RAGSettings,
        embedder: EmbeddingProvider,
        *,
        config: HybridConfig | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.embedder = embedder
        self.vector = VectorSearch(embedder)
        self.keyword = KeywordSearch()
        self.config = config or HybridConfig(
            rrf_k=settings.rrf_k,
            vector_candidates=settings.vector_candidates,
            keyword_candidates=settings.keyword_candidates,
        )

    def default_filters(self, **overrides) -> RetrievalFilters:
        base = {
            "chunking_version": self.settings.chunking_version,
            "embedding_model": self.settings.embedding_model,
            "embedding_version": self.settings.embedding_version,
            "min_similarity": self.settings.min_similarity,
        }
        base.update({key: value for key, value in overrides.items() if value is not None})
        return RetrievalFilters(**base)

    def search(
        self,
        query: str,
        *,
        method: str = "hybrid",
        top_k: int = 5,
        filters: RetrievalFilters | None = None,
    ) -> RetrievalResult:
        started = time.perf_counter()
        filters = filters or self.default_filters()
        if filters.chunking_version is None:
            filters = RetrievalFilters(
                equipment_scopes=filters.equipment_scopes,
                doc_types=filters.doc_types,
                doc_keys=filters.doc_keys,
                chunking_version=self.settings.chunking_version,
                embedding_model=filters.embedding_model,
                embedding_version=filters.embedding_version,
                min_similarity=filters.min_similarity,
            )
        warnings: list[str] = []
        embedding_ms = 0.0
        vector_items: list[RetrievedChunk] = []
        keyword_items: list[RetrievedChunk] = []

        if method not in ("vector", "keyword", "hybrid"):
            raise ValueError(f"未知检索方法 {method!r}，可选 vector/keyword/hybrid")

        if method in ("vector", "hybrid"):
            vector_items, embedding_ms = self.vector.search(
                self.session,
                query,
                filters=filters,
                top_k=top_k,
                candidate_k=(self.config.vector_candidates if method == "hybrid" else top_k),
            )
        if method in ("keyword", "hybrid"):
            keyword_items = self.keyword.search(
                self.session,
                query,
                filters=filters,
                top_k=top_k,
                candidate_k=(self.config.keyword_candidates if method == "hybrid" else top_k),
            )

        if method == "vector":
            results = vector_items[:top_k]
        elif method == "keyword":
            results = keyword_items[:top_k]
        else:
            results = reciprocal_rank_fusion(
                {"vector": vector_items, "keyword": keyword_items},
                rrf_k=self.config.rrf_k,
                top_k=top_k,
            )
            if not vector_items:
                warnings.append("向量检索无结果（可能低于相似度阈值或过滤过严）")
            if not keyword_items:
                warnings.append("关键词检索无结果（查询中没有可匹配的标识符/中文 bigram）")

        terms = extract_query_terms(query)
        return RetrievalResult(
            method=method,
            query=query,
            results=results,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            filters=filters.describe(),
            vector_candidates=len(vector_items),
            keyword_candidates=len(keyword_items),
            embedding_elapsed_ms=embedding_ms,
            query_terms=terms.as_dict() if method != "vector" else {},
            warnings=warnings,
        )
