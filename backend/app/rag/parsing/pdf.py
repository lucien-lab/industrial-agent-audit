"""PDF 解析：按页提取文本，保留页码、标题层级与段落块。

设计目标（工业文档优先）：
1. **页码可追溯**：每个段落块都带 `page_number`，后续 Chunk 的 `page_start/page_end`
   由此计算，绝不凭空生成页码；
2. **不破坏技术内容**：只做空白归一化与重复页眉/页脚剔除，不改写数字、单位、
   型号、报警代码与传感器名称；不做小写化、不做同义词替换；
3. **标题尽量识别**：以字号 + 粗体 + 编号模式三类信号判定，记录层级；
4. **失败可见**：单页解析异常记入 `PageText.error` 并把整篇标为 `partial`；
   完全提取不到文字的 PDF（例如扫描件）标为 `failed`，不补写任何内容；
5. 解析质量（每页字符数、疑似表格行、重复页眉页脚）写入 `quality_notes`，
   供入库时人工复核。

局限：不实现 OCR，不重建跨页表格，不做版面重构。扫描版文档会在状态中明确体现，
不会被伪装成解析成功。
"""

from __future__ import annotations

import hashlib
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.rag.errors import DocumentParseError

# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PageText:
    """单页解析结果。`error` 非空表示该页解析失败（页码仍保留）。"""

    page_number: int
    text: str
    char_count: int
    error: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TextBlock:
    """段落级文本块：切分模块的最小输入单位。"""

    order: int
    page_number: int
    text: str
    section_title: str | None
    section_level: int | None
    section_path: str | None
    is_heading: bool = False
    block_kind: str = "paragraph"  # heading / paragraph / table_like


@dataclass(frozen=True)
class SectionSpan:
    title: str
    level: int
    page_number: int


@dataclass(frozen=True)
class ParsedDocument:
    path: str
    file_sha256: str
    file_bytes: int
    page_count: int
    pages: tuple[PageText, ...]
    blocks: tuple[TextBlock, ...]
    sections: tuple[SectionSpan, ...]
    parse_status: str  # parsed / partial / failed
    warnings: tuple[str, ...] = ()
    quality_notes: tuple[str, ...] = ()
    removed_repeated_lines: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages if page.text)

    def page_text(self, page_number: int) -> str:
        for page in self.pages:
            if page.page_number == page_number:
                return page.text
        raise KeyError(page_number)


@dataclass(frozen=True)
class _Line:
    text: str
    size: float
    bold: bool


# --------------------------------------------------------------------------- #
# 文本清理
# --------------------------------------------------------------------------- #

_UNICODE_SPACES = {
    "\u00a0": " ",
    "\u2002": " ",
    "\u2003": " ",
    "\u2009": " ",
    "\u202f": " ",
    "\u200b": "",
    "\ufeff": "",
}
_WS_RE = re.compile(r"[ \t]+")
_PAGE_NUMBER_RE = re.compile(
    r"^(?:page\s*)?\d{1,4}(?:\s*(?:/|of)\s*\d{1,4})?$", re.IGNORECASE
)
_MULTI_SPACE_GAP_RE = re.compile(r"\S\s{3,}\S")
_SENTENCE_END = tuple("。！？.!?;；:：")


def normalize_line(raw: str) -> str:
    """归一化单行：统一空白、去掉控制字符；**不改动**大小写与符号。"""
    text = raw
    for src, dst in _UNICODE_SPACES.items():
        text = text.replace(src, dst)
    text = text.replace("\r", " ").replace("\n", " ")
    text = _WS_RE.sub(" ", text)
    return text.strip()


def _normalize_for_repeat_key(line: str) -> str:
    """页眉页脚重复判定用的键：数字替换为占位符，去掉页码噪声。"""
    key = normalize_line(line).lower()
    key = re.sub(r"\d+", "#", key)
    key = re.sub(r"\s+", " ", key)
    return key.strip(" -–—|·•")


