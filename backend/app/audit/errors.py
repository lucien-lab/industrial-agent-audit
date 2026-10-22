"""审计领域异常（与业务异常分离，避免把审计失败混进 Agent 错误分类）。"""

from __future__ import annotations

from typing import Any


class AuditError(RuntimeError):
    code = "audit_error"
    status_code = 500

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message[:500]
        self.details = details


class AuditNotFound(AuditError):
    code = "audit_not_found"
    status_code = 404


class AuditTraceUnreadable(AuditError):
    code = "audit_trace_unreadable"
    status_code = 422


class AuditUnauthorized(AuditError):
    code = "audit_unauthorized"
    status_code = 401


class AuditStorageUnavailable(AuditError):
    code = "audit_storage_unavailable"
    status_code = 503


class AuditInvalidRequest(AuditError):
    code = "audit_invalid_request"
    status_code = 422


__all__ = [
    "AuditError",
    "AuditInvalidRequest",
    "AuditNotFound",
    "AuditStorageUnavailable",
    "AuditTraceUnreadable",
    "AuditUnauthorized",
]
