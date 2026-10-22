"""P1-A/P2-4 修复：来源内容哈希唯一 + 导入状态支持 partial。

Revision ID: 0005_source_hash_identity
Revises: 0004_coverage_missing_time
Create Date: 2026-10-22

背景（Phase 1B 独立验收 P1-A）：
    原唯一键 `(source_id, source_row_number)` 中 source_id 由 `(source_file, file_sha256)`
    决定，文件名进入身份。字节完全相同、仅文件名不同的文件会被登记成第二个来源，
    导致同一批观测重复入库（实测 10,000 行 → 20,000 行）。

本迁移提供**数据库层硬保证**：
    1. `data_sources` 增加 UNIQUE(file_sha256)——同一内容只能有一个来源；
    2. `ingest_runs.status` 允许 `partial`，用于表达“部分成功”（P2-4）。

执行前会检查是否已存在重复 SHA-256：存在则中止并提示人工合并，绝不自动删除数据。
本迁移只新增约束/放宽取值域，不修改任何观测数据。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_source_hash_identity"
down_revision: str | None = "0004_coverage_missing_time"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_HASH_CONSTRAINT = "uq_data_sources_content_sha256"
# 注意：Alembic 会对 create_check_constraint 的名称再套一次命名约定
# （ck_<table>_<name>），因此这里必须传**短名**，否则入库会变成双前缀。
STATUS_CONSTRAINT_NAME = "status_valid"
STATUS_CONSTRAINT = "ck_ingest_runs_status_valid"
STATUS_VALUES = "('running', 'succeeded', 'failed', 'partial', 'dry_run')"
OLD_STATUS_VALUES = "('running', 'succeeded', 'failed', 'dry_run')"


def upgrade() -> None:
    bind = op.get_bind()

    duplicates = bind.execute(
        sa.text(
            "SELECT file_sha256, count(*) AS n, array_agg(source_file) AS files"
            " FROM data_sources GROUP BY file_sha256 HAVING count(*) > 1"
        )
    ).fetchall()
    if duplicates:
        detail = "; ".join(
            f"sha256={row.file_sha256[:12]}… 出现 {row.n} 次（{row.files}）" for row in duplicates
        )
        raise RuntimeError(
            "data_sources 中存在相同 SHA-256 的重复来源，无法安全建立唯一约束。"
            f" 请先人工确认并合并（不会自动删除）：{detail}"
        )

    op.create_unique_constraint(SOURCE_HASH_CONSTRAINT, "data_sources", ["file_sha256"])

    # 状态的 CHECK 约束在 0001 中以完整名称创建，实际入库名带重复前缀；
    # 这里按定义动态查找，兼容不同的历史命名。
    op.execute(
        """
        DO $$
        DECLARE c record;
        BEGIN
            FOR c IN
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'ingest_runs'::regclass
                  AND contype = 'c'
                  AND pg_get_constraintdef(oid) LIKE '%status%'
            LOOP
                EXECUTE format('ALTER TABLE ingest_runs DROP CONSTRAINT %I', c.conname);
            END LOOP;
        END $$;
        """
    )
    op.create_check_constraint(STATUS_CONSTRAINT_NAME, "ingest_runs", f"status IN {STATUS_VALUES}")


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE c record;
        BEGIN
            FOR c IN
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'ingest_runs'::regclass
                  AND contype = 'c'
                  AND pg_get_constraintdef(oid) LIKE '%status%'
            LOOP
                EXECUTE format('ALTER TABLE ingest_runs DROP CONSTRAINT %I', c.conname);
            END LOOP;
        END $$;
        """
    )
    op.create_check_constraint(STATUS_CONSTRAINT_NAME, "ingest_runs", f"status IN {OLD_STATUS_VALUES}")
    op.drop_constraint(SOURCE_HASH_CONSTRAINT, "data_sources", type_="unique")
