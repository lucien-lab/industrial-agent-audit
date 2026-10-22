"""Shell 脚本行为回归测试（P1-B / P2-5）。

覆盖：
- `.env` 只填充未设置的变量（不覆盖调用方环境变量）
- CPU 配置优先级：环境变量 > .env
- 迁移脚本连不上目标库时明确失败，不回退
- 危险操作（down-test）默认拒绝；只认实际连接的 *_test 库
- 日志与错误输出不泄露密码

这些测试只做只读操作；唯一会写入的是测试库自身（由 conftest 保证隔离）。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATE = PROJECT_ROOT / "scripts" / "migrate.sh"
DB_SH = PROJECT_ROOT / "scripts" / "db.sh"
ENV_LIB = PROJECT_ROOT / "scripts" / "lib" / "env.sh"
DB_TARGET = PROJECT_ROOT / "scripts" / "db_target.py"


def _env_file_value(key: str) -> str:
    """直接读取项目 .env（不依赖 os.environ，避免被 conftest 覆盖）。"""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.is_file():
        pytest.skip("缺少项目 .env，跳过脚本行为测试")
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    pytest.skip(f".env 中缺少 {key}")


def _run(args: list[str], env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("ALLOW_TEST_ROLLBACK", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        args, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=180
    )


def test_env_lib_only_fills_unset_variables(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.test"
    env_file.write_text(
        "AUDIT_FOO=from_file\nAUDIT_BAR=from_file\nAUDIT_QUOTED=\"quoted value\"\n",
        encoding="utf-8",
    )
    script = (
        f'source "{ENV_LIB}"; '
        "load_env_defaults \"$1\"; "
        'printf "%s|%s|%s" "${AUDIT_FOO}" "${AUDIT_BAR}" "${AUDIT_QUOTED}"'
    )
    completed = _run(
        ["bash", "-c", script, "bash", str(env_file)], {"AUDIT_FOO": "from_process"}
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "from_process|from_file|quoted value"


def test_env_lib_never_writes_secret_to_stdout(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.test"
    env_file.write_text("AUDIT_SECRET=super-secret-value\n", encoding="utf-8")
    completed = _run(
        ["bash", "-c", f'source "{ENV_LIB}"; load_env_defaults "$1"; echo ok', "bash", str(env_file)]
    )
    assert completed.returncode == 0
    assert "super-secret-value" not in completed.stdout + completed.stderr


def test_migrate_target_respects_caller_env_over_dotenv(database_url: str) -> None:
    """调用方显式传入 DATABASE_URL 时，不得被 .env 覆盖（P1-B 回归）。"""
    expected_db = database_url.rsplit("/", 1)[-1]
    completed = _run([str(MIGRATE), "target"], {"DATABASE_URL": database_url})
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"database={expected_db}" in completed.stdout


def test_migrate_target_uses_dotenv_when_env_missing() -> None:
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    completed = subprocess.run(
        [str(MIGRATE), "target"], cwd=PROJECT_ROOT, env=env, capture_output=True, text=True
    )
    expected_db = _env_file_value("DATABASE_URL").rsplit("/", 1)[-1]
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"database={expected_db}" in completed.stdout


def test_migrate_fails_on_nonexistent_database() -> None:
    """连到不存在的库必须明确失败，不能回退到 .env 的库。"""
    password = _env_file_value("POSTGRES_PASSWORD")
    url = f"postgresql+psycopg://app_user:{password}@127.0.0.1:5432/audit_missing_db_xyz"
    completed = _run([str(MIGRATE), "target"], {"DATABASE_URL": url})
    assert completed.returncode == 3, completed.stdout + completed.stderr
    output = completed.stdout + completed.stderr
    assert "无法连接" in output
    assert "industrial_agent_audit" not in output.replace("audit_missing_db_xyz", "")


def test_migrate_down_test_requires_explicit_opt_in(database_url: str) -> None:
    completed = _run([str(MIGRATE), "down-test"], {"TEST_DATABASE_URL": database_url})
    assert completed.returncode == 5
    assert "默认拒绝" in completed.stderr + completed.stdout


def test_migrate_down_test_refuses_non_test_database(database_url: str) -> None:
    """即使拿到生产库 URL 且显式允许，也必须按实际库名拒绝。"""
    production_url = _env_file_value("DATABASE_URL")
    with psycopg.connect(production_url.replace("postgresql+psycopg://", "postgresql://")) as conn:
        before = conn.execute("select version_num from alembic_version").fetchone()[0]

    completed = _run(
        [str(MIGRATE), "down-test"],
        {"TEST_DATABASE_URL": production_url, "ALLOW_TEST_ROLLBACK": "1"},
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 5, output
    assert "不以 _test 结尾" in output

    with psycopg.connect(production_url.replace("postgresql+psycopg://", "postgresql://")) as conn:
        after = conn.execute("select version_num from alembic_version").fetchone()[0]
    assert before == after, "生产库版本在拒绝回滚后不应发生变化"


def test_migrate_output_never_contains_password(database_url: str) -> None:
    password = _env_file_value("POSTGRES_PASSWORD")
    completed = _run([str(MIGRATE), "target"], {"DATABASE_URL": database_url})
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0
    assert password not in output
    assert "***" in output


def test_db_target_reports_live_database(database_url: str) -> None:
    expected_db = database_url.rsplit("/", 1)[-1]
    completed = _run(
        [sys.executable, str(DB_TARGET), "--url", database_url, "--expect-suffix", "_test"]
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f'"database": "{expected_db}"' in completed.stdout


def test_db_target_rejects_suffix_mismatch(database_url: str) -> None:
    completed = _run(
        [sys.executable, str(DB_TARGET), "--url", database_url, "--expect-suffix", "_prod"]
    )
    assert completed.returncode == 4
    assert "expect_suffix_failed" in completed.stdout


@pytest.mark.parametrize("script", [MIGRATE, DB_SH, ENV_LIB])
def test_scripts_pass_bash_syntax_check(script: Path) -> None:
    completed = _run(["bash", "-n", str(script)])
    assert completed.returncode == 0, completed.stderr


def test_migrate_help_lists_guard_commands() -> None:
    completed = _run([str(MIGRATE), "no-such-command"])
    assert completed.returncode == 2
    assert re.search(r"down-test", completed.stderr)
