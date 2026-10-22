"""`iaa-tui` 命令行入口。

用法：

```bash
iaa-tui                          # 连接 http://127.0.0.1:8000
iaa-tui --base-url http://127.0.0.1:9000
python -m app.tui --check        # 只做一次状态探测并退出（脚本/CI 用）
```

启动过程**不会**迁移数据库、重建知识库或重新导入数据；它只连接已经运行的后端。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import httpx

from app.tui import __version__
from app.tui.client import DEFAULT_BASE_URL, ApiError, IaaApiClient
from app.tui.status import ComponentState, SystemStatus, probe_system_status

ENV_BASE_URL = "IAA_API_BASE_URL"
ENV_AUDIT_TOKEN = "IAA_AUDIT_TOKEN"

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _load_project_env() -> None:
    """读取项目根 `.env`（不覆盖已有环境变量），让 IAA_API_BASE_URL 等配置生效。"""
    try:
        from app.core.config import load_env_file

        load_env_file()
    except Exception:  # pragma: no cover - .env 缺失或格式错误时忽略
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iaa-tui",
        description="工业智能运维 Agent 终端界面（Textual）。只连接已启动的 FastAPI 后端。",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"后端 API 地址（默认取环境变量 {ENV_BASE_URL} 或 {DEFAULT_BASE_URL}）",
    )
    parser.add_argument(
        "--audit-token",
        default=None,
        help=f"可选审计接口共享令牌（默认取环境变量 {ENV_AUDIT_TOKEN}；不会写入日志）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只探测一次系统状态并打印，不启动界面（有不可用服务时返回码 1）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="启动前健康检查超时秒数（默认 5）",
    )
    parser.add_argument(
        "--start-anyway",
        action="store_true",
        help="即使启动前健康检查失败也进入界面（状态页会显示 unavailable）",
    )
    parser.add_argument("--version", action="version", version=f"iaa-tui {__version__}")
    return parser


def resolve_config(args: argparse.Namespace) -> tuple[str, str | None]:
    base_url = args.base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL
    token = args.audit_token or os.environ.get(ENV_AUDIT_TOKEN) or None
    return base_url.rstrip("/"), token


def is_local(base_url: str) -> bool:
    if "://" not in base_url:
        return False
    host = base_url.split("://", 1)[1].split("/", 1)[0]
    hostname = host.rsplit(":", 1)[0].strip("[]")
    return hostname in LOCAL_HOSTS


def print_status(status: SystemStatus) -> None:
    print(f"系统状态：{status.overall.label_zh}（{'深度探测' if status.deep_probe else '快速探测'}）")
    print(f"API：{status.api_base_url or '(未设置)'}")
    width = max((len(item.label) for item in status.components), default=8)
    for item in status.components:
        print(f"  {item.label.ljust(width)}  {item.state.value:<11} {item.detail}")
    for note in status.notes:
        print(f"  ! {note}")


async def _preflight(base_url: str, token: str | None, timeout: float) -> tuple[bool, str]:
    client = IaaApiClient(
        base_url, audit_token=token, default_timeout=_timeout_of_seconds(timeout)
    )
    try:
        info = await client.health()
        return True, f"{info.service} v{info.version}"
    except ApiError as exc:
        return False, str(exc)
    finally:
        await client.close()


def _timeout_of_seconds(seconds: float) -> httpx.Timeout:
    return httpx.Timeout(connect=seconds, read=seconds, write=seconds, pool=seconds)


async def _check(base_url: str, token: str | None, deep: bool = False) -> int:
    client = IaaApiClient(base_url, audit_token=token)
    try:
        status = await probe_system_status(client, deep=deep)
    finally:
        await client.close()
    print_status(status)
    return 0 if status.state_of("api") is ComponentState.READY else 1


async def _run_app(app) -> int:  # pragma: no cover - 需要交互终端
    try:
        await app.run_async()
    finally:
        try:
            await app.client.close()
        except Exception:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_project_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    base_url, token = resolve_config(args)

    if args.check:
        return asyncio.run(_check(base_url, token))

    ok, detail = asyncio.run(_preflight(base_url, token, args.timeout))
    if not ok:
        print(
            f"[iaa-tui] 无法连接后端 {base_url}\n"
            f"          原因：{detail}\n"
            "          请先启动 API：scripts/start_api.sh"
            "（或 cd backend && python -m uvicorn app.main:app --port 8000）\n"
            "          如需在无后端时查看界面：加 --start-anyway",
            file=sys.stderr,
        )
        if not args.start_anyway:
            return 2
    if not is_local(base_url):
        print(
            f"[iaa-tui] 警告：后端 {base_url} 不是本机地址。审计/Trace/设备接口"
            "默认没有完整认证与权限控制，只应在本机或隔离网络使用。",
            file=sys.stderr,
        )

    from app.tui.app import create_app

    app = create_app(base_url, audit_token=token)
    print(
        f"[iaa-tui] 连接 {base_url}（{detail}）· Ctrl+K 切换页面 · F1 帮助 · Ctrl+Q 退出"
    )
    return asyncio.run(_run_app(app))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
