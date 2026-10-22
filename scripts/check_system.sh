#!/usr/bin/env bash
# 只读系统检查：后端状态 + 各数据库迁移版本。
#
# 用法：
#   scripts/check_system.sh             # 快速检查
#   scripts/check_system.sh --deep      # 额外真实调用一次 Embedding 与 LLM
#
# 安全：只读。不迁移数据库、不写入任何表、不打印连接串密码。
set -euo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT/backend"

# shellcheck source=scripts/lib/env.sh
source "$PROJECT_ROOT/scripts/lib/env.sh"
load_env_defaults "$PROJECT_ROOT/.env"

PY_RUNNER=${PY_RUNNER:-conda run -n ms_pointcloud_midterm}
BASE_URL=${IAA_API_BASE_URL:-http://127.0.0.1:8000}
CHECK_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --deep) CHECK_ARGS+=("--deep") ;;
    -h|--help)
      sed -n '2,/^set -euo pipefail$/p' "$SCRIPT_PATH" | grep '^#' | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

echo "== 后端 API =="
if curl -fsS -m 5 "$BASE_URL/health" >/dev/null 2>&1; then
  echo "API ok：$BASE_URL"
else
  echo "API 不可用：$BASE_URL（请先运行 scripts/start_api.sh）" >&2
fi

echo
echo "== 服务与数据库状态（真实探测）=="
# shellcheck disable=SC2086
$PY_RUNNER python -m app.tui --check --base-url "$BASE_URL" "${CHECK_ARGS[@]+"${CHECK_ARGS[@]}"}" || echo "存在不可用服务（返回码非 0）" >&2

echo
echo "== 各库 Alembic 版本（只读查询）=="
VERSION_SNIPPET="$(mktemp -t iaa_versions.XXXXXX.py)"
trap 'rm -f "$VERSION_SNIPPET"' EXIT
cat > "$VERSION_SNIPPET" <<'PYTHON'
import os

import psycopg

targets = [
    ("工业数据", os.environ.get("DATABASE_URL")),
    ("RAG 知识库", os.environ.get("RAG_DATABASE_URL")),
    ("审计库", os.environ.get("AUDIT_DATABASE_URL")),
    ("测试库", os.environ.get("TEST_DATABASE_URL")),
]
for label, url in targets:
    if not url:
        print(f"{label:<10} 未配置")
        continue
    dsn = url.replace("postgresql+psycopg://", "postgresql://")
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            row = conn.execute("select version_num from alembic_version").fetchone()
            db = conn.execute("select current_database()").fetchone()[0]
            print(f"{label:<10} {db:<32} {row[0] if row else '(无 alembic_version)'}")
    except Exception as exc:  # 检查脚本只报告，不抛堆栈
        print(f"{label:<10} 不可用：{type(exc).__name__}")
PYTHON
# shellcheck disable=SC2086
$PY_RUNNER python "$VERSION_SNIPPET"
