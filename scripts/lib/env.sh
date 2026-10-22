#!/usr/bin/env bash
# Shell 公共函数：配置加载与敏感信息脱敏。
#
# 设计原则（P1-B 修复的核心）：
#   配置优先级从高到低 = 命令行参数 > 调用方环境变量 > .env > 脚本内置默认值。
#   因此 `.env` 只允许**填充未设置的变量**，绝不覆盖调用方显式传入的值。
#   旧实现在 db.sh/migrate.sh 中使用 `set -a; source .env`，会静默覆盖调用方的
#   DATABASE_URL / TEST_DATABASE_URL，可能导致迁移落到错误的数据库。
#
# 用法：
#   source "$PROJECT_ROOT/scripts/lib/env.sh"
#   load_env_defaults "$PROJECT_ROOT/.env"

# shellcheck shell=bash

# 仅填充未设置的环境变量；已存在的变量保持调用方取值。
load_env_defaults() {
  local file="${1:-}"
  [[ -n "$file" && -f "$file" ]] || return 0

  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    # 跳过空行与注释
    [[ -z "${line//[[:space:]]/}" ]] && continue
    [[ "$line" == \#* ]] && continue
    [[ "$line" != *=* ]] && continue

    key="${line%%=*}"
    value="${line#*=}"
    key="${key//[[:space:]]/}"
    [[ -z "$key" ]] && continue

    # 去掉成对的包裹引号（.env 常见写法）
    if [[ ${#value} -ge 2 ]]; then
      if [[ "$value" == \"*\" || "$value" == \'*\' ]]; then
        value="${value:1:${#value}-2}"
      fi
    fi

    if [[ -z "${!key:-}" ]]; then
      export "$key=$value"
    fi
  done < "$file"
}

# 把连接串中的密码替换为 ***，用于日志与错误输出。
mask_url() {
  local url="${1:-}"
  if [[ -z "$url" ]]; then
    printf '%s' ""
    return 0
  fi
  if [[ "$url" =~ ^([A-Za-z0-9+.-]+://[^:/@]+):[^@]*@(.*)$ ]]; then
    printf '%s:***@%s' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
  else
    printf '%s' "$url"
  fi
}

# 从连接串中取出声明的数据库名（仅用于提示，真实身份以实际连接为准）。
url_database_name() {
  local url="${1:-}"
  local tail="${url##*/}"
  printf '%s' "${tail%%\?*}"
}

# 脱敏后输出“哪个库、哪个账号”，用于迁移前核对目标。
describe_url() {
  local url="${1:-}"
  printf '%s (db=%s)' "$(mask_url "$url")" "$(url_database_name "$url")"
}
