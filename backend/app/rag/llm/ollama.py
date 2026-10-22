"""Ollama Chat Provider（默认本地 qwen2.5:7b-instruct）。

- 使用 `/api/chat`（非流式）：一次请求拿到完整 JSON，便于记录 token 与耗时；
- 失败分类：超时 → LLMTimeout；429 → LLMRateLimited；连不上 → LLMUnavailable；
  其它错误 → LLMError；
- 不在这里解析业务 JSON，只负责传输与重试。
"""

from __future__ import annotations

import logging
import time

import httpx

from app.rag.errors import LLMError, LLMRateLimited, LLMTimeout, LLMUnavailable
from app.rag.http import HttpRetryExhausted, post_json_with_retry
from app.rag.llm.base import ChatMessage, LLMResponse

logger = logging.getLogger("industrial_agent_audit.rag.llm")


class OllamaChatModel:
    provider = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_s: float = 180.0,
        max_retries: int = 2,
        temperature: float = 0.0,
        num_ctx: int = 8192,
        max_tokens: int = 900,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.temperature = temperature
        self.num_ctx = num_ctx
        self.max_tokens = max_tokens
        self._client = client

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "options": {
                "temperature": self.temperature if temperature is None else temperature,
                "num_predict": max_tokens or self.max_tokens,
                "num_ctx": self.num_ctx,
            },
        }
        if json_mode:
            payload["format"] = "json"

        started = time.perf_counter()
        client = self._client or httpx.Client(timeout=self.timeout_s)
        try:
            try:
                data, outcome = post_json_with_retry(
                    f"{self.base_url}/api/chat",
                    payload,
                    timeout_s=self.timeout_s,
                    max_retries=self.max_retries,
                    client=client,
                )
            except HttpRetryExhausted as exc:
                details = {
                    "model": self.model,
                    "attempts": exc.attempts,
                    "status_code": exc.status_code,
                    "errors": exc.errors[-3:],
                }
                if exc.timed_out:
                    raise LLMTimeout(
                        f"语言模型调用超时（模型 {self.model}，超时 {self.timeout_s}s）", **details
                    ) from exc
                if exc.rate_limited:
                    raise LLMRateLimited(
                        f"语言模型限流（模型 {self.model}，已重试 {exc.attempts - 1} 次）", **details
                    ) from exc
                if exc.unreachable:
                    raise LLMUnavailable(
                        f"语言模型服务不可用：{self.base_url}", **details
                    ) from exc
                raise LLMError(f"语言模型调用失败：{exc}", **details) from exc
        finally:
            if self._client is None:
                client.close()

        message = (data or {}).get("message") or {}
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise LLMError("语言模型返回空内容", model=self.model)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "llm provider=%s model=%s elapsed_ms=%.1f prompt_tokens=%s eval_tokens=%s",
            self.provider,
            self.model,
            elapsed_ms,
            data.get("prompt_eval_count"),
            data.get("eval_count"),
        )
        return LLMResponse(
            text=text.strip(),
            model=self.model,
            provider=self.provider,
            elapsed_ms=elapsed_ms,
            prompt_tokens=_int_or_none(data.get("prompt_eval_count")),
            completion_tokens=_int_or_none(data.get("eval_count")),
            attempts=outcome.attempts,
            metadata={
                "done_reason": data.get("done_reason"),
                "total_duration_ns": data.get("total_duration"),
            },
        )


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
