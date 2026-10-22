"""数据库连接管理。

- 引擎与会话工厂按 URL 缓存，测试中可用 `dispose_engines()` 清理。
- 默认开启 `pool_pre_ping`，避免容器重启后拿到失效连接。
- 不在此处做任何“启动即建表”：结构一律由 Alembic 迁移管理。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_ENGINES: dict[str, Engine] = {}
_SESSION_FACTORIES: dict[str, sessionmaker[Session]] = {}


def get_engine(url: str | None = None) -> Engine:
    """按连接串获取（并缓存）引擎。"""
    target = url or get_settings().database_url
    engine = _ENGINES.get(target)
    if engine is None:
        engine = create_engine(
            target,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            future=True,
            # 让 psycopg3 以 TCP 方式连接并保留服务器时区语义；
            # 源时间使用 timestamp without time zone，不受此参数影响。
            connect_args={"application_name": "industrial_agent_audit"},
        )
        _ENGINES[target] = engine
    return engine


def get_session_factory(url: str | None = None) -> sessionmaker[Session]:
    target = url or get_settings().database_url
    factory = _SESSION_FACTORIES.get(target)
    if factory is None:
        factory = sessionmaker(bind=get_engine(target), expire_on_commit=False)
        _SESSION_FACTORIES[target] = factory
    return factory


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    """事务性会话：异常自动回滚，正常退出提交。"""
    session = get_session_factory(url)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_engines() -> None:
    """释放所有缓存引擎（测试与脚本收尾使用）。"""
    for engine in _ENGINES.values():
        engine.dispose()
    _ENGINES.clear()
    _SESSION_FACTORIES.clear()


def check_connection(url: str | None = None) -> dict[str, object]:
    """连通性自检，返回服务端版本、库名、pgvector 是否可用。"""
    if url:
        engine = create_engine(url, pool_pre_ping=True, future=True)
    else:
        engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT current_database() AS db, current_user AS usr,"
                " current_setting('server_version') AS version,"
                " (SELECT count(*) FROM pg_available_extensions WHERE name = 'vector')"
                " AS vector_available"
            )
        ).one()
    return {
        "database": row.db,
        "user": row.usr,
        "server_version": row.version,
        "pgvector_available": bool(row.vector_available),
    }
