"""LLM Judge 单元测试（使用脚本化模型，不调用真实 Ollama）。"""

from __future__ import annotations

import json

import pytest

from app.audit.config import AuditSettings
from app.audit.evidence import build_evidence_index
from app.audit.judge.judge import LLMJudge, normalize_judge_output, parse_judge_json
from app.audit.schemas import Verdict
from app.rag.llm import LLMResponse
from tests.audit_helpers import base_manifest, make_settings, make_trace

CATEGORIES = {
    "task_alignment": "task_misalignment",
    "evidence_support": "unsupported_claim",
    "overreach": "overreach",
    "injection_influence": "prompt_injection_influence",
    "false_refusal": "unnecessary_refusal",
}


class ScriptedModel:
    provider = "stub"
    model = "scripted-judge"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def chat(self, messages, **kwargs):  # noqa: ANN001
        text = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return LLMResponse(
            text=text,
            model=self.model,
            provider=self.provider,
            elapsed_ms=1.0,
            prompt_tokens=10,
            completion_tokens=5,
        )


class ExplodingModel:
    provider = "stub"
    model = "exploding"

    def chat(self, messages, **kwargs):  # noqa: ANN001
        raise RuntimeError("model unavailable")


def _task(
    name: str,
    verdict: str = "pass",
    *,
    claims: list[dict] | None = None,
    explanation: str = "ok",
) -> dict:
    return {
        "task": name,
        "verdict": verdict,
        "risk_category": CATEGORIES[name],
        "confidence": 0.8,
        "claims": claims or [],
        "evidence_refs": ["D2"],
        "explanation": explanation,
        "requires_human_review": verdict != "pass",
    }


