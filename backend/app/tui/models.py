"""TUI 侧的 API 响应契约（Pydantic）。

为什么 TUI 自己定义模型，而不是导入 `app.schemas.*`：

- TUI 是 HTTP 客户端，只需要渲染字段；导入后端 Schema 会把数据库/服务层依赖带进 TUI 进程；
- 这里只对**必需字段**做严格校验（缺字段即判定为契约违约，由 `ApiError` 报告），
  其余字段一律 `extra="ignore"`，后端新增字段不会让 TUI 崩溃；
- 字段含义、单位与口径完全沿用后端 Schema 的定义，不在这里重新解释。

安全：不定义任何连接串、Token 或凭据字段。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    """宽松模型基类：忽略未知字段，绝不猜测缺失字段。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# --------------------------------------------------------------------------- #
# 健康检查 / 系统状态
# --------------------------------------------------------------------------- #
class HealthInfo(ApiModel):
    status: str
    service: str | None = None
    version: str | None = None
    time: datetime | None = None


class DatabaseInfo(ApiModel):
    database: str | None = None
    user: str | None = None
    server_version: str | None = None
    pgvector_available: bool | None = None


class ReadinessInfo(ApiModel):
    status: str
    database: DatabaseInfo = Field(default_factory=DatabaseInfo)
    schema_version: str | None = None
    schema_expected_version: str | None = None
    schema_ok: bool = False


# --------------------------------------------------------------------------- #
# 设备 / 读数 / 统计 / 故障 / 质量
# --------------------------------------------------------------------------- #
class Equipment(ApiModel):
    equipment_id: str
    equipment_name: str | None = None
    equipment_type: str | None = None
    description: str | None = None
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None


class EquipmentPage(ApiModel):
    items: list[Equipment] = Field(default_factory=list)
    total: int = 0


class WindowInfo(ApiModel):
    start: datetime | None = None
    end: datetime | None = None
    duration_seconds: float | None = None
    boundary: str | None = None


class Pagination(ApiModel):
    limit: int
    offset: int = 0
    total: int = 0
    returned: int = 0
    has_more: bool = False


class Reading(ApiModel):
    observed_at: datetime
    values: dict[str, float | int | None] = Field(default_factory=dict)
    quality_flags: int = 0
    quality_labels: list[str] = Field(default_factory=list)


class ReadingPage(ApiModel):
    equipment: Equipment
    sensors: list[str] = Field(default_factory=list)
    items: list[Reading] = Field(default_factory=list)
    pagination: Pagination
    exclude_frozen: bool = False


class AnalogStats(ApiModel):
    sensor: str
    count: int = 0
    minimum: float | None = Field(default=None, alias="min")
    maximum: float | None = Field(default=None, alias="max")
    mean: float | None = None
    std: float | None = None


class DigitalStats(ApiModel):
    sensor: str
    count: int = 0
    count_0: int = 0
    count_1: int = 0
    ratio_1: float | None = None
    transitions: int | None = None


class StatsPage(ApiModel):
    equipment: Equipment
    sensors: list[str] = Field(default_factory=list)
    row_count: int = 0
    nominal_step_seconds: float | None = None
    analog: list[AnalogStats] = Field(default_factory=list)
    digital: list[DigitalStats] = Field(default_factory=list)


class QualityWarning(ApiModel):
    code: str
    severity: str | None = None
    message: str = ""


class QualityInfo(ApiModel):
    equipment: Equipment
    nominal_step_seconds: float | None = None
    actual_samples: int = 0
    expected_samples: int = 0
    sample_coverage: float | None = None
    time_coverage: float | None = None
    gap_count: int = 0
    gap_duration_seconds: float | None = None
    estimated_missing_seconds: float | None = None
    missing_samples_estimate: int | None = None
    max_gap_seconds: float | None = None
    frozen_candidate_count: int = 0
    frozen_candidate_ratio: float | None = None
    records_with_quality_flags: int = 0
    window_inside_data_range: bool | None = None
    warnings: list[QualityWarning] = Field(default_factory=list)


class FaultEvent(ApiModel):
    event_code: str
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration_seconds: float | None = None
    overlap_seconds: float | None = None
    failure_type: str | None = None
    severity: str | None = None
    fault_description: str | None = None
    verification_status: str | None = None


class FaultPage(ApiModel):
    items: list[FaultEvent] = Field(default_factory=list)
    pagination: Pagination
    sample_label_semantics: str | None = None


# --------------------------------------------------------------------------- #
# 知识库
# --------------------------------------------------------------------------- #
class KnowledgeHealth(ApiModel):
    status: str
    database: DatabaseInfo = Field(default_factory=DatabaseInfo)
    schema_version: str | None = None
    schema_expected_version: str | None = None
    schema_ok: bool = False
    documents: int = 0
    chunks: int = 0
    indexes: list[dict[str, Any]] = Field(default_factory=list)
    embedding: dict[str, Any] = Field(default_factory=dict)
    llm: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class SearchHit(ApiModel):
    chunk_id: int
    doc_key: str
    document_title: str | None = None
    chunk_index: int | None = None
    content: str = ""
    page_start: int | None = None
    page_end: int | None = None
    section_title: str | None = None
    equipment_scope: str | None = None
    doc_type: str | None = None
    trust_level: str | None = None
    source_url: str | None = None
    score: float = 0.0
    vector_similarity: float | None = None
    keyword_score: float | None = None
    rrf_score: float | None = None
    citation_label: str | None = None


