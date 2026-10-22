"""OpenAI 兼容 Chat Provider（可选）。

保留该实现用于说明“LLM 可替换”，本阶段默认使用本地 Ollama。
"""

from __future__ import annotations

import time

import httpx

from app.rag.errors import LLMError, LLMRateLimited, LLMTimeout, LLMUnavailable
from app.rag.http import HttpRetryExhausted, post_json_with_retry
from app.rag.llm.base import ChatMessage, LLMResponse


class OpenAICompatibleChatModel:
    provider = "openai"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 120.0,
        max_retries: int = 2,
        temperature: float = 0.0,
        max_tokens: int = 900,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise LLMError("LLM_API_KEY 未配置，无法使用 openai 兼容 LLM")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.temperature = temperature
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
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        client = self._client or httpx.Client(
            timeout=self.timeout_s, headers={"Authorization": f"Bearer {self.api_key}"}
        )
        try:
            try:
                data, outcome = post_json_with_retry(
                    f"{self.base_url}/chat/completions",
                    payload,
                    timeout_s=self.timeout_s,
                    max_retries=self.max_retries,
                    client=client,
                )
            except HttpRetryExhausted as exc:
                details = {"model": self.model, "attempts": exc.attempts}
                if exc.timed_out:
                    raise LLMTimeout("语言模型调用超时", **details) from exc
                if exc.rate_limited:
                    raise LLMRateLimited("语言模型限流", **details) from exc
                if exc.unreachable:
                    raise LLMUnavailable("语言模型服务不可用", **details) from exc
                raise LLMError(f"语言模型调用失败：{exc}", **details) from exc
        finally:
            client.close()

        choices = (data or {}).get("choices") or []
        if not choices:
            raise LLMError("语言模型响应缺少 choices", model=self.model)
        text = (choices[0].get("message") or {}).get("content") or ""
        if not text.strip():
            raise LLMError("语言模型返回空内容", model=self.model)
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text.strip(),
            model=self.model,
            provider=self.provider,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            attempts=outcome.attempts,
        )
