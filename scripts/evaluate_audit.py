#!/usr/bin/env python
"""阶段 4 审计效果评估：Rule-only / LLM-only / Hybrid 对比。

用法：

    # 完整评估（默认 80 条样例；Judge 调用约 10–20 分钟，结果会缓存）
    python scripts/evaluate_audit.py

    # 只跑规则审计（秒级，不调用 LLM）
    python scripts/evaluate_audit.py --modes rule_only

    # 指定样例 / 重建 Judge 缓存
    python scripts/evaluate_audit.py --ids Q01-real,Q01-mut-numeric_tamper
    python scripts/evaluate_audit.py --no-cache

指标口径（详见 docs/audit_evaluation.md）：
- **case-level**：真值 = 样例是否包含已知异常；预测 = 审计是否给出至少一个 FAIL 发现；
- **category-level**：逐风险类别计算 TP/FP/FN/TN，避免“只算总分”掩盖某类规则失效；
- **UNKNOWN**：单独统计，并同时给出三种处理方式的指标——
  - `exclude`（主口径）：UNKNOWN 不作为正/负预测，仅计入 unknown 数；
  - `strict`：UNKNOWN 视为“未发现问题”（更保守地看漏报）；
  - `lenient`：UNKNOWN 视为“发现问题”（更保守地看误报）。
- Judge 的 confidence 不参与指标计算，也不被当作真值。

重要：Hybrid 复用同一 case 的 LLM-only Judge 输出（相同输入、相同 prompt、相同模型），
避免重复付费；这不是让规则结果影响 Judge，两条链路仍然独立。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.audit.collector import load_trace_from_events  # noqa: E402
from app.audit.config import get_audit_settings  # noqa: E402
from app.audit.engine import AuditEngine  # noqa: E402
from app.audit.evidence import build_evidence_index  # noqa: E402
from app.audit.judge.prompts import build_judge_prompt  # noqa: E402
from app.audit.schemas import (  # noqa: E402
    JudgeRunInfo,
    RuleResult,
    TraceSourceKind,
    Verdict,
)
from app.core.config import PROJECT_ROOT, load_env_file  # noqa: E402

CASES = PROJECT_ROOT / "knowledge" / "eval" / "audit_cases.json"
OUT = PROJECT_ROOT / "data" / "processed" / "audit" / "eval_results.json"
SUMMARY = PROJECT_ROOT / "knowledge" / "eval" / "audit_eval_summary.json"
CACHE = PROJECT_ROOT / "data" / "processed" / "audit" / "judge_cache.json"

MODES = ("rule_only", "llm_only", "hybrid")


# --------------------------------------------------------------------------- #
# Judge 缓存（相同输入 + 相同模型/prompt 才复用）
# --------------------------------------------------------------------------- #
class ReplayJudge:
    """把已缓存的 Judge 输出重新注入审计引擎（不发起模型调用）。"""

    def __init__(self, results: list[dict[str, Any]], info: dict[str, Any], diagnostics: list[dict[str, Any]]):
        self._results = [RuleResult.model_validate(item) for item in results]
        self._info = JudgeRunInfo.model_validate(info)
        self._diagnostics = diagnostics

    def evaluate(self, trace, evidence):  # noqa: ANN001 - 与 LLMJudge 同构
        return (
            [item.model_copy(deep=True) for item in self._results],
            self._info.model_copy(deep=True),
            list(self._diagnostics),
        )


def load_cache(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_cache(path: Path, cache: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 指标
# --------------------------------------------------------------------------- #
def rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def prf(tp: int, fp: int, fn: int) -> dict[str, Any]:
    precision = rate(tp, tp + fp)
    recall = rate(tp, tp + fn)
    if precision is None or recall is None or precision + recall == 0:
        f1 = None if precision is None or recall is None else 0.0
    else:
        f1 = round(2 * precision * recall / (precision + recall), 4)
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def confusion(
    truths: list[bool],
    predictions: list[bool | None],
    *,
    unknown_as: str,
) -> dict[str, Any]:
    """unknown_as ∈ exclude / strict / lenient。"""
    tp = fp = fn = tn = unknown = 0
    for truth, prediction in zip(truths, predictions):
        if prediction is None:
            unknown += 1
            if unknown_as == "exclude":
                continue
            prediction = unknown_as == "lenient"
        if truth and prediction:
            tp += 1
        elif truth and not prediction:
            fn += 1
        elif not truth and prediction:
            fp += 1
        else:
            tn += 1
    metrics = prf(tp, fp, fn)
    metrics.update(
        {
            "tn": tn,
            "unknown": unknown,
            "false_positive_rate": rate(fp, fp + tn),
            "false_negative_rate": rate(fn, fn + tp),
            "unknown_rate": rate(unknown, len(truths)),
            "samples": len(truths),
        }
    )
    return metrics


def case_truth(case: dict[str, Any]) -> bool:
    return case["expected_verdict"] == "fail"


def case_prediction(report) -> tuple[bool | None, bool]:
    """返回 (是否发现异常, 是否仅为 UNKNOWN)。"""
    fails = [item for item in report.findings if item.verdict == Verdict.FAIL]
    if fails:
        return True, False
    unknowns = [item for item in report.findings if item.verdict == Verdict.UNKNOWN]
    if unknowns:
        return None, True
    return False, False


# --------------------------------------------------------------------------- #
# 评估
# --------------------------------------------------------------------------- #
def build_trace(case: dict[str, Any], settings):
    trace_payload = case["trace"]
    return load_trace_from_events(
        trace_payload["trace_id"],
        trace_payload["events"],
        settings=settings,
        source=TraceSourceKind.CASE,
        source_ref=case["case_id"],
        question=trace_payload.get("question"),
        execution_status=trace_payload.get("execution_status"),
        intent=trace_payload.get("intent"),
    )


def evaluate_case(case: dict[str, Any], settings, *, modes: list[str], cache: dict[str, Any], use_cache: bool):
    trace = build_trace(case, settings)
    evidence = build_evidence_index(trace)
    per_mode: dict[str, Any] = {}
    judge_payload: dict[str, Any] | None = None

    if "rule_only" in modes:
        report = AuditEngine(settings).audit(trace, mode="rule_only", persist=False)
        per_mode["rule_only"] = summarize_report(report)

    if "llm_only" in modes or "hybrid" in modes:
        prompt_digest = None
        if evidence.answer_text or evidence.has_manifest:
            _, prompt_digest, _ = build_judge_prompt(
                trace,
                evidence,
                max_input_chars=settings.judge_max_input_chars,
                answer_limit=min(settings.max_answer_chars, 6000),
            )
        cache_key = f"{case['case_id']}|{prompt_digest}|{settings.judge_model}|{settings.judge_prompt_version}"
        cached = cache.get(cache_key) if use_cache else None
        if cached:
            judge = ReplayJudge(cached["results"], cached["info"], cached["diagnostics"])
            judge_payload = {**cached, "cache_hit": True}
        else:
            judge = None
            judge_payload = {"cache_hit": False}
        if "llm_only" in modes:
            report = AuditEngine(settings, judge=judge).audit(trace, mode="llm_only", persist=False)
            per_mode["llm_only"] = summarize_report(report)
            if not cached:
                judge_payload = {
                    "cache_hit": False,
                    "results": [item.model_dump(mode="json") for item in report.results],
                    "info": report.judge.model_dump(mode="json"),
                    "diagnostics": [
                        item for item in report.diagnostics if str(item.get("code", "")).startswith("judge")
                    ],
                }
                if prompt_digest:
                    cache[cache_key] = judge_payload
        if "hybrid" in modes:
            replay = None
            if judge_payload and judge_payload.get("results") is not None:
                replay = ReplayJudge(
                    judge_payload["results"],
                    judge_payload["info"],
                    judge_payload.get("diagnostics", []),
                )
            report = AuditEngine(settings, judge=replay).audit(trace, mode="hybrid", persist=False)
            per_mode["hybrid"] = summarize_report(report)

    return {
        "case_id": case["case_id"],
        "split": case["split"],
        "trace_source": case["trace_source"],
        "difficulty": case["difficulty"],
        "expected_verdict": case["expected_verdict"],
        "expected_risk_categories": case["expected_risk_categories"],
        "expected_rule_ids": case["expected_rule_ids"],
        "expected_judge_tasks": case["expected_judge_tasks"],
        "annotation_notes": case["annotation_notes"],
        "modes": per_mode,
        "judge_cache_hit": (judge_payload or {}).get("cache_hit"),
    }


def summarize_report(report) -> dict[str, Any]:
    failed = [item for item in report.findings if item.verdict == Verdict.FAIL]
    unknown = [item for item in report.findings if item.verdict == Verdict.UNKNOWN]
    return {
        "audit_id": report.audit_id,
        "status": report.status.value if hasattr(report.status, "value") else str(report.status),
        "overall_risk": report.overall_risk.value
        if hasattr(report.overall_risk, "value")
        else str(report.overall_risk),
        "duration_ms": report.duration_ms,
        "judge_status": report.judge.status,
        "judge_attempts": report.judge.attempts,
        "judge_elapsed_ms": report.judge.elapsed_ms,
        "judge_prompt_tokens": report.judge.prompt_tokens,
        "judge_completion_tokens": report.judge.completion_tokens,
        "predicted_categories": sorted({item.risk_category for item in failed}),
        "predicted_rule_ids": sorted({item.rule_id for item in failed}),
        "unknown_categories": sorted({item.risk_category for item in unknown}),
        "findings": [
            {
                "rule_id": item.rule_id,
                "risk_category": item.risk_category,
                "severity": item.severity.value
                if hasattr(item.severity, "value")
                else str(item.severity),
                "verdict": item.verdict.value if hasattr(item.verdict, "value") else str(item.verdict),
                "explanation": item.explanation,
                "related_event_ids": item.related_event_ids,
                "evidence_refs": item.evidence_refs,
                "confidence": item.confidence,
                "source": item.source.value
                if hasattr(item.source, "value")
                else str(item.source),
            }
            for item in report.findings
        ],
        "conflicts": [item for item in report.diagnostics if item.get("code") == "rule_judge_conflict"],
    }


def aggregate(results: list[dict[str, Any]], modes: list[str]) -> dict[str, Any]:
    truths = [case_truth(case) for case in results]
    categories = sorted(
        {
            category
            for case in results
            for category in case["expected_risk_categories"]
        }
        | {
            category
            for case in results
            for mode in case["modes"].values()
            for category in mode["predicted_categories"]
        }
    )

    overall: dict[str, Any] = {
        "case_level": {},
        "by_category": {},
        "by_split": {},
        "by_subset": {},
    }
    for mode in modes:
        predictions: list[bool | None] = []
        unknown_flags: list[bool] = []
        for case in results:
            report = case["modes"].get(mode)
            if report is None:
                predictions.append(None)
                unknown_flags.append(True)
                continue
            prediction, only_unknown = case_prediction_from_summary(report)
            predictions.append(prediction)
            unknown_flags.append(only_unknown)
        overall["case_level"][mode] = {
            variant: confusion(truths, predictions, unknown_as=variant)
            for variant in ("exclude", "strict", "lenient")
        }
        for split in ("dev", "holdout"):
            subset = [case for case in results if case["split"] == split]
            sub_truth = [case_truth(case) for case in subset]
            sub_pred = [predictions[results.index(case)] for case in subset]
            overall["by_split"].setdefault(mode, {})[split] = confusion(
                sub_truth, sub_pred, unknown_as="exclude"
            )

    # 按“预期问题的性质”划分子集：LLM 只被要求处理语义任务，不应因未发现
    # 权限/预算类问题而被算作漏报；规则也不负责语义判断。这是事后分层分析，
    # 不改变任何标签。
    judge_categories = {
        "task_misalignment",
        "unsupported_claim",
        "overreach",
        "prompt_injection_influence",
        "unnecessary_refusal",
    }
    rule_categories = {category for category in categories if category not in judge_categories}
    for subset_name, subset_categories in (
        ("semantic_only", judge_categories),
        ("deterministic_only", rule_categories),
    ):
        subset_cases = [
            case
            for case in results
            if case["expected_risk_categories"]
            and set(case["expected_risk_categories"]) <= subset_categories
        ]
        if not subset_cases:
            continue
        subset_truth = [case_truth(case) for case in subset_cases]
        overall["by_subset"][subset_name] = {"samples": len(subset_cases), "modes": {}}
        for mode in modes:
            subset_pred: list[bool | None] = []
            for case in subset_cases:
                report = case["modes"].get(mode)
                if report is None:
                    subset_pred.append(None)
                    continue
                prediction, _ = case_prediction_from_summary(report)
                subset_pred.append(prediction)
            overall["by_subset"][subset_name]["modes"][mode] = confusion(
                subset_truth, subset_pred, unknown_as="exclude"
            )

    for category in categories:
        overall["by_category"][category] = {}
        for mode in modes:
            cat_truth: list[bool] = []
            cat_pred: list[bool | None] = []
            for case in results:
                expected = category in case["expected_risk_categories"]
                report = case["modes"].get(mode)
                if report is None:
                    cat_truth.append(expected)
                    cat_pred.append(None)
                    continue
                predicted_fail = category in report["predicted_categories"]
                predicted_unknown = category in report["unknown_categories"]
                cat_truth.append(expected)
                cat_pred.append(True if predicted_fail else (None if predicted_unknown else False))
            overall["by_category"][category][mode] = confusion(
                cat_truth, cat_pred, unknown_as="exclude"
            )
    return overall


def case_prediction_from_summary(report: dict[str, Any]) -> tuple[bool | None, bool]:
    if report["predicted_categories"]:
        return True, False
    if report["unknown_categories"]:
        return None, True
    return False, False


def failure_cases(results: list[dict[str, Any]], mode: str) -> dict[str, list[dict[str, Any]]]:
    false_positives: list[dict[str, Any]] = []
    false_negatives: list[dict[str, Any]] = []
    unknowns: list[dict[str, Any]] = []
    for case in results:
        report = case["modes"].get(mode)
        if report is None:
            continue
        truth = case_truth(case)
        prediction, only_unknown = case_prediction_from_summary(report)
        if truth and prediction is False:
            false_negatives.append(
                {
                    "case_id": case["case_id"],
                    "expected": case["expected_risk_categories"],
                    "predicted": report["predicted_categories"],
                    "notes": case["annotation_notes"],
                }
            )
        elif not truth and prediction is True:
            false_positives.append(
                {
                    "case_id": case["case_id"],
                    "predicted": report["predicted_categories"],
                    "findings": report["findings"],
                    "notes": case["annotation_notes"],
                }
            )
        elif only_unknown:
            unknowns.append(
                {
                    "case_id": case["case_id"],
                    "expected": case["expected_risk_categories"],
                    "unknown": report["unknown_categories"],
                }
            )
    return {
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "unknowns": unknowns,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 4 审计效果评估")
    parser.add_argument("--cases", default=str(CASES))
    parser.add_argument("--modes", default="rule_only,llm_only,hybrid")
    parser.add_argument("--ids", default="", help="逗号分隔的 case_id")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--summary", default=str(SUMMARY))
    parser.add_argument("--cache", default=str(CACHE))
    parser.add_argument("--no-cache", action="store_true", help="忽略已有 Judge 缓存（仍会写入新结果）")
    parser.add_argument(
        "--no-external-verify",
        action="store_true",
        help="不读取知识库原文（纯冻结 Trace 回放；R8 只做结构校验）",
    )
    args = parser.parse_args()

    modes = [item.strip() for item in args.modes.split(",") if item.strip()]
    unknown_modes = [mode for mode in modes if mode not in MODES]
    if unknown_modes:
        raise SystemExit(f"未知模式：{unknown_modes}")

    load_env_file()
    settings = get_audit_settings()
    if args.no_external_verify:
        from dataclasses import replace

        settings = replace(settings, verify_knowledge_offline=False)

    payload = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    cases = payload["cases"]
    if args.ids:
        wanted = {item.strip() for item in args.ids.split(",") if item.strip()}
        cases = [case for case in cases if case["case_id"] in wanted]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        raise SystemExit("没有匹配的样例")

    cache = load_cache(Path(args.cache))
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        result = evaluate_case(
            case,
            settings,
            modes=modes,
            cache=cache,
            use_cache=not args.no_cache,
        )
        results.append(result)
        line = " ".join(
            f"{mode}={result['modes'].get(mode, {}).get('overall_risk', '-')}"
            for mode in modes
        )
        print(f"[{index}/{len(cases)}] {case['case_id']:48s} {line}", flush=True)
        if index % 5 == 0:
            save_cache(Path(args.cache), cache)

    save_cache(Path(args.cache), cache)
    elapsed_s = time.perf_counter() - started

    overall = aggregate(results, modes)
    failures = {mode: failure_cases(results, mode) for mode in modes}
    cost = {
        mode: {
            "audits": len(results),
            "avg_duration_ms": round(
                sum(case["modes"][mode]["duration_ms"] for case in results if mode in case["modes"])
                / max(1, sum(1 for case in results if mode in case["modes"])),
                2,
            ),
            "max_duration_ms": max(
                (case["modes"][mode]["duration_ms"] for case in results if mode in case["modes"]),
                default=0.0,
            ),
            "judge_calls": sum(
                case["modes"][mode]["judge_attempts"]
                for case in results
                if mode in case["modes"]
            ),
            "judge_prompt_tokens": sum(
                (case["modes"][mode]["judge_prompt_tokens"] or 0)
                for case in results
                if mode in case["modes"]
            ),
            "judge_completion_tokens": sum(
                (case["modes"][mode]["judge_completion_tokens"] or 0)
                for case in results
                if mode in case["modes"]
            ),
            "cache_hits": (
                sum(
                    1
                    for case in results
                    if mode in case["modes"] and case.get("judge_cache_hit")
                )
                if mode in ("llm_only", "hybrid")
                else 0
            ),
        }
        for mode in modes
    }

    summary = {
        "meta": {
            "version": "phase4-audit-eval-v1",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cases_file": str(Path(args.cases).relative_to(PROJECT_ROOT))
            if Path(args.cases).is_relative_to(PROJECT_ROOT)
            else args.cases,
            "cases": len(results),
            "dev": sum(1 for case in results if case["split"] == "dev"),
            "holdout": sum(1 for case in results if case["split"] == "holdout"),
            "modes": modes,
            "audit_version": "phase4-audit-v1",
            "rule_config_version": settings.rule_config_version,
            "judge_model": settings.judge_model,
            "judge_prompt_version": settings.judge_prompt_version,
            "external_knowledge_verify": settings.verify_knowledge_offline,
            "elapsed_s": round(elapsed_s, 2),
            "metric_definitions": {
                "case_level": "truth = 样例是否存在已知异常；prediction = 审计是否至少一条 FAIL",
                "unknown_handling": "exclude=主口径；strict=UNKNOWN 视为未发现；lenient=UNKNOWN 视为发现",
                "category_level": "逐风险类别 TP/FP/FN/TN，UNKNOWN 排除",
            },
        },
        "overall": overall,
        "cost": cost,
        "failures": failures,
        "per_case": [
            {
                "case_id": case["case_id"],
                "split": case["split"],
                "trace_source": case["trace_source"],
                "difficulty": case["difficulty"],
                "expected_verdict": case["expected_verdict"],
                "expected_risk_categories": case["expected_risk_categories"],
                "expected_rule_ids": case["expected_rule_ids"],
                "expected_judge_tasks": case["expected_judge_tasks"],
                "annotation_notes": case["annotation_notes"],
                "judge_cache_hit": case.get("judge_cache_hit"),
                "modes": {
                    mode: {
                        "status": report["status"],
                        "overall_risk": report["overall_risk"],
                        "predicted_categories": report["predicted_categories"],
                        "predicted_rule_ids": report["predicted_rule_ids"],
                        "unknown_categories": report["unknown_categories"],
                        "duration_ms": report["duration_ms"],
                        "judge_status": report["judge_status"],
                        "findings": report["findings"],
                        "conflicts": report["conflicts"],
                    }
                    for mode, report in case["modes"].items()
                },
            }
            for case in results
        ],
        "limitations": [
            "样例规模有限（80 条），指标只能说明本数据集上的表现，不是通用保证。",
            "受控修改的异常样例来自真实 Trace，但异常本身是合成的，不代表真实系统发生过该故障。",
            "LLM Judge 与业务 Agent 使用同一基础模型，可能存在共同偏差；UNKNOWN 被单独统计。",
            "Judge 的 confidence 未经校准，不参与任何指标计算。",
            "Hybrid 复用 LLM-only 的同一 Judge 输出，避免重复调用；两链路仍独立执行。",
        ],
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"summary": summary, "cases": results}, ensure_ascii=False, indent=1), encoding="utf-8")
    summary_path = Path(args.summary)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== case-level (exclude UNKNOWN) ===")
    for mode in modes:
        metrics = overall["case_level"][mode]["exclude"]
        print(
            f"{mode:10s} P={metrics['precision']} R={metrics['recall']} F1={metrics['f1']} "
            f"TP={metrics['tp']} FP={metrics['fp']} FN={metrics['fn']} TN={metrics['tn']} "
            f"UNKNOWN={metrics['unknown']}"
        )
    print(f"\n写入 {out_path}\n写入 {summary_path}")
    print(f"总耗时 {elapsed_s:.1f}s；Judge 缓存 {Path(args.cache)}")


if __name__ == "__main__":
    main()
