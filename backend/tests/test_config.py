"""配置与凭据处理测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.config import ConfigError, get_settings, load_env_file, mask_password


def test_mask_password_hides_secret() -> None:
    masked = mask_password("postgresql+psycopg://user:supersecret@127.0.0.1:5432/db")
    assert "supersecret" not in masked
    assert masked == "postgresql+psycopg://user:***@127.0.0.1:5432/db"


def test_missing_database_url_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    empty_env = tmp_path / ".env"
    empty_env.write_text("# no config\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        get_settings(env_file=empty_env)


def test_requires_psycopg_driver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "DATABASE_URL=postgresql://user:pw@localhost:5432/db\n", encoding="utf-8"
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ConfigError):
        get_settings(env_file=env)


def test_env_file_does_not_override_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("DATABASE_URL=postgresql+psycopg://from:file@h/db\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://from:process@h/db")
    load_env_file(env)
    assert os.environ["DATABASE_URL"] == "postgresql+psycopg://from:process@h/db"
