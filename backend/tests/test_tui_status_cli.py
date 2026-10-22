"""Phase 5：系统状态聚合与 CLI 入口测试（纯逻辑，不启动界面）。"""

from __future__ import annotations

import pytest

from app.tui import cli
from app.tui.client import ApiError
from app.tui.models import HealthInfo, KnowledgeHealth, ReadinessInfo
from app.tui.status import (
    ComponentState,
    SystemStatus,
    probe_system_status,
    status_from_checks,
)

READY_PAYLOAD = ReadinessInfo(
    status="degraded",
    database={"database": "industrial_agent_audit"},
    schema_version="0004_coverage_missing_time",
    schema_expected_version="0008_agent_audit",
    schema_ok=False,
)
KNOWLEDGE_OK = KnowledgeHealth(
    status="ok",
    database={"database": "iaa_rag_dev"},
    schema_version="0007_knowledge_rag",
    schema_expected_version="0007_knowledge_rag",
    schema_ok=True,
    documents=6,
    chunks=1314,
    embedding={"model": "bge-m3", "probe": {"ok": True, "elapsed_ms": 120.0, "dim": 1024}},
    llm={"model": "qwen2.5:7b-instruct", "probe": {"ok": False, "error": "llm_unavailable: down"}},
)


class FakeClient:
    """最小可用的 client 替身：只实现 probe 需要的四个方法。"""

    base_url = "http://fake"

    def __init__(
        self,
        *,
        health: object = None,
        readiness: object = None,
        knowledge: object = None,
        audit: object = None,
    ) -> None:
        self._health = health
        self._readiness = readiness
        self._knowledge = knowledge
        self._audit = audit

    async def health(self):
        return self._raise_or(self._health, HealthInfo(status="ok", service="api", version="2.0.0"))

    async def readiness(self):
        return self._raise_or(self._readiness, READY_PAYLOAD)

    async def knowledge_health(self, *, check_services: bool = False):
        return self._raise_or(self._knowledge, KNOWLEDGE_OK)

    async def audit_summary(self, *, days: int = 30):
        return self._raise_or(self._audit, {"total_audits": 3})

    @staticmethod
    def _raise_or(value, default):
        if isinstance(value, BaseException):
            raise value
        return default if value is None else value


@pytest.mark.asyncio
async def test_probe_marks_schema_mismatch_as_degraded():
    status = await probe_system_status(FakeClient())
    assert status.state_of("api") is ComponentState.READY
    assert status.state_of("industrial_db") is ComponentState.DEGRADED
    assert status.state_of("rag") is ComponentState.READY
    assert status.state_of("audit") is ComponentState.READY
    # 未深度探测：LLM/Embedding 一律 unknown，不得当作 ready
    assert status.state_of("embedding") is ComponentState.UNKNOWN
    assert status.state_of("llm") is ComponentState.UNKNOWN
    assert status.overall is ComponentState.DEGRADED
    assert any("0004" in note for note in status.notes)


@pytest.mark.asyncio
async def test_probe_deep_detects_llm_failure():
    status = await probe_system_status(FakeClient(), deep=True)
    assert status.deep_probe is True
    assert status.state_of("embedding") is ComponentState.READY
    assert status.state_of("llm") is ComponentState.UNAVAILABLE
    assert "llm_unavailable" in (status.get("llm").detail if status.get("llm") else "")


@pytest.mark.asyncio
async def test_probe_handles_api_unreachable():
    down = ApiError("无法连接后端服务", kind="network")
    status = await probe_system_status(
        FakeClient(health=down, readiness=down, knowledge=down, audit=down)
    )
    assert status.state_of("api") is ComponentState.UNAVAILABLE
    assert status.overall is ComponentState.UNAVAILABLE
    assert any("start_api.sh" in note for note in status.notes)


@pytest.mark.asyncio
async def test_probe_marks_missing_audit_token_as_degraded():
    unauthorized = ApiError("需要令牌", status_code=401, code="audit_unauthorized")
    status = await probe_system_status(FakeClient(audit=unauthorized))
    assert status.state_of("audit") is ComponentState.DEGRADED
    assert any("IAA_AUDIT_TOKEN" in note for note in status.notes)


