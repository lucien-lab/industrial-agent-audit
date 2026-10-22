"""P2-2 修复：CHECK 约束名去重前缀，使数据库名与 ORM 元数据一致。

Revision ID: 0006_check_constraint_names
Revises: 0005_source_hash_identity
Create Date: 2026-10-22

背景：0001/0002 迁移在创建 CHECK 约束时传入的是**已带前缀**的名称，
而 Alembic 又按命名约定补了一次 `ck_<表名>_`，导致入库名为
`ck_data_gaps_ck_data_gaps_gap_end_after_start` 这类双前缀；其中两条还因
PostgreSQL 63 字符限制被截断并附加哈希（`..._match_2833`、`..._non_f743`）。
ORM 元数据中的名称是单前缀版本，两者不一致；`alembic check` 不比对 CHECK
约束，因此不会报警。后果是后续迁移若按 ORM 名称 drop/alter 约束会失败。

本迁移只做 `ALTER TABLE ... RENAME CONSTRAINT`，**不改变任何约束定义**，
也不触碰数据。改名逐条判断存在性，可安全重复执行。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_check_constraint_names"
down_revision: str | None = "0005_source_hash_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, old_name, new_name) —— new_name 与 ORM 元数据（命名约定生成）一致
RENAMES: tuple[tuple[str, str, str], ...] = (
    ("data_gaps", "ck_data_gaps_ck_data_gaps_gap_end_after_start", "ck_data_gaps_gap_end_after_start"),
    (
        "data_gaps",
        "ck_data_gaps_ck_data_gaps_missing_samples_positive",
        "ck_data_gaps_missing_samples_positive",
    ),
    (
        "data_sources",
        "ck_data_sources_ck_data_sources_file_sha256_hex",
        "ck_data_sources_file_sha256_hex",
    ),
    (
        "data_sources",
        "ck_data_sources_ck_data_sources_timezone_status_valid",
        "ck_data_sources_timezone_status_valid",
    ),
    ("fault_events", "ck_fault_events_ck_fault_events_end_after_start", "ck_fault_events_end_after_start"),
    (
        "fault_events",
        "ck_fault_events_ck_fault_events_verification_status_valid",
        "ck_fault_events_verification_status_valid",
    ),
    (
        "sensor_readings",
        "ck_sensor_readings_ck_sensor_readings_digital_signals_binary",
        "ck_sensor_readings_digital_signals_binary",
    ),
    (
        "sensor_readings",
        # 原名称 52 字符 + 双前缀后被截断为 63 字符并附加哈希
        "ck_sensor_readings_ck_sensor_readings_observed_at_match_2833",
        "ck_sensor_readings_observed_at_matches_source_text",
    ),
    (
        "sensor_readings",
        "ck_sensor_readings_ck_sensor_readings_quality_flags_non_f743",
        "ck_sensor_readings_quality_flags_non_negative",
    ),
)


def _rename(table: str, old: str, new: str) -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = '{table}'::regclass AND conname = '{old}'
            ) AND NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = '{table}'::regclass AND conname = '{new}'
            ) THEN
                EXECUTE format('ALTER TABLE {table} RENAME CONSTRAINT %I TO %I', '{old}', '{new}');
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    for table, old, new in RENAMES:
        _rename(table, old, new)

    # 0005 创建的状态约束名取决于命名约定是否生效（status_valid /
    # ck_ingest_runs_status_valid / 双前缀）。统一规范化为 ORM 名称。
    op.execute(
        """
        DO $$
        DECLARE c record;
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = 'ingest_runs'::regclass
                  AND conname = 'ck_ingest_runs_status_valid'
            ) THEN
                RETURN;
            END IF;
            FOR c IN
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'ingest_runs'::regclass
                  AND contype = 'c'
                  AND pg_get_constraintdef(oid) LIKE '%status%'
            LOOP
                EXECUTE format(
                    'ALTER TABLE ingest_runs RENAME CONSTRAINT %I TO ck_ingest_runs_status_valid',
                    c.conname
                );
                RETURN;
            END LOOP;
        END $$;
        """
    )


def downgrade() -> None:
    for table, old, new in RENAMES:
        _rename(table, new, old)
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = 'ingest_runs'::regclass AND conname = 'ck_ingest_runs_status_valid'
            ) THEN
                ALTER TABLE ingest_runs
                    RENAME CONSTRAINT ck_ingest_runs_status_valid TO status_valid;
            END IF;
        END $$;
        """
    )
