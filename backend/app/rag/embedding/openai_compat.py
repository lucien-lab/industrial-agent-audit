"""OpenAI 兼容 Embedding Provider（可选，用于替换本地模型）。

只需要 `EMBEDDING_BASE_URL` + `EMBEDDING_API_KEY` + `EMBEDDING_MODEL`。
本阶段默认不使用（无外部密钥），但保留该实现以证明模型层可替换：
换模型时必须同时换 `EMBEDDING_VERSION` 并重新向量化整个索引。
"""

from __future__ import annotations

import time

import httpx

from app.rag.embedding.base import EmbeddingBatch, l2_normalize
from app.rag.errors import EmbeddingDimensionMismatch, EmbeddingError
from app.rag.http import HttpRetryExhausted, post_json_with_retry


class OpenAICompatibleEmbeddingProvider:
    provider = "openai"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dim: int,
        version: str,
        batch_size: int = 16,
        timeout_s: float = 60.0,
        max_retries: int = 3,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise EmbeddingError("EMBEDDING_API_KEY 未配置，无法使用 openai 兼容 Embedding")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.version = version
        self.batch_size = max(1, batch_size)
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._client = client

    def embed_documents(self, texts: list[str]) -> EmbeddingBatch:
        return self._embed(texts)

    def embed_query(self, text: str) -> EmbeddingBatch:
        return self._embed([text])

    def _embed(self, texts: list[str]) -> EmbeddingBatch:
        if not texts:
            return EmbeddingBatch(
                vectors=[], model=self.model, dim=self.dim, texts=0,
                elapsed_ms=0.0, provider=self.provider,
            )
        started = time.perf_counter()
        vectors: list[list[float]] = []
        attempts_total = 0
        client = self._client or httpx.Client(
            timeout=self.timeout_s,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        try:
            for offset in range(0, len(texts), self.batch_size):
                batch = texts[offset : offset + self.batch_size]
                try:
                    payload, outcome = post_json_with_retry(
                        f"{self.base_url}/embeddings",
                        {"model": self.model, "input": batch},
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
                    ) from exc
                attempts_total += outcome.attempts
                data = payload.get("data") or []
                if len(data) != len(batch):
                    raise EmbeddingError(
                        "Embedding 响应数量与输入不一致",
                        expected=len(batch),
                        received=len(data),
                    )
                for item in sorted(data, key=lambda x: x.get("index", 0)):
                    vector = item.get("embedding") or []
                    if len(vector) != self.dim:
                        raise EmbeddingDimensionMismatch(
                            f"模型实际输出 {len(vector)} 维，配置为 {self.dim} 维",
                            actual_dim=len(vector),
                            configured_dim=self.dim,
                        )
                    vectors.append(l2_normalize([float(v) for v in vector]))
        finally:
            client.close()
        return EmbeddingBatch(
            vectors=vectors,
            model=self.model,
            dim=self.dim,
            texts=len(texts),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            provider=self.provider,
            attempts=max(1, attempts_total),
        )