@pytest.mark.asyncio
async def test_probe_handles_rag_degraded_and_warnings():
    knowledge = KnowledgeHealth(
        status="degraded",
        database={"database": "iaa_rag_dev"},
        schema_ok=True,
        documents=6,
        chunks=1314,
        warnings=["Embedding 服务探测失败"],
    )
    status = await probe_system_status(FakeClient(knowledge=knowledge))
    assert status.state_of("rag") is ComponentState.DEGRADED
    assert any("Embedding" in note for note in status.notes)


def test_status_from_checks_aggregates_worst_state():
    status = status_from_checks(
        {
            "api": ("ready", "ok"),
            "db": ("degraded", "schema"),
            "rag": ("unavailable", "down"),
        }
    )
    assert isinstance(status, SystemStatus)
    assert status.overall is ComponentState.UNAVAILABLE
    assert status.get("db").state.label_zh == "降级"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_resolve_config_precedence(monkeypatch):
    monkeypatch.setenv("IAA_API_BASE_URL", "http://env-host:8000")
    monkeypatch.setenv("IAA_AUDIT_TOKEN", "env-token")
    args = cli.build_parser().parse_args([])
    assert cli.resolve_config(args) == ("http://env-host:8000", "env-token")

    args = cli.build_parser().parse_args(
        ["--base-url", "http://cli-host:9000/", "--audit-token", "cli-token"]
    )
    assert cli.resolve_config(args) == ("http://cli-host:9000", "cli-token")


def test_resolve_config_defaults(monkeypatch):
    monkeypatch.delenv("IAA_API_BASE_URL", raising=False)
    monkeypatch.delenv("IAA_AUDIT_TOKEN", raising=False)
    args = cli.build_parser().parse_args([])
    base_url, token = cli.resolve_config(args)
    assert base_url == "http://127.0.0.1:8000"
    assert token is None


def test_is_local_detection():
    assert cli.is_local("http://127.0.0.1:8000")
    assert cli.is_local("http://localhost:8000/api")
    assert cli.is_local("http://[::1]:8000")
    assert not cli.is_local("http://10.0.0.5:8000")
    assert not cli.is_local("https://example.com")
    assert not cli.is_local("not-a-url")


def test_version_flag():
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0


def test_startup_fails_cleanly_when_backend_unreachable(capsys):
    # 端口 1 上没有服务：应立即失败并给出明确提示，返回码 2
    code = cli.main(["--base-url", "http://127.0.0.1:1", "--timeout", "0.4"])
    assert code == 2
    captured = capsys.readouterr()
    assert "无法连接后端" in captured.err
    assert "start_api.sh" in captured.err


@pytest.mark.asyncio
async def test_check_mode_uses_probe(monkeypatch, capsys):
    async def fake_probe(client, *, deep=False):
        return status_from_checks({"api": ("ready", "fake"), "db": ("degraded", "schema")})

    monkeypatch.setattr(cli, "probe_system_status", fake_probe)
    code = await cli._check("http://fake", None)
    assert code == 0  # API ready 时返回 0（即使其它服务降级）
    out = capsys.readouterr().out
    assert "系统状态" in out and "降级" in out


@pytest.mark.asyncio
async def test_check_mode_returns_nonzero_when_api_down(monkeypatch):
    async def fake_probe(client, *, deep=False):
        return status_from_checks({"api": ("unavailable", "down")})

    monkeypatch.setattr(cli, "probe_system_status", fake_probe)
    assert await cli._check("http://fake", None) == 1


def test_print_status_lists_every_component(capsys):
    status = status_from_checks(
        {"api": ("ready", "v2"), "industrial_db": ("degraded", "schema 0004"), "rag": ("ready", "ok")}
    )
    cli.print_status(status)
    out = capsys.readouterr().out
    assert "系统状态" in out
    assert "工业库" in out
    assert "degraded" in out
    assert "(未设置)" in out


def test_preflight_wraps_timeout(monkeypatch):
    """启动前健康检查超时也必须给出可读错误，而不是抛未捕获异常。"""

    class TimeoutClient:
        async def health(self):
            raise ApiError("请求超时", kind="timeout")

        async def close(self):
            return None

    monkeypatch.setattr(cli, "IaaApiClient", lambda *args, **kwargs: TimeoutClient())
    ok, detail = pytest.importorskip("asyncio").run(cli._preflight("http://fake", None, 0.1))
    assert ok is False
    assert "超时" in detail
