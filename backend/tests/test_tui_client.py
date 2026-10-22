"""Phase 5：统一 API Client 单元测试（使用 httpx.MockTransport，不访问网络）。

覆盖：正常返回、连接失败、超时、404/422/503、无效 JSON、缺失字段、
Token 行为、分页参数、幂等重试与非幂等不重试。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.tui import client as client_module
from app.tui.client import ApiError, IaaApiClient

BASE = "http://testserver"


def make_client(handler, **kwargs) -> IaaApiClient:
    kwargs.setdefault("max_retries", 2)
    kwargs.setdefault("retry_backoff", 0.0)
    return IaaApiClient(BASE, transport=httpx.MockTransport(handler), **kwargs)


def json_response(payload, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


HEALTH = {"status": "ok", "service": "industrial-agent-audit-api", "version": "2.0.0"}
READY = {
    "status": "degraded",
    "database": {"database": "industrial_agent_audit", "user": "app_user"},
    "schema_version": "0004_coverage_missing_time",
    "schema_expected_version": "0008_agent_audit",
    "schema_ok": False,
}
AGENT = {
    "trace_id": "abc123",
    "question": "q",
    "answer": "a",
    "execution_status": "completed",
    "intent": "data_only",
    "answer_status": "answered",
    "answer_mode": "llm",
    "tools_used": ["get_sensor_statistics"],
    "total_duration_ms": 12.5,
    "agent_version": "phase3-agent-v1",
}
ERROR_BODY = {
    "error": {
        "code": "equipment_not_found",
        "message": "设备 'nope' 不存在",
        "details": {"equipment_code": "nope"},
        "request_id": "req-1",
    }
}


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_health_ok_parses_contract():
    client = make_client(lambda request: json_response(HEALTH))
    async with client:
        info = await client.health()
    assert info.status == "ok"
    assert info.version == "2.0.0"


@pytest.mark.asyncio
async def test_readiness_degraded_is_not_treated_as_ok():
    client = make_client(lambda request: json_response(READY))
    async with client:
        ready = await client.readiness()
    assert ready.status == "degraded"
    assert ready.schema_ok is False
    assert ready.schema_version != ready.schema_expected_version


@pytest.mark.asyncio
async def test_query_parameters_and_pagination_are_forwarded():
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return json_response(
            {
                "equipment": {"equipment_id": "metropt3-apu-01"},
                "sensors": ["tp2"],
                "items": [],
                "pagination": {
                    "limit": 50,
                    "offset": 100,
                    "total": 240,
                    "returned": 0,
                    "has_more": True,
                },
                "exclude_frozen": False,
            }
        )

    client = make_client(handler)
    async with client:
        page = await client.readings(
            "metropt3-apu-01",
            start_time="2020-04-17T00:00:00",
            end_time="2020-04-18T00:00:00",
            sensors=["tp2", "comp"],
            limit=50,
            offset=100,
            exclude_frozen=True,
        )
    params = seen["request"].url.params
    assert params["limit"] == "50"
    assert params["offset"] == "100"
    assert params["exclude_frozen"] == "true"
    assert sorted(params.get_list("sensor")) == ["comp", "tp2"]
    assert page.pagination.has_more is True
    assert page.pagination.total == 240


@pytest.mark.asyncio
async def test_agent_chat_body_is_minimal_and_whitelisted():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return json_response(AGENT)

    client = make_client(handler)
    async with client:
        answer = await client.agent_chat("问题", equipment_id="metropt3-apu-01")
    assert seen["body"] == {"question": "问题", "equipment_id": "metropt3-apu-01"}
    assert answer.trace_id == "abc123"
    assert answer.tools_used == ["get_sensor_statistics"]


# --------------------------------------------------------------------------- #
# 传输层错误与重试
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_connection_error_is_wrapped_and_retried_for_get():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("refused", request=request)

    client = make_client(handler)
    async with client:
        with pytest.raises(ApiError) as excinfo:
            await client.health()
    error = excinfo.value
    assert error.kind == "network"
    assert error.status_code is None
    assert calls["n"] == 3  # 1 次 + 2 次重试（幂等 GET）


@pytest.mark.asyncio
async def test_timeout_is_reported_as_timeout_kind(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = make_client(handler)
    async with client:
        with pytest.raises(ApiError) as excinfo:
            await client.readiness()
    assert excinfo.value.kind == "timeout"
    assert "超时" in excinfo.value.message


@pytest.mark.asyncio
async def test_post_is_never_retried_on_503():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return json_response(
            {"error": {"code": "database_unavailable", "message": "数据库暂时不可用"}}, 503
        )

    client = make_client(handler)
    async with client:
        with pytest.raises(ApiError) as excinfo:
            await client.agent_chat("问题")
    assert calls["n"] == 1, "非幂等 POST 不允许自动重试"
    assert excinfo.value.status_code == 503
    assert excinfo.value.code == "database_unavailable"


@pytest.mark.asyncio
async def test_get_503_is_retried_then_reported():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return json_response({"error": {"code": "database_unavailable", "message": "down"}}, 503)

    client = make_client(handler)
    async with client:
        with pytest.raises(ApiError):
            await client.audit_summary()
    assert calls["n"] == 3


# --------------------------------------------------------------------------- #
# HTTP 错误结构
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_404_preserves_backend_error_structure():
    client = make_client(lambda request: json_response(ERROR_BODY, 404))
    async with client:
        with pytest.raises(ApiError) as excinfo:
            await client.get_equipment("nope")
    error = excinfo.value
    assert error.status_code == 404
    assert error.code == "equipment_not_found"
    assert error.request_id == "req-1"
    assert error.details == {"equipment_code": "nope"}
    assert "nope" in str(error)


@pytest.mark.asyncio
async def test_422_validation_error_details_are_kept():
    body = {
        "error": {
            "code": "invalid_parameter",
            "message": "请求参数校验失败",
            "details": {"errors": [{"field": "question", "type": "string_too_short"}]},
        }
    }
    client = make_client(lambda request: json_response(body, 422))
    async with client:
        with pytest.raises(ApiError) as excinfo:
            await client.agent_chat("")
    assert excinfo.value.status_code == 422
    assert excinfo.value.details["errors"][0]["field"] == "question"


@pytest.mark.asyncio
async def test_401_marks_auth_error_for_audit_endpoints():
    client = make_client(
        lambda request: json_response(
            {"error": {"code": "audit_unauthorized", "message": "需要令牌"}}, 401
        )
    )
    async with client:
        with pytest.raises(ApiError) as excinfo:
            await client.audit_runs()
    assert excinfo.value.is_auth is True


# --------------------------------------------------------------------------- #
# 响应契约
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_invalid_json_body_is_reported():
    client = make_client(
        lambda request: httpx.Response(200, content=b"<html>not json</html>")
    )
    async with client:
        with pytest.raises(ApiError) as excinfo:
            await client.health()
    assert excinfo.value.kind == "invalid_response"
    assert "非 JSON" in excinfo.value.message


@pytest.mark.asyncio
async def test_missing_required_field_is_reported():
    client = make_client(lambda request: json_response({"equipment_name": "缺 equipment_id"}))
    async with client:
        with pytest.raises(ApiError) as excinfo:
            await client.get_equipment("metropt3-apu-01")
    assert excinfo.value.kind == "invalid_response"
    assert "equipment_id" in excinfo.value.message


@pytest.mark.asyncio
async def test_agent_answer_without_trace_id_is_rejected():
    payload = dict(AGENT)
    payload.pop("trace_id")
    client = make_client(lambda request: json_response(payload))
    async with client:
        with pytest.raises(ApiError) as excinfo:
            await client.agent_chat("问题")
    assert excinfo.value.kind == "invalid_response"
    assert "trace_id" in excinfo.value.message


# --------------------------------------------------------------------------- #
# 审计令牌
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_audit_token_only_sent_to_audit_endpoints():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/health":
            return json_response(HEALTH)
        return json_response(
            {"items": [], "total": 0, "limit": 20, "offset": 0}
        )

    client = make_client(handler, audit_token="secret-token")
    async with client:
        await client.health()
        await client.audit_runs()
    assert "x-audit-token" not in {key.lower() for key in requests[0].headers}
    assert requests[1].headers["X-Audit-Token"] == "secret-token"
    # Token 不得出现在 repr / 日志文案中
    assert "secret-token" not in repr(client)


@pytest.mark.asyncio
async def test_audit_endpoints_work_without_token():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return json_response({"items": [], "total": 0, "limit": 20, "offset": 0})

    client = make_client(handler)
    async with client:
        await client.audit_runs()
    assert "X-Audit-Token" not in requests[0].headers
    assert client.has_audit_token is False


# --------------------------------------------------------------------------- #
# 其它
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_review_patch_body_and_no_retry():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return json_response({"finding_id": "F1", "review_status": "confirmed"})

    client = make_client(handler)
    async with client:
        result = await client.audit_review("F1", "confirmed", note="人工确认")
    assert result.review_status == "confirmed"
    assert calls[0].method == "PATCH"
    assert json.loads(calls[0].content) == {"review_status": "confirmed", "note": "人工确认"}


@pytest.mark.asyncio
async def test_retry_backoff_sleeps_between_get_attempts(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("refused", request=request)
        return json_response(HEALTH)

    client = make_client(handler, retry_backoff=0.5)
    async with client:
        info = await client.health()
    assert info.status == "ok"
    assert sleeps == [0.5, 1.0]
