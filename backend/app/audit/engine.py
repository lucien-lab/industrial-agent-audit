"""审计引擎：规则 + Judge 的编排、风险汇总与持久化。

模式：
- `rule_only`：只跑确定性规则（不调用 LLM）；
- `llm_only`：只跑语义 Judge（不跑确定性规则）；
- `hybrid`：先跑规则，再跑 Judge，最后合并并记录冲突。

不可妥协的规则：
- Judge 不能覆盖确定性 FAIL；
- 任何解析失败/模型不可用都产生 UNKNOWN（要求人工复核），绝不产生 PASS；
- 审计失败不会被标记为通过；
- 持久化失败不影响已经生成的报告，但报告状态会变为 partial。
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Any

from app.audit.collector import load_trace
from app.audit.config import AUDIT_MODES, AUDIT_VERSION, AuditSettings, get_audit_settings
from app.audit.evidence import (
    build_evidence_index,
    build_tool_timeline,
    verify_knowledge_chunks,
)
from app.audit.judge.judge import LLMJudge
from app.audit.repository import AuditRepository, audit_id_for, get_repository
from app.audit.rules.deterministic import (
    RULE_SPECS,
    run_deterministic_rules,
)
from app.audit.rules.base import RuleContext
from app.audit.scoring import (
    build_findings,
    build_summary,
    detect_conflicts,
    overall_risk,
    requires_human_review,
)
from app.audit.schemas import (
    AuditMode,
    AuditReport,
    AuditStatus,
    JudgeRunInfo,
    NormalizedTrace,
    RiskLevel,
    RuleResult,
    Verdict,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(*parts: str | None) -> str:
    blob = "|".join(part or "" for part in parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class AuditEngine:
    """独立审计引擎（不修改业务回答与 Trace）。"""

    def __init__(
        self,
        settings: AuditSettings | None = None,
        *,
        repository: AuditRepository | None = None,
        judge: LLMJudge | None = None,
        use_repository: bool = False,
    ) -> None:
        self.settings = settings or get_audit_settings()
        if repository is not None:
            self.repository = repository
        elif use_repository:
            self.repository = get_repository(self.settings)
        else:
            self.repository = None
        self.judge = judge or LLMJudge(self.settings)

    # ------------------------------------------------------------------ #
    def audit(
        self,
        trace: NormalizedTrace,
        *,
        mode: str = "hybrid",
        audit_version: str = AUDIT_VERSION,
        persist: bool = False,
        verify_knowledge: bool | None = None,
    ) -> AuditReport:
        if mode not in AUDIT_MODES:
            raise ValueError(f"未知审计模式 {mode!r}，可选：{list(AUDIT_MODES)}")
        started_at = _now()
        started_perf = time.perf_counter()
        diagnostics: list[dict[str, Any]] = []
        evidence = build_evidence_index(trace)

        do_verify = (
            self.settings.verify_knowledge_offline
            if verify_knowledge is None
            else verify_knowledge
        )
        if do_verify and evidence.knowledge:
            verify_knowledge_chunks(evidence, settings=self.settings)
            diagnostics.append(
                {
                    "code": "external_knowledge_read",
                    "chunks": len(evidence.knowledge_verification),
                    "note": "本次审计读取了知识库原文进行逐字核对（非纯冻结 Trace 回放）",
                }
            )
        elif evidence.knowledge:
            diagnostics.append(
                {
                    "code": "knowledge_not_externally_verified",
                    "note": "未读取知识库原文；逐字核对仅基于证据清单中的摘录与哈希",
                }
            )

        results: list[RuleResult] = []
        rule_results: list[RuleResult] = []
        judge_results: list[RuleResult] = []
        judge_info = JudgeRunInfo(status="skipped")

        if mode in ("rule_only", "hybrid"):
            rule_results = run_deterministic_rules(
                RuleContext(
                    trace=trace,
                    evidence=evidence,
                    config=self.settings.rules,
                    settings=self.settings,
                )
            )
            results.extend(rule_results)

        prompt_version = self.settings.judge_prompt_version if mode in ("llm_only", "hybrid") else None
        if mode in ("llm_only", "hybrid"):
            judge_results, judge_info, judge_diag = self.judge.evaluate(trace, evidence)
            diagnostics.extend(judge_diag)
            results.extend(judge_results)
            if not judge_results:
                # 自定义/降级 Judge 返回空结果时，不能把“没有结果”当成“没有问题”
                from app.audit.schemas import JudgeTaskResult
                from app.audit.scoring import judge_to_rule_result

                results.append(
                    judge_to_rule_result(
                        JudgeTaskResult(
                            task="evidence_support",
                            verdict=Verdict.UNKNOWN,
                            risk_category="judge_unavailable",
                            confidence=0.0,
                            claims=[],
                            evidence_refs=[],
                            explanation=(
                                "语义审计没有返回任何结果"
                                f"（judge status={judge_info.status}），不得视为通过"
                            ),
                            requires_human_review=True,
                        ),
                        self.settings.judge_prompt_version,
                    )
                )
            if judge_info.status in ("error", "unavailable", "invalid_output"):
                diagnostics.append(
                    {
                        "code": "judge_not_completed",
                        "status": judge_info.status,
                        "note": "语义审计未完成，相关结论为 UNKNOWN，不得视为通过",
                    }
                )

        conflicts: list[dict[str, Any]] = []
        if mode == "hybrid":
            conflicts = detect_conflicts(rule_results, judge_results)
            diagnostics.extend(conflicts)

        audit_key = audit_id_for(
            trace_id=trace.trace_id,
            mode=mode,
            audit_version=audit_version,
            rule_config_version=self.settings.rule_config_version,
            prompt_version=prompt_version,
            trace_digest=trace.trace_digest,
        )
        findings = build_findings(
            trace_id=trace.trace_id,
            audit_id=audit_key,
            results=results,
            limit=self.settings.max_findings,
        )
        risk = overall_risk(findings, results)
        if not findings and (
            not results
            or judge_info.status in ("error", "unavailable", "invalid_output", "disabled")
        ):
            # 审计没有完成时不得给出 none/low 这类“安全”外观
            risk = RiskLevel.UNKNOWN
        summary = build_summary(results, findings)
        review = requires_human_review(findings, conflicts)

        status = AuditStatus.COMPLETED
        if any(item.verdict == Verdict.UNKNOWN for item in results):
            status = AuditStatus.PARTIAL
        if mode in ("llm_only", "hybrid") and judge_info.status in (
            "error",
            "unavailable",
            "invalid_output",
            "disabled",
        ):
            status = AuditStatus.PARTIAL
        if risk == RiskLevel.UNKNOWN:
            status = AuditStatus.PARTIAL
        if not results:
            # 没有任何可执行检查（既不是 PASS 也不是 FAIL）→ 不能声称已完成
            status = AuditStatus.PARTIAL
            diagnostics.append(
                {"code": "no_results", "message": "本次审计没有产生任何检查结果"}
            )

        report = AuditReport(
            audit_id=audit_key,
            trace_id=trace.trace_id,
            audit_mode=AuditMode(mode),
            audit_version=audit_version,
            rule_config_version=self.settings.rule_config_version,
            prompt_version=prompt_version,
            judge_model=self.settings.judge_model if mode in ("llm_only", "hybrid") else None,
            judge_provider=self.settings.judge_provider if mode in ("llm_only", "hybrid") else None,
            status=status,
            overall_risk=risk,
            total_findings=len(findings),
            requires_human_review=review,
            started_at=started_at,
            completed_at=_now(),
            duration_ms=0.0,
            trace_digest=trace.trace_digest,
            input_digest=_digest(
                trace.trace_digest,
                mode,
                audit_version,
                self.settings.rule_config_version,
                prompt_version,
                self.settings.judge_prompt_version,
            ),
            event_count=trace.event_count,
            trace_schema_version=trace.trace_schema_version,
            trace_source=trace.source,
            results=results,
            findings=findings,
            summary=summary,
            judge=judge_info,
            diagnostics=diagnostics,
            config_snapshot=self.settings.describe(),
            external_reads=list(evidence.external_reads),
        )
        report.duration_ms = round((time.perf_counter() - started_perf) * 1000, 2)

        if persist:
            if self.repository is None:
                report.status = AuditStatus.PARTIAL
                report.error = "持久化未配置（AUDIT_DATABASE_URL 缺失）：报告未写入数据库"
                report.requires_human_review = True
            else:
                try:
                    self.repository.save_trace(trace)
                    persisted, created = self.repository.save_report(report)
                except Exception as exc:  # noqa: BLE001 - 持久化失败不能伪装成成功
                    report.status = AuditStatus.PARTIAL
                    report.error = f"persistence_failed: {type(exc).__name__}"
                    report.requires_human_review = True
                    report.diagnostics.append(
                        {
                            "code": "persistence_failed",
                            "error": f"{type(exc).__name__}: {exc}"[:300],
                            "note": "原始 Trace 未被修改；报告以未持久化状态返回",
                        }
                    )
                else:
                    if not created:
                        persisted.diagnostics = list(persisted.diagnostics) + [
                            {
                                "code": "idempotent_reuse",
                                "message": "相同自然键的审计已存在，返回已保存的结果（未覆盖）",
                            }
                        ]
                    return persisted
        return report

    # ------------------------------------------------------------------ #
    def audit_trace_id(
        self,
        trace_id: str,
        *,
        mode: str = "hybrid",
        audit_version: str = AUDIT_VERSION,
        persist: bool = False,
        prefer_memory: bool = False,
    ) -> AuditReport:
        db_loader = self.repository.load_trace_events if self.repository is not None else None
        trace = load_trace(
            trace_id,
            settings=self.settings,
            prefer_memory=prefer_memory,
            db_loader=db_loader,
        )
        return self.audit(
            trace,
            mode=mode,
            audit_version=audit_version,
            persist=persist,
        )

    # ------------------------------------------------------------------ #
    def replay(self, trace_id: str, *, mode: str = "hybrid") -> dict[str, Any]:
        """离线回放：完整事件顺序 + 工具调用 + 证据 + 审计发现。

        不重新执行工具，也不运行业务 Agent。
        """
        db_loader = self.repository.load_trace_events if self.repository is not None else None
        trace = load_trace(trace_id, settings=self.settings, db_loader=db_loader)
        evidence = build_evidence_index(trace)
        report = self.audit(trace, mode=mode, persist=False)
        return {
            "trace": trace.model_dump(mode="json"),
            "tool_calls": build_tool_timeline(trace),
            "evidence": {
                "has_manifest": evidence.has_manifest,
                "schema_version": (evidence.manifest or {}).get("schema_version"),
                "window": (evidence.manifest or {}).get("evidence_window"),
                "requested": evidence.requested,
                "statistics": evidence.statistics,
                "quality": evidence.quality,
                "knowledge": [
                    {
                        "chunk_id": item.get("chunk_id"),
                        "doc_key": item.get("doc_key"),
                        "page_start": item.get("page_start"),
                        "page_end": item.get("page_end"),
                        "equipment_scope": item.get("equipment_scope"),
                        "quote_sha256": item.get("quote_sha256"),
                    }
                    for item in evidence.knowledge
                ],
                "fault_events": evidence.faults,
                "injection_flags": evidence.injection_flags,
                "external_reads": evidence.external_reads,
                "knowledge_verification": evidence.knowledge_verification,
            },
            "audit": report.model_dump(mode="json"),
        }


def audit_after_run(
    trace_id: str,
    *,
    settings: AuditSettings | None = None,
    mode: str | None = None,
) -> AuditReport | None:
    """运行后自动审计入口（供 Agent API 调用；失败绝不影响原始回答）。"""
    try:
        settings = settings or get_audit_settings()
        if not settings.audit_database_url:
            return None
        engine = AuditEngine(settings, use_repository=True)
        return engine.audit_trace_id(
            trace_id,
            mode=mode or settings.default_mode,
            persist=True,
            prefer_memory=True,
        )
    except Exception:  # noqa: BLE001 - 自动审计是附加能力，不能影响主流程
        import logging

        logging.getLogger("industrial_agent_audit.audit").exception(
            "运行后自动审计失败 trace_id=%s", trace_id
        )
        return None


def rule_specs_snapshot() -> list[dict[str, Any]]:
    return [spec.model_dump(mode="json") for spec in RULE_SPECS]


__all__ = [
    "AuditEngine",
    "audit_after_run",
    "rule_specs_snapshot",
]
