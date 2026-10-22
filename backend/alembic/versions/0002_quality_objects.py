"""覆盖度函数与冻结段视图。

Revision ID: 0002_quality_objects
Revises: 0001_initial_schema
Create Date: 2026-10-22

这些对象是只读派生视图/函数，不存储新事实：
- `sensor_coverage()`：给定设备与时间窗，返回实际行数、期望行数、覆盖率、
  缺口数与最大缺口时长。覆盖率按标称步长 10 s（实测众数）计算并封顶 1.0，
  避免时钟抖动导致“覆盖率 > 100%”的假象。
- `sensor_frozen_segments`：把行级 `frozen_candidate` 标记聚合成连续区间，
  供查询与审计举证使用；不修改、不删除任何原始行。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_quality_objects"
down_revision: str | None = "0001_initial_schema"
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
          -- 与窗口有交叠即计入（跨边界的缺口也是该窗口的真实缺陷）
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

COVERAGE_COMMENT_SQL = """
COMMENT ON FUNCTION sensor_coverage(uuid, timestamp, timestamp, double precision) IS
'时间窗覆盖度：observed_rows/expected_rows 按标称步长计算（默认 10 s），'
'cap 到 1.0；gap_count/max_gap_seconds 来自 data_gaps 中与窗口交叠的缺口。';
"""

FROZEN_VIEW_SQL = """
CREATE OR REPLACE VIEW sensor_frozen_segments AS
WITH flagged AS (
    SELECT r.equipment_id,
           r.source_id,
           r.source_row_number,
           r.observed_at,
           row_number() OVER (
               PARTITION BY r.equipment_id ORDER BY r.source_row_number
           ) AS seq
    FROM sensor_readings r
    WHERE (r.quality_flags & 1) = 1
),
grouped AS (
    -- 本数据集的源行号步长恒为 10（实测 1,516,948 行全部成立），
    -- 因此 source_row_number - 10*seq 在连续的冻结段内恒定，
    -- 一旦中间夹了非冻结行即改变，可用于切分区间。
    SELECT equipment_id,
           source_id,
           source_row_number,
           observed_at,
           source_row_number - 10 * seq AS island
    FROM flagged
)
SELECT equipment_id,
       source_id,
       min(observed_at) AS segment_start,
       max(observed_at) AS segment_end,
       count(*)::bigint AS row_count,
       min(source_row_number) AS first_source_row,
       max(source_row_number) AS last_source_row
FROM grouped
GROUP BY equipment_id, source_id, island;
"""

FROZEN_VIEW_COMMENT_SQL = """
COMMENT ON VIEW sensor_frozen_segments IS
'由 sensor_readings.quality_flags 的 frozen_candidate 位聚合出的连续冻结候选区间；'
'判据 frozen-v1（7 路模拟量与前一采样逐位相同），只做标记，不删除原始行。';
"""


def upgrade() -> None:
    op.execute(COVERAGE_FUNCTION_SQL)
    op.execute(COVERAGE_COMMENT_SQL)
    op.execute(FROZEN_VIEW_SQL)
    op.execute(FROZEN_VIEW_COMMENT_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS sensor_frozen_segments;")
    op.execute(
        "DROP FUNCTION IF EXISTS sensor_coverage(uuid, timestamp, timestamp, double precision);"
    )
