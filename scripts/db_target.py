#!/usr/bin/env python
"""只读校验数据库目标：连接串声明 ≠ 实际连上的库时明确失败。

用途：迁移/回滚前确认“即将操作的是哪个数据库、哪个账号”，避免：
- 调用方的 DATABASE_URL 被 .env 覆盖后迁移到错误数据库（P1-B）；
- 连到不存在的库时静默回退到其它库；
- 日志里泄露密码。

退出码：
  0  连接成功且身份一致
  2  参数错误
  3  无法连接（库不存在 / 认证失败 / 网络不可达）
  4  连接到了非预期数据库

输出：JSON（默认）或单个字段（--print database）。

用法：
    python scripts/db_target.py --url "$DATABASE_URL"
    python scripts/db_target.py --url "$TEST_DATABASE_URL" --expect-suffix _test --print database
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlsplit

DRIVER_PREFIXES = ("postgresql+psycopg://", "postgresql+psycopg2://")


def mask_url(url: str) -> str:
    """把 `scheme://user:password@host` 的密码替换为 ***。"""
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    credentials, host = rest.rsplit("@", 1)
    if ":" not in credentials:
        return url
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


def to_libpq(url: str) -> str:
    for prefix in DRIVER_PREFIXES:
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    return url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读校验数据库连接目标")
    parser.add_argument("--url", required=True)
    parser.add_argument("--expect-database", default=None)
    parser.add_argument(
        "--expect-suffix", default=None, help="要求实际库名以该后缀结尾（例如 _test）"
    )
    parser.add_argument("--print", dest="output", choices=("json", "database"), default="json")
    parser.add_argument("--timeout", type=int, default=8)
    args = parser.parse_args(argv)

    libpq_url = to_libpq(args.url)
    declared_db = urlsplit(libpq_url).path.lstrip("/") or None

    try:
        import psycopg
    except ImportError:  # pragma: no cover - 依赖缺失时给出明确提示
        print("db_target: 缺少 psycopg，请在项目 Conda 环境中运行", file=sys.stderr)
        return 2

    try:
        with psycopg.connect(libpq_url, connect_timeout=args.timeout) as conn:
            row = conn.execute(
                "SELECT current_database(), current_user,"
                " current_setting('server_version')"
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 - 任何连接失败都必须明确暴露
        message = {
            "ok": False,
            "error": type(exc).__name__,
            "message": str(exc),
            "url": mask_url(args.url),
            "declared_database": declared_db,
        }
        if args.output == "database":
            print(json.dumps(message, ensure_ascii=False), file=sys.stderr)
        else:
            print(json.dumps(message, ensure_ascii=False))
        return 3

    database, user, version = row
    ok = True
    error = None
    if declared_db and declared_db != database:
        ok = False
        error = "database_mismatch"
    if args.expect_database and args.expect_database != database:
        ok = False
        error = "expect_database_failed"
    if args.expect_suffix and not str(database).endswith(args.expect_suffix):
        ok = False
        error = "expect_suffix_failed"

    result = {
        "ok": ok,
        "error": error,
        "database": database,
        "user": user,
        "server_version": version,
        "declared_database": declared_db,
        "url": mask_url(args.url),
    }

    if args.output == "database":
        if ok:
            print(database, end="")
        else:
            print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0 if ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
