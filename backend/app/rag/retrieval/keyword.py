"""关键词检索：为工业标识符与中文问题设计，不依赖 PostgreSQL 中文分词。

为什么不用默认 `to_tsvector('english'|'simple')` 直接检索中文：
- `simple` 配置按空白/标点切词，整句中文会变成一个（或几个）超长 token，
  用户问“储气压力异常”时无法命中文档中的“压力”；
- `english` 配置对中文同样无效，还会对标识符做词干化。

实现三层信号：
1. **ASCII 技术标识符精确包含**（TP2、TP3、Motor_current、DV_pressure、8.2 bar 等），
   用小写文本 + `LIKE '%term%'` 命中（`pg_trgm` GIN 索引可加速）；
2. **中文 bigram 覆盖**：把中文连续片段切成 2 字窗口，统计命中比例，
   不假设任何分词器；
3. `tsvector`（`simple` 配置）的 `ts_rank_cd` 作为辅助信号，对英文词命中做加权。

融合公式（显式、可解释，不做未归一化相加）：

    score = 0.45 * ascii_hit_ratio
          + 0.45 * cjk_bigram_hit_ratio
          + 0.10 * min(1, ts_rank_cd * 10)

命中比例的分母是“查询侧抽出的不同 term 数”，分子是“在 Chunk 小写文本中出现过的 term 数”。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.rag.retrieval.base import RetrievalFilters, RetrievedChunk

# ASCII 技术词：字母数字组合，允许 _ . - / 连接（TP2、Motor_current、DOE/EE-1340、8.2）
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-/]{1,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")

_ASCII_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "what", "which", "how",
    "are", "was", "were", "does", "did", "can", "could", "should", "would", "why",
    "when", "where", "who", "its", "their", "there", "have", "has", "had", "not",
    "you", "your", "about", "into", "than", "then", "them", "these", "those",
    "may", "might", "will", "shall", "been", "being", "also", "such", "more",
    "most", "some", "any", "all", "each", "other", "please", "tell", "give",
}

# 中文高频疑问词 bigram（降权不删：直接删会让短问题几乎无 term 可用）
_CJK_STOP_BIGRAMS = {
    "哪些", "什么", "怎么", "如何", "是否", "可以", "请问", "一下", "以及", "或者",
    "的是", "有哪", "可能", "涉及", "应该", "需要", "为什", "么样", "怎样", "介绍",
    "说明", "告诉", "帮我", "请问",
}

_COLUMNS = """
    c.id AS chunk_id,
    c.document_id,
    c.chunk_index,
    c.content,
    c.page_start,
    c.page_end,
    c.section_title,
    c.section_path,
    c.chunking_version,
    c.embedding_model,
    d.doc_key,
    d.title AS document_title,
    d.equipment_scope,
    d.doc_type,
    d.trust_level,
    d.source_url,
    d.version AS document_version,
    d.license
