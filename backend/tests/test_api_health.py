"""健康检查接口：进程存活 vs 数据库就绪。"""

from __future__ import annotations

import pytest

from app.db.session import dispose_engines

# 指向一个不会有人监听的端口：连接会被立即拒绝（ECONNREFUSED），无需等待超时
UNREACHABLE_DATABASE_URL = (
    "postgresql+psycopg://app_user:irrelevant@127.0.0.1:1/industrial_agent_audit"
)


@pytest.fixture()
def unreachable_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DATABASE_URL)
    dispose_engines()
    try:
        yield UNREACHABLE_DATABASE_URL
    finally:
        dispose_engines()


def test_health_returns_ok(api_client) -> None:
    response = api_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "industrial-agent-audit-api"
    assert body["version"]
    assert body["time"]


def test_health_does_not_require_database(api_client, unreachable_database) -> None:
    """存活探针不能因为数据库故障而失败（否则容器会被误重启）。"""
    response = api_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ready_reports_database_and_schema(api_client) -> None:
    response = api_client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["schema_ok"] is True
    assert body["schema_version"] == body["schema_expected_version"]
    database = body["database"]
    assert database["database"].endswith("_test")
    assert database["pgvector_available"] is True
    assert database["server_version"].startswith("17")


def test_ready_returns_503_when_database_unavailable(api_client, unreachable_database) -> None:
    response = api_client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "database_unavailable"
    # 不能泄漏连接串/密码
    assert "irrelevant" not in response.text
    assert "postgresql" not in response.text
