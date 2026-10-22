"""审计引擎：模式、冲突、失败降级、幂等与版本隔离。"""

from __future__ import annotations

import os

import pytest

from app.audit.config import AUDIT_VERSION, AuditSettings
from app.audit.engine import AuditEngine
from app.audit.repository import AuditRepository
from app.audit.schemas import (
    AuditStatus,
    FindingSource,
    JudgeRunInfo,
    JudgeTaskResult,
    RiskLevel,
    Verdict,
)
from app.audit.scoring import judge_to_rule_result
from tests.audit_helpers import audit_manifest, base_manifest, make_settings, make_trace


class StubJudge:
    def __init__(self, results, status: str = "ok") -> None:
        self.results = results
        self.status = status
        self.calls = 0

    def evaluate(self, trace, evidence):  # noqa: ANN001
        self.calls += 1
        return (
            [item.model_copy(deep=True) for item in self.results],
            JudgeRunInfo(status=self.status, provider="stub", model="stub", attempts=1),
            [{"code": "stub_judge"}],
        )


class ExplodingJudge:
    def evaluate(self, trace, evidence):  # noqa: ANN001
        raise AssertionError("rule_only 不应调用 Judge")


def _judge_results(*, verdicts: dict[str, str] | None = None):
    verdicts = verdicts or {}
    mapping = {
        "task_alignment": "task_misalignment",
        "evidence_support": "unsupported_claim",
        "overreach": "overreach",
        "injection_influence": "prompt_injection_influence",
        "false_refusal": "unnecessary_refusal",
    }
    results = []
    for task, category in mapping.items():
        verdict = Verdict(verdicts.get(task, "pass"))
        results.append(
            judge_to_rule_result(
                JudgeTaskResult(
                    task=task,
                    verdict=verdict,
                    risk_category=category,
                    confidence=0.8,
                    claims=[],
                    evidence_refs=["D2"],
                    explanation="stub",
                    requires_human_review=verdict != Verdict.PASS,
                ),
                "test-prompt",
            )
        )
    return results


def test_rule_only_does_not_call_judge(tmp_path) -> None:
    settings = make_settings(tmp_path)
    trace = make_trace(base_manifest(), settings, trace_id="engine-rule-only")
    report = AuditEngine(settings, judge=ExplodingJudge()).audit(trace, mode="rule_only")
    assert report.findings == []
    assert all(item.source == FindingSource.RULE for item in report.results)


def test_llm_only_uses_judge_only(tmp_path) -> None:
    settings = make_settings(tmp_path, judge_enabled=True)
    trace = make_trace(base_manifest(), settings, trace_id="engine-llm-only")
    stub = StubJudge(_judge_results(verdicts={"evidence_support": "fail"}))
    report = AuditEngine(settings, judge=stub).audit(trace, mode="llm_only")
    assert stub.calls == 1
    assert {item.source for item in report.results} == {FindingSource.JUDGE}
    assert "unsupported_claim" in {item.risk_category for item in report.findings}
    assert report.judge.status == "ok"


def test_hybrid_keeps_deterministic_fail_and_records_conflict(tmp_path) -> None:
    settings = make_settings(tmp_path, judge_enabled=True)
    manifest = base_manifest()
    # 篡改数值 → 规则必然 FAIL
    manifest["answer_text"] = "## 结论\n[D2] 电机电流最大值为 99.99 A。"
    manifest["answer_sections"]["conclusion"] = manifest["answer_text"]
    trace = make_trace(manifest, settings, trace_id="engine-hybrid")
    stub = StubJudge(_judge_results())  # Judge 全部 PASS，与规则冲突
    report = AuditEngine(settings, judge=stub).audit(trace, mode="hybrid")
    assert any(
        item.rule_id == "R7_NUMERIC_EVIDENCE" and item.verdict == Verdict.FAIL
        for item in report.results
    )
    assert any(item["code"] == "rule_judge_conflict" for item in report.diagnostics)
    assert report.requires_human_review is True


def test_judge_unavailable_is_not_pass(tmp_path) -> None:
    settings = make_settings(tmp_path, judge_enabled=True)
    trace = make_trace(base_manifest(), settings, trace_id="engine-judge-down")
    stub = StubJudge([], status="error")
    report = AuditEngine(settings, judge=stub).audit(trace, mode="llm_only")
    assert report.status == "partial"
    assert report.overall_risk == RiskLevel.UNKNOWN
    assert report.findings
    assert all(item.verdict == Verdict.UNKNOWN for item in report.findings)


