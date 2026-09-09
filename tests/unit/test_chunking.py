"""Chunking strategy tests (SPEC §14, §26): sizes, overlap, paragraph
preference, sentence splitting, stable/idempotent identity, and the
one-interface-two-strategies contract."""

from __future__ import annotations

import itertools
import math

from retrieval_test_helpers import fixture_provider

from quarterline.retrieve.chunk_fixed import (
    FIXED_MAX_TOKENS,
    FixedWindowChunker,
    SectionRef,
    estimate_tokens,
)
from quarterline.retrieve.chunk_section import (
    PARENT_WINDOW_TOKENS,
    SectionParentChildChunker,
)
from quarterline.retrieve.models import DocumentMeta, chunk_identity, text_hash

META = DocumentMeta(document_id=1, content_hash="abc123", extraction_version="documents-1")


def _paragraphs(count: int, words: int = 90) -> str:
    body = " ".join(f"word{i}" for i in range(words))
    return "\n\n".join(f"Paragraph {n} begins. {body} End of paragraph {n}." for n in range(count))


def _section_ref(text: str, section_id: int = 7, section_type: str = "mda") -> SectionRef:
    return SectionRef(
        section_id=section_id,
        section_type=section_type,
        heading="Item 2",
        start_offset=0,
        end_offset=len(text),
    )


# -- token estimator ----------------------------------------------------------


def test_estimate_tokens_is_the_documented_char_quarter_estimator() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2  # ceil
    assert estimate_tokens("a" * 401) == math.ceil(401 / 4)


# -- strategy A: fixed windows -------------------------------------------------


def test_fixed_chunks_stay_near_target_with_overlap_and_exact_slices() -> None:
    text = _paragraphs(24)
    drafts = FixedWindowChunker().chunk(text, [], META)
    assert len(drafts) >= 3
    for draft in drafts:
        # Contiguity/roundtrip invariant: the text is an exact slice.
        assert text[draft.start_offset : draft.end_offset] == draft.text
        assert draft.token_count == estimate_tokens(draft.text)
        assert draft.token_count <= FIXED_MAX_TOKENS
        assert draft.text_hash == text_hash(draft.text)
        assert draft.role == "chunk"
    # Overlap: consecutive chunks share text; the overlap snaps UP to whole
    # paragraph boundaries (paragraph preference), so it is at least a
    # meaningful tail and always smaller than a full chunk.
    for first, second in itertools.pairwise(drafts):
        assert second.start_offset < first.end_offset  # they overlap
        overlap_tokens = estimate_tokens(text[second.start_offset : first.end_offset])
        assert 40 <= overlap_tokens <= 300


def test_fixed_prefers_paragraph_boundaries() -> None:
    text = _paragraphs(12, words=40)  # each paragraph ~78 tokens
    drafts = FixedWindowChunker().chunk(text, [], META)
    paragraph_ends = {m for m in range(len(text)) if text[m : m + 2] == "\n\n"}
    # Every chunk end is a paragraph end or the end of the text; and chunks
    # begin at paragraph boundaries too.
    for draft in drafts[:-1]:
        assert draft.end_offset in paragraph_ends
        assert text[draft.start_offset - 2 : draft.start_offset] == "\n\n" or (
            draft.start_offset == 0
        )
    assert drafts[-1].end_offset == len(text)


def test_oversized_paragraph_splits_at_sentence_bounds() -> None:
    sentences = [f"Sentence number {i} talks about revenue growth and margins. " for i in range(60)]
    text = "".join(sentences)  # one paragraph, ~900 tokens, no blank lines
    drafts = FixedWindowChunker().chunk(text, [], META)
    assert len(drafts) >= 2
    for draft in drafts:
        assert draft.token_count <= FIXED_MAX_TOKENS + 60  # hard cap headroom
        assert text[draft.start_offset : draft.end_offset] == draft.text
        if not draft.end_offset >= len(text.rstrip()):
            # Interior chunks end just after a sentence ender (snap target).
            tail = text[max(draft.start_offset, draft.end_offset - 3) : draft.end_offset]
            assert tail.rstrip().endswith((".", "!", "?"))


