"""SQLAlchemy 声明式基类。

所有 ORM 模型都在 `app.models` 中注册到同一个 `Base.metadata`，
Alembic 的 env.py 通过该 metadata 做迁移比较。
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# 显式命名约定：让迁移脚本里的约束名稳定、可预测，便于后续手工编写迁移。
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