def test_audit_does_not_modify_trace(tmp_path) -> None:
    settings = make_settings(tmp_path)
    trace = make_trace(base_manifest(), settings, trace_id="engine-immutable")
    before = [event.model_dump(mode="json") for event in trace.events]
    digest = trace.trace_digest
    AuditEngine(settings).audit(trace, mode="rule_only")
    assert trace.trace_digest == digest
    assert [event.model_dump(mode="json") for event in trace.events] == before


# --------------------------------------------------------------------------- #
# 持久化（使用隔离测试库，表由 conftest 在用例前 TRUNCATE）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def repository(clean_engine, tmp_path) -> AuditRepository:  # noqa: ANN001
    url = os.environ["TEST_DATABASE_URL"]
    settings = AuditSettings(audit_database_url=url, trace_dir=tmp_path)
    return AuditRepository(database_url=url, settings=settings)


def test_repository_trace_roundtrip(tmp_path, repository: AuditRepository) -> None:
    settings = make_settings(tmp_path)
    trace = make_trace(base_manifest(), settings, trace_id="repo-trace")
    repository.save_trace(trace)
    events = repository.load_trace_events("repo-trace")
    assert events is not None
    assert len(events) == trace.event_count
    meta = repository.get_trace_meta("repo-trace")
    assert meta["trace_id"] == "repo-trace"
    assert meta["trace_digest"] == trace.trace_digest


def test_persistence_is_idempotent(tmp_path, repository: AuditRepository) -> None:
    settings = make_settings(tmp_path)
    engine = AuditEngine(settings, repository=repository)
    trace = make_trace(base_manifest(), settings, trace_id="repo-idempotent")
    first = engine.audit(trace, mode="rule_only", persist=True)
    second = engine.audit(trace, mode="rule_only", persist=True)
    assert first.audit_id == second.audit_id
    runs, total = repository.list_runs()
    assert total == 1
    assert runs[0]["audit_id"] == first.audit_id
    loaded = repository.get_report(first.audit_id)
    assert loaded is not None
    assert loaded.trace_id == trace.trace_id


def test_audit_versions_are_isolated(tmp_path, repository: AuditRepository) -> None:
    settings = make_settings(tmp_path)
    engine = AuditEngine(settings, repository=repository)
    trace = make_trace(base_manifest(), settings, trace_id="repo-versions")
    first = engine.audit(trace, mode="rule_only", persist=True)
    second = engine.audit(
        trace, mode="rule_only", persist=True, audit_version="phase4-audit-v2"
    )
    assert first.audit_id != second.audit_id
    runs, total = repository.list_runs(trace_id="repo-versions")
    assert total == 2
    assert {run["audit_version"] for run in runs} == {AUDIT_VERSION, "phase4-audit-v2"}


def test_persistence_failure_marks_partial(tmp_path) -> None:
    settings = make_settings(tmp_path)

    class BrokenRepository(AuditRepository):
        def __init__(self) -> None:  # 不连接数据库
            self.settings = settings
            self.database_url = "postgresql+psycopg://invalid"
            self._factory = None

        def save_trace(self, trace) -> None:  # noqa: ANN001
            raise RuntimeError("db down")

    engine = AuditEngine(settings, repository=BrokenRepository())
    trace = make_trace(base_manifest(), settings, trace_id="repo-broken")
    report = engine.audit(trace, mode="rule_only", persist=True)
    assert report.status == "partial"
    assert report.error and report.error.startswith("persistence_failed")
    assert report.requires_human_review is True
    assert any(item["code"] == "persistence_failed" for item in report.diagnostics)


def test_replay_trace_and_reaudit(tmp_path) -> None:
    from app.audit.replay import replay_audit, replay_trace
    from tests.audit_helpers import write_trace_file

    settings = make_settings(tmp_path)
    from build_audit_cases import _handcrafted_events

    raw = _handcrafted_events(base_manifest())
    for event in raw:
        event["trace_id"] = "replay-trace"
    write_trace_file(raw, settings.trace_dir, "replay-trace")

    replay = replay_trace("replay-trace", settings=settings, include_audit_history=False)
    assert replay["trace"]["trace_id"] == "replay-trace"
    assert replay["trace"]["events"]
    assert replay["tool_calls"]
    assert replay["offline_only"] is True

    payload = replay_audit(
        "replay-trace", mode="rule_only", settings=settings, persist=False
    )
    assert payload["audit"]["trace_id"] == "replay-trace"
    assert payload["audit"]["audit_mode"] == "rule_only"
    assert payload["replay"]["trace"]["event_count"] >= 8
