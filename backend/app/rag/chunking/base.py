"""Chunk 数据结构与切分策略公共逻辑。

约定：
- 每个 Chunk 只来自**一个文档**（跨文档合并在数据结构层就不可能发生）；
- `page_start` / `page_end` 来自实际贡献文本的段落块页码，不是估算；
- `chunking_version` 由调用方显式传入并落库，便于同一文档并存多套切分做对比；
- 切分只做“边界选择”，不改写文本内容。

两种策略：
- `FixedSizeChunker`：固定字符窗口 + 重叠，是**对照基线**，不利用章节信息；
- `StructuralChunker`：章节 → 段落聚合，尽量不拆散段落、不跨章节拼块。

哪个更好由 `scripts/evaluate_rag.py` 的实测指标决定，代码里不做任何“结构感知一定更优”
的假设。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Protocol

from app.rag.parsing.pdf import ParsedDocument


@dataclass(frozen=True)
class Chunk:
    chunk_index: int
    content: str
    page_start: int
    page_end: int
    section_title: str | None
    section_path: str | None
    char_count: int
    chunking_strategy: str
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


class Chunker(Protocol):
    strategy: str
    chunking_version: str

    def chunk(self, parsed: ParsedDocument) -> list[Chunk]: ...


# 句子/段落边界（用于避免把设备编号、报警说明从中间截断）
_BOUNDARY_RE = re.compile(r"(?<=[。！？；;.!?:])\s+|\n+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？；;.!?])\s+|\n+")


def split_sentences(text: str) -> list[str]:
    """按句末标点/换行切分，保留原始字符（不做任何改写）。"""
    parts = [part for part in _SENTENCE_SPLIT_RE.split(text) if part and part.strip()]
    return parts or ([text] if text.strip() else [])


def _safe_end(text: str, start: int, target_end: int, *, min_ratio: float = 0.55) -> int:
    """把窗口右边界回退到**最接近目标长度**的天然边界。"""
    hard_min = start + max(1, int((target_end - start) * min_ratio))
    best = -1
    for match in _BOUNDARY_RE.finditer(text, hard_min, target_end):
        best = match.end()
    if best > start:
        return best
    for index in range(target_end, hard_min, -1):
        if text[index - 1].isspace():
            return index
    return target_end


def _safe_start(text: str, start: int, end: int) -> int:
    """重叠窗口的左边界尽力对齐到词/句起点。"""
    if start <= 0:
        return 0
    for index in range(start, min(end, start + 200)):
        if index > 0 and text[index - 1].isspace():
            return index
    return start


@dataclass(frozen=True)
class _Unit:
    """段落级单元：可以跨页（标题块与其后首个段落合并时）。"""

    text: str
    page_start: int
    page_end: int
    section_title: str | None
    section_path: str | None
    is_heading: bool = False


def _units(parsed: ParsedDocument, *, merge_headings: bool) -> list[_Unit]:
    """把段落块转成切分单元。

    `merge_headings=True` 时，标题行并入其后第一个段落：标题单独成块既浪费索引，
    又会让 `section_title` 与实际内容脱节。找不到后续段落的标题保留为独立单元。
    """
    raw: list[_Unit] = []
    for block in parsed.blocks:
        text = block.text.strip()
        if not text:
            continue
        raw.append(
            _Unit(
                text=text,
                page_start=block.page_number,
                page_end=block.page_number,
                section_title=block.section_title,
                section_path=block.section_path,
                is_heading=block.is_heading,
            )
        )
    if not merge_headings:
        return raw

    merged: list[_Unit] = []
    pending: _Unit | None = None
    for unit in raw:
        if unit.is_heading:
            if pending is not None:
                merged.append(pending)  # 连续标题：先保留上一个
            pending = unit
            continue
        if pending is not None:
            merged.append(
                _Unit(
                    text=f"{pending.text}\n{unit.text}",
                    page_start=pending.page_start,
                    page_end=unit.page_end,
                    section_title=pending.section_title or unit.section_title,
                    section_path=pending.section_path or unit.section_path,
                    is_heading=False,
                )
            )
            pending = None
            continue
        merged.append(unit)
    if pending is not None:
        merged.append(pending)
    return merged


def _join_units(units: list[_Unit]) -> tuple[str, list[tuple[int, int, _Unit]]]:
    """把段落块拼接为带偏移索引的全文（块之间用两个换行）。"""
    pieces: list[str] = []
    spans: list[tuple[int, int, _Unit]] = []
    cursor = 0
    for unit in units:
        if pieces:
            pieces.append("\n\n")
            cursor += 2
        spans.append((cursor, cursor + len(unit.text), unit))
        pieces.append(unit.text)
        cursor += len(unit.text)
    return "".join(pieces), spans


def _page_span(
    start: int, end: int, spans: list[tuple[int, int, _Unit]]
) -> tuple[int, int]:
    pages: list[int] = []
    for span_start, span_end, unit in spans:
        if span_end > start and span_start < end:
            pages.extend([unit.page_start, unit.page_end])
    if not pages:
        return 1, 1
    return min(pages), max(pages)


class FixedSizeChunker:
    """固定长度切分：字符窗口 + 重叠，窗口边界尽量对齐句子/空白。

    不利用章节信息，可能把一个报警说明或参数表切成两半；作为对照基线，
    用于和结构感知切分做实测对比。
    """

    strategy = "fixed"

    def __init__(
        self,
        *,
        chunk_size: int = 900,
        chunk_overlap: int = 150,
        chunking_version: str = "fixed-v1",
        min_chunk_chars: int = 60,
    ) -> None:
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap 必须小于 chunk_size")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.chunking_version = chunking_version
        self.min_chunk_chars = min_chunk_chars

    def chunk(self, parsed: ParsedDocument) -> list[Chunk]:
        units = _units(parsed, merge_headings=False)
        if not units:
            return []
        text, spans = _join_units(units)
        chunks: list[Chunk] = []
        start = 0
        index = 0
        length = len(text)
        while start < length:
            current_start = start
            target_end = min(length, start + self.chunk_size)
            end = (
                target_end
                if target_end >= length
                else _safe_end(text, start, target_end)
            )
            if end <= start:  # 极端边界保护
                end = min(length, start + max(1, self.chunk_size // 2))
            content = text[start:end].strip()
            if len(content) >= self.min_chunk_chars or (end >= length and content):
                page_start, page_end = _page_span(start, end, spans)
                first = next(
                    (u for s, e, u in spans if e > start and s < end), units[0]
                )
                chunks.append(
                    Chunk(
                        chunk_index=index,
                        content=content,
                        page_start=page_start,
                        page_end=page_end,
                        section_title=first.section_title,
                        section_path=first.section_path,
                        char_count=len(content),
                        chunking_strategy=self.strategy,
                        metadata={
                            "strategy": self.strategy,
                            "chunk_size": self.chunk_size,
                            "chunk_overlap": self.chunk_overlap,
                            "multi_section": len(
                                {
                                    u.section_path
                                    for s, e, u in spans
                                    if e > start and s < end
                                }
                            )
                            > 1,
                        },
                    )
                )
                index += 1
            if end >= length:
                break
            next_start = max(0, end - self.chunk_overlap)
            start = _safe_start(text, next_start, end)
            if start <= current_start:  # 必须前进，避免死循环
                start = max(next_start, current_start + 1)
        return chunks


class StructuralChunker:
    """结构感知切分：按章节 → 段落聚合，超长段落按句子拆分。

    与固定切分的区别：
    - 章节变化时开启新 Chunk（不把两章内容拼在一起）；
    - 段落整体保留（除非单段超过 `chunk_size`，此时按句子边界拆分）；
    - 重叠只从上一 Chunk 尾部按句子回带，并计入容量上限，记录 `overlap_chars`；
    - 极端短块（如单个标题/表头）会与相邻块合并，避免产生无检索价值的碎片。
    """

    strategy = "structural"

    def __init__(
        self,
        *,
        chunk_size: int = 1000,
        chunk_overlap: int = 150,
        chunking_version: str = "structural-v1",
        min_chunk_chars: int = 120,
    ) -> None:
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap 必须小于 chunk_size")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.chunking_version = chunking_version
        self.min_chunk_chars = min_chunk_chars

    def chunk(self, parsed: ParsedDocument) -> list[Chunk]:
        units = _units(parsed, merge_headings=True)
        if not units:
            return []

        pieces: list[_Unit] = []
        for unit in units:
            if len(unit.text) <= self.chunk_size:
                pieces.append(unit)
                continue
            buffer = ""
            for sentence in split_sentences(unit.text):
                candidate = f"{buffer} {sentence}".strip() if buffer else sentence
                if len(candidate) <= self.chunk_size:
                    buffer = candidate
                    continue
                if buffer:
                    pieces.append(self._derive(unit, buffer))
                while len(sentence) > self.chunk_size:
                    cut = _safe_end(sentence, 0, self.chunk_size)
                    head = sentence[:cut].strip()
                    if head:
                        pieces.append(self._derive(unit, head))
                    sentence = sentence[cut:].strip()
                    if not sentence:
                        break
                buffer = sentence
            if buffer.strip():
                pieces.append(self._derive(unit, buffer.strip()))

        chunks: list[Chunk] = []
        current: list[_Unit] = []
        current_len = 0
        overlap_unit: _Unit | None = None

        def overlap_text() -> str:
            return self._tail(overlap_unit.text) if overlap_unit is not None else ""

        def flush() -> None:
            nonlocal current, current_len, overlap_unit
            if not current:
                return
            body = "\n\n".join(u.text for u in current).strip()
            carry = overlap_text()
            content = f"{carry}\n\n{body}".strip() if carry else body
            pages = [p for u in current for p in (u.page_start, u.page_end)]
            if overlap_unit is not None:
                pages.extend([overlap_unit.page_start, overlap_unit.page_end])
            first = current[0]
            chunks.append(
                Chunk(
                    chunk_index=len(chunks),
                    content=content,
                    page_start=min(pages),
                    page_end=max(pages),
                    section_title=first.section_title,
                    section_path=first.section_path,
                    char_count=len(content),
                    chunking_strategy=self.strategy,
                    metadata={
                        "strategy": self.strategy,
                        "chunk_size": self.chunk_size,
                        "chunk_overlap": self.chunk_overlap,
                        "overlap_chars": len(carry),
                        "heading_blocks": sum(1 for u in current if u.is_heading),
                        "page_numbers": sorted(set(pages)),
                    },
                )
            )
            overlap_unit = current[-1] if self.chunk_overlap > 0 else None
            current = []
            current_len = 0

        for unit in pieces:
            section_changed = bool(
                current and unit.section_path != current[-1].section_path
            )
            if section_changed:
                flush()
            capacity = self.chunk_size - len(overlap_text())
            if current and current_len + len(unit.text) + 2 > capacity:
                flush()
            current.append(unit)
            current_len += len(unit.text) + 2
        flush()

        # 极端短块合并：与前一 Chunk 合并（受限长度），避免索引被表头/碎片占满
        merged: list[Chunk] = []
        for chunk in chunks:
            if (
                merged
                and chunk.char_count < self.min_chunk_chars
                and merged[-1].char_count + chunk.char_count <= self.chunk_size * 1.15
            ):
                previous = merged.pop()
                merged.append(
                    Chunk(
                        chunk_index=previous.chunk_index,
                        content=f"{previous.content}\n\n{chunk.content}",
                        page_start=min(previous.page_start, chunk.page_start),
                        page_end=max(previous.page_end, chunk.page_end),
                        section_title=previous.section_title,
                        section_path=previous.section_path,
                        char_count=previous.char_count + chunk.char_count + 2,
                        chunking_strategy=self.strategy,
                        metadata={
                            **previous.metadata,
                            "merged_small_chunk": True,
                        },
                    )
                )
                continue
            merged.append(chunk)

        return [
            Chunk(
                chunk_index=index,
                content=chunk.content,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                section_title=chunk.section_title,
                section_path=chunk.section_path,
                char_count=chunk.char_count,
                chunking_strategy=chunk.chunking_strategy,
                metadata=chunk.metadata,
            )
            for index, chunk in enumerate(merged)
        ]

    def _derive(self, unit: _Unit, text: str) -> _Unit:
        return _Unit(
            text=text,
            page_start=unit.page_start,
            page_end=unit.page_end,
            section_title=unit.section_title,
            section_path=unit.section_path,
            is_heading=unit.is_heading,
        )

    def _tail(self, text: str) -> str:
        """从上一块尾部取不超过 overlap 的完整句子。"""
        if self.chunk_overlap <= 0:
            return ""
        tail = text[-self.chunk_overlap :]
        sentences = split_sentences(tail)
        if len(sentences) > 1:
            return " ".join(sentences[1:]).strip()[-self.chunk_overlap :]
        return tail.strip()


STRATEGIES: dict[str, type[FixedSizeChunker] | type[StructuralChunker]] = {
    "fixed": FixedSizeChunker,
    "structural": StructuralChunker,
}


def get_chunker(
    strategy: str,
    *,
    chunk_size: int = 1000,
    chunk_overlap: int = 150,
    chunking_version: str | None = None,
) -> FixedSizeChunker | StructuralChunker:
    """按名称构造切分器（未知策略明确报错，不静默回退）。"""
    if strategy not in STRATEGIES:
        raise ValueError(f"未知切分策略 {strategy!r}，可选：{sorted(STRATEGIES)}")
    cls = STRATEGIES[strategy]
    return cls(  # type: ignore[return-value]
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        chunking_version=chunking_version or f"{strategy}-v1",
    )
