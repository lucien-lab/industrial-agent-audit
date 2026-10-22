#!/usr/bin/env bash
# 数据库生命周期入口：一条命令启动/停止 Phase 1B PostgreSQL(pgvector)。
#
# 用法：
#   scripts/db.sh up       启动数据库并等待健康检查通过
#   scripts/db.sh down     停止容器（保留 named volume 中的数据）
#   scripts/db.sh status   查看容器与健康状态
#   scripts/db.sh config   打印解析后的配置（密码脱敏，不启动容器）
#   scripts/db.sh logs     跟踪数据库日志
#   scripts/db.sh psql     进入 psql 交互终端
#
# 配置优先级（P1-B 修复）：
#   调用方环境变量 > .env（只填充未设置项） > 脚本内置默认值（不含密码）。
#   容器内的环境变量由脚本显式传给 compose，因此不会被 .env 静默覆盖。
#
# 执行引擎选择：本机 docker 可用则直接用；否则回退到已运行的 Lima 虚拟机内 docker。
# 回退只改变执行位置，compose 文件、卷、端口与环境变量完全一致。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# shellcheck source=scripts/lib/env.sh
source "$PROJECT_ROOT/scripts/lib/env.sh"
load_env_defaults "$PROJECT_ROOT/.env"

missing=()
for var in POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD; do
  if [[ -z "${!var:-}" ]]; then
    missing+=("$var")
  fi
done
if (( ${#missing[@]} > 0 )); then
  echo "缺少必需配置：${missing[*]}（请写入 .env 或通过环境变量提供；脚本不提供默认密码）" >&2
  exit 1
fi
POSTGRES_PORT="${POSTGRES_PORT:-5432}"

# compose 运行在虚拟机内，无法继承宿主机环境变量；这里生成仅当前用户可读的临时
# env 文件显式传递解析后的配置，退出时删除（避免把密码写进仓库或日志）。
COMPOSE_ENV_FILE="$PROJECT_ROOT/.env.compose.$$"
cleanup() { rm -f "$COMPOSE_ENV_FILE"; }
trap cleanup EXIT
( umask 077; cat > "$COMPOSE_ENV_FILE" <<EOF
POSTGRES_DB=$POSTGRES_DB
POSTGRES_USER=$POSTGRES_USER
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
POSTGRES_PORT=$POSTGRES_PORT
POSTGRES_TEST_DB=${POSTGRES_TEST_DB:-industrial_agent_audit_test}
EOF
)

LIMA_INSTANCE="${LIMA_INSTANCE:-dev}"
USE_LIMA=0

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  USE_LIMA=0
elif command -v limactl >/dev/null 2>&1 && limactl list -q "$LIMA_INSTANCE" >/dev/null 2>&1; then
  USE_LIMA=1
  echo "[db] 使用 Lima 实例 '$LIMA_INSTANCE' 内的 docker 引擎" >&2
else
  echo "找不到可用的 docker 引擎（本机 docker 或 Lima 实例 '$LIMA_INSTANCE'）。" >&2
  exit 1
fi

# 统一入口：把 docker 命令原样传给当前可用的引擎。
docker_cmd() {
  if [[ "$USE_LIMA" == "1" ]]; then
    local quoted=""
    local arg
    for arg in "$@"; do
      quoted+=" $(printf '%q' "$arg")"
    done
    # sg 保证非交互环境下也具备 docker 组权限
    limactl shell "$LIMA_INSTANCE" -- sg docker -c \
      "cd $(printf '%q' "$PROJECT_ROOT") && docker$quoted"
  else
    docker "$@"
  fi
}

compose() {
  docker_cmd compose --env-file "$COMPOSE_ENV_FILE" "$@"
}

wait_healthy() {
  local cid status
  cid="$(compose ps -q db | tr -d '[:space:]')"
  if [[ -z "$cid" ]]; then
    echo "[db] 未找到容器" >&2
    return 1
  fi
  for _ in $(seq 1 60); do
    status="$(docker_cmd inspect --format '{{.State.Health.Status}}' "$cid" 2>/dev/null | tr -d '[:space:]')"
    if [[ "$status" == "healthy" ]]; then
      echo "[db] healthy"
      return 0
    fi
    sleep 2
  done
  echo "[db] 健康检查超时（最后状态：${status:-unknown}），请查看 scripts/db.sh logs" >&2
  return 1
}

case "${1:-up}" in
  up)
    compose up -d
    wait_healthy
    ;;
  down)
    compose down
    ;;
  status)
    compose ps
    ;;
  config)
    echo "POSTGRES_DB=$POSTGRES_DB"
    echo "POSTGRES_USER=$POSTGRES_USER"
    echo "POSTGRES_PASSWORD=***"
    echo "POSTGRES_PORT=$POSTGRES_PORT"
    echo "DATABASE_URL=$(mask_url "${DATABASE_URL:-}")"
    echo "TEST_DATABASE_URL=$(mask_url "${TEST_DATABASE_URL:-}")"
    ;;
  logs)
    compose logs -f --tail=50 db
    ;;
  psql)
    compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
    ;;
  *)
    echo "未知子命令：$1（可用 up/down/status/config/logs/psql）" >&2
    exit 2
    ;;
esac
