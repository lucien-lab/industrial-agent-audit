"""确定性规则 R1–R10 的违规 / 正常 / 无法判断场景。"""

from __future__ import annotations

import hashlib

import pytest

from app.audit.schemas import RiskLevel, Verdict
from tests.audit_helpers import (
    audit_manifest,
    base_manifest,
    make_settings,
    results_by_rule,
)

RISKY_QUALITY = {
    "actual_samples": 5000,
    "expected_samples": 8640,
    "sample_coverage": 0.579,
    "time_coverage": 0.60,
    "gap_count": 4,
    "frozen_candidate_count": 120,
    "frozen_candidate_ratio": 0.02,
    "window_inside_data_range": True,
    "warnings": [
        {"code": "low_sample_coverage", "severity": "warning", "message": "样本覆盖率低于阈值"}
    ],
}

CLEAN_QUALITY = {
    "actual_samples": 8600,
    "expected_samples": 8640,
    "sample_coverage": 0.995,
    "time_coverage": 0.995,
    "gap_count": 0,
    "frozen_candidate_count": 0,
    "window_inside_data_range": True,
    "warnings": [],
}

KNOWLEDGE_TEXT = "TP2 (bar) – the measure of the pressure on the compressor."


def _knowledge_manifest(**overrides):
    quote_hash = hashlib.sha256(KNOWLEDGE_TEXT.encode("utf-8")).hexdigest()
    manifest = base_manifest(**overrides)
    manifest["knowledge"] = [
        {
            "chunk_id": 12,
            "doc_key": "metropt3-data-description",
            "document_title": "MetroPT-3 Data Description",
            "page_start": 2,
            "page_end": 2,
            "equipment_scope": "metropt3_direct",
            "trust_level": "official_primary",
            "excerpt": KNOWLEDGE_TEXT,
            "quote_sha256": quote_hash,
        }
    ]
    manifest["citations"] = [
        {
            "label": "K1",
            "kind": "knowledge",
            "chunk_id": 12,
            "doc_key": "metropt3-data-description",
            "page_start": 2,
            "page_end": 2,
            "equipment_scope": "metropt3_direct",
            "quote": KNOWLEDGE_TEXT,
            "quote_sha256": quote_hash,
        }
    ]
    manifest["answer_sections"]["knowledge"] = [
        {"text": "TP2 表示压缩机压力 [K1]", "labels": ["K1"]}
    ]
    manifest["answer_text"] = (
        "## 结论\n[D2] 电机电流最大值 6.36 A；[K1] TP2 表示压缩机压力。"
    )
    return manifest


def _has_fail(report, rule_id: str) -> bool:
    return any(item.verdict == Verdict.FAIL for item in results_by_rule(report, rule_id))


def _fail_categories(report) -> set[str]:
    return {
        item.risk_category
        for item in report.findings
        if item.verdict == Verdict.FAIL
    }


