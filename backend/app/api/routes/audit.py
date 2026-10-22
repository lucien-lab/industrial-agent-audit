"""Agent-Probe 审计接口（阶段 4）。

边界（必须与文档一致）：
- **Observability + Audit**：读取已有 Trace、执行规则/语义审计、返回可解释结果；
- **不是运行时拦截**：离线审计不能阻止已经完成的操作；接口也不会执行 Agent 或工具；
- 只写独立审计库（`AUDIT_DATABASE_URL`），不写工业数据与 RAG 表；
- 不接受客户端提交的事件、SQL 或代码；trace_id / audit_id 全部做字符集校验；
- 若未配置 `AUDIT_API_TOKEN`，本接口只应绑定本机或隔离环境（见 README 部署限制）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query

from app.audit.collector import TraceLoadError, TraceNotFoundError, load_trace, validate_trace_id
from app.audit.config import AUDIT_MODES, AUDIT_VERSION, get_audit_settings
from app.audit.engine import AuditEngine, rule_specs_snapshot
from app.audit.errors import (
    AuditInvalidRequest,
    AuditNotFound,
    AuditStorageUnavailable,
    AuditTraceUnreadable,
    AuditUnauthorized,
)
from app.audit.repository import get_repository
from app.audit.schemas import AuditReport
from app.schemas.audit import (
    AuditEvaluateRequest,
    AuditFindingOut,
    AuditRunsPage,
    AuditSummaryOut,
    AuditTraceDetailOut,
    FindingsPage,
    ReviewUpdateOut,
    ReviewUpdateRequest,
    RuleSpecOut,
)
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/api/v1/audit", tags=["Audit"])

_AUDIT_ERRORS = {
    401: {"model": ErrorResponse, "description": "缺少或错误的 X-Audit-Token"},
    404: {"model": ErrorResponse, "description": "Trace 或审计记录不存在"},
    422: {"model": ErrorResponse, "description": "参数不合法（trace_id/分页/审计模式）"},
    503: {"model": ErrorResponse, "description": "审计库或 Judge 不可用"},
}


def require_audit_access(x_audit_token: Annotated[str | None, Header()] = None) -> None:
    """可选的共享令牌校验。

    未配置 `AUDIT_API_TOKEN` 时不校验，但部署必须限制在本机/隔离网络。
    配置后必须提供正确的 `X-Audit-Token`；不做复杂 RBAC。
    """
    settings = get_audit_settings()
    if settings.api_token and x_audit_token != settings.api_token:
        raise AuditUnauthorized("审计接口需要有效的 X-Audit-Token")


AccessDep = Depends(require_audit_access)


def _repository() -> Any:
    repository = get_repository()
    return repository


@router.get(
    "/traces/{trace_id}",
    response_model=AuditTraceDetailOut,
    summary="查询执行链与审计摘要",
    description=(
        "返回规范化事件顺序、工具调用与返回、证据清单、已有审计记录。\n\n"
        "优先读取 `trace-<trace_id>.jsonl`（权威来源），其次进程内 Trace，最后审计事件索引。\n"
        "本接口不会重新运行 Agent 或工具。"
    ),
    responses=_AUDIT_ERRORS,
)
def get_trace(trace_id: str, _: None = AccessDep) -> AuditTraceDetailOut:
    try:
        validated = validate_trace_id(trace_id)
    except TraceLoadError as exc:
        raise AuditTraceUnreadable(str(exc), trace_id=trace_id) from exc
    repository = _repository()
    try:
        trace = load_trace(
            validated,
            db_loader=repository.load_trace_events if repository is not None else None,
        )
    except TraceNotFoundError as exc:
        raise AuditNotFound(str(exc), trace_id=validated) from exc
    except TraceLoadError as exc:
        raise AuditTraceUnreadable(str(exc), trace_id=validated) from exc
    from app.audit.evidence import build_evidence_index, build_tool_timeline

    evidence = build_evidence_index(trace)
    audits: list[dict[str, Any]] = []
    if repository is not None:
        try:
            audits = repository.audits_for_trace(validated)
        except Exception as exc:  # noqa: BLE001 - 审计库不可用不应影响读 Trace
            raise AuditStorageUnavailable(
                f"审计库暂时不可用：{type(exc).__name__}", trace_id=validated
            ) from exc
    return AuditTraceDetailOut(
        trace=trace,
        tool_calls=build_tool_timeline(trace),
        evidence={
            "has_manifest": evidence.has_manifest,
            "manifest_schema_version": (evidence.manifest or {}).get("schema_version"),
            "requested": evidence.requested,
            "evidence_window": (evidence.manifest or {}).get("evidence_window"),
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
        audits=audits,
        external_reads=evidence.external_reads,
        offline_only=not evidence.external_reads,
    )


@router.post(
    "/traces/{trace_id}/evaluate",
    response_model=AuditReport,
    summary="对指定 Trace 发起审计",
    description=(
        "支持 `rule_only` / `llm_only` / `hybrid`。相同 Trace、相同模式、相同审计版本与"
        "规则配置重复调用会返回已保存的结果（幂等），不会覆盖历史版本。\n\n"
        "`llm_only` / `hybrid` 会调用本机 Ollama（默认 qwen2.5:7b-instruct）。"
        "Judge 解析失败、超时或不可用都会产生 UNKNOWN 并要求人工复核，**不会**被当成通过。"
    ),
    responses=_AUDIT_ERRORS,
)
def evaluate_trace(
    trace_id: str,
    payload: AuditEvaluateRequest,
    _: None = AccessDep,
) -> AuditReport:
    if payload.mode not in AUDIT_MODES:
        raise AuditInvalidRequest(f"未知审计模式 {payload.mode!r}")
    try:
        validated = validate_trace_id(trace_id)
    except TraceLoadError as exc:
        raise AuditTraceUnreadable(str(exc), trace_id=trace_id) from exc

    repository = _repository()
    if payload.persist and repository is None:
        raise AuditStorageUnavailable(
            "未配置 AUDIT_DATABASE_URL，无法持久化审计结果；可改用 persist=false 仅返回报告"
        )
    settings = get_audit_settings()
    engine = AuditEngine(settings, repository=repository, use_repository=False)
    try:
        trace = load_trace(
            validated,
            settings=settings,
            db_loader=repository.load_trace_events if repository is not None else None,
        )
    except TraceNotFoundError as exc:
        raise AuditNotFound(str(exc), trace_id=validated) from exc
    except TraceLoadError as exc:
        raise AuditTraceUnreadable(str(exc), trace_id=validated) from exc

    report = engine.audit(
        trace,
        mode=payload.mode,
        audit_version=payload.audit_version or AUDIT_VERSION,
        persist=payload.persist,
        verify_knowledge=payload.verify_knowledge,
    )
    if payload.persist and report.error and report.error.startswith("persistence_failed"):
        raise AuditStorageUnavailable(report.error, trace_id=validated)
    return report


@router.get(
    "/runs/{audit_id}",
    response_model=AuditReport,
    summary="获取完整审计结果",
    description="返回规则/Judge 逐条结果、发现、证据引用、配置快照与诊断信息（不含凭据）。",
    responses=_AUDIT_ERRORS,
)
def get_audit_run(audit_id: str, _: None = AccessDep) -> AuditReport:
    if not audit_id.startswith("aud-") or len(audit_id) > 64:
        raise AuditInvalidRequest("audit_id 格式不合法")
    repository = _repository()
    if repository is None:
        raise AuditStorageUnavailable("未配置 AUDIT_DATABASE_URL")
    try:
        report = repository.get_report(audit_id)
    except Exception as exc:  # noqa: BLE001
        raise AuditStorageUnavailable(f"审计库暂时不可用：{type(exc).__name__}") from exc
    if report is None:
        raise AuditNotFound(f"audit_id {audit_id!r} 不存在", audit_id=audit_id)
    return report


@router.get(
    "/runs",
    response_model=AuditRunsPage,
    summary="列出审计运行（按 trace / 模式 / 风险筛选）",
    responses=_AUDIT_ERRORS,
)
def list_audit_runs(
    trace_id: str | None = Query(default=None, max_length=64),
    mode: str | None = Query(default=None, description="rule_only / llm_only / hybrid"),
    risk: str | None = Query(default=None, description="none/low/medium/high/critical/unknown"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=100_000),
    _: None = AccessDep,
) -> AuditRunsPage:
    if mode and mode not in AUDIT_MODES:
        raise AuditInvalidRequest(f"未知审计模式 {mode!r}")
    if risk and risk not in ("none", "low", "medium", "high", "critical", "unknown"):
        raise AuditInvalidRequest(f"未知风险等级 {risk!r}")
    repository = _repository()
    if repository is None:
        raise AuditStorageUnavailable("未配置 AUDIT_DATABASE_URL")
    items, total = repository.list_runs(
        trace_id=trace_id, mode=mode, risk=risk, limit=limit, offset=offset
    )
    return AuditRunsPage(items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/findings",
    response_model=FindingsPage,
    summary="筛选审计发现",
    description=(
        "支持按风险等级、规则类别、严重度、trace_id、审计 ID 与人工复核状态筛选。"
        "只返回 FAIL / UNKNOWN 发现（PASS 结果在 GET /runs/{audit_id} 的 results 中）。"
    ),
    responses=_AUDIT_ERRORS,
)
def list_findings(
    trace_id: str | None = Query(default=None, max_length=64),
    audit_id: str | None = Query(default=None, max_length=64),
    risk_category: str | None = Query(default=None, max_length=64),
    rule_id: str | None = Query(default=None, max_length=64),
    severity: str | None = Query(default=None),
    verdict: str | None = Query(default=None),
    review_status: str | None = Query(default=None),
    started_after: str | None = Query(default=None, description="ISO 8601 时间（含）"),
    started_before: str | None = Query(default=None, description="ISO 8601 时间（含）"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
    _: None = AccessDep,
) -> FindingsPage:
    from app.audit.repository import _parse_dt  # noqa: PLC2701 - 复用同一时间解析口径

    if severity and severity not in ("info", "low", "medium", "high", "critical"):
        raise AuditInvalidRequest(f"未知严重度 {severity!r}")
    if verdict and verdict not in ("pass", "fail", "not_applicable", "unknown"):
        raise AuditInvalidRequest(f"未知结论 {verdict!r}")
    if review_status and review_status not in ("pending", "confirmed", "dismissed"):
        raise AuditInvalidRequest(f"未知复核状态 {review_status!r}")
    parsed_after = _parse_dt(started_after)
    parsed_before = _parse_dt(started_before)
    if started_after and parsed_after is None:
        raise AuditInvalidRequest("started_after 必须是 ISO 8601 时间")
    if started_before and parsed_before is None:
        raise AuditInvalidRequest("started_before 必须是 ISO 8601 时间")
    repository = _repository()
    if repository is None:
        raise AuditStorageUnavailable("未配置 AUDIT_DATABASE_URL")
    items, total = repository.list_findings(
        audit_id=audit_id,
        trace_id=trace_id,
        risk_category=risk_category,
        severity=severity,
        verdict=verdict,
        review_status=review_status,
        rule_id=rule_id,
        started_after=parsed_after,
        started_before=parsed_before,
        limit=limit,
        offset=offset,
    )
    return FindingsPage(
        items=[AuditFindingOut.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/summary",
    response_model=AuditSummaryOut,
    summary="审计统计摘要",
    description=(
        "返回审计次数、风险分布、规则命中分布、Judge 判断分布与平均审计耗时。"
        "这些是审计运行的计数，不是 Agent 正确率的统计保证。"
    ),
    responses=_AUDIT_ERRORS,
)
def audit_summary(
    days: int = Query(default=30, ge=1, le=365),
    _: None = AccessDep,
) -> AuditSummaryOut:
    repository = _repository()
    if repository is None:
        raise AuditStorageUnavailable("未配置 AUDIT_DATABASE_URL")
    return AuditSummaryOut.model_validate(repository.summary(days=days))


@router.get(
    "/rules",
    response_model=list[RuleSpecOut],
    summary="列出确定性审计规则",
    description="返回规则元信息（ID、类别、严重度、适用条件与证据要求），用于解释审计结果。",
)
def list_rules() -> list[RuleSpecOut]:
    return [RuleSpecOut.model_validate(item) for item in rule_specs_snapshot()]


@router.patch(
    "/findings/{finding_id}/review",
    response_model=ReviewUpdateOut,
    summary="更新人工复核状态",
    description="只修改审计发现的人工复核状态，不修改原始 Trace、Agent 回答或风险结论。",
    responses=_AUDIT_ERRORS,
)
def review_finding(
    finding_id: str,
    payload: ReviewUpdateRequest,
    _: None = AccessDep,
) -> ReviewUpdateOut:
    if not finding_id.startswith("F") or len(finding_id) > 64:
        raise AuditInvalidRequest("finding_id 格式不合法")
    repository = _repository()
    if repository is None:
        raise AuditStorageUnavailable("未配置 AUDIT_DATABASE_URL")
    updated = repository.update_review(finding_id, payload.review_status, payload.note)
    if updated is None:
        raise AuditNotFound(f"finding_id {finding_id!r} 不存在", finding_id=finding_id)
    return ReviewUpdateOut.model_validate(updated)


__all__ = ["router", "require_audit_access"]
