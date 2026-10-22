"""工具契约、错误分类映射与带超时/重试的执行器。

工具统一契约（只读）：
- 输入：Pydantic 模型校验后的参数（白名单字段，客户端无法提交任意工具代码）；
- 输出：`ToolResult(status, data, evidence)`，`status` ∈ {ok, empty}；
- 失败：抛异常，由 `run_tool` 映射成 `error_category`（见 `app.agent.errors`）；
- 工具实现**必须**复用 Phase 1C Service / Phase 2 检索，禁止在工具里写 SQL；
- 每次调用都有独立超时；只有 retryable 类别才重试，且重试次数有上限。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.agent.errors import ERROR_CATEGORIES, is_retryable
from app.rag.errors import (
    EmbeddingDimensionMismatch,
    EmbeddingError,
    IndexModelMismatch,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
    RetrievalError,
)
from app.services.errors import (
    EquipmentNotFound,
    InvalidParameter,
    InvalidTimeRange,
    ServiceError,
    TimezoneNotSupported,
    UnsupportedSensor,
    WindowTooLarge,
)

TOOL_STATUSES = ("ok", "empty", "error")


@dataclass
class EvidenceItem:
    """一条来自工具的原始证据（标签在证据整合阶段统一分配）。"""

    kind: Literal["data", "knowledge"]
    origin_tool: str
    source: dict[str, Any]
    detail: dict[str, Any]
    quote: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "origin_tool": self.origin_tool,
            "source": self.source,
            "detail": self.detail,
            "quote": self.quote,
        }


@dataclass
class ToolResult:
    status: Literal["ok", "empty"]
    data: dict[str, Any]
    evidence: list[EvidenceItem] = field(default_factory=list)


@dataclass
class ToolOutcome:
    """一次工具调用（含重试）的最终结果。"""

    tool: str
    status: Literal["ok", "empty", "error"]
    input: dict[str, Any]
    data: dict[str, Any]
    evidence: list[EvidenceItem]
    attempts: int
    elapsed_ms: float
    started_at: str
    ended_at: str
    error: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "empty")

    @property
    def error_category(self) -> str | None:
        return self.error.get("category") if self.error else None

    @property
    def error_code(self) -> str | None:
        return self.error.get("code") if self.error else None

    def summary(self) -> dict[str, Any]:
        """写入 Trace 的输出摘要（小、结构化、无敏感信息）。"""
        summary: dict[str, Any] = {
            "tool": self.tool,
            "status": self.status,
            "attempts": self.attempts,
        }
        if self.error:
            summary["error_category"] = self.error.get("category")
            summary["error_code"] = self.error.get("code")
        data = self.data or {}
        for key in ("rows", "returned", "total", "count", "row_count", "method", "top_k"):
            if key in data:
                summary[key] = data[key]
        if "equipment" in data and isinstance(data["equipment"], dict):
            summary["equipment_code"] = data["equipment"].get("equipment_code")
        return summary

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "status": self.status,
            "input": self.input,
            "attempts": self.attempts,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "data": self.data,
            "evidence": [item.to_dict() for item in self.evidence],
            "error": self.error,
        }


class AgentToolContext(Protocol):
    """工具执行所需的运行时依赖（由 `app.agent.context.AgentContext` 实现）。"""

    def open_data_session(self): ...  # contextmanager[Session]

    def open_knowledge_session(self): ...  # contextmanager[Session]


ToolImpl = Callable[[AgentToolContext, BaseModel], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    """工具登记项。"""

    name: str
    category: Literal["data", "knowledge"]
    description: str
    input_model: type[BaseModel]
    impl: ToolImpl
    requires_equipment: bool = False
    max_return_items: int = 200
    timeout_s: float = 20.0
    max_retries: int = 1
    read_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "requires_equipment": self.requires_equipment,
            "max_return_items": self.max_return_items,
            "timeout_s": self.timeout_s,
            "max_retries": self.max_retries,
            "read_only": self.read_only,
            "input_schema": self.input_model.model_json_schema(),
        }


# --------------------------------------------------------------------------- #
# 异常 → 错误类别映射（显式表，不做字符串判断）
# --------------------------------------------------------------------------- #
_SERVICE_ERROR_CATEGORY: dict[str, str] = {
    "equipment_not_found": "not_found",
    "invalid_parameter": "invalid_input",
    "invalid_time_range": "invalid_input",
    "timezone_not_supported": "invalid_input",
    "unsupported_sensor": "invalid_input",
    "window_too_large": "invalid_input",
}


def classify_exception(exc: BaseException) -> tuple[str, str, str]:
    """返回 (category, code, 可公开的 message)。"""
    if isinstance(exc, ValidationError):
        message = "；".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in exc.errors()[:5]
        )
        return "invalid_input", "tool_invalid_input", message or "参数校验失败"
    if isinstance(exc, EquipmentNotFound):
        return "not_found", exc.code, exc.message
    if isinstance(exc, ServiceError):
        category = _SERVICE_ERROR_CATEGORY.get(exc.code, "service_error")
        return category, exc.code, exc.message
    if isinstance(
        exc, (InvalidParameter, InvalidTimeRange, TimezoneNotSupported, UnsupportedSensor, WindowTooLarge)
    ):  # pragma: no cover - 上面的 ServiceError 分支已覆盖
        return "invalid_input", exc.code, exc.message
    if isinstance(exc, LLMTimeout):
        return "timeout", exc.code, exc.message
    if isinstance(exc, (LLMRateLimited, LLMUnavailable, EmbeddingError, RetrievalError)):
        if isinstance(exc, (EmbeddingDimensionMismatch, IndexModelMismatch)):
            return "config_error", exc.code, exc.message
        return "transient", exc.code, exc.message
    if isinstance(exc, (PoolTimeoutError, OperationalError, InterfaceError)):
        return "transient", "database_unavailable", "数据库暂时不可用"
    if isinstance(exc, DBAPIError):
        return "service_error", "database_error", "数据库查询失败"
    if isinstance(exc, FutureTimeout):
        return "timeout", "tool_timeout", "工具执行超时"
    if isinstance(exc, TimeoutError):
        return "timeout", "tool_timeout", "工具执行超时"
    return "internal", type(exc).__name__, "工具内部错误"


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def build_error(category: str, code: str, message: str, *, attempts: int = 1) -> dict[str, Any]:
    return {
        "category": category,
        "code": code,
        "message": message[:300],
        "attempts": attempts,
        "retryable": is_retryable(category),
        "blocks_dependents": ERROR_CATEGORIES.get(category, {}).get("blocks_dependents", False),
    }


def run_tool(
    spec: ToolSpec,
    raw_params: dict[str, Any],
    ctx: AgentToolContext,
    *,
    deadline_epoch: float | None = None,
) -> ToolOutcome:
    """校验参数并执行工具，带超时与有限重试。"""
    started_at = _iso_now()
    started = time.perf_counter()
    attempts = 0
    last_error: dict[str, Any] | None = None

    try:
        payload = spec.input_model.model_validate(raw_params or {})
    except ValidationError as exc:
        category, code, message = classify_exception(exc)
        return ToolOutcome(
            tool=spec.name,
            status="error",
            input=_safe_input(raw_params),
            data={},
            evidence=[],
            attempts=0,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            started_at=started_at,
            ended_at=_iso_now(),
            error=build_error(category, code, message, attempts=0),
        )

    normalized_input = payload.model_dump(exclude_none=True)
    max_attempts = 1 + max(spec.max_retries, 0)

    while attempts < max_attempts:
        attempts += 1
        # 每次尝试都重新检查整轮截止时间，避免“重试把预算吃光”
        if deadline_epoch is not None and time.time() >= deadline_epoch:
            last_error = build_error("budget", "agent_timeout", "整轮执行已超时", attempts=attempts)
            break
        remaining = spec.timeout_s
        if deadline_epoch is not None:
            remaining = min(remaining, max(deadline_epoch - time.time(), 0.001))
        try:
            result = _call_with_timeout(spec, payload, ctx, timeout_s=remaining)
        except BaseException as exc:  # noqa: BLE001 - 统一分类，逐层向上报告
            category, code, message = classify_exception(exc)
            last_error = build_error(category, code, message, attempts=attempts)
            if is_retryable(category) and attempts < max_attempts:
                time.sleep(min(0.2 * attempts, 1.0))
                continue
            break
        return ToolOutcome(
            tool=spec.name,
            status=result.status,
            input=normalized_input,
            data=result.data,
            evidence=result.evidence,
            attempts=attempts,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            started_at=started_at,
            ended_at=_iso_now(),
        )

    return ToolOutcome(
        tool=spec.name,
        status="error",
        input=normalized_input,
        data={},
        evidence=[],
        attempts=attempts,
        elapsed_ms=(time.perf_counter() - started) * 1000,
        started_at=started_at,
        ended_at=_iso_now(),
        error=last_error or build_error("internal", "tool_failed", "工具执行失败", attempts=attempts),
    )


def _call_with_timeout(
    spec: ToolSpec,
    payload: BaseModel,
    ctx: AgentToolContext,
    *,
    timeout_s: float,
) -> ToolResult:
    """在线程池中执行工具，用 `future.result(timeout)` 施加硬超时。

    说明：Python 无法强制终止线程，超时后放弃等待并让连接随会话关闭；
    数据库侧另有 `statement_timeout` 兜底（见 `app.agent.context`）。
    """
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"agent-tool-{spec.name}")
    try:
        future = executor.submit(spec.impl, ctx, payload)
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeout as exc:
            future.cancel()
            raise TimeoutError(f"{spec.name} 超过 {timeout_s:.1f}s 未返回") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _safe_input(params: dict[str, Any]) -> dict[str, Any]:
    """记录到 Trace/响应里的输入摘要（仅标量与小列表）。"""
    safe: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
        elif isinstance(value, (list, tuple)):
            safe[key] = [str(item)[:40] for item in list(value)[:8]]
        else:
            safe[key] = str(type(value).__name__)
    return safe


__all__ = [
    "TOOL_STATUSES",
    "EvidenceItem",
    "ToolImpl",
    "ToolOutcome",
    "ToolResult",
    "ToolSpec",
    "build_error",
    "classify_exception",
    "run_tool",
]
