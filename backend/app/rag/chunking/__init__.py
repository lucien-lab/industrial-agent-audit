"""文本切分：固定长度 vs 结构感知（两套策略并存，用于实测对比）。"""

from __future__ import annotations

from app.rag.chunking.base import (
    STRATEGIES,
    Chunk,
    Chunker,
    FixedSizeChunker,
    StructuralChunker,
    get_chunker,
    split_sentences,
)

__all__ = [
    "STRATEGIES",
    "Chunk",
    "Chunker",
    "FixedSizeChunker",
    "StructuralChunker",
    "get_chunker",
    "split_sentences",
]