def test_clean_handcrafted_trace_has_no_findings(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(base_manifest(), settings)
    assert report.findings == []
    assert report.overall_risk == RiskLevel.NONE
    assert report.status == "completed"


def test_r1_unregistered_tool(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(base_manifest(), settings, mutation="unregistered_tool")
    assert _has_fail(report, "R1_TOOL_WHITELIST")
    assert "unauthorized_tool" in _fail_categories(report)
    finding = next(item for item in report.findings if item.rule_id == "R1_TOOL_WHITELIST")
    assert finding.related_event_ids


def test_r1b_invalid_tool_parameters(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(base_manifest(), settings, mutation="invalid_params")
    assert _has_fail(report, "R1B_TOOL_PARAMETERS")
    assert "invalid_tool_params" in _fail_categories(report)


def test_r2_budget_violation(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(base_manifest(), settings, mutation="budget_overflow")
    assert _has_fail(report, "R2_TOOL_BUDGET")
    assert "budget_violation" in _fail_categories(report)


def test_r2_timeout_budget(tmp_path) -> None:
    settings = make_settings(tmp_path, rules=make_settings(tmp_path).rules.__class__(total_timeout_s=0.001))
    report = audit_manifest(base_manifest(), settings)
    assert _has_fail(report, "R2_TOOL_BUDGET")


def test_r3_duplicate_call(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(base_manifest(), settings, mutation="duplicate_call")
    assert _has_fail(report, "R3_REPEAT_CALLS")
    result = next(
        item for item in results_by_rule(report, "R3_REPEAT_CALLS") if item.verdict == Verdict.FAIL
    )
    assert result.details["count"] == 2


def test_r4_timezone_window(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(base_manifest(), settings, mutation="timezone_window")
    assert _has_fail(report, "R4_TIME_RANGE")


def test_r4_widened_window(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(base_manifest(), settings, mutation="widen_window")
    assert _has_fail(report, "R4_TIME_RANGE")
    assert "time_range_mismatch" in _fail_categories(report)


def test_r5_wrong_equipment(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(base_manifest(), settings, mutation="wrong_equipment")
    assert _has_fail(report, "R5_EQUIPMENT_ID")
    assert "equipment_mismatch" in _fail_categories(report)


def test_r6_quality_ignored(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(
        base_manifest(quality=RISKY_QUALITY), settings, mutation="quality_ignored"
    )
    assert _has_fail(report, "R6_DATA_QUALITY")
    assert "data_quality_ignored" in _fail_categories(report)


def test_r6_quality_acknowledged_passes(tmp_path) -> None:
    settings = make_settings(tmp_path)
    manifest = base_manifest(quality=RISKY_QUALITY)
    manifest["answer_text"] = (
        "## 结论\n[D2] 电机电流最大值 6.36 A；注意窗口样本覆盖率仅 57.9%，"
        "存在采样缺口与冻结候选，结论受数据质量限制。"
    )
    manifest["answer_sections"]["conclusion"] = manifest["answer_text"]
    manifest["answer_sections"]["uncertainties"] = ["样本覆盖率不足，结论需谨慎"]
    report = audit_manifest(manifest, settings)
    assert not _has_fail(report, "R6_DATA_QUALITY")


def test_r6_not_applicable_without_quality_evidence(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(base_manifest(), settings)
    assert all(
        item.verdict == Verdict.NOT_APPLICABLE
        for item in results_by_rule(report, "R6_DATA_QUALITY")
    )


def test_r7_numeric_tamper(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(base_manifest(), settings, mutation="numeric_tamper")
    assert _has_fail(report, "R7_NUMERIC_EVIDENCE")
    assert "numeric_evidence_mismatch" in _fail_categories(report)


def test_r7_statistic_swap_detects_mean_written_as_max(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(base_manifest(), settings, mutation="statistic_swap")
    result = next(
        item for item in results_by_rule(report, "R7_NUMERIC_EVIDENCE") if item.verdict == Verdict.FAIL
    )
    assert result.details["statistic"] == "max"
    assert result.details["matched_statistic"] == "mean"


def test_r7_unit_mismatch(tmp_path) -> None:
    settings = make_settings(tmp_path)
    manifest = base_manifest()
    manifest["answer_text"] = "## 结论\n[D2] 电机电流最大值为 6.36 bar。"
    manifest["answer_sections"]["conclusion"] = manifest["answer_text"]
    report = audit_manifest(manifest, settings)
    assert _has_fail(report, "R7_NUMERIC_EVIDENCE")
    result = next(
        item for item in results_by_rule(report, "R7_NUMERIC_EVIDENCE") if item.verdict == Verdict.FAIL
    )
    assert result.details["unit"] == "bar"


def test_r7_unknown_when_fact_text_missing(tmp_path) -> None:
    settings = make_settings(tmp_path)
    manifest = base_manifest()
    for key in ("facts", "statistics"):
        manifest.pop(key)
    manifest["citations"] = []
    report = audit_manifest(manifest, settings)
    verdicts = {str(item.verdict) for item in results_by_rule(report, "R7_NUMERIC_EVIDENCE")}
    assert "Verdict.UNKNOWN" in verdicts


def test_r8_citation_chunk_missing(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(_knowledge_manifest(), settings, mutation="citation_chunk_missing")
    assert _has_fail(report, "R8_KNOWLEDGE_CITATION")


def test_r8_scope_invalid(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(_knowledge_manifest(), settings, mutation="citation_scope_invalid")
    assert _has_fail(report, "R8_KNOWLEDGE_CITATION")


def test_r8_quote_not_in_excerpt(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(_knowledge_manifest(), settings, mutation="citation_quote_replaced")
    assert _has_fail(report, "R8_KNOWLEDGE_CITATION")
    result = next(
        item for item in results_by_rule(report, "R8_KNOWLEDGE_CITATION") if item.verdict == Verdict.FAIL
    )
    assert any("未出现在" in problem for problem in result.details.get("problems", []))


def test_r8_clean_citation_passes(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(_knowledge_manifest(), settings)
    assert not _has_fail(report, "R8_KNOWLEDGE_CITATION")


def test_r8_injection_followed(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(_knowledge_manifest(), settings, mutation="injection_followed")
    assert _has_fail(report, "R8_KNOWLEDGE_CITATION")
    assert "citation_integrity" in _fail_categories(report)


@pytest.mark.parametrize(
    "mutation,label",
    [
        ("missing_run_start", "run_start"),
        ("missing_tool_result", "tool_result"),
        ("missing_answer_event", "answer"),
    ],
)
def test_r9_chain_integrity(tmp_path, mutation: str, label: str) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(base_manifest(), settings, mutation=mutation)
    assert _has_fail(report, "R9_CHAIN_INTEGRITY")
    assert "chain_integrity" in _fail_categories(report)


def test_r10_tool_failure_described_as_success(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(base_manifest(), settings, mutation="tool_failure_as_success")
    assert _has_fail(report, "R10_STATUS_CONSISTENCY")
    assert "status_inconsistency" in _fail_categories(report)


def test_r10_timeout_not_acknowledged(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(base_manifest(), settings, mutation="timeout_unacknowledged")
    assert _has_fail(report, "R10_STATUS_CONSISTENCY")


def test_legacy_trace_without_manifest_is_unknown_not_pass(tmp_path) -> None:
    settings = make_settings(tmp_path)
    report = audit_manifest(None, settings)
    assert report.overall_risk == RiskLevel.UNKNOWN
    assert report.status == "partial"
    # 依赖证据的规则必须 UNKNOWN
    for rule_id in ("R6_DATA_QUALITY", "R7_NUMERIC_EVIDENCE"):
        assert any(
            item.verdict == Verdict.UNKNOWN for item in results_by_rule(report, rule_id)
        )
    assert not any(item.verdict == Verdict.FAIL for item in report.findings)
