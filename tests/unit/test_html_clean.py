"""Unit tests for SEC HTML cleaning (SPEC §13.2) — offline, no network."""

from __future__ import annotations

import pytest

from quarterline.ingest.html_clean import (
    ContentValidationError,
    clean_html,
    normalize_ws,
    validate_html_content,
)


def test_scripts_and_styles_stripped_and_never_retained() -> None:
    html = (
        b"<html><head><script>alert('xss');</script><style>.x { color: red; }</style></head>"
        b"<body><p>Safe paragraph text.</p></body></html>"
    )
    doc = clean_html(html)
    assert "alert" not in doc.text
    assert "xss" not in doc.text
    assert "color: red" not in doc.text and ".x" not in doc.text
    assert doc.metadata["scripts_removed"] == 1
    assert doc.metadata["styles_removed"] == 1
    assert "Safe paragraph text." in doc.text


def test_offsets_roundtrip_and_block_layout() -> None:
    html = (
        b"<html><body><h1>Heading One</h1><p>First paragraph.</p>"
        b"<p>Second paragraph.</p></body></html>"
    )
    doc = clean_html(html)
    assert [b.kind for b in doc.blocks] == ["heading", "paragraph", "paragraph"]
    assert doc.text == "Heading One\n\nFirst paragraph.\n\nSecond paragraph."

    cursor = 0
    for block in doc.blocks:
        # Exact roundtrip into the cleaned text.
        assert doc.text[block.start_offset : block.end_offset] == block.text
        assert block.start_offset == cursor
        cursor = block.end_offset + 2  # "\n\n" separator between blocks
    assert doc.blocks[-1].end_offset == len(doc.text)


def test_offsets_roundtrip_on_whitespace_heavy_markup() -> None:
    html = (
        b"<html><body>\n  <div>\n    <font>Line   one&nbsp;text.</font>\n  </div>\n"
        b"  <div>   </div>\n  <p>Line\ttwo.</p>\n</body></html>"
    )
    doc = clean_html(html)
    # Whitespace-only block is dropped, nbsp/tabs normalized.
    assert doc.blocks[0].text == "Line one text."
    assert doc.blocks[1].text == "Line two."
    for block in doc.blocks:
        assert doc.text[block.start_offset : block.end_offset] == block.text


def test_inline_xbrl_degrades_to_text() -> None:
    html = (
        b"<html><body>"
        b"<ix:header><ix:hidden><ix:nonNumeric name='d'>hidden facts</ix:nonNumeric></ix:hidden></ix:header>"
        b"<p>Revenue was <ix:nonFraction contextref='c1' name='us-gaap:Revenues'>1,234</ix:nonFraction> "
        b"million.</p>"
        b"</body></html>"
    )
    doc = clean_html(html)
    assert "Revenue was 1,234 million." in doc.text
    assert "hidden facts" not in doc.text  # ix:header metadata dropped, not leaked
    assert doc.metadata["ix_header_removed"] == 1
    assert doc.metadata["ix_unwrapped"] == 1


def test_table_becomes_readable_rows_not_a_blob() -> None:
    html = (
        b"<html><body><table>"
        b"<tr><th>Item</th><th>Amount</th></tr>"
        b"<tr><td>Revenue</td><td>100</td></tr>"
        b"<tr><td>   </td><td></td></tr>"
        b"</table></body></html>"
    )
    doc = clean_html(html)
    tables = [b for b in doc.blocks if b.kind == "table"]
    assert len(tables) == 1
    assert "Item | Amount" in tables[0].text
    assert "Revenue | 100" in tables[0].text
    # Whitespace-only row dropped; one row line remains.
    assert tables[0].text.count("\n") == 1
    for block in doc.blocks:
        assert doc.text[block.start_offset : block.end_offset] == block.text


def test_navigation_boilerplate_removed() -> None:
    html = (
        b"<html><body>"
        b"<nav><a href='/'>Home</a> <a href='/search'>Search EDGAR</a></nav>"
        b"<div class='menu'>Menu junk</div>"
        b"<div id='sidebar'>Sidebar junk</div>"
        b"<p>Real content paragraph.</p>"
        b"<footer>Site footer disclaimers</footer>"
        b"</body></html>"
    )
    doc = clean_html(html)
    assert "Search EDGAR" not in doc.text
    assert "Menu junk" not in doc.text
    assert "Sidebar junk" not in doc.text
    assert "Site footer disclaimers" not in doc.text
    assert "Real content paragraph." in doc.text
    assert doc.metadata["nav_removed"] >= 3


def test_hidden_elements_removed() -> None:
    html = (
        b"<html><body>"
        b"<p style='display:none'>invisible secret</p>"
        b"<p style='visibility: hidden'>hush</p>"
        b"<p hidden>attr hidden</p>"
        b"<p>visible text</p>"
        b"</body></html>"
    )
    doc = clean_html(html)
    assert "invisible secret" not in doc.text
    assert "hush" not in doc.text
    assert "attr hidden" not in doc.text
    assert "visible text" in doc.text
    assert doc.metadata["hidden_removed"] == 3


def test_edgar_sgml_document_wrapper_stripped() -> None:
    raw = (
        b"<DOCUMENT>\n<TYPE>EX-99.1\n<SEQUENCE>2\n<FILENAME>a.htm\n<TEXT>\n"
        b"<html><body><p>Exhibit body text.</p></body></html>\n</DOCUMENT>"
    )
    doc = clean_html(raw)
    assert "EX-99.1" not in doc.text
    assert "a.htm" not in doc.text
    assert "Exhibit body text." in doc.text


def test_content_validation_rejects_non_html_and_empty() -> None:
    with pytest.raises(ContentValidationError):
        clean_html(b"")
    with pytest.raises(ContentValidationError):
        clean_html(b"   \n  ")
    with pytest.raises(ContentValidationError):
        clean_html(b"this is plain text, not html at all")


def test_validate_html_content_returns_decoded_text() -> None:
    decoded = validate_html_content(b"<html><body><p>caf\xc3\xa9</p></body></html>")
    assert "caf\xe9" in decoded


def test_document_metadata() -> None:
    doc = clean_html(b"<html><head><title>Doc title</title></head><body><p>x</p></body></html>")
    assert doc.metadata["title"] == "Doc title"
    assert doc.metadata["parser"] in ("lxml", "html.parser")
    assert doc.metadata["parser_version"]
    assert doc.metadata["block_count"] == 1
    assert doc.metadata["block_kinds"] == {"paragraph": 1}
    assert doc.metadata["characters"] == len(doc.text)


def test_normalize_ws() -> None:
    assert normalize_ws("  a \t b\u00a0\u200b c  ") == "a b c"
    assert normalize_ws("line1\nline2") == "line1 line2"
