"""统一 API 客户端：TUI 与后端之间的唯一通道。

设计要点（对应 `docs/tui_design.md` 的“统一 API Client”一节）：

1. 只使用 `httpx.AsyncClient`，异步发起请求，TUI 事件循环不被阻塞；
2. 每个操作有独立的超时：普通查询 30 s、知识检索 60 s、Agent 问答 300 s、
   混合审计（LLM Judge）600 s——超时值来自后端文档中的实测耗时，不是拍脑袋；
3. 错误统一为 `ApiError`，保留后端 `{"error": {...}}` 的 `code/message/details/request_id`；
4. **只对幂等 GET** 做有限重试（网络错误 / 503 / 504），POST / PATCH 一律不自动重试，
   避免重复生成审计记录或重复触发 Agent 执行；
5. `X-Audit-Token` 只加在审计接口上，任何日志、异常与界面文案都不打印 Token；
6. 响应通过 Pydantic 模型校验必需字段；缺失字段 → `ApiError(kind="invalid_response")`
   而不是静默渲染空值。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.tui.models import (
    AgentAnswer,
    AgentTraceSummary,
    AuditReport,
    AuditRuleSpec,
    AuditRunsPage,
    AuditTraceDetail,
    Equipment,
    EquipmentPage,
    FaultPage,
    HealthInfo,
    KnowledgeHealth,
    QualityInfo,
    ReadinessInfo,
    ReadingPage,
    ReviewResult,
    SearchResults,
    StatsPage,
)

logger = logging.getLogger("industrial_agent_audit.tui.client")

T = TypeVar("T", bound=BaseModel)

DEFAULT_BASE_URL = os.environ.get("IAA_API_BASE_URL", "http://127.0.0.1:8000")

# 超时（秒）：connect 短、read 按操作类型区分
TIMEOUT_QUERY = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
TIMEOUT_KNOWLEDGE = httpx.Timeout(connect=5.0, read=90.0, write=10.0, pool=5.0)
TIMEOUT_AGENT = httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)
TIMEOUT_AUDIT = httpx.Timeout(connect=5.0, read=600.0, write=10.0, pool=5.0)

RETRYABLE_STATUS = frozenset({502, 503, 504})


class ApiError(Exception):
    """统一的 API 失败表示。

    `kind` 取值：
    - `http`：后端返回了 4xx/5xx（`status_code` 有值）；
    - `timeout`：请求超时；
    - `network`：连接失败、DNS、连接被拒绝等传输层错误；
    - `invalid_response`：2xx 但响应体不是合法 JSON 或缺少必需字段。
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "http",
        status_code: int | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
        method: str | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.status_code = status_code
        self.code = code
        self.details = details or {}
        self.request_id = request_id
        self.method = method
        self.path = path

    def __str__(self) -> str:  # pragma: no cover - 简单格式化
        parts = []
        if self.status_code is not None:
            parts.append(f"HTTP {self.status_code}")
        if self.code:
            parts.append(str(self.code))
        prefix = " / ".join(parts)
        base = f"{prefix}: {self.message}" if prefix else self.message
        if self.request_id:
            base = f"{base} [request_id={self.request_id}]"
        return base

    @property
    def retryable(self) -> bool:
        """该错误是否属于“短暂网络/服务端抖动”，可在幂等请求上重试。"""
        return self.kind in {"timeout", "network"} or (
            self.status_code in RETRYABLE_STATUS if self.status_code else False
        )

    @property
    def is_auth(self) -> bool:
        return self.status_code == 401


