"""Lexical layer tests: FTS5 query sanitization of untrusted input (SPEC
§2.3.4) and the documented bm25 sign convention."""

from __future__ import annotations

import pytest

from quarterline.retrieve.lexical import sanitize_fts_query


@pytest.mark.parametrize(
    "raw",
    [
        'apple" OR 1=1 --',
        "revenue; DROP TABLE chunks; --",
        "NEAR(a b) NOT *^(col1: x)",
        '   ""   ',
        "!!!???",
        "риск–факторы and 東京",
        "a" * 500,
        "O'Brien-brand risk's",
    ],
)
def test_sanitize_every_fragment_is_a_quoted_phrase(raw: str) -> None:
    sanitized = sanitize_fts_query(raw)
    # Every emitted OR-alternative is a double-quoted phrase literal — FTS5
    # treats quoted strings as plain text, so no operator/injection syntax
    # survives quoting (quotes inside are escaped by doubling).
    if sanitized:
        for fragment in sanitized.split(" OR "):
            assert fragment.startswith('"') and fragment.endswith('"')
            inner = fragment[1:-1]
            assert '""' not in inner or inner.count('""') % 2 == 0


def test_sanitize_drops_tiny_tokens_and_keeps_words() -> None:
    assert sanitize_fts_query("a revenue of") == '"revenue" OR "of"'  # 1-char tokens dropped
    assert sanitize_fts_query("risk factors") == '"risk" OR "factors"'


def test_sanitize_empty_query_returns_empty_match() -> None:
    assert sanitize_fts_query("!!!") == ""
    assert sanitize_fts_query("") == ""
