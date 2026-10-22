"""统一异常处理、请求标识、数据库不可用与连接释放。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.db.session import dispose_engines, get_engine
from app.services import equipment_info

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


def test_error_envelope_shape(api_client, api_dataset) -> None:
    response = api_client.get("/api/v1/equipment/unknown")
    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details", "request_id"}
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]


def test_request_id_is_echoed(api_client, api_dataset) -> None:
    response = api_client.get("/health", headers={"X-Request-ID": "test-request-id"})
    assert response.headers["X-Request-ID"] == "test-request-id"
    assert float(response.headers["X-Response-Time-Ms"]) >= 0


def test_database_unavailable_returns_503(api_client, unreachable_database) -> None:
    response = api_client.get("/api/v1/equipment")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "database_unavailable"
    assert response.headers.get("Retry-After") == "5"
    # 不泄漏连接串、密码、SQL
    assert "irrelevant" not in response.text
    assert "postgresql" not in response.text
    assert "SELECT" not in response.text.upper()


def test_unexpected_error_is_masked(api_client, api_dataset, monkeypatch) -> None:
    def boom(session, equipment_code):  # noqa: ANN001
        raise RuntimeError("internal detail: SELECT * FROM secret_table")

    monkeypatch.setattr(equipment_info, "get_equipment_info", boom)
    response = api_client.get("/api/v1/equipment/metropt3-apu-01")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert "secret_table" not in response.text
    assert "SELECT" not in response.text


def test_sessions_are_released(api_client, api_dataset) -> None:
    """连续请求结束后连接池不应有被占用的连接（Session 正确释放）。"""
    for _ in range(5):
        assert (
            api_client.get(
                "/api/v1/equipment/metropt3-apu-01/readings",
                params={
                    "start_time": "2020-02-01T00:00:00",
                    "end_time": "2020-02-01T00:01:00",
                },
            ).status_code
            == 200
        )
    pool = get_engine().pool
    assert pool.checkedout() == 0
    assert pool.checkedin() >= 0


def test_openapi_document_is_generated(api_client) -> None:
    response = api_client.get("/openapi.json")
    assert response.status_code == 200
    spec = response.json()
    assert spec["info"]["title"].startswith("工业设备数据查询服务")
    paths = set(spec["paths"])
    assert {
        "/health",
        "/ready",
        "/api/v1/equipment",
        "/api/v1/equipment/{equipment_id}",
        "/api/v1/equipment/{equipment_id}/readings",
        "/api/v1/equipment/{equipment_id}/stats",
        "/api/v1/equipment/{equipment_id}/faults",
        "/api/v1/equipment/{equipment_id}/quality",
    } <= paths
    # 每个业务接口都要有错误响应文档
    readings = spec["paths"]["/api/v1/equipment/{equipment_id}/readings"]["get"]
    assert {"200", "404", "422"} <= set(readings["responses"])


def test_swagger_ui_is_served(api_client) -> None:
    response = api_client.get("/docs")
    assert response.status_code == 200
    assert "swagger-ui" in response.text


def test_testclient_uses_error_handlers_not_stacktraces(api_dataset) -> None:
    """用会抛出栈的客户端配置验证：即便服务端异常，响应体仍是统一 JSON。"""
    client = TestClient(create_app(), raise_server_exceptions=False)
    try:
        response = client.get("/api/v1/equipment/unknown-equipment")
        assert response.headers["content-type"].startswith("application/json")
        assert "Traceback" not in response.text
    finally:
        client.close()


def test_asgi_entrypoint_exposes_app() -> None:
    """部署入口 `uvicorn app.main:app` 必须可用。"""
    from app.main import app as asgi_app

    assert asgi_app.title.startswith("工业设备数据查询服务")
    assert asgi_app.version
