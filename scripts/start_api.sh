#!/usr/bin/env bash
# 启动 FastAPI 后端（不迁移数据库、不导入数据、不重建知识库）。
#
# 用法：
#   scripts/start_api.sh                 # 127.0.0.1:8000
#   scripts/start_api.sh --port 9000
#   scripts/start_api.sh --reload        # 开发热重载
#
# 配置：连接串等来自 .env（只填充未设置的变量）。脚本不做任何数据库写入。
set -euo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT/backend"

# shellcheck source=scripts/lib/env.sh
source "$PROJECT_ROOT/scripts/lib/env.sh"
load_env_defaults "$PROJECT_ROOT/.env"

PY_RUNNER=${PY_RUNNER:-conda run -n ms_pointcloud_midterm}
HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8000}
RELOAD=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --reload) RELOAD="--reload"; shift ;;
    -h|--help)
      sed -n '2,/^set -euo pipefail$/p' "$SCRIPT_PATH" | grep '^#' | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "缺少 DATABASE_URL：请检查项目根 .env（本项目不在脚本里提供默认连接串）" >&2
  exit 1
fi

cat <<EOF
[iaa] 启动后端（只读查询服务，不做迁移/导入）
[iaa] 监听 http://$HOST:$PORT
[iaa] 工业库：$(describe_url "${DATABASE_URL}")
[iaa] 知识库：$(describe_url "${RAG_DATABASE_URL:-$DATABASE_URL}")
[iaa] 审计库：$(describe_url "${AUDIT_DATABASE_URL:-}")
[iaa] 注意：未启用完整认证，请只在本机或隔离网络使用
EOF

# shellcheck disable=SC2086
exec $PY_RUNNER python -m uvicorn app.main:app --host "$HOST" --port "$PORT" $RELOAD
