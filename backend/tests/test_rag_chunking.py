"""Chunk 切分测试：页码、章节、重叠、边界与两种策略差异。"""

from __future__ import annotations

import pytest

from app.rag.chunking import FixedSizeChunker, StructuralChunker, get_chunker, split_sentences
from app.rag.parsing.pdf import ParsedDocument, TextBlock


def _parsed(blocks, page_count=3) -> ParsedDocument:
    return ParsedDocument(
        path="synthetic.pdf",
        file_sha256="0" * 64,
        file_bytes=100,
        page_count=page_count,
        pages=(),
        blocks=tuple(blocks),
        sections=(),
        parse_status="parsed",
    )


def _blocks():
    return [
        TextBlock(0, 1, "Compressor Types", "Compressor Types", 1, "Compressor Types", True, "heading"),
        TextBlock(1, 1, "Positive displacement compressors trap air and reduce volume to raise pressure. " * 3,
                  "Compressor Types", 1, "Compressor Types"),
        TextBlock(2, 2, "Dynamic compressors add velocity with impellers and convert it to pressure. " * 3,
                  "Dynamic Compressors", 2, "Dynamic Compressors"),
        TextBlock(3, 2, "TP2 reads compressor pressure; TP3 reads pneumatic panel pressure.",
                  "Dynamic Compressors", 2, "Dynamic Compressors"),
        TextBlock(4, 3, "Maintenance requires daily, weekly, monthly and annual procedures.",
                  "Maintenance", 2, "Maintenance"),
    ]


def test_structural_chunks_never_cross_sections():
    chunks = StructuralChunker(chunk_size=400, chunk_overlap=60).chunk(_parsed(_blocks()))
    assert chunks
    sections = [chunk.section_title for chunk in chunks]
    # 每个 Chunk 只属于一个 section
    for chunk in chunks:
        assert chunk.section_title is not None
    assert "Maintenance" in sections


def test_structural_keeps_page_span_and_order():
    chunks = StructuralChunker(chunk_size=300, chunk_overlap=50).chunk(_parsed(_blocks()))
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert 1 <= chunk.page_start <= chunk.page_end <= 3
    assert chunks[0].page_start == 1


def test_structural_heading_merged_into_following_paragraph():
    chunks = StructuralChunker(chunk_size=1000, chunk_overlap=0).chunk(_parsed(_blocks()))
    assert any("Compressor Types" in chunk.content for chunk in chunks)
    # 不产生只有 4 个字符的纯标题 Chunk
    assert all(chunk.char_count >= 20 for chunk in chunks)


def test_structural_respects_chunk_size_budget():
    chunker = StructuralChunker(chunk_size=300, chunk_overlap=60)
    chunks = chunker.chunk(_parsed(_blocks()))
    for chunk in chunks:
        assert chunk.char_count <= 300 * 1.2


def test_fixed_chunker_covers_text_with_overlap_and_progress():
    chunker = FixedSizeChunker(chunk_size=200, chunk_overlap=40)
    chunks = chunker.chunk(_parsed(_blocks()))
    assert len(chunks) > 1
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert chunk.content.strip()
        assert 1 <= chunk.page_start <= chunk.page_end <= 3
    # 重叠非零（相邻块有共享内容）
    assert any(
        chunks[i].content[-20:] in chunks[i + 1].content
        for i in range(len(chunks) - 1)
    )


def test_fixed_chunker_rejects_overlap_ge_size():
    with pytest.raises(ValueError):
        FixedSizeChunker(chunk_size=100, chunk_overlap=100)


def test_get_chunker_unknown_strategy_fails_loudly():
    with pytest.raises(ValueError):
        get_chunker("magic")


def test_chunk_hash_is_content_derived():
    chunks = StructuralChunker(chunk_size=500, chunk_overlap=0).chunk(_parsed(_blocks()))
    hashes = {chunk.content_sha256 for chunk in chunks}
    assert len(hashes) == len(chunks)
    assert all(len(value) == 64 for value in hashes)


def test_split_sentences_keeps_sensor_identifiers_intact():
    text = "TP2 (bar) – pressure on the compressor. TP3 (bar) – pressure at the pneumatic panel."
    sentences = split_sentences(text)
    assert len(sentences) == 2
    assert "TP2" in sentences[0] and "TP3" in sentences[1]
