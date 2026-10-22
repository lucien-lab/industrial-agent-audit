"""Agent-Probe 行为审计模块（阶段 4）。

边界：
- **Observability + Audit**：读取阶段 3 的结构化 Trace，按确定性规则与 LLM Judge 判断异常；
- **不做 Protection**：不在 Agent 执行前阻断操作；离线审计不能阻止已经完成的操作；
- 不重新实现业务 Agent，不执行工业数据工具，不修改原始 Trace/回答；
- 审计结果写入独立审计库，不触碰主库与 RAG Embedding。

快速入口：
    from app.audit import AuditEngine
    report = AuditEngine(use_repository=True).audit_trace_id(trace_id, mode="hybrid", persist=True)
"""

from app.audit.config import AUDIT_MODES, AUDIT_VERSION, AuditSettings, get_audit_settings
from app.audit.engine import AuditEngine, audit_after_run
from app.audit.schemas import (
    AuditMode,
    AuditReport,
    AuditStatus,
    ClaimVerdict,
    Finding,
    NormalizedTrace,
    ReviewStatus,
    RiskLevel,
    RuleResult,
    Severity,
    Verdict,
)

__all__ = [
    "AUDIT_MODES",
    "AUDIT_VERSION",
    "AuditEngine",
    "AuditMode",
    "AuditReport",
    "AuditSettings",
    "AuditStatus",
    "ClaimVerdict",
    "Finding",
    "NormalizedTrace",
    "ReviewStatus",
    "RiskLevel",
    "RuleResult",
    "Severity",
    "Verdict",
    "audit_after_run",
    "get_audit_settings",
]