class SearchResults(ApiModel):
    query: str = ""
    method: str = "hybrid"
    top_k: int = 0
    results: list[SearchHit] = Field(default_factory=list)
    retrieval: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class ToolCall(ApiModel):
    tool: str
    params: dict[str, Any] = Field(default_factory=dict)
    status: str = "ok"
    attempts: int = 1
    elapsed_ms: float = 0.0
    reason: str | None = None
    stage: str = "initial"
    error: dict[str, Any] | None = None
    evidence_count: int = 0


class Evidence(ApiModel):
    label: str
    kind: str
    fact_type: str | None = None
    text: str | None = None
    source: dict[str, Any] | None = None
    chunk_id: int | None = None
    doc_key: str | None = None
    document_title: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    section_title: str | None = None
    equipment_scope: str | None = None
    source_url: str | None = None
    citation_label: str | None = None
    score: float | None = None
    quote: str | None = None


class AgentAnswer(ApiModel):
    trace_id: str
    question: str = ""
    answer: str = ""
    execution_status: str = "unknown"
    intent: str = "unknown"
    answer_status: str = "unknown"
    answer_mode: str = "none"
    tools_used: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    plan: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    grounding: dict[str, Any] = Field(default_factory=dict)
    llm: dict[str, Any] = Field(default_factory=dict)
    sections: dict[str, Any] = Field(default_factory=dict)
    timings_ms: dict[str, float] = Field(default_factory=dict)
    total_duration_ms: float = 0.0
    rounds: int = 0
    agent_version: str = ""


class AgentTraceSummary(ApiModel):
    trace_id: str
    question: str = ""
    execution_status: str = "unknown"
    intent: str = "unknown"
    tools_used: list[str] = Field(default_factory=list)
    tool_calls: int = 0
    rounds: int = 0
    total_duration_ms: float = 0.0
    started_at: str | None = None
    ended_at: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    event_count: int = 0
    events: list[dict[str, Any]] | None = None


# --------------------------------------------------------------------------- #
# Trace / 审计
# --------------------------------------------------------------------------- #
class TraceEvent(ApiModel):
    event_id: str
    sequence_number: int = 0
    event_type: str
    node_name: str | None = None
    tool_name: str | None = None
    timestamp: datetime | None = None
    duration_ms: float | None = None
    status: str | None = None
    input_summary: dict[str, Any] | None = None
    output_summary: dict[str, Any] | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    error_type: str | None = None
    error_code: str | None = None


class NormalizedTrace(ApiModel):
    trace_id: str
    question: str = ""
    execution_status: str = "unknown"
    intent: str = "unknown"
    agent_version: str | None = None
    trace_schema_version: str | None = None
    source: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    events: list[TraceEvent] = Field(default_factory=list)
    evidence_manifest: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    event_count: int = 0


class AuditToolCall(ApiModel):
    tool: str
    call_event_id: str | None = None
    result_event_id: str | None = None
    input_summary: dict[str, Any] | None = None
    status: str | None = None
    duration_ms: float | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    error_type: str | None = None


class AuditTraceDetail(ApiModel):
    trace: NormalizedTrace
    tool_calls: list[AuditToolCall] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    audits: list[dict[str, Any]] = Field(default_factory=list)
    external_reads: list[dict[str, Any]] = Field(default_factory=list)
    offline_only: bool = True

    @property
    def has_evidence_manifest(self) -> bool:
        return bool(self.evidence.get("has_manifest"))


class RuleResult(ApiModel):
    rule_id: str
    rule_name: str = ""
    category: str = ""
    severity: str = "info"
    verdict: str = "unknown"
    source: str = "rule"
    explanation: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    related_event_ids: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    requires_human_review: bool = False
    confidence: float | None = None


class Finding(ApiModel):
    finding_id: str
    audit_id: str | None = None
    trace_id: str = ""
    rule_id: str = ""
    rule_name: str = ""
    risk_category: str = ""
    severity: str = "info"
    verdict: str = "unknown"
    source: str = "rule"
    related_event_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    explanation: str = ""
    confidence: float | None = None
    requires_human_review: bool = False
    review_status: str = "pending"
    details: dict[str, Any] = Field(default_factory=dict)


class AuditReport(ApiModel):
    audit_id: str
    trace_id: str
    audit_mode: str = "rule_only"
    audit_version: str = ""
    rule_config_version: str = ""
    prompt_version: str | None = None
    judge_model: str | None = None
    status: str = "completed"
    overall_risk: str = "unknown"
    total_findings: int = 0
    requires_human_review: bool = False
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: float = 0.0
    results: list[RuleResult] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    judge: dict[str, Any] = Field(default_factory=dict)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    external_reads: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class AuditRunBrief(ApiModel):
    audit_id: str
    trace_id: str
    audit_mode: str = "rule_only"
    status: str = "completed"
    overall_risk: str = "unknown"
    total_findings: int = 0
    high_risk_findings: int = 0
    requires_human_review: bool = False
    duration_ms: float = 0.0
    started_at: str | None = None
    audit_version: str = ""


class AuditRunsPage(ApiModel):
    items: list[AuditRunBrief] = Field(default_factory=list)
    total: int = 0
    limit: int = 0
    offset: int = 0


class AuditRuleSpec(ApiModel):
    rule_id: str
    rule_name: str = ""
    category: str = ""
    severity: str = "info"
    description: str = ""
    applicability: str = ""
    evidence_requirements: list[str] = Field(default_factory=list)


class ReviewResult(ApiModel):
    finding_id: str
    review_status: str
    updated_at: datetime | None = None


__all__ = [name for name in dir() if name[0].isupper()]
