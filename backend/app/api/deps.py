"""FastAPI 依赖：数据库会话与查询参数校验。

会话约定（只读服务）：
- 每个请求一个 Session，请求结束**一定** close，避免连接泄漏；
- 事务在结束时统一 rollback：本服务不写数据，回滚可立即释放快照，
  也让“误写”在架构上不可能被提交。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.orm import Session

from app.db.session import get_session_factory

logger = logging.getLogger("industrial_agent_audit.api")


def get_session() -> Iterator[Session]:
    """请求级数据库会话（异常安全、必定释放）。"""
    session = get_session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    else:
        session.rollback()  # 只读请求：显式结束事务，尽快释放快照与连接
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]

# --------------------------------------------------------------------------- #
# 查询参数（在 Schema/OpenAPI 中显式声明类型、边界与示例）
# --------------------------------------------------------------------------- #
StartTimeQuery = Annotated[
    str,
    Query(
        description="窗口起点（含），无时区 ISO 8601，例如 2020-04-18T00:00:00",
        examples=["2020-04-18T00:00:00"],
        openapi_examples={
            "fault-e1": {
                "summary": "官方故障 E1 当天",
                "value": "2020-04-18T00:00:00",
            }
        },
    ),
]
EndTimeQuery = Annotated[
    str,
    Query(
        description="窗口终点（不含），无时区 ISO 8601，必须晚于 start_time",
        examples=["2020-04-19T00:00:00"],
        openapi_examples={
            "fault-e1": {
                "summary": "官方故障 E1 次日 00:00（左闭右开）",
                "value": "2020-04-19T00:00:00",
            }
        },
    ),
]
SensorQuery = Annotated[
    list[str] | None,
    Query(
        description=(
            "传感器列名，可重复传参（?sensor=tp2&sensor=comp）或用逗号分隔；"
            "省略表示全部 15 路。可选值见 GET /api/v1/equipment/{id}/stats 的 sensors 说明"
        ),
        examples=["tp2", "comp"],
    ),
]
LimitQuery = Annotated[
    int,
    Query(
        ge=1,
        le=1000,
        description="返回条数上限，默认 100，最大 1000（硬限制，超出返回 422）",
    ),
]
OffsetQuery = Annotated[int, Query(ge=0, description="偏移量，配合 limit 翻页")]
ExcludeFrozenQuery = Annotated[
    bool,
    Query(
        description=(
            "是否排除“冻结候选”行（质量位 1）。默认 false：默认视图与库内原始数据完全一致，"
            "不静默丢弃质量异常记录"
        )
    ),
]

# --------------------------------------------------------------------------- #
# 知识库会话（阶段 2）
# --------------------------------------------------------------------------- #
def get_knowledge_session() -> Iterator[Session]:
    """知识库会话：连接 RAG 数据库（未单独配置时回退 DATABASE_URL）。

    知识检索在本阶段是**只读**操作，因此请求结束统一 rollback：
    不产生任何隐式写入，并立即释放快照。
    """
    from app.rag.config import get_rag_settings

    try:
        url = get_rag_settings().database_url
    except ValueError as exc:  # 配置缺失时给出明确错误，而不是静默连默认库
        raise RuntimeError(f"知识库配置错误：{exc}") from exc
    session = get_session_factory(url)()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    else:
        session.rollback()
    finally:
        session.close()


KnowledgeSessionDep = Annotated[Session, Depends(get_knowledge_session)]
