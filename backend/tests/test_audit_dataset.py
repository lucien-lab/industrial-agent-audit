"""审计评估集的完整性与防泄漏检查。

这些测试检查**数据集本身**，不评价审计器表现：
- 数量、来源、split 与标签完整性；
- 同一原始 Trace 的派生样例不得跨 dev/holdout；
- 失败样例必须有明确的预期风险类别；
- 真实样例必须能追溯到真实 trace_id。
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from app.core.config import PROJECT_ROOT

CASES_PATH = PROJECT_ROOT / "knowledge" / "eval" / "audit_cases.json"

KNOWN_CATEGORIES = {
    "unauthorized_tool",
    "invalid_tool_params",
    "budget_violation",
    "redundant_tool_call",
    "time_range_mismatch",
    "equipment_mismatch",
    "data_quality_ignored",
    "numeric_evidence_mismatch",
    "citation_integrity",
    "chain_integrity",
    "status_inconsistency",
    "task_misalignment",
    "unsupported_claim",
    "overreach",
    "prompt_injection_influence",
    "unnecessary_refusal",
}

KNOWN_SOURCES = {"real_agent_trace", "mutated_real_trace", "handcrafted_events"}


@pytest.fixture(scope="module")
def dataset() -> dict:
    if not CASES_PATH.is_file():
        pytest.skip("审计评估集尚未生成：先运行 scripts/build_audit_cases.py")
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def test_dataset_size_and_split(dataset) -> None:
    cases = dataset["cases"]
    assert len(cases) >= 80, "阶段 4 要求审计测试集不少于 80 条"
    split_counts = defaultdict(int)
    for case in cases:
        split_counts[case["split"]] += 1
    assert split_counts["holdout"] >= 20
    assert split_counts["dev"] >= 20
    assert dataset["summary"]["total"] == len(cases)


def test_every_case_has_required_fields(dataset) -> None:
    required = {
        "case_id",
        "trace_source",
        "split",
        "expected_verdict",
        "expected_risk_categories",
        "expected_evidence",
        "difficulty",
        "annotation_notes",
        "derived_from",
        "trace",
    }
    for case in dataset["cases"]:
        assert required <= set(case), case["case_id"]
        assert case["trace_source"] in KNOWN_SOURCES
        assert case["split"] in {"dev", "holdout"}
        assert case["expected_verdict"] in {"pass", "fail", "unknown"}
        assert case["annotation_notes"].strip()
        assert case["trace"]["events"]


def test_failed_cases_have_expected_categories(dataset) -> None:
    for case in dataset["cases"]:
        if case["expected_verdict"] != "fail":
            continue
        assert case["expected_risk_categories"], case["case_id"]
        assert set(case["expected_risk_categories"]) <= KNOWN_CATEGORIES, case["case_id"]


def test_pass_cases_have_no_expected_risk(dataset) -> None:
    for case in dataset["cases"]:
        if case["expected_verdict"] == "pass":
            assert case["expected_risk_categories"] == [], case["case_id"]


def test_real_cases_traceable(dataset) -> None:
    real = [case for case in dataset["cases"] if case["trace_source"] == "real_agent_trace"]
    assert len(real) >= 10
    for case in real:
        assert case["derived_from"]["trace_id"]
        assert case["derived_from"]["mutation"] is None
        assert case["trace"]["trace_id"] == case["derived_from"]["trace_id"]


def test_mutated_cases_have_provenance(dataset) -> None:
    mutated = [case for case in dataset["cases"] if case["trace_source"] == "mutated_real_trace"]
    assert len(mutated) >= 40
    for case in mutated:
        provenance = case["derived_from"]
        assert provenance["trace_id"]
        assert provenance["mutation"]
        assert provenance["original_trace_preserved"] is True
        assert case["case_id"].endswith(provenance["mutation"])
        assert case["trace"]["trace_id"] != provenance["trace_id"]


def test_handcrafted_cases_are_marked(dataset) -> None:
    handcrafted = [
        case for case in dataset["cases"] if case["trace_source"] == "handcrafted_events"
    ]
    assert len(handcrafted) >= 8
    for case in handcrafted:
        assert case["derived_from"]["trace_id"] is None
        assert case["trace"]["trace_id"].startswith("handcrafted-")


def test_no_split_leakage_per_base_trace(dataset) -> None:
    splits: dict[str, set[str]] = defaultdict(set)
    for case in dataset["cases"]:
        base = case["derived_from"].get("trace_id") or case["case_id"].split("-")[0]
        splits[str(base)].add(case["split"])
    leaks = {base: values for base, values in splits.items() if len(values) > 1}
    assert not leaks, f"同一 base trace 跨 split：{leaks}"


def test_annotation_policy_present(dataset) -> None:
    policy = dataset.get("annotation_policy", "")
    assert "人工" in policy
    assert "自动生成" in policy
