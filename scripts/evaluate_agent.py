#!/usr/bin/env python
"""Agent 评估（阶段 3）：工具选择、数值正确性、引用有效性、无依据结论、耗时。

用法：

    # 完整评估（51 题，真实模型 + 真实 RAG + 真实工业数据；约 5–10 分钟）
    python scripts/evaluate_agent.py

    # 只跑指定题目 / 用 LLM 规划器对比
    python scripts/evaluate_agent.py --ids A01,B01,C01 --planner rule
    python scripts/evaluate_agent.py --planner llm --ids A01,A04,B01,B10,C01,C04,G01,I01,I04

    # 只做确定性渲染（不调用 LLM，用于快速回归；不是真实能力评估）
    python scripts/evaluate_agent.py --answer-mode deterministic --out /tmp/agent_quick.json

输出：
- `--out`      每题的完整结果（含回答、工具调用、接地校验），默认 `data/processed/agent/agent_eval.json`
- `--summary`  聚合指标，默认 `knowledge/eval/agent_eval_summary.json`

指标口径见 `knowledge/eval/agent_questions.json` 的 `metric_definition`。
期望值由人工用 Service/SQL 与文档页码核对，**不由被评估的 Agent 生成**。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.agent.config import get_agent_settings  # noqa: E402
from app.agent.context import AgentContext  # noqa: E402
from app.agent.evidence import extract_numbers  # noqa: E402
from app.agent.service import AgentRequest, run_agent  # noqa: E402
from app.core.config import PROJECT_ROOT, load_env_file  # noqa: E402
from app.rag.answer import normalize_for_quote_match  # noqa: E402

DEFAULT_QUESTIONS = PROJECT_ROOT / "knowledge" / "eval" / "agent_questions.json"
DEFAULT_OUT = PROJECT_ROOT / "data" / "processed" / "agent" / "agent_eval.json"
DEFAULT_SUMMARY = PROJECT_ROOT / "knowledge" / "eval" / "agent_eval_summary.json"

# 确诊式措辞（出现即视为“把可能性表述为结论”）
DIAGNOSIS_RE = re.compile(
    r"已确认故障|确定为故障|确诊|必然是|一定是故障|肯定是故障|必定是"
)


def load_questions(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("questions"):
        raise SystemExit(f"{path} 中没有 questions")
    return data


def _numbers_in(text: str) -> list[float]:
    return extract_numbers(text or "")


def check_number(answer: str, value: float, tolerance: float) -> bool:
    return any(abs(number - value) <= tolerance for number in _numbers_in(answer))


def check_text(answer: str, items: list[str]) -> bool:
    return all(item in answer for item in items)


def page_overlap(item: dict, pages: list[int]) -> bool:
    start = item.get("page_start")
    end = item.get("page_end") or start
    if start is None:
        return False
    return any(start <= page <= end for page in pages)


def verify_citation_verbatim(session: Session, citations: list[dict]) -> list[dict]:
    """逐条核对：被引用 Chunk 的 quote 是否逐字出现在数据库原文中。"""
    problems: list[dict] = []
    for citation in citations:
        if citation.get("kind") != "knowledge":
            continue
        chunk_id = citation.get("chunk_id")
        quote = citation.get("quote") or ""
        if chunk_id is None:
            problems.append({"label": citation.get("label"), "reason": "缺少 chunk_id"})
            continue
        row = session.execute(
            text("SELECT content FROM document_chunks WHERE id = :id"), {"id": chunk_id}
        ).first()
        if row is None:
            problems.append({"label": citation.get("label"), "reason": f"Chunk {chunk_id} 不存在"})
            continue
        if normalize_for_quote_match(quote) not in normalize_for_quote_match(row[0]):
            problems.append(
                {
                    "label": citation.get("label"),
                    "reason": "引用摘录未能逐字出现在该 Chunk 原文中",
                    "chunk_id": chunk_id,
                }
            )
    return problems


def evaluate_question(
    item: dict,
    *,
    settings,
    data_url: str,
    knowledge_url: str,
    citation_session: Session,
) -> dict[str, Any]:
    overrides = item.get("request_overrides") or {}
    request_kwargs: dict[str, Any] = {
        "question": item["question"],
        "equipment_id": overrides.get("equipment_id"),
        "start_time": overrides.get("start_time"),
        "end_time": overrides.get("end_time"),
        "sensors": overrides.get("sensors"),
        "planner": None,
    }
    refused_at_entry: str | None = None
    started = time.perf_counter()
    ctx = AgentContext(settings=settings, data_url=data_url, knowledge_url=knowledge_url)
    try:
        try:
            response = run_agent(
                AgentRequest(**request_kwargs), settings=settings, ctx=ctx
            )
        except Exception as exc:  # noqa: BLE001 - 入口拒绝（如时区）是预期行为之一
            from app.agent.errors import AgentError
            from app.services.errors import ServiceError

            if not isinstance(exc, (ServiceError, AgentError)):
                raise
            refused_at_entry = getattr(exc, "code", type(exc).__name__)
            return {
                "id": item["id"],
                "category": item["category"],
                "question": item["question"],
                "status": "rejected",
                "rejected_code": refused_at_entry,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "answer": "",
                "tools_used": [],
                "tool_calls": [],
                "tool_count": 0,
                "rounds": 0,
                "checks": [],
                "checks_forbidden_hits": [],
                "evidence_requirements": [],
                "numeric_violations": [],
                "citation_issues": [],
                "answer_mode": "none",
                "answer_status": "rejected",
                "intent": "unknown",
                "evidence_labels": [],
                "llm": {},
                "warnings": [f"入口拒绝：{refused_at_entry}"],
                "errors": [{"category": "invalid_input", "code": refused_at_entry}],
                "answer_excerpt": "",
                "citation_problems": [],
                "unsupported_conclusion": False,
                "status_match": item["expected_status"] == "rejected",
                "tool_precision": 1.0 if not item["expected_tools"] else 0.0,
                "tool_recall": 1.0 if not item["expected_tools"] else 0.0,
                "tool_exact": not item["expected_tools"],
            }
    finally:
        ctx.close()
    duration_ms = round((time.perf_counter() - started) * 1000, 2)

    actual_tools = {call["tool"] for call in response.tool_calls}
    required = set(item.get("expected_tools") or [])
    allowed = set(item.get("allowed_tools") or [])
    hit = required & actual_tools
    # precision：实际执行的工具中，属于“必需或允许”的比例（额外工具才算多余）
    extra = actual_tools - required - allowed
    precision = (
        (len(actual_tools) - len(extra)) / len(actual_tools)
        if actual_tools
        else (1.0 if not required else 0.0)
    )
    recall = len(hit) / len(required) if required else 1.0

    checks: list[dict[str, Any]] = []
    for check in item.get("checks") or []:
        if check["kind"] == "numeric":
            ok = check_number(response.answer, float(check["value"]), float(check["tolerance"]))
            checks.append(
                {
                    "kind": "numeric",
                    "expected": check["value"],
                    "tolerance": check["tolerance"],
                    "note": check.get("note"),
                    "ok": ok,
                }
            )
        elif check["kind"] == "text_contains":
            ok = check_text(response.answer, check["items"])
            checks.append({"kind": "text_contains", "expected": check["items"], "ok": ok})

    forbidden_numbers = [
        value
        for value in (item.get("checks_forbidden") or [])
        if check_number(response.answer, float(value), 0.001)
    ]

    evidence_requirements: list[dict[str, Any]] = []
    for requirement in item.get("expected_evidence") or []:
        # 同一需求可由多份文档/页码满足（与 Phase 2 的 also_ok 口径一致）
        alternatives = [requirement] + list(requirement.get("also_ok") or [])
        satisfied = any(
            citation.get("kind") == "knowledge"
            and citation.get("doc_key") == alternative["doc_key"]
            and page_overlap(citation, alternative["pages"])
            for citation in response.citations
            for alternative in alternatives
        )
        evidence_requirements.append(
            {
                "doc_key": requirement["doc_key"],
                "pages": requirement["pages"],
                "satisfied": satisfied,
            }
        )
    citation_problems = verify_citation_verbatim(citation_session, response.citations)

    answer_text = response.answer or ""
    sections = response.sections or {}
    # 确诊式措辞只在“模型叙述部分”（结论 + 推断）里检查：
    # 事实清单中可能出现“不会被自动视为已确认故障样本”这类否定句，不应误判
    narrative = " ".join(
        [
            str(sections.get("conclusion") or ""),
            " ".join(str(note) for note in (sections.get("inference") or [])),
            " ".join(
                str(point.get("text") or "") for point in (sections.get("knowledge") or [])
            ),
        ]
    )
    unsupported = bool(DIAGNOSIS_RE.search(narrative)) or bool(
        response.grounding.get("numeric_violations")
    )

    return {
        "id": item["id"],
        "category": item["category"],
        "question": item["question"],
        "status": response.execution_status,
        "intent": response.intent,
        "answer_mode": response.answer_mode,
        "answer_status": response.answer_status,
        "tools_used": response.tools_used,
        "tool_calls": [
            {
                "tool": call["tool"],
                "status": call["status"],
                "stage": call["stage"],
                "elapsed_ms": call["elapsed_ms"],
                "error_category": (call.get("error") or {}).get("category"),
            }
            for call in response.tool_calls
        ],
        "tool_count": len(response.tool_calls),
        "rounds": response.rounds,
        "checks": checks,
        "checks_forbidden_hits": forbidden_numbers,
        "evidence_requirements": evidence_requirements,
        "citation_problems": citation_problems,
        "numeric_violations": response.grounding.get("numeric_violations") or [],
        "citation_issues": response.grounding.get("citation_issues") or [],
        "unsupported_conclusion": bool(unsupported),
        "narrative_excerpt": narrative[:600],
        "status_match": response.execution_status == item["expected_status"],
        "tool_precision": round(precision, 4),
        "tool_recall": round(recall, 4),
        "tool_exact": precision == 1.0 and recall == 1.0,
        "extra_tools": sorted(extra),
        "missing_tools": sorted(required - actual_tools),
        "duration_ms": duration_ms,
        "answer_excerpt": answer_text[:1500],
        "evidence_labels": [citation.get("label") for citation in response.citations],
        "llm": response.llm,
        "warnings": response.warnings,
        "errors": response.errors,
    }


def run_fault_scenarios(
    scenarios: list[dict], *, settings, data_url: str, knowledge_url: str
) -> list[dict[str, Any]]:
    """注入真实故障（知识库不可达、工具超时、预算不足），验证可恢复与不编造。"""
    outcomes: list[dict[str, Any]] = []
    for scenario in scenarios:
        run_settings = settings
        for key, value in (scenario.get("settings_overrides") or {}).items():
            run_settings = replace(run_settings, **{key: value})
        ctx_kwargs: dict[str, Any] = {"settings": run_settings, "data_url": data_url}
        ctx_kwargs["knowledge_url"] = (scenario.get("context_overrides") or {}).get(
            "knowledge_url", knowledge_url
        )
        ctx = AgentContext(**ctx_kwargs)
        started = time.perf_counter()
        try:
            response = run_agent(
                AgentRequest(question=scenario["question"]), settings=run_settings, ctx=ctx
            )
            categories = {
                (call.get("error") or {}).get("category") for call in response.tool_calls
            }
            expected_category = scenario.get("expect_error_category")
            outcomes.append(
                {
                    "id": scenario["id"],
                    "description": scenario["description"],
                    "status": response.execution_status,
                    "status_ok": response.execution_status in scenario["expect_status"],
                    "error_categories": sorted(item for item in categories if item),
                    "error_category_ok": (
                        expected_category is None or expected_category in categories
                    ),
                    # 失败时不得出现“编造数值”：数值接地违规 + 确诊措辞都要为空
                    "fabrication_free": not response.grounding.get("numeric_violations")
                    and not DIAGNOSIS_RE.search(response.answer or ""),
                    "tool_count": len(response.tool_calls),
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "answer_excerpt": (response.answer or "")[:600],
                }
            )
        finally:
            ctx.close()
    return outcomes


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [record for record in records if record["status"] != "rejected"]
    numeric_checks = [
        check
        for record in records
        for check in record.get("checks", [])
        if check["kind"] == "numeric"
    ]
    text_checks = [
        check
        for record in records
        for check in record.get("checks", [])
        if check["kind"] == "text_contains"
    ]
    evidence_requirements = [
        requirement
        for record in records
        for requirement in record.get("evidence_requirements", [])
    ]
    tool_calls = [
        call for record in evaluated for call in record.get("tool_calls", [])
    ]
    knowledge_runs = [
        record for record in evaluated if "search_technical_knowledge" in record["tools_used"]
    ]
    citation_problems = [
        problem for record in records for problem in record.get("citation_problems", [])
    ]
    citations_total = sum(
        len([label for label in record.get("evidence_labels", []) if str(label).startswith("K")])
        for record in records
    )

    def _rate(values: list[bool]) -> float:
        return round(sum(1 for value in values if value) / len(values), 4) if values else 0.0

    by_category: dict[str, dict[str, Any]] = {}
    for category, group in _group_by(records, "category").items():
        by_category[category] = {
            "count": len(group),
            "tool_exact_rate": _rate([record["tool_exact"] for record in group]),
            "status_match_rate": _rate([record["status_match"] for record in group]),
            "avg_tool_calls": round(
                sum(record.get("tool_count", 0) for record in group) / len(group), 2
            ),
            "avg_duration_ms": round(
                sum(record.get("duration_ms", 0.0) for record in group) / len(group), 2
            ),
            "unsupported_conclusion_rate": _rate(
                [record["unsupported_conclusion"] for record in group]
            ),
        }

    return {
        "questions": len(records),
        "questions_executed": len(evaluated),
        "questions_rejected_at_entry": len(records) - len(evaluated),
        "tool_selection": {
            "exact_match_rate": _rate([record["tool_exact"] for record in records]),
            "precision": round(
                sum(record["tool_precision"] for record in records) / len(records), 4
            ),
            "recall": round(sum(record["tool_recall"] for record in records) / len(records), 4),
            "extra_tool_cases": [
                {"id": record["id"], "extra": record["extra_tools"]}
                for record in records
                if record.get("extra_tools")
            ],
            "missing_tool_cases": [
                {"id": record["id"], "missing": record["missing_tools"]}
                for record in records
                if record.get("missing_tools")
            ],
        },
        "tool_execution": {
            "total_calls": len(tool_calls),
            "success_rate": _rate([call["status"] in ("ok", "empty") for call in tool_calls]),
            "error_rate": _rate([call["status"] == "error" for call in tool_calls]),
            "empty_result_rate": _rate([call["status"] == "empty" for call in tool_calls]),
            "error_categories": dict(
                Counter(
                    call.get("error_category")
                    for call in tool_calls
                    if call.get("error_category")
                )
            ),
        },
        "numeric_accuracy": {
            "numeric_checks": len(numeric_checks),
            "passed": sum(1 for check in numeric_checks if check["ok"]),
            "rate": _rate([check["ok"] for check in numeric_checks]),
            "text_checks": len(text_checks),
            "text_passed": sum(1 for check in text_checks if check["ok"]),
            "text_rate": _rate([check["ok"] for check in text_checks]),
        },
        "citation_accuracy": {
            "knowledge_answer_runs": len(knowledge_runs),
            "evidence_requirement_rate": _rate(
                [requirement["satisfied"] for requirement in evidence_requirements]
            ),
            "evidence_requirements": len(evidence_requirements),
            "citation_verbatim_rate": (
                round((citations_total - len(citation_problems)) / citations_total, 4)
                if citations_total
                else None
            ),
            "problem_cases": citation_problems[:10],
        },
        "unsupported_conclusions": {
            "cases": sum(1 for record in records if record["unsupported_conclusion"]),
            "rate": _rate([record["unsupported_conclusion"] for record in records]),
            "ids": [record["id"] for record in records if record["unsupported_conclusion"]],
        },
        "efficiency": {
            "avg_tool_calls_per_question": round(
                sum(record.get("tool_count", 0) for record in records) / len(records), 2
            ),
            "avg_duration_ms": round(
                sum(record.get("duration_ms", 0.0) for record in records) / len(records), 2
            ),
            "max_duration_ms": round(
                max(record.get("duration_ms", 0.0) for record in records), 2
            ),
        },
        "status_match_rate": _rate([record["status_match"] for record in records]),
        "by_category": by_category,
        "failures": [
            {
                "id": record["id"],
                "category": record["category"],
                "why": _failure_reason(record),
            }
            for record in records
            if not record["tool_exact"]
            or not record["status_match"]
            or record["unsupported_conclusion"]
            or any(not check["ok"] for check in record.get("checks", []))
            or record.get("citation_problems")
        ],
    }


def _failure_reason(record: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not record["tool_exact"]:
        reasons.append(
            f"工具选择不匹配：缺少 {record.get('missing_tools')}，多出 {record.get('extra_tools')}"
        )
    if not record["status_match"]:
        reasons.append(f"执行状态 {record['status']} 与期望不一致")
    failed_checks = [check for check in record.get("checks", []) if not check["ok"]]
    if failed_checks:
        reasons.append(f"事实检查未通过：{[check.get('expected') for check in failed_checks]}")
    if record.get("citation_problems"):
        reasons.append("引用未通过逐字校验")
    if record["unsupported_conclusion"]:
        reasons.append("回答含确诊式措辞或数值接地违规")
    return reasons


def _group_by(records: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record[key]].append(record)
    return dict(grouped)


def _dotenv_value(key: str) -> str | None:
    env_file = Path(PROJECT_ROOT) / ".env"
    if not env_file.is_file():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        name, value = text.split("=", 1)
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 评估（阶段 3）")
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--ids", default=None, help="只跑指定题目，逗号分隔，如 A01,B01")
    parser.add_argument("--planner", choices=["rule", "llm"], default="rule")
    parser.add_argument(
        "--answer-mode", choices=["auto", "llm", "deterministic"], default="auto"
    )
    parser.add_argument(
        "--data-url",
        default=None,
        help="工业数据连接串（默认取 .env 的 DATABASE_URL；只读会话）",
    )
    parser.add_argument(
        "--knowledge-url",
        default=None,
        help="知识库连接串（默认取 .env 的 RAG_DATABASE_URL）",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--skip-fault-scenarios", action="store_true")
    args = parser.parse_args()

    load_env_file()
    data_url = args.data_url or _dotenv_value("DATABASE_URL")
    knowledge_url = args.knowledge_url or _dotenv_value("RAG_DATABASE_URL") or data_url
    if not data_url or not knowledge_url:
        raise SystemExit("缺少 DATABASE_URL / RAG_DATABASE_URL，拒绝在未知数据源上评估")

    payload = load_questions(args.questions)
    questions = payload["questions"]
    if args.ids:
        wanted = {item.strip() for item in args.ids.split(",") if item.strip()}
        questions = [item for item in questions if item["id"] in wanted]
        if not questions:
            raise SystemExit(f"没有匹配的题目：{sorted(wanted)}")
    if args.limit:
        questions = questions[: args.limit]

    settings = replace(
        get_agent_settings(),
        planner=args.planner,
        answer_mode=args.answer_mode,
        trace_write_files=False,
    )
    print(
        f"题目 {len(questions)} 条 | planner={args.planner} | answer_mode={args.answer_mode}"
        f" | data={data_url.split('/')[-1]} | knowledge={knowledge_url.split('/')[-1]}"
    )

    citation_engine = create_engine(knowledge_url, pool_pre_ping=True, future=True)
    records: list[dict[str, Any]] = []
    started = time.perf_counter()
    try:
        with Session(citation_engine) as citation_session:
            for index, item in enumerate(questions, start=1):
                record = evaluate_question(
                    item,
                    settings=settings,
                    data_url=data_url,
                    knowledge_url=knowledge_url,
                    citation_session=citation_session,
                )
                records.append(record)
                flag = "OK " if record["tool_exact"] and record["status_match"] else "!! "
                print(
                    f"[{index:>2}/{len(questions)}] {flag}{record['id']} {record['category']:<22}"
                    f" status={record['status']:<21} tools={record['tool_count']}"
                    f" {record['duration_ms']:.0f}ms"
                )
    finally:
        citation_engine.dispose()

    fault_results: list[dict[str, Any]] = []
    if not args.skip_fault_scenarios:
        fault_results = run_fault_scenarios(
            payload.get("fault_scenarios") or [],
            settings=settings,
            data_url=data_url,
            knowledge_url=knowledge_url,
        )
        for result in fault_results:
            print(
                f"[fault] {result['id']} status={result['status']:<21}"
                f" errors={result['error_categories']} status_ok={result['status_ok']}"
            )

    summary = aggregate(records)
    summary["fault_scenarios"] = {
        "count": len(fault_results),
        "status_ok_rate": (
            round(sum(1 for item in fault_results if item["status_ok"]) / len(fault_results), 4)
            if fault_results
            else None
        ),
        "fabrication_free_rate": (
            round(
                sum(1 for item in fault_results if item["fabrication_free"]) / len(fault_results),
                4,
            )
            if fault_results
            else None
        ),
        "details": fault_results,
    }
    summary["config"] = {
        "planner": args.planner,
        "answer_mode": args.answer_mode,
        "agent_version": payload.get("agent_version"),
        "question_set": str(args.questions.resolve().relative_to(PROJECT_ROOT.resolve())),
        "knowledge_database": knowledge_url.rsplit("/", 1)[-1],
        "industrial_database": data_url.rsplit("/", 1)[-1],
        "total_duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    summary["methodology_limits"] = [
        "页码/文档命中是页面级代理指标，不等于“该片段在语义上足以支持结论”；"
        "引用逐字校验只证明摘录真实存在于 Chunk 中。",
        "数值检查采用容差匹配，未覆盖“数值正确但表述口径错误”的情况。",
        "“无依据结论”由措辞规则 + 数值接地自动识别，不能替代人工语义复核。",
        "工具执行失败只在 fault_scenarios 中注入（知识库不可达、统计超时、预算不足），"
        "未模拟网络抖动、数据库连接池耗尽等环境级故障。",
    ]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "summary": summary,
                "fault_scenarios": fault_results,
                "records": records,
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )

    print("\n=== 汇总 ===")
    for key in ("tool_selection", "tool_execution", "numeric_accuracy", "citation_accuracy",
                "unsupported_conclusions", "efficiency"):
        print(f"{key}: {json.dumps(summary[key], ensure_ascii=False)}")
    print(f"status_match_rate: {summary['status_match_rate']}")
    print(f"fault_scenarios: {json.dumps(summary['fault_scenarios']['status_ok_rate'])} / "
          f"fabrication_free={summary['fault_scenarios']['fabrication_free_rate']}")
    print(f"\n明细：{args.out}")
    print(f"汇总：{args.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
