"""运行配置：只从环境变量 / 项目根 .env 读取，源码内不保存任何凭据。

设计约束：
- 密码、连接串只能来自 `.env`（已被 .gitignore 忽略）或进程环境变量；
- 缺失 `DATABASE_URL` 时直接抛出明确异常，而不是回退到某个“默认数据库”，
  避免误连到非预期实例（尤其是含真实数据的库）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# backend/app/core/config.py -> backend/app -> backend -> 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[3]

DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


class ConfigError(RuntimeError):
    """配置缺失或不合法。"""


def load_env_file(path: Path | str | None = None) -> None:
    """把 .env 中的键值写入 os.environ（不覆盖已存在的环境变量）。

    只支持 `KEY=VALUE` 与 `#` 注释，避免为几个配置项引入额外依赖。
    """
    env_path = Path(path) if path is not None else DEFAULT_ENV_FILE
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    """数据库相关配置。时间不走时区换算，见 docs/database_design.md。"""

    database_url: str
    test_database_url: str | None
    log_level: str

    @property
    def safe_database_url(self) -> str:
        """用于日志输出的连接串（隐去密码）。"""
        return mask_password(self.database_url)


def mask_password(url: str) -> str:
    """把 `scheme://user:password@host` 中的密码替换为 ***。"""
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    credentials, host_part = rest.rsplit("@", 1)
    if ":" not in credentials:
        return url
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host_part}"


def get_settings(env_file: Path | str | None = None) -> Settings:
    """读取配置；`DATABASE_URL` 必填。"""
    load_env_file(env_file)
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigError(
            "缺少 DATABASE_URL。请复制 .env.example 为 .env 并填写连接信息；"
            "源码中不提供任何默认连接串。"
        )
    if not database_url.startswith("postgresql+psycopg://"):
        raise ConfigError(
            "DATABASE_URL 必须使用 psycopg 3 驱动，形如 "
            "postgresql+psycopg://user:password@host:port/dbname"
        )
    return Settings(
        database_url=database_url,
        test_database_url=os.environ.get("TEST_DATABASE_URL") or None,
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
