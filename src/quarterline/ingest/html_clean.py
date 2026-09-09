"""SEC filing HTML -> clean, block-structured text with stable character offsets.

Pipeline (SPEC §13.2): content validation -> strip scripts/styles/navigation ->
extract headings, paragraphs and tables (tables as readable text rows, not
blobs) -> normalize whitespace -> record ``start_offset``/``end_offset`` of
every block *into the cleaned text*, so later waves can resolve evidence spans
exactly::

    cleaned.text[block.start_offset:block.end_offset] == block.text

Inline XBRL (``ix:*``) tags degrade to their text content; the hidden
``ix:header`` metadata block is dropped entirely. Scripts are never executed
and their content is never retained (SPEC §2.3.4). Retrieved filing text is
treated as untrusted inert input: nothing here evaluates or interprets it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from bs4.element import (
    CData,
    Comment,
    Declaration,
    Doctype,
    NavigableString,
    ProcessingInstruction,
    Tag,
)

#: Version stamped on documents extracted with this parser (SPEC §9.7).
HTML_CLEAN_VERSION = "html_clean-1"

#: Block kinds produced by this cleaner.
KIND_HEADING = "heading"
KIND_PARAGRAPH = "paragraph"
KIND_TABLE = "table"

#: Separator inserted between consecutive blocks in the cleaned text.
BLOCK_SEPARATOR = "\n\n"


class ContentValidationError(ValueError):
    """Raised when downloaded content is not usable HTML (SPEC §13.2 validation)."""


@dataclass(frozen=True)
class TextBlock:
    """One heading/paragraph/table block with offsets into the cleaned text."""

    kind: str  # heading | paragraph | table
    text: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class CleanedDocument:
    """Cleaned full text plus ordered blocks and document-level metadata."""

    text: str
    blocks: list[TextBlock]
    metadata: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Content validation
# ---------------------------------------------------------------------------

_HTML_SNIFF_NEEDLES = (
    "<!doctype html",
    "<html",
    "<body",
    "<div",
    "<span",
    "<p>",
    "<p ",
    "<table",
    "<font",
    "<h1",
)

#: EDGAR as-filed SGML wrapper: <DOCUMENT>...<TYPE>...<TEXT>  ...  </DOCUMENT>
_EDGAR_SGML_PROLOGUE_RE = re.compile(r"\A\s*<document\b.*?<text>\s*", re.IGNORECASE | re.DOTALL)
_EDGAR_SGML_TRAILER_RE = re.compile(r"</document>\s*\Z", re.IGNORECASE)


def decode_content(content: bytes) -> str:
    """Decode raw bytes (utf-8-sig, then cp1252, then lossy utf-8)."""
    if not content:
        raise ContentValidationError("document content is empty")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def validate_html_content(content: bytes) -> str:
    """Validate that ``content`` is non-empty HTML and return it decoded.

    Raises :class:`ContentValidationError` for empty content or content that
    does not look like HTML (e.g. a plain-text error page).
    """
    text = decode_content(content)
    head = text[:8192].lower()
    if not any(needle in head for needle in _HTML_SNIFF_NEEDLES):
        raise ContentValidationError("content does not look like HTML (no HTML markup found)")
    return text


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")
_INVISIBLES = {"\u200b", "\ufeff", "\u00ad"}  # zero-width space, BOM, soft hyphen


def normalize_ws(text: str) -> str:
    """Collapse whitespace runs to single spaces and strip invisibles/edges."""
    for char in _INVISIBLES:
        text = text.replace(char, " ")
    text = text.replace("\xa0", " ")
    return _WHITESPACE_RE.sub(" ", text).strip()


# ---------------------------------------------------------------------------
# Strip pass: scripts, styles, inline-XBRL chrome, navigation boilerplate
# ---------------------------------------------------------------------------

_SCRIPTISH_TAGS = ("script", "style", "noscript", "template", "iframe", "object", "embed")
_NAV_TAG_NAMES = {"nav", "footer", "aside"}
_NAV_CLASS_RE = re.compile(r"(?i)\b(nav|navbar|topnav|menu|breadcrumb|sidebar|footer)\b")
_HIDDEN_STYLE_MARKERS = ("display:none", "visibility:hidden")


def _is_hidden(tag: Tag) -> bool:
    style = str(tag.get("style") or "").lower().replace(" ", "")
    if any(marker in style for marker in _HIDDEN_STYLE_MARKERS):
        return True
    return tag.has_attr("hidden")


def _decompose_if_alive(tag: Tag) -> bool:
    """Decompose ``tag`` unless an ancestor removal already destroyed it."""
    if getattr(tag, "decomposed", False):
        return False
    tag.decompose()
    return True


def _strip_untrusted_and_furniture(soup: BeautifulSoup, counters: dict[str, int]) -> None:
    """Remove scripts/styles (never executed, never retained), ix: chrome,
    hidden elements, and navigation boilerplate."""
    # 1. scripts/styles and other non-text furniture.
    for name in _SCRIPTISH_TAGS:
        for tag in list(soup.find_all(name)):
            if _decompose_if_alive(tag):
                key = "styles_removed" if name == "style" else "scripts_removed"
                counters[key] += 1

    # 2. Inline XBRL: header/hidden metadata is dropped; content tags unwrap
    #    to their text (ix:nonFraction -> "1,234", etc.).
    for tag in list(soup.find_all(True)):
        if getattr(tag, "decomposed", False):
            continue
        name = (tag.name or "").lower()
        if not name.startswith("ix:"):
            continue
        if name in ("ix:header", "ix:hidden"):
            if _decompose_if_alive(tag):
                counters["ix_header_removed"] += 1
        else:
            tag.unwrap()
            counters["ix_unwrapped"] += 1

    # 3. Hidden elements (style-based and attribute-based).
    for tag in list(soup.find_all(True)):
        if getattr(tag, "decomposed", False):
            continue
        if _is_hidden(tag) and _decompose_if_alive(tag):
            counters["hidden_removed"] += 1

    # 4. Navigation boilerplate: structural tags + nav-ish class/id hooks.
    for tag in list(soup.find_all(True)):
        if getattr(tag, "decomposed", False):
            continue
        name = (tag.name or "").lower()
        classes = " ".join(tag.get("class") or [])
        element_id = str(tag.get("id") or "")
        nav_hit = (
            name in _NAV_TAG_NAMES
            or _NAV_CLASS_RE.search(classes)
            or _NAV_CLASS_RE.search(element_id)
        )
        if nav_hit and _decompose_if_alive(tag):
            counters["nav_removed"] += 1


# ---------------------------------------------------------------------------
# Block extraction (document order)
# ---------------------------------------------------------------------------

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCK_TAGS = {
    "p",
    "div",
    "table",
    "section",
    "article",
    "main",
    "li",
    "ul",
    "ol",
    "td",
    "th",
    "tr",
    "blockquote",
    "pre",
    "center",
    "figure",
    "figcaption",
    "dl",
    "dt",
    "dd",
}
# Strings that are not real text content (bs4 subclasses of NavigableString).
_NON_TEXT_STRINGS = (Comment, CData, ProcessingInstruction, Declaration, Doctype)


def _table_text(table: Tag) -> str:
    """Render a table as readable text rows: ``cell | cell`` per row."""
    rows: list[str] = []
    # Only outermost rows: layout tables nested in cells stay inside their
    # cell's text instead of being double-counted as separate rows.
    for tr in table.find_all("tr"):
        if tr.find_parent("tr") is not None:
            continue
        cells = tr.find_all(("td", "th"), recursive=False)
        if cells:
            values = [normalize_ws(cell.get_text(" ")) for cell in cells]
        else:
            values = [normalize_ws(tr.get_text(" "))]
        values = [value for value in values if value]
        if values:
            rows.append(" | ".join(values))
    return "\n".join(rows)


def _walk_blocks(element: Tag) -> list[tuple[str, str]]:
    """Depth-first extraction of (kind, normalized text) in document order."""
    blocks: list[tuple[str, str]] = []
    run: list[str] = []

    def flush_run() -> None:
        text = normalize_ws("".join(run))
        run.clear()
        if text:
            blocks.append((KIND_PARAGRAPH, text))

    for child in element.children:
        if isinstance(child, _NON_TEXT_STRINGS):
            continue
        if isinstance(child, NavigableString):
            run.append(str(child))
            continue
        name = (child.name or "").lower()
        if name in _HEADING_TAGS:
            flush_run()
            text = normalize_ws(child.get_text(" "))
            if text:
                blocks.append((KIND_HEADING, text))
        elif name == "table":
            flush_run()
            text = _table_text(child)
            if text:
                blocks.append((KIND_TABLE, text))
        elif name in _BLOCK_TAGS:
            flush_run()
            blocks.extend(_walk_blocks(child))
        else:
            # Inline element (b/i/span/font/a/sub/sup/ix:* remnants): keep in
            # the current text run so inline formatting does not split prose.
            run.append(child.get_text(" "))
    flush_run()
    return blocks


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def clean_html(content: bytes) -> CleanedDocument:
    """Validate, clean, and block-structure an HTML filing document.

    The returned ``text`` is the normalized cleaned document text; every block's
    offsets index exactly into that text.
    """
    decoded = validate_html_content(content)

    # EDGAR as-filed SGML wrapper (<DOCUMENT>...<TEXT> ... </DOCUMENT>) is not
    # HTML markup: strip it before parsing so the wrapper labels never leak
    # into the cleaned text.
    decoded = _EDGAR_SGML_PROLOGUE_RE.sub("", decoded, count=1)
    decoded = _EDGAR_SGML_TRAILER_RE.sub("", decoded, count=1)

    try:
        soup = BeautifulSoup(decoded, "lxml")
        parser = "lxml"
    except ValueError:  # FeatureNotFound (ValueError) or markup libxml2 rejects
        soup = BeautifulSoup(decoded, "html.parser")
        parser = "html.parser"

    counters = {
        "scripts_removed": 0,
        "styles_removed": 0,
        "nav_removed": 0,
        "hidden_removed": 0,
        "ix_header_removed": 0,
        "ix_unwrapped": 0,
    }
    _strip_untrusted_and_furniture(soup, counters)

    title = ""
    if soup.title is not None:
        title = normalize_ws(soup.title.get_text(" "))

    root = soup.body if soup.body is not None else soup
    raw_blocks = _walk_blocks(root)

    # Assemble cleaned text and exact per-block offsets into it.
    parts: list[str] = []
    blocks: list[TextBlock] = []
    cursor = 0
    for kind, text in raw_blocks:
        if parts:
            cursor += len(BLOCK_SEPARATOR)
        start = cursor
        cursor += len(text)
        blocks.append(TextBlock(kind=kind, text=text, start_offset=start, end_offset=cursor))
        parts.append(text)
    full_text = BLOCK_SEPARATOR.join(parts)

    kind_counts: dict[str, int] = {}
    for block in blocks:
        kind_counts[block.kind] = kind_counts.get(block.kind, 0) + 1

    metadata: dict[str, object] = {
        "title": title,
        "parser": parser,
        "parser_version": HTML_CLEAN_VERSION,
        "block_count": len(blocks),
        "block_kinds": kind_counts,
        "characters": len(full_text),
        **counters,
    }
    return CleanedDocument(text=full_text, blocks=blocks, metadata=metadata)
