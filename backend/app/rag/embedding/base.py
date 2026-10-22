"""Embedding Provider 抽象。

约束：
- 输出必须是**真实模型**返回的向量；失败抛异常，绝不返回零向量或哈希向量充数；
- 维度必须与配置和数据库 `vector(dim)` 一致，否则抛 `EmbeddingDimensionMismatch`；
- 统一做 L2 归一化，配合 pgvector 余弦距离使用；
- 每次批量调用记录数量、耗时、失败数与重试次数，供入库报告使用。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol


def l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        raise ValueError("Embedding 向量为零向量，模型输出异常")
    return [value / norm for value in vector]


def vector_literal(vector: list[float]) -> str:
    """pgvector 文本字面量：'[0.1,0.2,...]'。"""
    return "[" + ",".join(f"{value:.8f}" for value in vector) + "]"


@dataclass
class EmbeddingBatch:
    vectors: list[list[float]]
    model: str
    dim: int
    texts: int
    elapsed_ms: float
    provider: str
    attempts: int = 1
    prompt_tokens: int | None = None
    metadata: dict[str, object] = field(default_factory=dict)


class EmbeddingProvider(Protocol):
    provider: str
    model: str
    dim: int
    version: str

    def embed_documents(self, texts: list[str]) -> EmbeddingBatch: ...

    def embed_query(self, text: str) -> EmbeddingBatch: ...
