"""健康检查：区分“进程存活”与“数据库就绪”。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.api.app_meta import APP_VERSION, SERVICE_NAME
from app.db.session import check_connection, get_engine
from app.schemas.responses import HealthResponse, ReadinessResponse

router = APIRouter(tags=["健康检查"], responses={})

# 代码期望的 Alembic 版本：新增迁移时同步更新。
# 版本不一致**不会**让 /ready 返回 503（结构差异由部署流程负责），
# 但会把 `schema_ok` 置为 false，便于运维与监控发现“代码先上、迁移未上”。
EXPECTED_SCHEMA_VERSION = "0008_agent_audit"


def _current_schema_version() -> str | None:
    """读取 alembic_version；表不存在时返回 None。"""
    try:
        with get_engine().connect() as conn:
            return conn.scalar(text("SELECT version_num FROM alembic_version"))
    except DBAPIError:
        return None


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="进程存活检查",
    description=(
        "只检查 FastAPI 进程是否可以响应 HTTP 请求，**不访问数据库**。\n\n"
        "用于容器/进程级存活探针（liveness）。"
    ),
)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=SERVICE_NAME,
        version=APP_VERSION,
        time=datetime.now(timezone.utc),
    )


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="数据库就绪检查",
    description=(
        "检查数据库连通性、服务端版本、pgvector 可用性与当前 Alembic 版本。\n\n"
        "- 数据库不可用时返回 **503**（`code=database_unavailable`）；\n"
        "- 数据库可用但结构版本与代码期望不一致时返回 200，"
        "`status=degraded`、`schema_ok=false`（便于灰度部署时不误杀流量）。"
    ),
    responses={
        503: {"description": "数据库不可用"},
    },
)
def ready() -> ReadinessResponse:
    info = check_connection()  # 连接失败会抛 OperationalError -> 统一处理器返回 503
    actual_version = _current_schema_version()
    schema_ok = actual_version == EXPECTED_SCHEMA_VERSION
    return ReadinessResponse(
        status="ok" if schema_ok else "degraded",
        database=info,
        schema_version=actual_version,
        schema_expected_version=EXPECTED_SCHEMA_VERSION,
        schema_ok=schema_ok,
    )
