"""覆盖度函数补充“时间覆盖率”。

Revision ID: 0003_coverage_time_ratio
Revises: 0002_quality_objects
Create Date: 2026-10-22

两种覆盖率含义不同，必须同时提供，避免用一个数字掩盖另一种口径：

- `coverage_ratio`      = 实际行数 / 期望行数（按标称 10 s 步长的行数口径，封顶 1.0）
- `time_coverage_ratio` = 1 − 窗口内缺口时长 / 窗口时长（时间口径，与 Phase 1A 报告一致）

二者在存在时钟抖动时不相等：例如 E3 区间行数为 17,315、期望 18,901，
行覆盖率 0.9161，而时间覆盖率 0.9114（Phase 1A 报告口径）。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_coverage_time_ratio"
down_revision: str | None = "0002_quality_objects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COVERAGE_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION sensor_coverage(
    p_equipment_id uuid,
    p_start timestamp without time zone,
    p_end timestamp without time zone,
    p_step_seconds double precision DEFAULT 10.0
)
RETURNS TABLE (
    observed_rows bigint,
    expected_rows bigint,
    coverage_ratio double precision,
    time_coverage_ratio double precision,
    gap_count bigint,
    missing_samples bigint,
    max_gap_seconds double precision
)
LANGUAGE sql
STABLE
AS $$
    WITH observed AS (
        SELECT count(*)::bigint AS rows
        FROM sensor_readings r
        WHERE r.equipment_id = p_equipment_id
          AND r.observed_at >= p_start
          AND r.observed_at <= p_end
    ),
    gaps AS (
        SELECT count(*)::bigint AS gap_count,
               COALESCE(sum(g.missing_samples), 0)::bigint AS missing_samples,
               COALESCE(max(g.gap_seconds), 0)::double precision AS max_gap_seconds,
               -- 只统计落在窗口内的那部分缺口时长，跨边界缺口按裁剪计算
               COALESCE(sum(
                   EXTRACT(epoch FROM (
                       LEAST(g.gap_end, p_end) - GREATEST(g.gap_start, p_start)
                   ))
               ), 0)::double precision AS gap_seconds_in_window
        FROM data_gaps g
        WHERE g.equipment_id = p_equipment_id
          AND g.gap_start < p_end
          AND g.gap_end > p_start
    ),
    expected AS (
        SELECT GREATEST(
                   floor(EXTRACT(epoch FROM (p_end - p_start)) / p_step_seconds)::bigint + 1,
                   0
               ) AS rows,
               EXTRACT(epoch FROM (p_end - p_start))::double precision AS window_seconds
    )
    SELECT observed.rows,
           expected.rows,
           CASE
               WHEN expected.rows > 0
               THEN LEAST(observed.rows::double precision / expected.rows, 1.0)
               ELSE NULL
           END,
           CASE
               WHEN expected.window_seconds > 0
               THEN GREATEST(1.0 - gaps.gap_seconds_in_window / expected.window_seconds, 0.0)
               ELSE NULL
           END,
           gaps.gap_count,
           gaps.missing_samples,
           gaps.max_gap_seconds
    FROM observed, gaps, expected;
$$;
"""

COVERAGE_COMMENT_SQL = """
COMMENT ON FUNCTION sensor_coverage(uuid, timestamp, timestamp, double precision) IS
'时间窗覆盖度：coverage_ratio=行数口径（实际/期望，按标称步长，封顶 1.0）；'
'time_coverage_ratio=时间口径（1-窗口内缺口时长/窗口时长）；'
'gap_count/max_gap_seconds 来自 data_gaps 中与窗口交叠的缺口。';
"""


def upgrade() -> None:
    # 返回列发生变化，必须先删除再重建
    op.execute(
        "DROP FUNCTION IF EXISTS sensor_coverage(uuid, timestamp, timestamp, double precision);"
    )
    op.execute(COVERAGE_FUNCTION_SQL)
    op.execute(COVERAGE_COMMENT_SQL)


def downgrade() -> None:
    """恢复 0002 的 4 列版本（时间覆盖率列不存在）。"""
    op.execute(
        "DROP FUNCTION IF EXISTS sensor_coverage(uuid, timestamp, timestamp, double precision);"
    )
    op.execute(COVERAGE_V2_FUNCTION_SQL)
    op.execute(
        "COMMENT ON FUNCTION sensor_coverage(uuid, timestamp, timestamp, double precision) IS "
        "'时间窗覆盖度（行数口径）。';"
    )


COVERAGE_V2_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION sensor_coverage(
    p_equipment_id uuid,
    p_start timestamp without time zone,
    p_end timestamp without time zone,
    p_step_seconds double precision DEFAULT 10.0
)
RETURNS TABLE (
    observed_rows bigint,
    expected_rows bigint,
    coverage_ratio double precision,
    gap_count bigint,
    missing_samples bigint,
    max_gap_seconds double precision
)
LANGUAGE sql
STABLE
AS $$
    WITH observed AS (
        SELECT count(*)::bigint AS rows
        FROM sensor_readings r
        WHERE r.equipment_id = p_equipment_id
          AND r.observed_at >= p_start
          AND r.observed_at <= p_end
    ),
    gaps AS (
        SELECT count(*)::bigint AS gap_count,
               COALESCE(sum(g.missing_samples), 0)::bigint AS missing_samples,
               COALESCE(max(g.gap_seconds), 0)::double precision AS max_gap_seconds
        FROM data_gaps g
        WHERE g.equipment_id = p_equipment_id
          AND g.gap_start < p_end
          AND g.gap_end > p_start
    ),
    expected AS (
        SELECT GREATEST(
                   floor(EXTRACT(epoch FROM (p_end - p_start)) / p_step_seconds)::bigint + 1,
                   0
               ) AS rows
    )
    SELECT observed.rows,
           expected.rows,
           CASE
               WHEN expected.rows > 0
               THEN LEAST(observed.rows::double precision / expected.rows, 1.0)
               ELSE NULL
           END,
           gaps.gap_count,
           gaps.missing_samples,
           gaps.max_gap_seconds
    FROM observed, gaps, expected;
$$;
"""
