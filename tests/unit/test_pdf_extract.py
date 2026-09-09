"""Unit tests for PDF text extraction (SPEC §13.3) — offline, synthetic PDFs."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from quarterline.ingest.pdf_extract import extract_pdf

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "documents"

PARA = (
    "Page filler paragraph with realistic management discussion style text for "
    "the parser to extract, mentioning revenue and operating results. "
)


def _text_pdf(pages: int = 2) -> bytes:
    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page()
        page.insert_textbox(
            fitz.Rect(50, 50, page.rect.width - 50, page.rect.height - 50),
            f"Quarterly review page {index + 1}. " + PARA * 8,
            fontsize=11,
            fontname="helv",
        )
    data = doc.tobytes()
    doc.close()
    return data


def _image_only_pdf() -> bytes:
    """A PDF whose page is a rendered image: no extractable text layer."""
    src = fitz.open()
    page = src.new_page()
    page.insert_textbox(fitz.Rect(50, 50, 540, 700), "This text becomes pixels only.", fontsize=12)
    pix = page.get_pixmap(dpi=90)
    src.close()

    doc = fitz.open()
    image_page = doc.new_page(width=pix.width, height=pix.height)
    image_page.insert_image(image_page.rect, pixmap=pix)
    data = doc.tobytes()
    doc.close()
    return data


def test_committed_text_pdf_extracts_ok_with_per_page_offsets() -> None:
    content = (FIXTURES / "synthetic_mda.pdf").read_bytes()
    result = extract_pdf(content)

    assert result.extraction_status == "ok"
    assert result.page_count == 1
    assert result.content_hash == hashlib.sha256(content).hexdigest()
    assert len(result.pages) == 1

    page = result.pages[0]
    assert page.page_number == 1
    assert page.start_offset == 0
    assert page.end_offset == len(page.text)
    assert "Northwind Manufacturing" in page.text

    # Offset consistency inside the extracted document text.
    assert result.text == page.text
    assert result.text[page.start_offset : page.end_offset] == page.text


def test_multipage_pdf_offsets_are_consistent() -> None:
    result = extract_pdf(_text_pdf(pages=3))
    assert result.extraction_status == "ok"
    assert result.page_count == 3
    assert [p.page_number for p in result.pages] == [1, 2, 3]

    assert result.text == "\n".join(p.text for p in result.pages)
    cursor = 0
    for page in result.pages:
        assert result.text[page.start_offset : page.end_offset] == page.text
        assert page.start_offset == cursor
        cursor = page.end_offset + 1  # "\n" page separator
    assert result.pages[-1].end_offset == len(result.text)
    assert "Quarterly review page 2." in result.pages[1].text


def test_image_only_pdf_is_needs_ocr_and_not_indexed() -> None:
    result = extract_pdf(_image_only_pdf())
    assert result.extraction_status == "needs_ocr"
    assert result.page_count == 1
    assert result.pages, "page structure is retained"
    assert all(not p.text.strip() for p in result.pages)
    assert "needs_ocr" in result.notes or "scanned" in result.notes
    assert result.content_hash


def test_whitespace_only_text_is_needs_ocr() -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(50, 50, 500, 700), "   ", fontsize=11)
    data = doc.tobytes()
    doc.close()
    result = extract_pdf(data)
    assert result.extraction_status == "needs_ocr"


def test_corrupt_bytes_fail_cleanly() -> None:
    result = extract_pdf(b"\x00\x01this is not a pdf at all")
    assert result.extraction_status == "failed"
    assert result.pages == []
    assert result.page_count == 0
    assert "cannot open PDF" in result.notes
    assert result.content_hash == hashlib.sha256(b"\x00\x01this is not a pdf at all").hexdigest()


def test_empty_bytes_fail_cleanly() -> None:
    result = extract_pdf(b"")
    assert result.extraction_status == "failed"
    assert "empty" in result.notes


@pytest.mark.parametrize("payload", [b"%PDF-1.4 truncated"])
def test_broken_header_only_pdf_never_reports_ok(payload: bytes) -> None:
    result = extract_pdf(payload)
    # A broken-but-headered file must never produce silent empty "ok" text.
    assert result.extraction_status in ("failed", "needs_ocr")
