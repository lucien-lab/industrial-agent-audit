"""Alembic 运行环境。

连接来源优先级（禁止硬编码密码）：
1. `-x db_url=...`（测试与 CI 显式指定隔离库）
2. 环境变量 `ALEMBIC_DATABASE_URL`
3. `.env` / 环境变量中的 `DATABASE_URL`
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 允许 `alembic` 在任意工作目录下执行
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import ConfigError, get_settings  # noqa: E402
from app.models import Base  # noqa: E402  （导入即注册全部模型）

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    x_args = context.get_x_argument(as_dictionary=True)
    if x_args.get("db_url"):
        return x_args["db_url"]
    import os

    explicit = os.environ.get("ALEMBIC_DATABASE_URL", "").strip()
    if explicit:
        return explicit
    try:
        return get_settings().database_url
    except ConfigError as exc:  # 明确报错，不静默回退
        raise SystemExit(f"[alembic] {exc}") from exc


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
