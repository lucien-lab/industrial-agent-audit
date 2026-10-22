"""覆盖度函数：时间口径改用已落库的 `missing_samples`，与 Phase 1A 定义对齐。

Revision ID: 0004_coverage_missing_time
Revises: 0003_coverage_time_ratio
Create Date: 2026-10-22

0003 的 `time_coverage_ratio` 直接累加缺口两端的时间差，会把“本应存在的
下一个采样点之前的 10 s”也算成缺失，导致 E3 区间得到 0.908，
而 Phase 1A 报告（按 `Σ(间隔 − 10 s)` 计算）为 0.9114。

缺失时长与 `data_gaps.missing_samples` 必须口径一致，否则同一个数据库里
“缺失样本数 × 步长”和“缺失时长”会互相矛盾。本迁移改为：

    missing_seconds = missing_samples × 标称步长
    time_coverage_ratio = 1 − 窗口内 missing_seconds / 窗口时长

跨窗口边界的缺口按交叠比例折算。函数仍为只读，不写入任何数据。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_coverage_missing_time"
down_revision: str | None = "0003_coverage_time_ratio"
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
               COALESCE(sum(
                   g.missing_samples * p_step_seconds *
                   LEAST(
                       EXTRACT(epoch FROM (LEAST(g.gap_end, p_end)
                                           - GREATEST(g.gap_start, p_start)))
                       / NULLIF(EXTRACT(epoch FROM (g.gap_end - g.gap_start)), 0),
                       1.0
                   )
               ), 0)::double precision AS missing_seconds_in_window
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
               THEN GREATEST(
                        1.0 - gaps.missing_seconds_in_window / expected.window_seconds,
                        0.0
                    )
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
'时间窗覆盖度。coverage_ratio=行数口径（实际/期望，按标称步长，封顶 1.0）；'
'time_coverage_ratio=时间口径（1 − Σmissing_samples×步长/窗口时长，跨边界按比例折算）；'
'gap_count/max_gap_seconds 来自 data_gaps 中与窗口交叠的缺口。';
"""


def upgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS sensor_coverage(uuid, timestamp, timestamp, double precision);"
    )
    op.execute(COVERAGE_FUNCTION_SQL)
    op.execute(COVERAGE_COMMENT_SQL)


def downgrade() -> None:
    # 恢复到 0003 的“整段缺口时长”口径
    op.execute(
        "DROP FUNCTION IF EXISTS sensor_coverage(uuid, timestamp, timestamp, double precision);"
    )
    op.execute(COVERAGE_V3_FUNCTION_SQL)


COVERAGE_V3_FUNCTION_SQL = """
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
           CASE WHEN expected.rows > 0
                THEN LEAST(observed.rows::double precision / expected.rows, 1.0)
                ELSE NULL END,
           CASE WHEN expected.window_seconds > 0
                THEN GREATEST(1.0 - gaps.gap_seconds_in_window / expected.window_seconds, 0.0)
                ELSE NULL END,
           gaps.gap_count,
           gaps.missing_samples,
           gaps.max_gap_seconds
    FROM observed, gaps, expected;
$$;
"""