def _output(tasks: list[dict] | None = None, **overrides) -> str:
    payload = {
        "tasks": tasks
        if tasks is not None
        else [_task(name) for name in CATEGORIES],
        "overall_comment": "test",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


@pytest.fixture()
def judge_settings(tmp_path) -> AuditSettings:
    return make_settings(tmp_path, judge_enabled=True)


def _evaluate(settings, manifest, model):
    trace = make_trace(manifest, settings, trace_id="judge-test")
    evidence = build_evidence_index(trace)
    return LLMJudge(settings, chat_model=model).evaluate(trace, evidence)


def test_valid_structured_output(judge_settings) -> None:
    claims = [
        {
            "claim_id": "c1",
            "claim": "最大值为 6.36",
            "verdict": "supported",
            "evidence_refs": ["D2"],
            "explanation": "证据 D2",
        }
    ]
    tasks = [_task(name, claims=claims if name == "evidence_support" else []) for name in CATEGORIES]
    results, info, diagnostics = _evaluate(judge_settings, base_manifest(), ScriptedModel([_output(tasks)]))
    assert info.status == "ok"
    assert info.attempts == 1
    assert len(results) == 5
    assert all(item.verdict == Verdict.PASS for item in results)
    evidence_result = next(item for item in results if item.rule_id == "J_EVIDENCE_SUPPORT")
    assert evidence_result.details["claims"][0]["verdict"] == "supported"


def test_invalid_json_is_retried_then_unknown(judge_settings) -> None:
    model = ScriptedModel(["这不是 JSON", "仍然不是 JSON"])
    results, info, diagnostics = _evaluate(judge_settings, base_manifest(), model)
    assert info.status == "error"
    assert info.attempts == 2
    assert any(item.rule_id == "J_JUDGE_UNAVAILABLE" and item.verdict == Verdict.UNKNOWN for item in results)
    assert any(item["code"] == "judge_parse_error" for item in diagnostics)


def test_invalid_then_valid_output(judge_settings) -> None:
    model = ScriptedModel(["bad", _output()])
    results, info, _ = _evaluate(judge_settings, base_manifest(), model)
    assert info.status == "ok"
    assert info.attempts == 2
    assert len(results) == 5


def test_model_exception_degrades_to_unknown(judge_settings) -> None:
    results, info, diagnostics = _evaluate(judge_settings, base_manifest(), ExplodingModel())
    assert info.status == "error"
    assert results[0].rule_id == "J_JUDGE_UNAVAILABLE"
    assert results[0].verdict == Verdict.UNKNOWN
    assert results[0].requires_human_review is True


def test_missing_tasks_become_unknown(judge_settings) -> None:
    payload = {"tasks": [_task("task_alignment")], "overall_comment": "partial"}
    results, info, diagnostics = _evaluate(
        judge_settings, base_manifest(), ScriptedModel([json.dumps(payload)])
    )
    assert info.status == "ok"
    missing = [item for item in results if item.rule_id != "J_TASK_ALIGNMENT"]
    assert len(missing) == 4
    assert all(item.verdict == Verdict.UNKNOWN for item in missing)
    assert any("缺少任务" in str(item.get("message", "")) for item in diagnostics)


def test_pipe_placeholder_task_names_are_inferred(judge_settings) -> None:
    tasks = []
    for name in CATEGORIES:
        item = _task(name)
        item["task"] = "task_alignment|evidence_support|overreach|injection_influence|false_refusal"
        tasks.append(item)
    results, info, diagnostics = _evaluate(
        judge_settings, base_manifest(), ScriptedModel([_output(tasks)])
    )
    assert info.status == "ok"
    assert {item.rule_id for item in results} == {
        "J_TASK_ALIGNMENT",
        "J_EVIDENCE_SUPPORT",
        "J_OVERREACH",
        "J_INJECTION_INFLUENCE",
        "J_FALSE_REFUSAL",
    }
    assert any("按 risk_category 推断" in str(item.get("message", "")) for item in diagnostics)


def test_unsupported_claim_forces_fail(judge_settings) -> None:
    tasks = []
    for name in CATEGORIES:
        if name == "evidence_support":
            tasks.append(
                _task(
                    name,
                    "pass",
                    claims=[
                        {
                            "claim_id": "c1",
                            "claim": "最大值为 9.99",
                            "verdict": "unsupported",
                            "evidence_refs": ["D2"],
                            "explanation": "证据不含该数值",
                        }
                    ],
                )
            )
        else:
            tasks.append(_task(name))
    results, _, _ = _evaluate(judge_settings, base_manifest(), ScriptedModel([_output(tasks)]))
    evidence_result = next(item for item in results if item.rule_id == "J_EVIDENCE_SUPPORT")
    assert evidence_result.verdict == Verdict.FAIL


def test_focused_refusal_check_overrides_lenient_pass(judge_settings) -> None:
    manifest = base_manifest()
    manifest["answer_text"] = "## 结论\n证据不足，无法回答该问题。"
    manifest["answer_sections"]["conclusion"] = manifest["answer_text"]
    focused = json.dumps(
        {
            "verdict": "fail",
            "evidence_sufficient": True,
            "explanation": "统计证据已包含最大值，拒答不合理",
            "confidence": 0.9,
        }
    )
    model = ScriptedModel([_output(), focused])
    results, info, diagnostics = _evaluate(judge_settings, manifest, model)
    refusal = next(item for item in results if item.rule_id == "J_FALSE_REFUSAL")
    assert refusal.verdict == Verdict.FAIL
    assert info.attempts == 2
    assert any(item["code"] == "judge_focused_refusal" for item in diagnostics)


def test_judge_disabled(tmp_path) -> None:
    settings = make_settings(tmp_path, judge_enabled=False)
    trace = make_trace(base_manifest(), settings, trace_id="judge-disabled")
    evidence = build_evidence_index(trace)
    results, info, diagnostics = LLMJudge(settings).evaluate(trace, evidence)
    assert results == []
    assert info.status == "disabled"
    assert diagnostics[0]["code"] == "judge_disabled"


def test_parse_judge_json_tolerates_code_fence() -> None:
    payload, error = parse_judge_json('```json\n{"tasks": [], "overall_comment": "x"}\n```')
    assert error is None
    assert payload["overall_comment"] == "x"


def test_normalize_rejects_non_object() -> None:
    payload, error = parse_judge_json("[1,2,3]")
    assert payload is None
    assert error
