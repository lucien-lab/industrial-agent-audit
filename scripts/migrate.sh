#!/usr/bin/env bash
# Alembic 迁移入口（P1-B 修复版）。
#
# 子命令：
#   scripts/migrate.sh up            升级主库到 head
#   scripts/migrate.sh up-test       升级测试库到 head
#   scripts/migrate.sh current       查看主库当前版本
#   scripts/migrate.sh current-test  查看测试库当前版本
#   scripts/migrate.sh check         校验模型与迁移是否漂移（不写库）
#   scripts/migrate.sh heads         列出迁移头（不连数据库）
#   scripts/migrate.sh target        只读打印“将要连接的数据库/账号”，不做迁移
#   scripts/migrate.sh down-test     测试库回滚一步（默认拒绝，需显式允许）
#
# 配置优先级（从高到低）：
#   1) 命令行参数          scripts/migrate.sh up --url postgresql+psycopg://...
#   2) 调用方环境变量      DATABASE_URL=... scripts/migrate.sh up
#   3) 项目根 .env         （仅填充未设置的变量，绝不覆盖上面两级）
#   4) 脚本内置默认值      除 PY_RUNNER 外，不提供任何数据库默认值（缺失即报错）
#
# 安全约束：
#   - 连不上目标库时**明确失败**，不回退到其它数据库；
#   - 执行迁移前用真实连接校验“声明库名 == 实际库名”；
#   - 回滚类操作默认拒绝：需 ALLOW_TEST_ROLLBACK=1，且**实际连接**的库名以 _test 结尾；
#   - 输出连接串时统一脱敏，不打印密码。
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# shellcheck source=scripts/lib/env.sh
source "$PROJECT_ROOT/scripts/lib/env.sh"
load_env_defaults "$PROJECT_ROOT/.env"

PY_RUNNER=${PY_RUNNER:-conda run -n ms_pointcloud_midterm}
TARGET_HELPER="$PROJECT_ROOT/scripts/db_target.py"

command_name="${1:-up}"
shift || true

CLI_URL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --url)
      CLI_URL="${2:-}"
      [[ -z "$CLI_URL" ]] && { echo "--url 需要一个连接串" >&2; exit 2; }
      shift 2
      ;;
    *)
      echo "未知参数：$1" >&2
      exit 2
      ;;
  esac
done

die() {
  echo "[migrate] $*" >&2
  exit 1
}

# 解析要操作的连接串：命令行 > 环境变量（已由 load_env_defaults 保证不被 .env 覆盖）。
resolve_url() {
  local kind="$1" # main | test
  if [[ -n "$CLI_URL" ]]; then
    printf '%s' "$CLI_URL"
    return 0
  fi
  if [[ "$kind" == "test" ]]; then
    if [[ -z "${TEST_DATABASE_URL:-}" ]]; then
      echo "[migrate] 缺少 TEST_DATABASE_URL（命令行 --url / 环境变量 / .env 均未提供）" >&2
      return 1
    fi
    printf '%s' "$TEST_DATABASE_URL"
  else
    if [[ -z "${DATABASE_URL:-}" ]]; then
      echo "[migrate] 缺少 DATABASE_URL（命令行 --url / 环境变量 / .env 均未提供）" >&2
      return 1
    fi
    printf '%s' "$DATABASE_URL"
  fi
}

# 用真实连接校验目标身份，输出实际库名；失败时明确退出（不回退）。
verify_target() {
  local url="$1" out db
  out="$($PY_RUNNER python "$TARGET_HELPER" --url "$url" 2>/dev/null || true)"
  db="$(printf '%s' "$out" | sed -n 's/.*"database": "\([^"]*\)".*/\1/p')"
  if [[ -z "$db" || "$out" == *'"ok": false'* ]]; then
    echo "[migrate] 无法连接目标数据库或身份不符，已中止（不会尝试其它库）。" >&2
    echo "[migrate] 目标：$(mask_url "$url")" >&2
    [[ -n "$out" ]] && echo "[migrate] 原因：$out" >&2
    return 3
  fi
  printf '%s' "$db"
}

run_alembic() {
  # alembic.ini 位于 backend/，script_location 为相对路径，必须在 backend/ 下执行
  # shellcheck disable=SC2086
  ( cd "$PROJECT_ROOT/backend" && $PY_RUNNER alembic "$@" )
}

alembic_on() {
  local url="$1"
  shift
  ALEMBIC_DATABASE_URL="$url" run_alembic "$@"
}

report_target() {
  local label="$1" db="$2" url="$3"
  echo "[migrate] ${label}：database=${db} url=$(mask_url "$url")"
}

require_dangerous_guard() {
  local actual_db="$1"
  if [[ "${ALLOW_TEST_ROLLBACK:-}" != "1" ]]; then
    echo "[migrate] 拒绝执行回滚：危险操作默认拒绝，需显式设置 ALLOW_TEST_ROLLBACK=1。" >&2
    exit 5
  fi
  if [[ "$actual_db" != *_test ]]; then
    echo "[migrate] 拒绝执行回滚：实际连接的数据库 '$actual_db' 不以 _test 结尾。" >&2
    exit 5
  fi
}

case "$command_name" in
  heads)
    run_alembic heads
    ;;
  target)
    url="$(resolve_url main)" || exit 2
    db="$(verify_target "$url")" || exit $?
    report_target "target" "$db" "$url"
    ;;
  up)
    url="$(resolve_url main)" || exit 2
    db="$(verify_target "$url")" || exit $?
    report_target "upgrade" "$db" "$url"
    alembic_on "$url" upgrade head
    ;;
  up-test)
    url="$(resolve_url test)" || exit 2
    db="$(verify_target "$url")" || exit $?
    if [[ "$db" != *_test ]]; then
      echo "[migrate] 注意：目标 '$db' 不以 _test 结尾，但 up-test 只做升级、不做回滚。" >&2
    fi
    report_target "upgrade-test" "$db" "$url"
    alembic_on "$url" upgrade head
    ;;
  current)
    url="$(resolve_url main)" || exit 2
    db="$(verify_target "$url")" || exit $?
    report_target "current" "$db" "$url"
    alembic_on "$url" current
    ;;
  current-test)
    url="$(resolve_url test)" || exit 2
    db="$(verify_target "$url")" || exit $?
    report_target "current-test" "$db" "$url"
    alembic_on "$url" current
    ;;
  check)
    url="$(resolve_url main)" || exit 2
    db="$(verify_target "$url")" || exit $?
    report_target "check" "$db" "$url"
    alembic_on "$url" check
    ;;
  down-test)
    url="$(resolve_url test)" || exit 2
    db="$(verify_target "$url")" || exit $?
    require_dangerous_guard "$db"
    report_target "downgrade-test" "$db" "$url"
    alembic_on "$url" downgrade -1
    ;;
  *)
    echo "未知子命令：$command_name" >&2
    echo "可用：up | up-test | current | current-test | check | heads | target | down-test" >&2
    exit 2
    ;;
esac
