"""统一异常处理：把领域异常、参数校验失败、数据库故障翻译成稳定的 JSON 错误结构。

安全边界：
- 响应体只包含错误码、可读说明和白名单化的补充信息；
- **绝不**回显 SQL、连接串、密码、数据库堆栈；
- 完整异常仅写入服务端日志（含 request_id，便于定位）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from app.schemas.common import ErrorDetail, ErrorResponse
from app.rag.errors import RAGError
from app.services.errors import ServiceError

logger = logging.getLogger("industrial_agent_audit.api")

# 领域错误码 -> HTTP 状态码
STATUS_BY_CODE: dict[str, int] = {
    "equipment_not_found": 404,
    "invalid_parameter": 422,
    "invalid_time_range": 422,
    "timezone_not_supported": 422,
    "unsupported_sensor": 422,
    "window_too_large": 422,
    # 阶段 2：知识库
    "document_parse_error": 422,
    "embedding_error": 503,
    "embedding_dimension_mismatch": 503,
    "index_model_mismatch": 503,
    "retrieval_error": 503,
    "llm_error": 503,
    "llm_timeout": 504,
    "llm_rate_limited": 503,
    "llm_unavailable": 503,
    "rag_error": 500,
}

GENERIC_INTERNAL_MESSAGE = "服务内部错误，请联系管理员并提供 request_id"
DATABASE_UNAVAILABLE_MESSAGE = "数据库暂时不可用，请稍后重试"


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            details=details or {},
            request_id=_request_id(request),
        )
    )
    return JSONResponse(
        status_code=status_code, content=body.model_dump(mode="json"), headers=headers
    )


def install_error_handlers(app: FastAPI) -> None:
    """注册全部异常处理器（顺序无关，按异常类型精确匹配）。"""

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
        status_code = STATUS_BY_CODE.get(exc.code, 500)
        if status_code >= 500:  # pragma: no cover - 目前没有 5xx 级领域错误
            logger.exception("领域错误 %s: %s", exc.code, exc.message)
        else:
            logger.info(
                "请求被拒绝 code=%s path=%s request_id=%s",
                exc.code,
                request.url.path,
                _request_id(request),
            )
        return error_response(
            request,
            status_code=status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
        )

    @app.exception_handler(RAGError)
    async def _rag_error(request: Request, exc: RAGError) -> JSONResponse:
        status_code = STATUS_BY_CODE.get(exc.code, 500)
        log = logger.warning if status_code >= 500 else logger.info
        log(
            "RAG 错误 code=%s path=%s request_id=%s details=%s",
            exc.code,
            request.url.path,
            _request_id(request),
            exc.details,
        )
        return error_response(
            request,
            status_code=status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {
                "field": ".".join(str(part) for part in err.get("loc", ()) if part != "body"),
                "type": err.get("type"),
                "message": err.get("msg"),
            }
            for err in exc.errors()
        ]
        return error_response(
            request,
            status_code=422,
            code="invalid_parameter",
            message="请求参数校验失败",
            details={"errors": errors},
        )

    @app.exception_handler(OperationalError)
    async def _operational_error(request: Request, exc: OperationalError) -> JSONResponse:
        # 连接失败、连接被中断、服务端关闭等：503 而不是 500
        logger.warning(
            "数据库不可用 path=%s request_id=%s error=%s",
            request.url.path,
            _request_id(request),
            type(exc.orig).__name__,
            exc_info=True,
        )
        return error_response(
            request,
            status_code=503,
            code="database_unavailable",
            message=DATABASE_UNAVAILABLE_MESSAGE,
            details={"hint": "检查数据库进程、网络连通性与连接池配置"},
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(InterfaceError)
    async def _interface_error(request: Request, exc: InterfaceError) -> JSONResponse:
        logger.warning(
            "数据库接口错误 path=%s request_id=%s", request.url.path, _request_id(request)
        )
        return error_response(
            request,
            status_code=503,
            code="database_unavailable",
            message=DATABASE_UNAVAILABLE_MESSAGE,
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(DBAPIError)
    async def _dbapi_error(request: Request, exc: DBAPIError) -> JSONResponse:
        logger.error(
            "数据库错误 path=%s request_id=%s error=%s",
            request.url.path,
            _request_id(request),
            type(exc.orig).__name__,
            exc_info=True,
        )
        return error_response(
            request,
            status_code=500,
            code="internal_error",
            message=GENERIC_INTERNAL_MESSAGE,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "未处理异常 path=%s request_id=%s", request.url.path, _request_id(request)
        )
        return error_response(
            request,
            status_code=500,
            code="internal_error",
            message=GENERIC_INTERNAL_MESSAGE,
        )
