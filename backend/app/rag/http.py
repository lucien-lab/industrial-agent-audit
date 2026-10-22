"""带重试的 HTTP 调用公共逻辑（Embedding 与 LLM 共用）。

重试策略（显式、可观测）：
- 429 限流：按 `Retry-After`（若有）或指数退避等待后重试；
- 5xx / 连接失败 / 超时：指数退避重试；
- 其它 4xx（例如模型不存在、请求体非法）：**立即失败**，重试没有意义；
- 每次尝试都会记录到 `attempts`，失败时抛出带上下文的领域异常。
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger("industrial_agent_audit.rag.http")


@dataclass
class RetryOutcome:
    response: httpx.Response | None
    attempts: int
    elapsed_ms: float
    errors: list[str] = field(default_factory=list)


class HttpRetryExhausted(RuntimeError):
    """重试次数用尽（由调用方翻译成 EmbeddingError / LLMError）。"""

    def __init__(
        self,
        message: str,
        *,
        attempts: int,
        status_code: int | None = None,
        rate_limited: bool = False,
        timed_out: bool = False,
        unreachable: bool = False,
        errors: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.status_code = status_code
        self.rate_limited = rate_limited
        self.timed_out = timed_out
        self.unreachable = unreachable
        self.errors = errors or []


def post_json_with_retry(
    url: str,
    payload: dict[str, Any],
    *,
    timeout_s: float,
    max_retries: int,
    backoff_base_s: float = 0.8,
    backoff_max_s: float = 12.0,
    client: httpx.Client | None = None,
) -> tuple[dict[str, Any], RetryOutcome]:
    """POST JSON 并带重试。返回 (响应 JSON, 重试统计)。"""
    attempts = 0
    errors: list[str] = []
    started = time.perf_counter()
    own_client = client is None
    http = client or httpx.Client(timeout=timeout_s)
    try:
        while True:
            attempts += 1
            try:
                response = http.post(
                    url, json=payload, timeout=timeout_s, headers={"Accept": "application/json"}
                )
            except httpx.TimeoutException as exc:
                errors.append(f"attempt {attempts}: timeout after {timeout_s}s ({exc})")
                if attempts > max_retries:
                    raise HttpRetryExhausted(
                        f"请求 {url} 超时，重试 {attempts - 1} 次后仍失败",
                        attempts=attempts,
                        timed_out=True,
                        errors=errors,
                    ) from exc
                _sleep(attempts, backoff_base_s, backoff_max_s)
                continue
            except httpx.HTTPError as exc:
                errors.append(f"attempt {attempts}: transport error ({exc})")
                if attempts > max_retries:
                    raise HttpRetryExhausted(
                        f"无法连接 {url}：{type(exc).__name__}",
                        attempts=attempts,
                        unreachable=True,
                        errors=errors,
                    ) from exc
                _sleep(attempts, backoff_base_s, backoff_max_s)
                continue

            if response.status_code in (200, 201):
                return response.json(), RetryOutcome(
                    response=response,
                    attempts=attempts,
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                    errors=errors,
                )

            detail = _safe_detail(response)
            errors.append(f"attempt {attempts}: HTTP {response.status_code} {detail}")
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempts > max_retries:
                    raise HttpRetryExhausted(
                        f"{url} 返回 HTTP {response.status_code}，重试 {attempts - 1} 次后仍失败",
                        attempts=attempts,
                        status_code=response.status_code,
                        rate_limited=response.status_code == 429,
                        errors=errors,
                    )
                retry_after = _retry_after(response)
                _sleep(attempts, backoff_base_s, backoff_max_s, retry_after=retry_after)
                continue

            # 其它 4xx：请求本身有问题，重试无意义
            raise HttpRetryExhausted(
                f"{url} 返回 HTTP {response.status_code}：{detail}",
                attempts=attempts,
                status_code=response.status_code,
                errors=errors,
            )
    finally:
        if own_client:
            http.close()


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _sleep(attempt: int, base: float, cap: float, *, retry_after: float | None = None) -> None:
    if retry_after is not None:
        delay = min(cap, retry_after)
    else:
        delay = min(cap, base * (2 ** (attempt - 1)))
        delay += random.uniform(0, delay * 0.25)  # 抖动，避免并发齐打
    logger.info("重试等待 %.2fs（第 %d 次尝试失败）", delay, attempt)
    time.sleep(delay)


def _safe_detail(response: httpx.Response) -> str:
    """只保留有限长度的服务端错误摘要，避免日志/响应注入超长内容。"""
    try:
        body = response.text
    except Exception:  # pragma: no cover - 极端情况
        return ""
    body = " ".join(body.split())
    return body[:300]