def _is_probably_table_line(line: str) -> bool:
    """疑似表格行：多列之间出现 3 个以上连续空格，或数字/单位密度很高。"""
    if _MULTI_SPACE_GAP_RE.search(line):
        return True
    tokens = line.split()
    if len(tokens) < 4:
        return False
    numeric = sum(1 for t in tokens if re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?%?", t))
    return numeric / len(tokens) >= 0.4


# --------------------------------------------------------------------------- #
# 标题识别
# --------------------------------------------------------------------------- #

_NUMBERED_HEADING_RE = re.compile(
    r"^(?P<num>(?:\d{1,2})(?:\.\d{1,2}){0,3})[.)]?\s+(?P<title>[A-Z\u4e00-\u9fff].{2,90})$"
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_BULLET_PREFIXES = ("•", "-", "–", "—", "*", "·", "▪", "o ")
_HEADING_BLOCKLIST = {
    "references",
    "training",
    "contents",
    "index",
}


def _looks_like_heading(
    line: _Line, body_size: float, *, has_explicit_numbering: bool = False
) -> bool:
    text = line.text.strip()
    if not (2 <= len(text) <= 120):
        return False
    if text.endswith((",", ";", "，", "；")):
        return False
    if text.startswith(_BULLET_PREFIXES):
        return False
    words = text.split()
    is_cjk = bool(_CJK_RE.search(text))
    if is_cjk and len(text) > 40:  # 中文标题一般较短
        return False
    if not is_cjk and len(words) > 14:
        return False
    if text.endswith("."):
        # “2.3.1 Pump.” 这类标题允许以句号结尾，但普通句子不允许
        return bool(has_explicit_numbering)
    size_signal = line.size >= body_size * 1.08 and line.size > body_size + 0.4
    bold_signal = line.bold and line.size >= body_size - 0.2
    if has_explicit_numbering and (size_signal or bold_signal or line.size >= body_size):
        return True
    if size_signal and (line.bold or len(words) <= 10):
        return True
    return bold_signal and len(words) <= 8 and len(text) <= 70


def _heading_level(text: str, line: _Line, body_size: float) -> int:
    match = _NUMBERED_HEADING_RE.match(text.strip())
    if match:
        return min(4, match.group("num").count(".") + 1)
    if line.size >= body_size * 1.4:
        return 1
    if line.size >= body_size * 1.15:
        return 2
    return 3


def _heading_title(text: str) -> str:
    match = _NUMBERED_HEADING_RE.match(text.strip())
    if match:
        return f"{match.group('num')} {match.group('title').strip()}"
    return text.strip()


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #


class PdfParser:
    """PyMuPDF 解析器。可注入参数便于测试与调节。"""

    def __init__(
        self,
        *,
        strip_repeated_headers: bool = True,
        repeat_page_ratio: float = 0.6,
        repeat_min_pages: int = 3,
        min_page_chars: int = 40,
        max_header_footer_lines: int = 3,
    ) -> None:
        self.strip_repeated_headers = strip_repeated_headers
        self.repeat_page_ratio = repeat_page_ratio
        self.repeat_min_pages = repeat_min_pages
        self.min_page_chars = min_page_chars
        self.max_header_footer_lines = max_header_footer_lines

    # -- 对外入口 ---------------------------------------------------------- #
    def parse(self, path: Path | str) -> ParsedDocument:
        started = time.perf_counter()
        file_path = Path(path)
        if not file_path.is_file():
            raise DocumentParseError("文档不存在", path=str(file_path))
        raw = file_path.read_bytes()
        try:
            import fitz  # PyMuPDF，延迟导入便于给出清晰错误
        except ImportError as exc:  # pragma: no cover - 环境已安装
            raise DocumentParseError("缺少 PyMuPDF 依赖，无法解析 PDF") from exc

        try:
            document = fitz.open(stream=raw, filetype="pdf")
        except Exception as exc:
            raise DocumentParseError(
                "PDF 无法打开（可能已损坏或受密码保护）", path=str(file_path)
            ) from exc

        warnings: list[str] = []
        quality_notes: list[str] = []
        try:
            if document.needs_pass:
                raise DocumentParseError("PDF 受密码保护，本阶段不支持解密", path=str(file_path))
            page_count = document.page_count
            page_lines: list[list[_Line]] = []
            page_errors: dict[int, str] = {}
            for index in range(page_count):
                page_number = index + 1
                try:
                    page = document.load_page(index)
                    page_lines.append(self._extract_lines(page))
                except Exception as exc:  # 单页失败不放弃整篇
                    page_lines.append([])
                    page_errors[page_number] = f"{type(exc).__name__}: {exc}"
                    warnings.append(f"第 {page_number} 页解析失败：{type(exc).__name__}")
            metadata = dict(document.metadata or {})
        finally:
            document.close()

        body_size = self._body_font_size(page_lines)
        removed_keys: set[str] = set()
        if self.strip_repeated_headers and page_count >= 2:
            removed_keys = self._detect_repeated_lines(page_lines)
            if removed_keys:
                quality_notes.append(
                    f"剔除重复页眉/页脚 {len(removed_keys)} 种"
                )

        pages: list[PageText] = []
        blocks: list[TextBlock] = []
        sections: list[SectionSpan] = []
        block_order = 0
        section_stack: list[tuple[int, str]] = []

        for index, lines in enumerate(page_lines):
            page_number = index + 1
            kept: list[_Line] = []
            for position, line in enumerate(lines):
                if not line.text:
                    continue
                is_edge = (
                    position < self.max_header_footer_lines
                    or position >= len(lines) - self.max_header_footer_lines
                )
                if is_edge and _normalize_for_repeat_key(line.text) in removed_keys:
                    continue
                if _PAGE_NUMBER_RE.fullmatch(line.text.strip()):
                    continue
                kept.append(line)

            page_text = "\n".join(line.text for line in kept)
            pages.append(
                PageText(
                    page_number=page_number,
                    text=page_text,
                    char_count=len(page_text),
                    error=page_errors.get(page_number),
                )
            )

            paragraph: list[str] = []

            def flush_paragraph() -> None:
                nonlocal block_order, paragraph
                if not paragraph:
                    return
                text = "\n".join(paragraph).strip()
                paragraph = []
                if not text:
                    return
                title = section_stack[-1][1] if section_stack else None
                level = section_stack[-1][0] if section_stack else None
                path_text = " > ".join(title for _, title in section_stack) or None
                blocks.append(
                    TextBlock(
                        order=block_order,
                        page_number=page_number,
                        text=text,
                        section_title=title,
                        section_level=level,
                        section_path=path_text,
                        block_kind=(
                            "table_like"
                            if sum(_is_probably_table_line(l) for l in text.split("\n"))
                            >= max(1, len(text.split("\n")) // 2)
                            else "paragraph"
                        ),
                    )
                )
                block_order += 1

            for line in kept:
                numbered = _NUMBERED_HEADING_RE.match(line.text.strip())
                if _looks_like_heading(
                    line, body_size, has_explicit_numbering=bool(numbered)
                ):
                    flush_paragraph()
                    title = _heading_title(line.text)
                    level = _heading_level(line.text, line, body_size)
                    while section_stack and section_stack[-1][0] >= level:
                        section_stack.pop()
                    section_stack.append((level, title))
                    sections.append(
                        SectionSpan(title=title, level=level, page_number=page_number)
                    )
                    blocks.append(
                        TextBlock(
                            order=block_order,
                            page_number=page_number,
                            text=line.text.strip(),
                            section_title=title,
                            section_level=level,
                            section_path=" > ".join(t for _, t in section_stack),
                            is_heading=True,
                            block_kind="heading",
                        )
                    )
                    block_order += 1
                    continue
                paragraph.append(line.text)
            flush_paragraph()

        parse_status, status_notes = self._assess(pages, page_count)
        quality_notes.extend(status_notes)

        if any(page.error for page in pages):
            parse_status = "failed" if parse_status == "failed" else "partial"

        non_empty = [p for p in pages if p.char_count >= self.min_page_chars]
        quality_notes.append(f"有效页 {len(non_empty)}/{page_count}（每页 ≥{self.min_page_chars} 字符）")
        if blocks:
            table_like = sum(1 for b in blocks if b.block_kind == "table_like")
            quality_notes.append(f"段落块 {len(blocks)}，疑似表格块 {table_like}")

        return ParsedDocument(
            path=str(file_path),
            file_sha256=hashlib.sha256(raw).hexdigest(),
            file_bytes=len(raw),
            page_count=page_count,
            pages=tuple(pages),
            blocks=tuple(blocks),
            sections=tuple(sections),
            parse_status=parse_status,
            warnings=tuple(warnings),
            quality_notes=tuple(quality_notes),
            removed_repeated_lines=tuple(sorted(removed_keys)),
            metadata={
                "title": (metadata.get("title") or "").strip() or None,
                "author": (metadata.get("author") or "").strip() or None,
                "subject": (metadata.get("subject") or "").strip() or None,
                "producer": (metadata.get("producer") or "").strip() or None,
                "format": metadata.get("format"),
                "body_font_size": round(body_size, 2),
            },
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    # -- 内部实现 ---------------------------------------------------------- #
    @staticmethod
    def _extract_lines(page) -> list[_Line]:
        data = page.get_text("dict", sort=True)
        lines: list[_Line] = []
        for block in data.get("blocks", []):
            if block.get("type") != 0:  # 图片块不提取文字
                continue
            for line in block.get("lines", []):
                spans = [s for s in line.get("spans", []) if s.get("text")]
                if not spans:
                    continue
                text = normalize_line("".join(s["text"] for s in spans))
                if not text:
                    continue
                size = max(float(s.get("size") or 0) for s in spans)
                bold = any(
                    "bold" in str(s.get("font", "")).lower()
                    or bool(int(s.get("flags") or 0) & 16)
                    for s in spans
                )
                lines.append(_Line(text=text, size=size, bold=bold))
        return lines

    @staticmethod
    def _body_font_size(page_lines: list[list[_Line]]) -> float:
        weighted: list[float] = []
        for lines in page_lines:
            for line in lines:
                weighted.extend([line.size] * max(1, len(line.text)))
        if not weighted:
            return 0.0
        return float(statistics.median(weighted))

    def _detect_repeated_lines(self, page_lines: list[list[_Line]]) -> set[str]:
        """找出在多页固定位置反复出现的短行（页眉/页脚/水印）。"""
        counter: dict[str, int] = {}
        for lines in page_lines:
            edge = lines[: self.max_header_footer_lines] + lines[
                -self.max_header_footer_lines :
            ]
            seen: set[str] = set()
            for line in edge:
                key = _normalize_for_repeat_key(line.text)
                if len(key) < 6 or key in seen:
                    continue
                seen.add(key)
                counter[key] = counter.get(key, 0) + 1

        page_count = len(page_lines)
        threshold = max(self.repeat_min_pages, int(page_count * self.repeat_page_ratio))
        # 页数很少时降级为“出现在全部页”
        if page_count < self.repeat_min_pages:
            threshold = page_count
        return {key for key, count in counter.items() if count >= threshold}

    def _assess(
        self, pages: list[PageText], page_count: int
    ) -> tuple[str, list[str]]:
        if page_count == 0:
            return "failed", ["PDF 没有任何页面"]
        total_chars = sum(page.char_count for page in pages)
        if total_chars == 0:
            return (
                "failed",
                ["全部页面均未提取到文字：可能是扫描件或纯图片 PDF，本阶段不实现 OCR"],
            )
        empty_pages = [p.page_number for p in pages if p.char_count < self.min_page_chars]
        notes: list[str] = []
        if empty_pages:
            notes.append(
                "低文本页（可能为图片/图表/扫描页）："
                + ", ".join(str(n) for n in empty_pages[:20])
                + ("..." if len(empty_pages) > 20 else "")
            )
        status = "partial" if empty_pages else "parsed"
        return status, notes


def parse_pdf(path: Path | str, **kwargs: object) -> ParsedDocument:
    """模块级便捷函数。"""
    return PdfParser(**kwargs).parse(path)  # type: ignore[arg-type]
