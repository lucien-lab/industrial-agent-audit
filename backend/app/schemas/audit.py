"""审计 API 契约（阶段 4）。

安全约定：
- 请求体只允许白名单字段（`extra="forbid"`），客户端不能提交 trace 事件、SQL 或代码；
- 分页有硬上限，避免无界查询；
- 响应只返回脱敏后的事件摘要、证据引用与审计结论，不含连接串/密码/API Key。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.audit.schemas import NormalizedTrace

AuditModeLiteral = Literal["rule_only", "llm_only", "hybrid"]
RiskLiteral = Literal["none", "low", "medium", "high", "critical", "unknown"]
SeverityLiteral = Literal["info", "low", "medium", "high", "critical"]
VerdictLiteral = Literal["pass", "fail", "not_applicable", "unknown"]
ReviewLiteral = Literal["pending", "confirmed", "dismissed"]


class AuditEvaluateRequest(BaseModel):
    mode: AuditModeLiteral = Field(
        default="hybrid",
        description="rule_only=只跑确定性规则；llm_only=只跑语义 Judge；hybrid=两者结合",
    )
    audit_version: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]{1,64}$",
        description="审计版本（默认 phase4-audit-v1）；不同版本的结果互不覆盖",
    )
    verify_knowledge: bool | None = Field(
        default=None,
        description=(
            "是否读取知识库原文核对引用。true 会记录外部读取，此时不是纯冻结 Trace 回放；"
            "省略时使用 AUDIT_VERIFY_KNOWLEDGE_OFFLINE 配置"
        ),
    )
    persist: bool = Field(default=True, description="是否写入审计库（需要 AUDIT_DATABASE_URL）")

    model_config = ConfigDict(extra="forbid")


class AuditFindingOut(BaseModel):
    finding_id: str
    audit_id: str
    trace_id: str
    rule_id: str
    rule_name: str
    risk_category: str
    severity: SeverityLiteral
    verdict: VerdictLiteral
    source: Literal["rule", "judge"]
    related_event_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    explanation: str
    confidence: float | None = None
    requires_human_review: bool = False
    review_status: ReviewLiteral
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None

    model_config = ConfigDict(extra="ignore")


class FindingsPage(BaseModel):
    items: list[AuditFindingOut] = Field(default_factory=list)
    total: int = Field(description="满足条件的总条数")
    limit: int
    offset: int


class AuditRunBrief(BaseModel):
    audit_id: str
    trace_id: str
    audit_mode: AuditModeLiteral
    audit_version: str
    rule_config_version: str
    prompt_version: str | None = None
    judge_model: str | None = None
    status: Literal["completed", "partial", "failed"]
    overall_risk: RiskLiteral
    total_findings: int
    high_risk_findings: int = 0
    requires_human_review: bool = False
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: float = 0.0
    trace_digest: str

    model_config = ConfigDict(extra="ignore")


class AuditRunsPage(BaseModel):
    items: list[AuditRunBrief] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class AuditTraceDetailOut(BaseModel):
    trace: NormalizedTrace
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    audits: list[dict[str, Any]] = Field(default_factory=list)
    external_reads: list[dict[str, Any]] = Field(default_factory=list)
    offline_only: bool = Field(
        default=True,
        description="true = 只使用冻结 Trace；false = 本视图读取了外部知识库原文",
    )


class AuditSummaryOut(BaseModel):
    total_audits: int = 0
    audits_by_mode: dict[str, int] = Field(default_factory=dict)
    audits_by_risk: dict[str, int] = Field(default_factory=dict)
    audits_by_status: dict[str, int] = Field(default_factory=dict)
    total_findings: int = 0
    findings_by_category: dict[str, int] = Field(default_factory=dict)
    findings_by_severity: dict[str, int] = Field(default_factory=dict)
    pending_human_review: int = 0
    judge_verdict_distribution: dict[str, int] = Field(default_factory=dict)
    judge_calls_total: int = 0
    avg_duration_ms: float = 0.0
    max_duration_ms: float = 0.0
    window_days: int = 30


class ReviewUpdateRequest(BaseModel):
    review_status: ReviewLiteral
    note: str | None = Field(default=None, max_length=500)

    model_config = ConfigDict(extra="forbid")


class ReviewUpdateOut(BaseModel):
    finding_id: str
    review_status: ReviewLiteral
    updated_at: datetime | None = None


class RuleSpecOut(BaseModel):
    rule_id: str
    rule_name: str
    description: str
    category: str
    severity: SeverityLiteral
    applicability: str
    evidence_requirements: list[str] = Field(default_factory=list)


__all__ = [
    "AuditEvaluateRequest",
    "AuditFindingOut",
    "AuditRunBrief",
    "AuditRunsPage",
    "AuditSummaryOut",
    "AuditTraceDetailOut",
    "FindingsPage",
    "ReviewUpdateOut",
    "ReviewUpdateRequest",
    "RuleSpecOut",
]
