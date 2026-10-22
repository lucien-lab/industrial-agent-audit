"""审计数据契约（Pydantic v2）。

三层严格分离，禁止混用：
1. **原始执行事件**（`AuditEvent` / `NormalizedTrace`）：来自阶段 3 Trace，只读；
2. **审计检测结果**（`RuleResult` / `Finding` / `JudgeOutput`）：审计器产生，带来源标记；
3. **审计器自身运行日志**（`AuditReport.diagnostics`）：解析失败、超时、外部读取等。

`UNKNOWN` 表示“证据不足，无法判断”，**不等于通过**；任何统计都必须单独计数。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AuditMode(str, Enum):
    RULE_ONLY = "rule_only"
    LLM_ONLY = "llm_only"
    HYBRID = "hybrid"


class AuditStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"  # 部分规则/Judge 无法完成（证据不足或调用失败）
    FAILED = "failed"


class RiskLevel(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class ClaimVerdict(str, Enum):
    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    UNSUPPORTED = "unsupported"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class FindingSource(str, Enum):
    RULE = "rule"
    JUDGE = "judge"


class ReviewStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"


class TraceSourceKind(str, Enum):
    FILE = "file"
    MEMORY = "memory"
    DATABASE = "database"
    INLINE = "inline"
    CASE = "case"


class AuditEvent(BaseModel):
    """规范化后的单条执行事件（字段可与阶段 3 结构一一对应）。"""

    event_id: str = Field(description="事件稳定标识，在同一 trace 内唯一")
    trace_id: str
    sequence_number: int = Field(ge=0, description="按 Trace 记录顺序的序号（0 起）")
    event_type: str
    node_name: str | None = None
    tool_name: str | None = None
    timestamp: str | None = Field(default=None, description="事件结束时间（ISO 8601）")
    started_at: str | None = None
    duration_ms: float | None = None
    status: str | None = None
    input_summary: Any = None
    output_summary: Any = None
    evidence_refs: list[str] = Field(default_factory=list)
    error_type: str | None = None
    error_code: str | None = None
    schema_version: str = "audit-event-v1"
    source_index: int = Field(default=0, description="原始事件在文件/列表中的下标")
    raw_sequence_number: int | None = Field(
        default=None, description="Trace 原始 seq（缺失时为 null）"
    )
    meta: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="ignore")


class NormalizedTrace(BaseModel):
    """一次 Agent 执行的规范化视图。"""

    trace_id: str
    question: str = ""
    execution_status: str = "unknown"
    intent: str = "unknown"
    agent_version: str | None = None
    trace_schema_version: str = "phase3-trace-v1"
    source: TraceSourceKind = TraceSourceKind.FILE
    source_ref: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    events: list[AuditEvent] = Field(default_factory=list)
    evidence_manifest: dict[str, Any] | None = None
    trace_digest: str = ""
    raw_digest: str | None = None
    event_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    problems: list[dict[str, Any]] = Field(
        default_factory=list, description="规范化阶段发现的结构问题（缺失/重复/乱序）"
    )

    model_config = ConfigDict(extra="ignore")

    def events_of(self, *event_types: str) -> list[AuditEvent]:
        return [event for event in self.events if event.event_type in event_types]

    def tool_calls(self) -> list[AuditEvent]:
        return self.events_of("tool_call")

    def tool_results(self) -> list[AuditEvent]:
        return self.events_of("tool_result")


class RuleSpec(BaseModel):
    """规则元信息（可解释性的一部分）。"""

    rule_id: str
    rule_name: str
    description: str
    category: str
    severity: Severity
    applicability: str
    evidence_requirements: list[str] = Field(default_factory=list)


class RuleResult(BaseModel):
    rule_id: str
    rule_name: str
    category: str
    severity: Severity
    verdict: Verdict
    source: FindingSource = FindingSource.RULE
    explanation: str
    evidence_refs: list[str] = Field(default_factory=list)
    related_event_ids: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    requires_human_review: bool = False
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @property
    def is_risk(self) -> bool:
        return self.verdict in (Verdict.FAIL, Verdict.UNKNOWN)


class JudgeClaim(BaseModel):
    claim_id: str = ""
    claim: str = ""
    verdict: ClaimVerdict = ClaimVerdict.INSUFFICIENT_EVIDENCE
    evidence_refs: list[str] = Field(default_factory=list)
    explanation: str = ""


class JudgeTaskResult(BaseModel):
    """单个 Judge 任务的结论（见 `docs/audit_design.md` 的 Prompt 契约）。"""

    task: str
    verdict: Verdict
    risk_category: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    claims: list[JudgeClaim] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    explanation: str = ""
    requires_human_review: bool = False


class JudgeOutput(BaseModel):
    tasks: list[JudgeTaskResult] = Field(default_factory=list)
    overall_comment: str = ""
    schema_version: str = "audit-judge-output-v1"
    raw_model: str | None = None
    prompt_version: str | None = None


class JudgeRunInfo(BaseModel):
    status: Literal["ok", "disabled", "unavailable", "invalid_output", "error", "skipped"] = "skipped"
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    input_digest: str | None = None
    elapsed_ms: float | None = None
    attempts: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None
    raw_output_chars: int | None = None


class Finding(BaseModel):
    """一条审计发现（只保存 FAIL / UNKNOWN；PASS 结果在 report.results 中）。"""

    finding_id: str
    audit_id: str | None = None
    trace_id: str
    rule_id: str
    rule_name: str
    risk_category: str
    severity: Severity
    verdict: Verdict
    source: FindingSource
    related_event_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    explanation: str
    confidence: float | None = None
    requires_human_review: bool = False
    review_status: ReviewStatus = ReviewStatus.PENDING
    details: dict[str, Any] = Field(default_factory=dict)


class AuditSummary(BaseModel):
    total_rules: int = 0
    passed: int = 0
    failed: int = 0
    unknown: int = 0
    not_applicable: int = 0
    findings: int = 0
    findings_by_severity: dict[str, int] = Field(default_factory=dict)
    findings_by_category: dict[str, int] = Field(default_factory=dict)


class AuditReport(BaseModel):
    """一次审计的完整结果（可序列化、可入库、可回放）。"""

    audit_id: str
    trace_id: str
    audit_mode: AuditMode
    audit_version: str
    rule_config_version: str
    prompt_version: str | None = None
    judge_model: str | None = None
    judge_provider: str | None = None
    status: AuditStatus
    overall_risk: RiskLevel
    total_findings: int = 0
    requires_human_review: bool = False
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: float = 0.0
    trace_digest: str = ""
    input_digest: str | None = None
    event_count: int = 0
    trace_schema_version: str = "phase3-trace-v1"
    trace_source: TraceSourceKind = TraceSourceKind.FILE
    results: list[RuleResult] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    summary: AuditSummary = Field(default_factory=AuditSummary)
    judge: JudgeRunInfo = Field(default_factory=JudgeRunInfo)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    external_reads: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None

    model_config = ConfigDict(use_enum_values=True)


class ReplayResult(BaseModel):
    """`GET /api/v1/audit/traces/{id}` 的完整回放视图。"""

    trace: NormalizedTrace
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    audits: list[dict[str, Any]] = Field(default_factory=list)


__all__ = [
    "AuditEvent",
    "AuditMode",
    "AuditReport",
    "AuditStatus",
    "AuditSummary",
    "ClaimVerdict",
    "Finding",
    "FindingSource",
    "JudgeClaim",
    "JudgeOutput",
    "JudgeRunInfo",
    "JudgeTaskResult",
    "NormalizedTrace",
    "ReplayResult",
    "ReviewStatus",
    "RiskLevel",
    "RuleResult",
    "RuleSpec",
    "Severity",
    "TraceSourceKind",
    "Verdict",
]
