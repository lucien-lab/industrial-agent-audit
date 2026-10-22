"""FastAPI 应用工厂。

分层：Router（HTTP 校验/装配） → Service（领域逻辑，可被 Agent Tool 复用）
      → Repository（`app/services/queries.py` 的 SQLAlchemy 查询） → PostgreSQL。

本模块只做应用装配：路由注册、异常处理、请求标识与日志、连接池释放。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.api.app_meta import APP_VERSION, DESCRIPTION, SERVICE_NAME, TAGS_METADATA
from app.api.errors import install_error_handlers
from app.api.routes import agent, equipment, faults, health, knowledge, quality, readings, stats
from app.api.routes import audit as audit_routes
from app.db.session import dispose_engines

logger = logging.getLogger("industrial_agent_audit.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动日志 + 关闭时释放数据库连接池。"""
    logger.info("服务启动：%s %s", SERVICE_NAME, APP_VERSION)
    try:
        yield
    finally:
        dispose_engines()
        logger.info("服务关闭：数据库连接池已释放")


def create_app() -> FastAPI:
    app = FastAPI(
        title="工业设备数据查询服务 (industrial-agent-audit)",
        description=DESCRIPTION,
        version=APP_VERSION,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
        contact={"name": "industrial-agent-audit"},
        license_info={"name": "仅为学习与演示用途；数据来自 MetroPT-3 公开数据集"},
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """为每个请求生成 request_id，记录访问日志与耗时（不记录敏感参数）。"""
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "method=%s path=%s duration_ms=%.1f request_id=%s status=500",
                request.method,
                request.url.path,
                elapsed_ms,
                request_id,
            )
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.1f}"
        logger.info(
            "method=%s path=%s status=%s duration_ms=%.1f request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            request_id,
        )
        return response

    install_error_handlers(app)

    app.include_router(health.router)
    app.include_router(equipment.router)
    app.include_router(readings.router)
    app.include_router(stats.router)
    app.include_router(faults.router)
    app.include_router(quality.router)
    app.include_router(knowledge.router)
    app.include_router(agent.router)
    app.include_router(audit_routes.router)

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {
            "service": SERVICE_NAME,
            "version": APP_VERSION,
            "docs": "/docs",
            "openapi": "/openapi.json",
            "health": "/health",
            "ready": "/ready",
        }

    return app


app = create_app()