"""


@dataclass(frozen=True)
class QueryTerms:
    ascii_terms: tuple[str, ...]
    cjk_bigrams: tuple[str, ...]
    tsquery: str | None

    @property
    def is_empty(self) -> bool:
        return not self.ascii_terms and not self.cjk_bigrams

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "ascii_terms": list(self.ascii_terms),
            "cjk_bigrams": list(self.cjk_bigrams),
        }


def extract_query_terms(query: str) -> QueryTerms:
    """抽取 ASCII 标识符与中文 bigram；去重且保持出现顺序。"""
    ascii_terms: list[str] = []
    seen: set[str] = set()
    for match in _ASCII_TOKEN_RE.finditer(query):
        token = match.group(0).strip("._-/").lower()
        if len(token) < 2 or token in _ASCII_STOPWORDS or token in seen:
            continue
        if token.isdigit() and len(token) < 3:
            continue
        seen.add(token)
        ascii_terms.append(token)

    bigrams: list[str] = []
    bigram_seen: set[str] = set()
    for run in _CJK_RUN_RE.findall(query):
        if len(run) == 1:
            continue
        for index in range(len(run) - 1):
            gram = run[index : index + 2]
            if gram in _CJK_STOP_BIGRAMS or gram in bigram_seen:
                continue
            bigram_seen.add(gram)
            bigrams.append(gram)

    tsquery = None
    if ascii_terms:
        # term 已被正则限制在 [a-z0-9_.-/]，再剔除非 tsquery 安全字符
        safe = [re.sub(r"[^a-z0-9_]", "", term) for term in ascii_terms]
        safe = [term for term in safe if term]
        if safe:
            tsquery = " | ".join(safe)
    return QueryTerms(tuple(ascii_terms), tuple(bigrams), tsquery)


def _build_filter_sql(filters: RetrievalFilters) -> tuple[str, dict[str, object]]:
    clauses: list[str] = []
    params: dict[str, object] = {}
    if filters.equipment_scopes:
        clauses.append("d.equipment_scope = ANY(:equipment_scopes)")
        params["equipment_scopes"] = list(filters.equipment_scopes)
    if filters.doc_types:
        clauses.append("d.doc_type = ANY(:doc_types)")
        params["doc_types"] = list(filters.doc_types)
    if filters.doc_keys:
        clauses.append("d.doc_key = ANY(:doc_keys)")
        params["doc_keys"] = list(filters.doc_keys)
    if filters.chunking_version:
        clauses.append("c.chunking_version = :chunking_version")
        params["chunking_version"] = filters.chunking_version
    if filters.embedding_model:
        clauses.append("c.embedding_model = :embedding_model")
        params["embedding_model"] = filters.embedding_model
    if filters.embedding_version:
        clauses.append("c.embedding_version = :embedding_version")
        params["embedding_version"] = filters.embedding_version
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def _row_to_chunk(row, *, score: float, matched_terms: list[str]) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=int(row.chunk_id),
        document_id=str(row.document_id),
        doc_key=row.doc_key,
        document_title=row.document_title,
        chunk_index=int(row.chunk_index),
        content=row.content,
        page_start=int(row.page_start),
        page_end=int(row.page_end),
        section_title=row.section_title,
        section_path=row.section_path,
        equipment_scope=row.equipment_scope,
        doc_type=row.doc_type,
        trust_level=row.trust_level,
        source_url=row.source_url,
        document_version=row.document_version,
        license=row.license,
        chunking_version=row.chunking_version,
        embedding_model=row.embedding_model,
        score=score,
        keyword_score=score,
        matched_terms=matched_terms,
    )


class KeywordSearch:
    """基于 PostgreSQL 的关键词检索（LIKE/bigram + tsvector 辅助）。"""

    method = "keyword"

    def __init__(self, *, ascii_weight: float = 0.45, cjk_weight: float = 0.45, ts_weight: float = 0.10):
        total = ascii_weight + cjk_weight + ts_weight
        self.ascii_weight = ascii_weight / total
        self.cjk_weight = cjk_weight / total
        self.ts_weight = ts_weight / total

    def search(
        self,
        session: Session,
        query: str,
        *,
        filters: RetrievalFilters,
        top_k: int,
        candidate_k: int | None = None,
    ) -> list[RetrievedChunk]:
        terms = extract_query_terms(query)
        limit = max(top_k, candidate_k or top_k)
        if terms.is_empty:
            return []

        payload = {
            "ascii": list(terms.ascii_terms),
            "cjk": list(terms.cjk_bigrams),
        }
        terms_json = json.dumps(
            [{"t": term, "k": kind} for kind, values in payload.items() for term in values]
        )
        filter_sql, filter_params = _build_filter_sql(filters)
        ts_clause = (
            "c.search_tsv @@ to_tsquery('simple', :tsquery)"
            if terms.tsquery
            else "FALSE"
        )
        ts_rank = (
            "ts_rank_cd(c.search_tsv, to_tsquery('simple', :tsquery))"
            if terms.tsquery
            else "0.0"
        )

        sql = f"""
            SELECT {_COLUMNS},
                   h.ascii_hits,
                   h.cjk_hits,
                   {ts_rank} AS ts_rank,
                   (CASE WHEN h.ascii_hits > 0 OR h.cjk_hits > 0 OR {ts_clause}
                         THEN 1 ELSE 0 END) AS any_hit
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            CROSS JOIN LATERAL (
                SELECT
                    count(*) FILTER (WHERE t.k = 'ascii') AS ascii_hits,
                    count(*) FILTER (WHERE t.k = 'cjk') AS cjk_hits
                FROM jsonb_to_recordset(CAST(:terms_json AS jsonb)) AS t(t text, k text)
                WHERE c.search_text LIKE '%' || t.t || '%'
            ) h
            WHERE (h.ascii_hits > 0 OR h.cjk_hits > 0 OR {ts_clause})
            {filter_sql}
            ORDER BY any_hit DESC,
                     (
                        :ascii_weight * (h.ascii_hits::float / GREATEST(:n_ascii, 1))
                      + :cjk_weight * (h.cjk_hits::float / GREATEST(:n_cjk, 1))
                      + :ts_weight * LEAST(1.0, {ts_rank} * 10.0)
                     ) DESC,
                     ts_rank DESC,
                     c.id ASC
            LIMIT :limit
        """
        params = {
            "terms_json": terms_json,
            "n_ascii": len(terms.ascii_terms),
            "n_cjk": len(terms.cjk_bigrams),
            "ascii_weight": self.ascii_weight,
            "cjk_weight": self.cjk_weight,
            "ts_weight": self.ts_weight,
            "limit": limit,
            **filter_params,
        }
        if terms.tsquery:
            params["tsquery"] = terms.tsquery

        rows = session.execute(text(sql), params).all()
        results: list[RetrievedChunk] = []
        for rank, row in enumerate(rows, start=1):
            ascii_hits = int(row.ascii_hits)
            cjk_hits = int(row.cjk_hits)
            score = (
                self.ascii_weight * (ascii_hits / max(1, len(terms.ascii_terms)))
                + self.cjk_weight * (cjk_hits / max(1, len(terms.cjk_bigrams)))
                + self.ts_weight * min(1.0, float(row.ts_rank) * 10.0)
            )
            matched = [
                term
                for term in (*terms.ascii_terms, *terms.cjk_bigrams)
                if term in (row.content or "").lower()
            ][:20]
            chunk = _row_to_chunk(row, score=score, matched_terms=matched)
            chunk.keyword_rank = rank
            results.append(chunk)
        return results


def keyword_search(
    session: Session,
    query: str,
    *,
    filters: RetrievalFilters,
    top_k: int = 10,
    candidate_k: int | None = None,
) -> list[RetrievedChunk]:
    return KeywordSearch().search(
        session, query, filters=filters, top_k=top_k, candidate_k=candidate_k
    )
