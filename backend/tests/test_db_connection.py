"""数据库连接、扩展与迁移结构测试。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

from app.core.config import mask_password
from app.db.session import check_connection

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_connection_and_version(database_url: str) -> None:
    info = check_connection(database_url)
    assert info["pgvector_available"] is True
    assert info["server_version"].startswith("17")
    assert "password" not in str(info)


def test_extension_installed(engine) -> None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).one()
    assert row[0].startswith("0.")


def test_expected_tables_and_indexes(engine) -> None:
    with engine.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
            )
        }
        indexes = {
            r[0]
            for r in conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
            )
        }
    assert set(
        {
            "equipment",
            "data_sources",
            "sensor_readings",
            "fault_events",
            "ingest_runs",
            "data_gaps",
        }
    ) <= tables
    assert "uq_sensor_readings_source_row" in indexes
    assert "ix_sensor_readings_equipment_observed_at" in indexes
    assert "ix_sensor_readings_quality_flags" in indexes


def test_unique_constraint_on_source_row(engine) -> None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes"
                " WHERE indexname = 'uq_sensor_readings_source_row'"
            )
        ).one()
    assert "UNIQUE" in row[0]
    assert "source_id" in row[0] and "source_row_number" in row[0]


def test_alembic_head_is_current(database_url: str) -> None:
    import os

    env = os.environ.copy()
    env["ALEMBIC_DATABASE_URL"] = database_url
    current = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    heads = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    head_revision = heads.stdout.split()[0]
    assert head_revision in current.stdout


def test_models_match_migrations(database_url: str) -> None:
    """`alembic check` 必须报告无待生成迁移（模型与迁移不漂移）。"""
    import os

    env = os.environ.copy()
    env["ALEMBIC_DATABASE_URL"] = database_url
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "No new upgrade operations detected" in completed.stdout


def test_constraint_names_match_orm_metadata(engine) -> None:
    """P2-2 回归：数据库约束名必须与 ORM 元数据一致，且不允许双前缀。

    Alembic 的 `check` 不比对 CHECK 约束，因此这里独立断言。
    """
    from app.models import Base

    model = {
        (table.name, constraint.name)
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if constraint.name
    }
    with engine.connect() as conn:
        db = {
            (row[0], row[1])
            for row in conn.execute(
                text(
                    "SELECT conrelid::regclass::text, conname FROM pg_constraint"
                    " WHERE connamespace = 'public'::regnamespace"
                )
            )
        }
    missing = sorted(model - db)
    assert missing == [], f"ORM 元数据中的约束在数据库里不存在：{missing}"

    doubled = sorted(name for _table, name in db if name.count("ck_") > 1)
    assert doubled == [], f"存在双前缀 CHECK 约束名：{doubled}"


def test_ingest_status_allows_partial(engine) -> None:
    """P2-4 回归：ingest_runs.status 必须允许 partial。"""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conrelid = 'ingest_runs'::regclass AND contype = 'c'"
                " AND pg_get_constraintdef(oid) LIKE '%status%'"
            )
        ).fetchone()
    assert row is not None
    assert "partial" in row[0]


def test_mask_password_on_settings() -> None:
    from app.core.config import get_settings

    settings = get_settings()
    assert "***" in settings.safe_database_url or "@" not in settings.database_url