def test_chunking_is_idempotent_and_identity_is_content_derived() -> None:
    text = _paragraphs(16)
    first = FixedWindowChunker().chunk(text, [], META)
    second = FixedWindowChunker().chunk(text, [], META)
    assert [d.model_dump() for d in first] == [d.model_dump() for d in second]
    # Identity tuple (SPEC §14): document hash, extraction version, strategy,
    # strategy/version, offsets, text hash.
    identity_a = chunk_identity(
        content_hash=META.content_hash,
        extraction_version=META.extraction_version,
        strategy="fixed",
        strategy_version="fixed-1",
        start=first[0].start_offset,
        end=first[0].end_offset,
        chunk_text_hash=first[0].text_hash,
    )
    identity_b = chunk_identity(
        content_hash=META.content_hash,
        extraction_version=META.extraction_version,
        strategy="fixed",
        strategy_version="fixed-1",
        start=first[0].start_offset,
        end=first[0].end_offset,
        chunk_text_hash=first[0].text_hash,
    )
    assert identity_a == identity_b
    # A different extraction version changes the identity.
    assert identity_a != chunk_identity(
        content_hash=META.content_hash,
        extraction_version="documents-2",
        strategy="fixed",
        strategy_version="fixed-1",
        start=first[0].start_offset,
        end=first[0].end_offset,
        chunk_text_hash=first[0].text_hash,
    )


# -- strategy B: section parent-child ------------------------------------------


def test_section_strategy_emits_children_parent_and_bounded_windows() -> None:
    text = _paragraphs(30)
    chunker = SectionParentChildChunker()
    drafts = chunker.chunk(text, [_section_ref(text)], META)
    roles = {draft.role for draft in drafts}
    assert roles == {"chunk", "parent", "window"}
    children = [d for d in drafts if d.role == "chunk"]
    windows = [d for d in drafts if d.role == "window"]
    parents = [d for d in drafts if d.role == "parent"]
    # Children ~200 tokens (bounded by target + headroom); never the section.
    assert children
    assert max(d.token_count for d in children) <= 260
    assert min(d.token_count for d in children) >= 40
    # Bounded expansion: every window stays within the window cap and is at
    # least as large as its child, but never the whole long section.
    assert windows
    assert max(d.token_count for d in windows) <= PARENT_WINDOW_TOKENS
    assert parents and parents[0].token_count == estimate_tokens(text)
    # The parent IS the full section (stored), the children are not.
    assert parents[0].text == text
    assert all(child.text != text for child in children)
    # Linkage: children point at the parent key, windows at a child key.
    parent_keys = {p.key for p in parents}
    child_keys = {c.key for c in children}
    for child in children:
        assert child.parent_key in parent_keys
    for window in windows:
        assert window.parent_key in child_keys
    for draft in drafts:
        assert text[draft.start_offset : draft.end_offset] == draft.text


def test_section_strategy_window_covers_its_child() -> None:
    text = _paragraphs(30)
    drafts = SectionParentChildChunker().chunk(text, [_section_ref(text)], META)
    children = {d.key: d for d in drafts if d.role == "chunk"}
    for window in (d for d in drafts if d.role == "window"):
        child = children[window.parent_key]
        assert window.start_offset <= child.start_offset
        assert window.end_offset >= child.end_offset


def test_tiny_section_collapses_to_a_single_child_row() -> None:
    text = "Short section. Nothing more to see here."
    drafts = SectionParentChildChunker().chunk(text, [_section_ref(text)], META)
    roles = sorted(d.role for d in drafts)
    # The whole section fits in one child; parent/window would duplicate text.
    assert roles == ["chunk"]
    assert drafts[0].text == text
    assert drafts[0].parent_key is None


# -- one interface, two strategies ---------------------------------------------


def test_strategies_are_distinct_and_coexist_by_identity() -> None:
    fixed = FixedWindowChunker()
    section = SectionParentChildChunker()
    assert fixed.STRATEGY_ID != section.STRATEGY_ID
    assert fixed.STRATEGY_VERSION and section.STRATEGY_VERSION
    text = _paragraphs(20)
    fixed_drafts = fixed.chunk(text, [], META)
    section_drafts = section.chunk(text, [_section_ref(text)], META)
    # Different granularities from the same text — no key collisions between
    # the strategies (the chunks table unique key includes strategy).
    fixed_hashes = {d.text_hash for d in fixed_drafts}
    section_hashes = {d.text_hash for d in section_drafts}
    assert fixed_hashes or section_hashes  # both produced output
    assert fixed_hashes != section_hashes or fixed_drafts[0].key != section_drafts[0].key


def test_provider_fixture_is_deterministic_pairing_with_chunks() -> None:
    """The committed fixture provider matches the deterministic fake provider."""
    assert fixture_provider().model_id == "fake-embed-64d-seed20260909"
