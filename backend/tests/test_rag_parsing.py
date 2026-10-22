"""PDF 解析测试：真实生成 PDF，不使用 Mock 解析结果。

覆盖：
- 正常多页解析与页码映射；
- 重复页眉/页脚剔除但不删正文；
- 标题识别（字号 + 粗体）；
- 技术数字、单位、型号、传感器名不被清洗破坏；
- 每页解析失败 → partial 且记录页码与错误；
- 无文字（扫描/空白）PDF → failed；
- 损坏/加密 PDF → DocumentParseError；
- 表格行保留。
"""

from __future__ import annotations

import fitz
import pytest

from app.rag.errors import DocumentParseError
from app.rag.parsing.pdf import PdfParser, normalize_line, parse_pdf


def _make_pdf(path, pages: list[str], *, header: str | None = None) -> None:
    document = fitz.open()
    for index, text in enumerate(pages, start=1):
        page = document.new_page()
        y = 72
        if header:
            page.insert_text((72, 36), header, fontsize=8)
        page.insert_text((72, y), f"page-{index}", fontsize=8)
        y += 18
        for line in text.split("\n"):
            page.insert_text((72, y), line, fontsize=11)
            y += 14
    document.save(path)
    document.close()


def test_parse_multipage_keeps_page_mapping(tmp_path):
    path = tmp_path / "manual.pdf"
    _make_pdf(
        path,
        [
            "Pump overview and safety notes for the air production unit.",
            "Pressure limits: 8.2 bar at TP2; TP3 reads the pneumatic panel.",
            "Oil temperature sensor TP3 details and maintenance notes.",
        ],
        header="ACME INDUSTRIAL MANUAL",
    )
    parsed = parse_pdf(path)

    assert parsed.parse_status == "parsed"
    assert parsed.page_count == 3
    assert [page.page_number for page in parsed.pages] == [1, 2, 3]
    assert "8.2 bar" in parsed.page_text(2)
    assert "TP2" in parsed.page_text(2)
    # 重复页眉被剔除，正文与页码映射保持
    assert "ACME INDUSTRIAL MANUAL" not in parsed.text
    assert parsed.removed_repeated_lines
    for block in parsed.blocks:
        assert 1 <= block.page_number <= 3
    assert parsed.file_sha256 and len(parsed.file_sha256) == 64


def test_parse_preserves_units_models_and_sensor_names(tmp_path):
    path = tmp_path / "spec.pdf"
    _make_pdf(
        path,
        [
            "Model GA30-90 rated at 125 psig.\n"
            "Sensor TP2 reads 8.2 bar; Motor_current is 7 A.\n"
            "Alarm code E-204 resets after 30 s.\n"
            "3,000 Hours / 6,000 Hours service interval."
        ],
    )
    parsed = parse_pdf(path)
    text = parsed.page_text(1)
    for token in ("GA30-90", "125 psig", "TP2", "8.2 bar", "Motor_current", "E-204", "3,000 Hours"):
        assert token in text, token


def test_parse_detects_heading_by_font_size_and_bold(tmp_path):
    path = tmp_path / "headings.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 60), "Compressor Types", fontsize=18, fontname="hebo")
    page.insert_text((72, 90), "Positive displacement compressors trap air and reduce volume.", fontsize=10)
    page.insert_text((72, 120), "Maintenance", fontsize=13, fontname="hebo")
    page.insert_text((72, 150), "Inspect filters and change lubricant as specified.", fontsize=10)
    document.save(path)
    document.close()

    parsed = parse_pdf(path)
    titles = [section.title for section in parsed.sections]
    assert "Compressor Types" in titles
    assert "Maintenance" in titles


def test_page_level_failure_marks_document_partial(tmp_path, monkeypatch):
    path = tmp_path / "partial.pdf"
    _make_pdf(path, ["first page text " * 10, "second page text " * 10, "third page text " * 10])

    original = PdfParser._extract_lines

    def flaky(page):
        if page.number == 1:  # 第 2 页
            raise RuntimeError("synthetic extraction failure")
        return original(page)

    monkeypatch.setattr(PdfParser, "_extract_lines", staticmethod(flaky))
    parsed = parse_pdf(path)

    assert parsed.parse_status == "partial"
    assert parsed.pages[1].error and "synthetic extraction failure" in parsed.pages[1].error
    assert any("第 2 页解析失败" in warning for warning in parsed.warnings)
    # 其它页面仍然可用
    assert parsed.page_text(1).strip()
    assert parsed.page_text(3).strip()


def test_blank_pdf_is_failed_not_faked(tmp_path):
    path = tmp_path / "scanned.pdf"
    document = fitz.open()
    document.new_page()  # 无文字，模拟扫描页
    document.save(path)
    document.close()

    parsed = parse_pdf(path)
    assert parsed.parse_status == "failed"
    assert parsed.blocks == ()
    assert any("OCR" in note for note in parsed.quality_notes)


def test_corrupt_pdf_raises_document_parse_error(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4 this is not a real pdf")
    with pytest.raises(DocumentParseError):
        parse_pdf(path)


def test_missing_file_raises(tmp_path):
    with pytest.raises(DocumentParseError):
        parse_pdf(tmp_path / "nope.pdf")


def test_normalize_line_keeps_case_and_symbols():
    assert normalize_line("  TP2\u00a0(bar) – 8.2  bar  ") == "TP2 (bar) – 8.2 bar"
    assert normalize_line("Motor_current = 7A") == "Motor_current = 7A"


def test_table_like_block_is_preserved(tmp_path):
    path = tmp_path / "table.pdf"
    _make_pdf(
        path,
        [
            "Parameter 25 psig 125 psig 175 psig\n"
            "Stage 1 2 3\n"
            "Flow 4.5 7.2 9.8"
        ],
    )
    parsed = parse_pdf(path)
    text = parsed.page_text(1)
    assert "25 psig" in text and "175 psig" in text
    assert any(block.block_kind == "table_like" for block in parsed.blocks)
