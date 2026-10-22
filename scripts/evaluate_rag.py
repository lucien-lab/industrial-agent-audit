#!/usr/bin/env python
"""RAG 检索与生成评估（阶段 2）。

用法：
    # 完整评估（3 种检索方法 × 2 种切分策略 + 52 题真实 RAG 问答）
    python scripts/evaluate_rag.py

    # 只跑检索对比（不调用 LLM）
    python scripts/evaluate_rag.py --skip-generation

指标口径（写在 knowledge/eval/questions.json 的 metric_definition 中）：
- 相关性：doc_key 相同且 Chunk 页码区间与预期页码有交集；
- Recall@5 / HitRate@5 / MRR@10 / Precision@5，可回答题宏平均；
- 不可回答题不参与检索指标，但参与生成评估的“正确拒答”统计。

生成评估：
- 自动检查：引用有效性、引用是否落在预期证据、关键点覆盖、禁止出现的编造内容；
- 人工复核：输出 `knowledge/eval/generation_manual_review.json` 模板，
  人工填写 evidence_supported / citation_correct / unsupported_claims 后再运行
  `--review <file>` 即可汇总人工标注结果。自动评分不能替代人工判断。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.rag.answer import RAGAnswerer  # noqa: E402
from app.rag.config import get_rag_settings  # noqa: E402
from app.rag.embedding import get_embedding_provider  # noqa: E402
from app.rag.llm import get_chat_model  # noqa: E402
from app.rag.retrieval import KnowledgeRetriever, RetrievalFilters  # noqa: E402

PROJECT_ROOT = BACKEND_DIR.parent
REFUSAL_RE = re.compile(
    r"无法确认|无法回答|不足以|证据不足|未提及|没有(?:找到|提及|相关)|不包含|不能确认",
    re.IGNORECASE,
)


def load_questions(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = data.get("questions") or []
    if not questions:
        raise SystemExit(f"{path} 中没有 questions")
    return questions


def check_expected_pages(session, questions: list[dict]) -> list[str]:
    """校验人工标注的预期页码没有超出文档实际页数。"""
    page_counts = {
        row.doc_key: row.page_count
        for row in session.execute(
            text("SELECT doc_key, page_count FROM documents")
        ).all()
    }
    warnings: list[str] = []
    for question in questions:
        for expected in question.get("expected") or []:
            doc_key = expected["doc_key"]
            if doc_key not in page_counts:
                warnings.append(f"{question['id']}: 文档 {doc_key} 不在知识库中")
                continue
            for page in expected["pages"]:
                if page_counts[doc_key] and page > page_counts[doc_key]:
                    warnings.append(
                        f"{question['id']}: 页码 {page} 超出 {doc_key} 的 {page_counts[doc_key]} 页"
                    )
    return warnings


def page_overlap(chunk, expected: dict) -> bool:
    """Chunk 是否满足某一条 (doc_key, pages) 证据定位。"""
    if chunk.doc_key != expected["doc_key"]:
        return False
    return any(
        chunk.page_start <= page <= chunk.page_end for page in expected["pages"]
    )


def requirement_met(chunk, requirement: dict) -> bool:
    """一条证据需求可由主文档页码或 also_ok 中的其它文档/页码满足。"""
    if page_overlap(chunk, requirement):
        return True
    return any(page_overlap(chunk, alternative) for alternative in requirement.get("also_ok") or [])


def requirement_satisfied(requirement: dict, chunks: list) -> bool:
    return any(requirement_met(chunk, requirement) for chunk in chunks)


def is_relevant(chunk, requirements: list[dict]) -> bool:
    return any(requirement_met(chunk, requirement) for requirement in requirements)


def retrieval_metrics(question: dict, results: list) -> dict[str, float] | None:
    expected = question.get("expected") or []
    if not question.get("answerable") or not expected:
        return None
    top5 = results[:5]
    top10 = results[:10]
    matched_expected = sum(
        1 for requirement in expected if requirement_satisfied(requirement, top5)
    )
    recall = matched_expected / len(expected)
    hit = 1.0 if matched_expected > 0 else 0.0
    first_rank = None
    for rank, chunk in enumerate(top10, start=1):
        if is_relevant(chunk, expected):
            first_rank = rank
            break
    reciprocal = 1.0 / first_rank if first_rank else 0.0
    relevant_top5 = sum(1 for chunk in top5 if is_relevant(chunk, expected))
    precision = relevant_top5 / len(top5) if top5 else 0.0
    return {
        "recall@5": recall,
        "hit@5": hit,
        "mrr@10": reciprocal,
        "precision@5": precision,
        "expected_total": len(expected),
        "matched_expected": matched_expected,
        "first_relevant_rank": first_rank,
    }


def aggregate(per_question: list[dict]) -> dict[str, float | int]:
    scored = [item for item in per_question if item is not None]
    if not scored:
        return {"questions": 0}
    count = len(scored)
    return {
        "questions": count,
        "recall@5": round(sum(item["recall@5"] for item in scored) / count, 4),
        "hit@5": round(sum(item["hit@5"] for item in scored) / count, 4),
        "mrr@10": round(sum(item["mrr@10"] for item in scored) / count, 4),
        "precision@5": round(sum(item["precision@5"] for item in scored) / count, 4),
    }


def run_retrieval(
    session,
    settings,
    questions: list[dict],
    *,
    chunking_version: str,
    methods: list[str],
    top_k: int,
) -> dict[str, object]:
    embedder = get_embedding_provider(settings)
    retriever = KnowledgeRetriever(session, settings, embedder)
    output: dict[str, object] = {}
    for method in methods:
        per_question: list[dict] = []
        elapsed: list[float] = []
        rows: list[dict] = []
        for question in questions:
            filters = RetrievalFilters(
                equipment_scopes=("metropt3_direct", "generic_compressor"),
                chunking_version=chunking_version,
                embedding_model=settings.embedding_model,
                embedding_version=settings.embedding_version,
            )
            started = time.perf_counter()
            result = retriever.search(
                question["question"], method=method, top_k=top_k, filters=filters
            )
            elapsed.append((time.perf_counter() - started) * 1000)
            metrics = retrieval_metrics(question, result.results)
            per_question.append(metrics)
            rows.append(
                {
                    "id": question["id"],
                    "category": question["category"],
                    "answerable": question["answerable"],
                    "metrics": metrics,
                    "top": [
                        {
                            "doc_key": chunk.doc_key,
                            "page_start": chunk.page_start,
                            "page_end": chunk.page_end,
                            "score": round(chunk.score, 5),
                            "vector_similarity": (
                                round(chunk.vector_similarity, 5)
                                if chunk.vector_similarity is not None
                                else None
                            ),
                            "keyword_score": (
                                round(chunk.keyword_score, 5)
                                if chunk.keyword_score is not None
                                else None
                            ),
                        }
                        for chunk in result.results
                    ],
                    "query_terms": result.query_terms,
                    "warnings": result.warnings,
                }
            )

        by_category: dict[str, dict] = {}
        for category in sorted({q["category"] for q in questions}):
            subset = [
                item[1]
                for item in zip(questions, per_question)
                if item[0]["category"] == category
            ]
            by_category[category] = aggregate(subset)
        output[method] = {
            "overall": aggregate(per_question),
            "by_category": by_category,
            "mean_elapsed_ms": round(sum(elapsed) / max(1, len(elapsed)), 2),
            "questions": rows,
        }
    return output


def keyword_hits(answer: str, points: list[str]) -> dict[str, bool]:
    lowered = answer.lower()
    result: dict[str, bool] = {}
    for point in points:
        alternatives = [part.strip().lower() for part in point.split("|") if part.strip()]
        result[point] = any(alternative in lowered for alternative in alternatives)
    return result


def run_generation(
    session,
    settings,
    questions: list[dict],
    *,
    chunking_version: str,
    method: str,
    top_k: int,
    sleep_s: float,
) -> dict[str, object]:
    embedder = get_embedding_provider(settings)
    chat_model = get_chat_model(settings)
    retriever = KnowledgeRetriever(session, settings, embedder)
    answerer = RAGAnswerer(session, settings, retriever, chat_model)
    rows: list[dict] = []
    for question in questions:
        filters = RetrievalFilters(
            equipment_scopes=("metropt3_direct", "generic_compressor"),
            chunking_version=chunking_version,
            embedding_model=settings.embedding_model,
            embedding_version=settings.embedding_version,
        )
        answer = answerer.answer(
            question["question"], top_k=top_k, method=method, filters=filters
        )
        expected = question.get("expected") or []
        citation_overlap = all(
            requirement_satisfied(requirement, answer.citations) for requirement in expected
        ) if expected else False
        coverage = keyword_hits(answer.answer, question.get("expected_answer_points") or [])
        forbidden_hits = [
            term
            for term in question.get("forbidden_terms") or []
            if term.lower() in answer.answer.lower()
        ]
        refusal = bool(REFUSAL_RE.search(answer.answer)) or answer.status == "insufficient_evidence"
        payload = {
            "id": question["id"],
            "category": question["category"],
            "question": question["question"],
            "answerable": question["answerable"],
            "must_refuse": question.get("must_refuse", False),
            "status": answer.status,
            "llm_status": answer.llm_status,
            "answer": answer.answer,
            "citations": [citation.to_dict() for citation in answer.citations],
            "citation_validation": answer.citation_validation,
            "citation_matches_expected": citation_overlap,
            "expected_point_coverage": {
                "matched": sum(1 for hit in coverage.values() if hit),
                "total": len(coverage),
                "detail": coverage,
            },
            "forbidden_hits": forbidden_hits,
            "refusal_detected": refusal,
            "refusal_correct": bool(question.get("must_refuse")) and refusal and not forbidden_hits,
            "timings_ms": {key: round(value, 2) for key, value in answer.timings.items()},
            "retrieval_top": [
                {
                    "doc_key": chunk.doc_key,
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "score": round(chunk.score, 5),
                }
                for chunk in answer.retrieval.results[:5]
            ],
            "warnings": answer.warnings,
            "prompt_chars": answer.prompt_chars,
        }
        rows.append(payload)
        print(
            f"  {payload['id']} {payload['status']:22s} cites={len(answer.citations):2d} "
            f"cov={payload['expected_point_coverage']['matched']}/"
            f"{payload['expected_point_coverage']['total']} "
            f"forbid={len(forbidden_hits)} llm={payload['timings_ms'].get('llm', 0):.0f}ms"
        )
        if sleep_s:
            time.sleep(sleep_s)

    answerable = [row for row in rows if row["answerable"]]
    must_refuse = [row for row in rows if row["must_refuse"]]
    answered = [row for row in rows if row["status"] == "answered"]
    summary = {
        "questions": len(rows),
        "answered": len(answered),
        "insufficient_evidence": sum(
            1 for row in rows if row["status"] == "insufficient_evidence"
        ),
        "no_evidence": sum(1 for row in rows if row["status"] == "no_evidence"),
        "llm_failures": sum(1 for row in rows if row["llm_status"] == "failed"),
        "citation_valid_rate": round(
            sum(1 for row in rows if row["citation_validation"].get("valid")) / len(rows), 4
        ),
        "quote_supported_all_rate": round(
            sum(
                1
                for row in rows
                if row["citation_validation"].get("quote_supported") == "all"
            )
            / len(rows),
            4,
        ),
        "quote_supported_not_provided": sum(
            1
            for row in rows
            if row["citation_validation"].get("quote_supported") == "not_provided"
        ),
        "quote_supported_failed": sum(
            1
            for row in rows
            if row["citation_validation"].get("quote_supported") in ("failed_all_invalid", "partial")
        ),
        "citation_matches_expected_rate": round(
            sum(1 for row in answerable if row["citation_matches_expected"])
            / max(1, len(answerable)),
            4,
        ),
        "mean_expected_point_coverage": round(
            sum(
                row["expected_point_coverage"]["matched"]
                / max(1, row["expected_point_coverage"]["total"])
                for row in answerable
            )
            / max(1, len(answerable)),
            4,
        ),
        "must_refuse_questions": len(must_refuse),
        "refusal_correct_rate": round(
            sum(1 for row in must_refuse if row["refusal_correct"]) / max(1, len(must_refuse)),
            4,
        ),
        "forbidden_term_questions": sum(1 for row in rows if row["forbidden_hits"]),
        "avg_retrieval_ms": round(
            sum(row["timings_ms"].get("retrieval", 0) for row in rows) / len(rows), 2
        ),
        "avg_llm_ms": round(
            sum(row["timings_ms"].get("llm", 0) for row in rows) / len(rows), 2
        ),
        "avg_total_ms": round(
            sum(row["timings_ms"].get("total", 0) for row in rows) / len(rows), 2
        ),
    }
    return {"summary": summary, "questions": rows}


def write_review_template(path: Path, generation: dict) -> None:
    if path.exists():
        return
    labels = {}
    for row in generation["questions"]:
        labels[row["id"]] = {
            "evidence_supported": "",
            "citation_correct": "",
            "unsupported_claims": "",
            "notes": "",
        }
    payload = {
        "rubric": "yes / partial / no；人工阅读回答与其引用原文后填写，禁止用模型自评代替",
        "reviewer": "",
        "reviewed_at": "",
        "labels": labels,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def summarize_manual(review: dict, generation: dict) -> dict[str, object]:
    labels = review.get("labels") or {}
    valid = {"yes", "partial", "no"}
    counted = {
        "evidence_supported": defaultdict(int),
        "citation_correct": defaultdict(int),
        "unsupported_claims": defaultdict(int),
    }
    reviewed = 0
    for row in generation["questions"]:
        label = labels.get(row["id"]) or {}
        if not any(label.get(key) in valid for key in counted):
            continue
        reviewed += 1
        for key in counted:
            value = label.get(key)
            if value in valid:
                counted[key][value] += 1
    return {
        "reviewed_questions": reviewed,
        "reviewer": review.get("reviewer"),
        "reviewed_at": review.get("reviewed_at"),
        "distribution": {key: dict(value) for key, value in counted.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 检索与生成评估")
    parser.add_argument("--questions", type=Path, default=PROJECT_ROOT / "knowledge" / "eval" / "questions.json")
    parser.add_argument("--chunking-versions", default="structural-v1,fixed-v1")
    parser.add_argument("--methods", default="vector,keyword,hybrid")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--generation-chunking", default="structural-v1")
    parser.add_argument("--generation-method", default="hybrid")
    parser.add_argument("--generation-top-k", type=int, default=5)
    parser.add_argument("--skip-retrieval", action="store_true")
    parser.add_argument(
        "--reuse-generation",
        type=Path,
        default=None,
        help="复用已有评估文件中的 generation 结果（保证人工标注与当时的回答一一对应）",
    )
    parser.add_argument("--skip-generation", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--sleep", type=float, default=0.0, help="每题之间的等待秒数（限流保护）")
    parser.add_argument("--review", type=Path, default=PROJECT_ROOT / "knowledge" / "eval" / "generation_manual_review.json")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "data" / "processed" / "knowledge" / "rag_eval.json")
    args = parser.parse_args()

    settings = get_rag_settings()
    questions = load_questions(args.questions)
    if args.limit:
        questions = questions[: args.limit]
    methods = [item.strip() for item in args.methods.split(",") if item.strip()]
    versions = [item.strip() for item in args.chunking_versions.split(",") if item.strip()]

    engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    started = time.perf_counter()
    payload: dict[str, object] = {
        "evaluated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "questions_file": str(args.questions.relative_to(PROJECT_ROOT)),
        "question_count": len(questions),
        "embedding_model": settings.embedding_model,
        "embedding_version": settings.embedding_version,
        "llm_model": settings.llm_model,
        "chunking_versions": versions,
        "methods": methods,
    }
    try:
        page_warnings = check_expected_pages(session, questions)
        payload["page_warnings"] = page_warnings
        for warning in page_warnings:
            print(f"[warn] {warning}")

        if not args.skip_retrieval:
            payload["retrieval"] = {}
            for version in versions:
                print(f"[retrieval] chunking_version={version}")
                payload["retrieval"][version] = run_retrieval(
                    session,
                    settings,
                    questions,
                    chunking_version=version,
                    methods=methods,
                    top_k=args.top_k,
                )
                for method in methods:
                    overall = payload["retrieval"][version][method]["overall"]
                    print(f"  {method:8s} {overall}")

        if args.reuse_generation is not None:
            previous = json.loads(args.reuse_generation.read_text(encoding="utf-8"))
            if "generation" not in previous:
                raise SystemExit(f"{args.reuse_generation} 中没有 generation 结果")
            payload["generation"] = previous["generation"]
            payload["generation_reused_from"] = str(args.reuse_generation)
            print("[generation] 复用既有回答：" + str(args.reuse_generation))
        if not args.skip_generation and args.reuse_generation is None:
            print(f"[generation] chunking={args.generation_chunking} method={args.generation_method}")
            generation = run_generation(
                session,
                settings,
                questions,
                chunking_version=args.generation_chunking,
                method=args.generation_method,
                top_k=args.generation_top_k,
                sleep_s=args.sleep,
            )
            payload["generation"] = generation
            print(f"[generation] summary: {json.dumps(generation['summary'], ensure_ascii=False)}")
        if "generation" in payload:
            write_review_template(args.review, payload["generation"])
            if args.review.exists():
                review = json.loads(args.review.read_text(encoding="utf-8"))
                payload["generation"]["manual_review"] = summarize_manual(
                    review, payload["generation"]
                )
                print(
                    f"[generation] manual review: {json.dumps(payload['generation']['manual_review'], ensure_ascii=False)}"
                )
    finally:
        session.close()
        engine.dispose()

    payload["total_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] {args.out} ({payload['total_elapsed_ms'] / 1000:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