class IaaApiClient:
    """工业智能运维 Agent 的只读 API 客户端（审计写接口除外）。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        audit_token: str | None = None,
        max_retries: int = 2,
        retry_backoff: float = 0.25,
        transport: httpx.AsyncBaseTransport | None = None,
        trust_env: bool = False,
        default_timeout: httpx.Timeout | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_timeout = default_timeout or TIMEOUT_QUERY
        # Token 只保存在内存，禁止出现在 repr / 日志 / 界面
        self._audit_token = audit_token or None
        self.max_retries = max(0, max_retries)
        self.retry_backoff = retry_backoff
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.default_timeout,
            transport=transport,
            trust_env=trust_env,
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "IaaApiClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def __repr__(self) -> str:  # pragma: no cover - 避免泄漏 Token
        token_state = "set" if self._audit_token else "unset"
        return f"IaaApiClient(base_url={self.base_url!r}, audit_token={token_state})"

    @property
    def has_audit_token(self) -> bool:
        return bool(self._audit_token)

    # ------------------------------------------------------------------ #
    # 核心请求
    # ------------------------------------------------------------------ #
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: httpx.Timeout | None = None,
        audit: bool = False,
        model: type[T] | None = None,
        parser: Callable[[Any], T] | None = None,
    ) -> T | Any:
        request_timeout = timeout or self.default_timeout
        idempotent = method.upper() in {"GET", "HEAD"}
        attempts = 1 + (self.max_retries if idempotent else 0)
        headers: dict[str, str] = {}
        if audit and self._audit_token:
            headers["X-Audit-Token"] = self._audit_token

        last_error: ApiError | None = None
        for attempt in range(attempts):
            try:
                response = await self._client.request(
                    method,
                    path,
                    params=_clean_params(params),
                    json=json,
                    headers=headers,
                    timeout=request_timeout,
                )
            except httpx.TimeoutException as exc:
                last_error = ApiError(
                    f"请求超时（{_timeout_hint(request_timeout)}）",
                    kind="timeout",
                    method=method,
                    path=path,
                )
                last_error.__cause__ = exc
            except httpx.HTTPError as exc:
                last_error = ApiError(
                    f"无法连接后端服务（{type(exc).__name__}）",
                    kind="network",
                    method=method,
                    path=path,
                )
                last_error.__cause__ = exc
            else:
                if response.status_code in RETRYABLE_STATUS and idempotent and attempt + 1 < attempts:
                    last_error = _http_error(response, method, path)
                    await asyncio.sleep(self.retry_backoff * (2**attempt))
                    continue
                return self._parse_response(response, method, path, model, parser)

            if attempt + 1 < attempts:
                await asyncio.sleep(self.retry_backoff * (2**attempt))
                continue
            break

        assert last_error is not None
        logger.debug("API 失败 %s %s kind=%s", method, path, last_error.kind)
        raise last_error

    def _parse_response(
        self,
        response: httpx.Response,
        method: str,
        path: str,
        model: type[T] | None,
        parser: Callable[[Any], T] | None,
    ) -> T | Any:
        if response.status_code >= 400:
            raise _http_error(response, method, path)
        try:
            data = response.json()
        except ValueError as exc:
            raise ApiError(
                "后端返回了非 JSON 响应",
                kind="invalid_response",
                status_code=response.status_code,
                method=method,
                path=path,
            ) from exc
        if model is not None:
            try:
                return model.model_validate(data)
            except ValidationError as exc:
                raise ApiError(
                    f"后端响应缺少必需字段或不合法：{_brief_validation(exc)}",
                    kind="invalid_response",
                    status_code=response.status_code,
                    method=method,
                    path=path,
                    details={"errors": exc.errors()[:5]},
                ) from exc
        if parser is not None:
            return parser(data)
        return data

    # ------------------------------------------------------------------ #
    # 健康 / 就绪
    # ------------------------------------------------------------------ #
    async def health(self) -> HealthInfo:
        return await self._request("GET", "/health", model=HealthInfo)

    async def readiness(self) -> ReadinessInfo:
        return await self._request("GET", "/ready", model=ReadinessInfo)

    # ------------------------------------------------------------------ #
    # 设备
    # ------------------------------------------------------------------ #
    async def list_equipment(self, *, limit: int = 50, offset: int = 0) -> EquipmentPage:
        return await self._request(
            "GET",
            "/api/v1/equipment",
            params={"limit": limit, "offset": offset},
            model=EquipmentPage,
        )

    async def get_equipment(self, equipment_id: str) -> Equipment:
        return await self._request(
            "GET",
            f"/api/v1/equipment/{equipment_id}",
            model=Equipment,
        )

    async def readings(
        self,
        equipment_id: str,
        *,
        start_time: str,
        end_time: str,
        sensors: list[str] | None = None,
        limit: int = 200,
        offset: int = 0,
        exclude_frozen: bool = False,
    ) -> ReadingPage:
        return await self._request(
            "GET",
            f"/api/v1/equipment/{equipment_id}/readings",
            params={
                "start_time": start_time,
                "end_time": end_time,
                "sensor": sensors,
                "limit": limit,
                "offset": offset,
                "exclude_frozen": exclude_frozen,
            },
            model=ReadingPage,
        )

    async def stats(
        self,
        equipment_id: str,
        *,
        start_time: str,
        end_time: str,
        sensors: list[str] | None = None,
        exclude_frozen: bool = False,
    ) -> StatsPage:
        return await self._request(
            "GET",
            f"/api/v1/equipment/{equipment_id}/stats",
            params={
                "start_time": start_time,
                "end_time": end_time,
                "sensor": sensors,
                "exclude_frozen": exclude_frozen,
            },
            model=StatsPage,
        )

    async def faults(
        self,
        equipment_id: str,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> FaultPage:
        return await self._request(
            "GET",
            f"/api/v1/equipment/{equipment_id}/faults",
            params={
                "start_time": start_time,
                "end_time": end_time,
                "limit": limit,
                "offset": offset,
            },
            model=FaultPage,
        )

    async def quality(
        self, equipment_id: str, *, start_time: str, end_time: str
    ) -> QualityInfo:
        return await self._request(
            "GET",
            f"/api/v1/equipment/{equipment_id}/quality",
            params={"start_time": start_time, "end_time": end_time},
            model=QualityInfo,
        )

    # ------------------------------------------------------------------ #
    # 知识库
    # ------------------------------------------------------------------ #
    async def knowledge_health(self, *, check_services: bool = False) -> KnowledgeHealth:
        return await self._request(
            "GET",
            "/api/v1/knowledge/health",
            params={"check_services": check_services},
            model=KnowledgeHealth,
            timeout=TIMEOUT_KNOWLEDGE if check_services else TIMEOUT_QUERY,
        )

    async def knowledge_search(
        self,
        query: str,
        *,
        method: str = "hybrid",
        top_k: int = 5,
        equipment_scopes: list[str] | None = None,
    ) -> SearchResults:
        return await self._request(
            "POST",
            "/api/v1/knowledge/search",
            json={
                "query": query,
                "method": method,
                "top_k": top_k,
                **(
                    {"equipment_scopes": equipment_scopes}
                    if equipment_scopes is not None
                    else {}
                ),
            },
            model=SearchResults,
            timeout=TIMEOUT_KNOWLEDGE,
        )

    # ------------------------------------------------------------------ #
    # Agent
    # ------------------------------------------------------------------ #
    async def agent_chat(
        self,
        question: str,
        *,
        equipment_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        sensors: list[str] | None = None,
    ) -> AgentAnswer:
        body: dict[str, Any] = {"question": question}
        if equipment_id:
            body["equipment_id"] = equipment_id
        if start_time and end_time:
            body["start_time"] = start_time
            body["end_time"] = end_time
        if sensors:
            body["sensors"] = sensors
        return await self._request(
            "POST",
            "/api/v1/agent/chat",
            json=body,
            model=AgentAnswer,
            timeout=TIMEOUT_AGENT,
        )

    async def agent_trace(self, trace_id: str, *, include_events: bool = True) -> AgentTraceSummary:
        return await self._request(
            "GET",
            f"/api/v1/agent/traces/{trace_id}",
            params={"include_events": include_events},
            model=AgentTraceSummary,
        )

    # ------------------------------------------------------------------ #
    # 审计（Agent-Probe）
    # ------------------------------------------------------------------ #
    async def audit_trace(self, trace_id: str) -> AuditTraceDetail:
        return await self._request(
            "GET",
            f"/api/v1/audit/traces/{trace_id}",
            model=AuditTraceDetail,
            audit=True,
            timeout=TIMEOUT_KNOWLEDGE,
        )

    async def audit_evaluate(
        self,
        trace_id: str,
        *,
        mode: str = "rule_only",
        persist: bool = True,
        verify_knowledge: bool | None = None,
    ) -> AuditReport:
        body: dict[str, Any] = {"mode": mode, "persist": persist}
        if verify_knowledge is not None:
            body["verify_knowledge"] = verify_knowledge
        return await self._request(
            "POST",
            f"/api/v1/audit/traces/{trace_id}/evaluate",
            json=body,
            model=AuditReport,
            audit=True,
            timeout=TIMEOUT_AUDIT,
        )

    async def audit_run(self, audit_id: str) -> AuditReport:
        return await self._request(
            "GET",
            f"/api/v1/audit/runs/{audit_id}",
            model=AuditReport,
            audit=True,
        )

    async def audit_runs(
        self,
        *,
        trace_id: str | None = None,
        mode: str | None = None,
        risk: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> AuditRunsPage:
        return await self._request(
            "GET",
            "/api/v1/audit/runs",
            params={
                "trace_id": trace_id,
                "mode": mode,
                "risk": risk,
                "limit": limit,
                "offset": offset,
            },
            model=AuditRunsPage,
            audit=True,
        )

    async def audit_summary(self, *, days: int = 30) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/audit/summary",
            params={"days": days},
            parser=lambda data: data,
            audit=True,
        )

    async def audit_rules(self) -> list[AuditRuleSpec]:
        data = await self._request("GET", "/api/v1/audit/rules", parser=lambda d: d)
        return [AuditRuleSpec.model_validate(item) for item in data]

    async def audit_findings(
        self,
        *,
        trace_id: str | None = None,
        audit_id: str | None = None,
        severity: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/api/v1/audit/findings",
            params={
                "trace_id": trace_id,
                "audit_id": audit_id,
                "severity": severity,
                "limit": limit,
                "offset": offset,
            },
            parser=lambda data: data,
            audit=True,
        )

    async def audit_review(
        self, finding_id: str, review_status: str, note: str | None = None
    ) -> ReviewResult:
        body: dict[str, Any] = {"review_status": review_status}
        if note:
            body["note"] = note
        return await self._request(
            "PATCH",
            f"/api/v1/audit/findings/{finding_id}/review",
            json=body,
            model=ReviewResult,
            audit=True,
        )


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #
def _clean_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    if not params:
        return None
    cleaned: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            cleaned[key] = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            if not value:
                continue
            cleaned[key] = [str(item) for item in value]
        else:
            cleaned[key] = value
    return cleaned or None


def _http_error(response: httpx.Response, method: str, path: str) -> ApiError:
    code: str | None = None
    message = f"后端返回 HTTP {response.status_code}"
    details: dict[str, Any] = {}
    request_id = response.headers.get("X-Request-ID")
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = str(error.get("message") or message)
            raw_details = error.get("details")
            if isinstance(raw_details, dict):
                details = raw_details
            request_id = error.get("request_id") or request_id
        elif payload.get("detail"):
            message = str(payload["detail"])
    return ApiError(
        message,
        kind="http",
        status_code=response.status_code,
        code=code,
        details=details,
        request_id=request_id,
        method=method,
        path=path,
    )


def _timeout_hint(timeout: httpx.Timeout) -> str:
    read = getattr(timeout, "read", None)
    return f"read={read}s" if read is not None else "unknown"


def _brief_validation(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "未知校验错误"
    first = errors[0]
    loc = ".".join(str(part) for part in first.get("loc", ()))
    return f"{loc}: {first.get('msg')}"


__all__ = [
    "ApiError",
    "DEFAULT_BASE_URL",
    "IaaApiClient",
    "TIMEOUT_AGENT",
    "TIMEOUT_AUDIT",
    "TIMEOUT_KNOWLEDGE",
    "TIMEOUT_QUERY",
]
