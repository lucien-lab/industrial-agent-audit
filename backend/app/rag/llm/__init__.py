"""LLM Provider 工厂。"""

from __future__ import annotations

import os

from app.rag.config import RAGSettings
from app.rag.errors import LLMError
from app.rag.llm.base import ChatMessage, ChatModel, LLMResponse
from app.rag.llm.ollama import OllamaChatModel
from app.rag.llm.openai_compat import OpenAICompatibleChatModel

PROVIDERS = ("ollama", "openai")


def get_chat_model(settings: RAGSettings) -> ChatModel:
    provider = settings.llm_provider
    if provider == "ollama":
        return OllamaChatModel(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout_s=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
            temperature=settings.llm_temperature,
            num_ctx=settings.llm_num_ctx,
            max_tokens=settings.llm_max_tokens,
        )
    if provider == "openai":
        return OpenAICompatibleChatModel(
            base_url=settings.llm_base_url,
            api_key=os.environ.get("LLM_API_KEY", ""),
            model=settings.llm_model,
            timeout_s=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
            temperature=settings.llm_temperature,
            max_tokens=settings.llm_max_tokens,
        )
    raise LLMError(f"未知 LLM_PROVIDER={provider!r}，可选：{list(PROVIDERS)}")


__all__ = ["ChatMessage", "ChatModel", "LLMResponse", "PROVIDERS", "get_chat_model"]
