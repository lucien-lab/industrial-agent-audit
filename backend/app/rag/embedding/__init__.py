"""Embedding Provider 工厂：按配置选择实现，未知 provider 明确报错。"""

from __future__ import annotations

import os

from app.rag.config import RAGSettings
from app.rag.embedding.base import EmbeddingBatch, EmbeddingProvider, l2_normalize, vector_literal
from app.rag.embedding.ollama import OllamaEmbeddingProvider
from app.rag.embedding.openai_compat import OpenAICompatibleEmbeddingProvider
from app.rag.errors import EmbeddingError

PROVIDERS = ("ollama", "openai")


def get_embedding_provider(settings: RAGSettings) -> EmbeddingProvider:
    provider = settings.embedding_provider
    if provider == "ollama":
        return OllamaEmbeddingProvider(
            base_url=settings.embedding_base_url,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
            version=settings.embedding_version,
            batch_size=settings.embedding_batch_size,
            timeout_s=settings.embedding_timeout_s,
            max_retries=settings.embedding_max_retries,
        )
    if provider == "openai":
        return OpenAICompatibleEmbeddingProvider(
            base_url=settings.embedding_base_url,
            api_key=os.environ.get("EMBEDDING_API_KEY", ""),
            model=settings.embedding_model,
            dim=settings.embedding_dim,
            version=settings.embedding_version,
            batch_size=settings.embedding_batch_size,
            timeout_s=settings.embedding_timeout_s,
            max_retries=settings.embedding_max_retries,
        )
    raise EmbeddingError(
        f"未知 EMBEDDING_PROVIDER={provider!r}，可选：{list(PROVIDERS)}"
    )


__all__ = [
    "EmbeddingBatch",
    "EmbeddingProvider",
    "PROVIDERS",
    "get_embedding_provider",
    "l2_normalize",
    "vector_literal",
]
