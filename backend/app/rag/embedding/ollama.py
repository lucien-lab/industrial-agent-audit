"""Ollama Embedding Provider（默认本地部署，无需 API Key）。

实测：`bge-m3` 通过 Ollama `/api/embed` 输出 **1024 维** dense 向量。
模型名与维度都必须在配置中显式声明，并在首次调用后校验真实维度。
"""

from __future__ import annotations

import logging
import time

import httpx

from app.rag.embedding.base import EmbeddingBatch, l2_normalize
from app.rag.errors import EmbeddingDimensionMismatch, EmbeddingError
from app.rag.http import HttpRetryExhausted, post_json_with_retry

logger = logging.getLogger("industrial_agent_audit.rag.embedding")


class OllamaEmbeddingProvider:
    provider = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        dim: int,
        version: str,
        batch_size: int = 8,
        timeout_s: float = 120.0,
        max_retries: int = 3,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self.version = version
        self.batch_size = max(1, batch_size)
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._client = client

    # -- 公共 API ---------------------------------------------------------- #
    def embed_documents(self, texts: list[str]) -> EmbeddingBatch:
        return self._embed(texts)

    def embed_query(self, text: str) -> EmbeddingBatch:
        return self._embed([text])

    # -- 内部实现 ---------------------------------------------------------- #
    def _embed(self, texts: list[str]) -> EmbeddingBatch:
        if not texts:
            return EmbeddingBatch(
                vectors=[],
                model=self.model,
                dim=self.dim,
                texts=0,
                elapsed_ms=0.0,
                provider=self.provider,
            )
        started = time.perf_counter()
        vectors: list[list[float]] = []
        attempts_total = 0
        prompt_tokens = 0
        own_client = self._client is None
        client = self._client or httpx.Client(timeout=self.timeout_s)
        try:
            for offset in range(0, len(texts), self.batch_size):
                batch = texts[offset : offset + self.batch_size]
                try:
                    payload, outcome = post_json_with_retry(
                        f"{self.base_url}/api/embed",
                        {
                            "model": self.model,
                            "input": batch,
                            "truncate": True,
                            "options": {"num_ctx": 8192},
                        },
                        timeout_s=self.timeout_s,
                        max_retries=self.max_retries,
                        client=client,
                    )
                except HttpRetryExhausted as exc:
                    raise EmbeddingError(
                        f"Embedding 调用失败（模型 {self.model}）：{exc}",
                        model=self.model,
                        attempts=exc.attempts,
                        status_code=exc.status_code,
                        errors=exc.errors[-3:],
                        batch_offset=offset,
                    ) from exc
                attempts_total += outcome.attempts
                embeddings = payload.get("embeddings")
                if not isinstance(embeddings, list) or len(embeddings) != len(batch):
                    raise EmbeddingError(
                        "Embedding 响应数量与输入不一致",
                        model=self.model,
                        expected=len(batch),
                        received=len(embeddings) if isinstance(embeddings, list) else None,
                    )
                for vector in embeddings:
                    if not isinstance(vector, list) or not vector:
                        raise EmbeddingError("Embedding 响应包含空向量", model=self.model)
                    if len(vector) != self.dim:
                        raise EmbeddingDimensionMismatch(
                            f"模型 {self.model} 实际输出 {len(vector)} 维，"
                            f"配置为 {self.dim} 维；请同步修改 EMBEDDING_DIM 与数据库 vector 维度",
                            model=self.model,
                            actual_dim=len(vector),
                            configured_dim=self.dim,
                        )
                    vectors.append(l2_normalize([float(v) for v in vector]))
                prompt_tokens += int(payload.get("prompt_eval_count") or 0)
        finally:
            if own_client:
                client.close()

        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "embedding provider=%s model=%s texts=%d dim=%d elapsed_ms=%.1f attempts=%d",
            self.provider,
            self.model,
            len(texts),
            self.dim,
            elapsed_ms,
            attempts_total,
        )
        return EmbeddingBatch(
            vectors=vectors,
            model=self.model,
            dim=self.dim,
            texts=len(texts),
            elapsed_ms=elapsed_ms,
            provider=self.provider,
            attempts=max(1, attempts_total),
            prompt_tokens=prompt_tokens or None,
        )
