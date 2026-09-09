"""PyMuPDF text extraction with per-page character offsets (SPEC §13.3).

Guarantees:

- per-page ``start_offset``/``end_offset`` are consistent within the extracted
  document text (``"".join`` semantics: pages are separated by a single
  ``"\\n"`` and every page slice round-trips exactly);
- scanned / no-text PDFs are marked ``needs_ocr`` — empty text is never
  silently indexed (SPEC §13.3);
- unparseable bytes yield ``failed`` rather than an exception or fake text;
- the content hash (sha256 hex) of the raw bytes is always returned, so
  provenance survives even for failed extractions.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

#: Version stamped on documents extracted with this parser.
PDF_EXTRACT_VERSION = "pymupdf-1"

#: Below this many extractable characters a PDF is treated as scanned
#: (``needs_ocr``) rather than indexed as near-empty text.
MIN_OK_TEXT_CHARS = 25

#: Separator between page texts in the assembled document text.
PAGE_SEPARATOR = "\n"

_STATUS_OK = "ok"
_STATUS_NEEDS_OCR = "needs_ocr"
_STATUS_FAILED = "failed"


@dataclass(frozen=True)
class PageText:
    """One page's text with offsets into the assembled extraction text."""

    page_number: int  # 1-based
    text: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class PdfExtraction:
    """Result of :func:`extract_pdf` (SPEC §13.3 retention fields)."""

    pages: list[PageText] = field(default_factory=list)
    extraction_status: str = _STATUS_FAILED  # ok | needs_ocr | failed
    content_hash: str = ""
    page_count: int = 0
    notes: str = ""

    @property
    def text(self) -> str:
        """Assembled text all page offsets index into."""
        return PAGE_SEPARATOR.join(page.text for page in self.pages)


_LINE_END_SPACES_RE = re.compile(r"[ \t]+\n")


def _normalize_page_text(raw: str) -> str:
    """Normalize newlines and trailing spaces; keep internal line structure."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _LINE_END_SPACES_RE.sub("\n", text)
    return text.strip()


def _open_pdf(content: bytes) -> Any:
    try:
        import pymupdf  # PyMuPDF (current import name)
    except ImportError:  # pragma: no cover - dependency is declared in pyproject
        import fitz as pymupdf  # type: ignore[no-redef]
    return pymupdf.open(stream=content, filetype="pdf")


def extract_pdf(content: bytes) -> PdfExtraction:
    """Extract text page-by-page from a PDF, with honest status reporting."""
    content_hash = hashlib.sha256(content or b"").hexdigest()
    if not content:
        return PdfExtraction(
            extraction_status=_STATUS_FAILED,
            content_hash=content_hash,
            page_count=0,
            notes="empty content",
        )

    try:
        doc = _open_pdf(content)
    except (RuntimeError, ValueError, TypeError) as exc:
        # MuPDF raises RuntimeError subclasses for broken documents.
        return PdfExtraction(
            extraction_status=_STATUS_FAILED,
            content_hash=content_hash,
            page_count=0,
            notes=f"cannot open PDF: {exc}",
        )

    page_count = 0
    try:
        page_count = doc.page_count
        raw_pages = [_normalize_page_text(doc[i].get_text("text")) for i in range(page_count)]
    except (RuntimeError, ValueError) as exc:
        return PdfExtraction(
            extraction_status=_STATUS_FAILED,
            content_hash=content_hash,
            page_count=page_count,
            notes=f"text extraction error: {exc}",
        )
    finally:
        doc.close()

    visible_chars = sum(len(page.strip()) for page in raw_pages)
    if visible_chars < MIN_OK_TEXT_CHARS:
        return PdfExtraction(
            pages=_page_texts(raw_pages),
            extraction_status=_STATUS_NEEDS_OCR,
            content_hash=content_hash,
            page_count=page_count,
            notes=(
                f"only {visible_chars} extractable text characters across "
                f"{page_count} page(s); likely scanned — text not indexed "
                "(needs_ocr, SPEC §13.3)"
            ),
        )
    return PdfExtraction(
        pages=_page_texts(raw_pages),
        extraction_status=_STATUS_OK,
        content_hash=content_hash,
        page_count=page_count,
    )


def _page_texts(raw_pages: list[str]) -> list[PageText]:
    pages: list[PageText] = []
    cursor = 0
    for number, text in enumerate(raw_pages, start=1):
        if pages:
            cursor += len(PAGE_SEPARATOR)
        start = cursor
        cursor += len(text)
        pages.append(PageText(page_number=number, text=text, start_offset=start, end_offset=cursor))
    return pages
