#!/bin/bash
# 初始化时启用 pgvector 扩展（Alembic 迁移也会再次幂等创建，二者不冲突）。
# 同时创建独立测试库，避免 pytest 触碰含真实数据的库（见 docs/phase1b_report.md）。
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
SQL

TEST_DB="${POSTGRES_TEST_DB:-industrial_agent_audit_test}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
SELECT 'CREATE DATABASE ${TEST_DB} OWNER ${POSTGRES_USER}'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = '${TEST_DB}')\gexec
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$TEST_DB" <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
SQL
