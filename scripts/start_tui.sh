#!/usr/bin/env bash
# 启动 Textual TUI（连接已经运行的后端；不会启动/迁移数据库，也不会重建知识库）。
#
# 用法：
#   scripts/start_tui.sh                        # 连接 http://127.0.0.1:8000
#   scripts/start_tui.sh --base-url http://127.0.0.1:9000
#   IAA_AUDIT_TOKEN=xxx scripts/start_tui.sh    # 审计接口启用共享令牌时
#
# 优先使用已安装的 `iaa-tui` 入口；没有安装时回退到 `python -m app.tui`。
set -euo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT/backend"

# shellcheck source=scripts/lib/env.sh
source "$PROJECT_ROOT/scripts/lib/env.sh"
load_env_defaults "$PROJECT_ROOT/.env"

PY_RUNNER=${PY_RUNNER:-conda run -n ms_pointcloud_midterm}
BASE_URL=${IAA_API_BASE_URL:-http://127.0.0.1:8000}

args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-url) BASE_URL="$2"; args+=("--base-url" "$2"); shift 2 ;;
    -h|--help)
      sed -n '2,/^set -euo pipefail$/p' "$SCRIPT_PATH" | grep '^#' | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) args+=("$1"); shift ;;
  esac
done

if ! curl -fsS -m 3 "$BASE_URL/health" >/dev/null 2>&1; then
  echo "[iaa] 无法连接后端 $BASE_URL —— 请先运行：scripts/start_api.sh" >&2
  exit 2
fi

if $PY_RUNNER sh -c 'command -v iaa-tui' >/dev/null 2>&1; then
  # shellcheck disable=SC2086
  exec $PY_RUNNER iaa-tui --base-url "$BASE_URL" "${args[@]+"${args[@]}"}"
fi

echo "[iaa] 未找到 iaa-tui 入口，回退到 python -m app.tui（如需安装：pip install -e backend）" >&2
# shellcheck disable=SC2086
exec $PY_RUNNER python -m app.tui --base-url "$BASE_URL" "${args[@]+"${args[@]}"}"
