"""Strategy A — fixed token windows with paragraph preference (SPEC §14).

Defaults: ~400-token chunks with ~80-token overlap. Chunks are contiguous
slices of the input text, so ``document_text[start:end] == chunk.text`` always
holds (the evidence roundtrip invariant).

Token accounting: the single documented estimator for the whole retrieval
package is :func:`estimate_tokens` (``ceil(chars / 4)``). It is an
*approximation* (real tokenizers vary by vocabulary and content); every
consumer therefore keeps conservative headroom — the fixed target is 400 with
a hard cap of 460 estimated tokens, and the context assembler reserves 10% of
its budget (see ``context.py``).

The shared text-block machinery (paragraph/sentence splitting, packing,
overlap backtracking) lives here and is reused by the section strategy.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from quarterline.retrieve.models import ChunkDraft, DocumentMeta, text_hash

#: Approximate token estimator: ~4 characters per token for English filing
#: text. Approximate by design (SPEC §14 token accounting); callers must keep
#: conservative prompt headroom. Never use for billing-grade counting.
_CHARS_PER_TOKEN = 4

#: Strategy A defaults (SPEC §14).
FIXED_TARGET_TOKENS = 400
FIXED_OVERLAP_TOKENS = 80
#: Hard cap: a chunk may exceed the target only up to this estimate, giving
#: headroom against the approximate estimator before context assembly caps.
FIXED_MAX_TOKENS = 460


def estimate_tokens(text: str) -> int:
    """Approximate token count of ``text`` (``ceil(chars / 4)``).

    This is the one documented token estimator for the retrieval package.
    Labeled approximate (SPEC §14): do not treat results as exact tokenizer
    output; leave headroom in budgets.
    """
    if not text:
        return 0
    return max(1, math.ceil(len(text) / _CHARS_PER_TOKEN))


@runtime_checkable
class ChunkStrategy(Protocol):
    """One interface, two strategies (SPEC §14)."""

    STRATEGY_ID: str
    STRATEGY_VERSION: str

    def chunk(
        self, document_text: str, sections: list[SectionRef], document_meta: DocumentMeta
    ) -> list[ChunkDraft]: ...


@dataclass(frozen=True)
class SectionRef:
    """Chunker-side view of one ``sections`` row (offsets are relative to the
    ``document_text`` handed to :meth:`ChunkStrategy.chunk`)."""

    section_id: int | None
    section_type: str | None
    heading: str | None
    start_offset: int
    end_offset: int
    page_start: int | None = None
    page_end: int | None = None


# ---------------------------------------------------------------------------
# Text blocks: paragraphs, sentences, oversized-paragraph splitting
# ---------------------------------------------------------------------------

_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")
#: Sentence end followed by whitespace; keeps the ender inside the sentence.
_SENTENCE_END_RE = re.compile(r"[.!?][\"'\u2019\u201d)\]]*(?=\s|$)")


@dataclass(frozen=True)
class _Block:
    """Minimal indivisible span of text (a paragraph or, for oversized
    paragraphs, one sentence / word-window)."""

    start: int
    end: int


def _split_paragraphs(text: str) -> list[_Block]:
    """Contiguous paragraph spans separated by blank lines."""
    spans: list[_Block] = []
    cursor = 0
    for match in _BLANK_LINE_RE.finditer(text):
        start, end = match.span()
        if start > cursor:
            spans.append(_Block(cursor, start))
        cursor = end
    if cursor < len(text):
        spans.append(_Block(cursor, len(text)))
    # Drop whitespace-only paragraphs (kept offsets stay contiguous).
    return [b for b in spans if text[b.start : b.end].strip()]


def _split_sentences(paragraph: str, base: int) -> list[_Block]:
    """Sentence spans (offsets shifted by ``base``) inside one paragraph."""
    spans: list[_Block] = []
    cursor = 0
    for match in _SENTENCE_END_RE.finditer(paragraph):
        end = match.end()
        # Absorb trailing whitespace into the preceding sentence so sentences
        # stay gap-free and contiguous slices remain exact.
        while end < len(paragraph) and paragraph[end] in " \t":
            end += 1
        if end > cursor and paragraph[cursor:end].strip():
            spans.append(_Block(base + cursor, base + end))
        cursor = end
    if cursor < len(paragraph) and paragraph[cursor:].strip():
        spans.append(_Block(base + cursor, base + len(paragraph)))
    return spans


def _split_hard(text: str, base: int, max_tokens: int) -> list[_Block]:
    """Last resort for a single sentence above the cap: word-boundary windows."""
    step = max_tokens * _CHARS_PER_TOKEN
    spans: list[_Block] = []
    pos = 0
    while pos < len(text):
        end = min(pos + step, len(text))
        if end < len(text):
            cut = text.rfind(" ", pos + step // 2, end)
            if cut > pos:
                end = cut + 1
        if text[pos:end].strip():
            spans.append(_Block(base + pos, base + end))
        pos = end
    return spans


def _atomic_blocks(text: str, max_tokens: int) -> list[_Block]:
    """Paragraphs, with oversized paragraphs split at sentence bounds."""
    blocks: list[_Block] = []
    for para in _split_paragraphs(text):
        if estimate_tokens(text[para.start : para.end]) <= max_tokens:
            blocks.append(para)
            continue
        sentences = _split_sentences(text[para.start : para.end], para.start)
        for sentence in sentences:
            if estimate_tokens(text[sentence.start : sentence.end]) <= max_tokens:
                blocks.append(sentence)
            else:
                blocks.extend(
                    _split_hard(text[sentence.start : sentence.end], sentence.start, max_tokens)
                )
    return blocks


# ---------------------------------------------------------------------------
# Packing with overlap backtracking
# ---------------------------------------------------------------------------


def _pack_chunks(
    text: str, blocks: list[_Block], *, target_tokens: int, max_tokens: int, overlap_tokens: int
) -> list[tuple[int, int]]:
    """Greedy paragraph packing + ~overlap-token backtrack.

    Returns contiguous ``[start, end)`` spans. Deterministic: a pure function
    of the text and the three size parameters. Progress is guaranteed: every
    iteration either advances the chunk-start block strictly forward or stops.
    """
    spans: list[tuple[int, int]] = []
    n = len(blocks)
    idx = 0  # first block included in the current chunk
    prev_end = -1
    while idx < n:
        # Pack blocks [idx, j) while under target; a single oversized block
        # becomes its own (already sentence/hard-split) span, trimmed to max.
        total = 0
        j = idx
        while j < n:
            block_tokens = estimate_tokens(text[blocks[j].start : blocks[j].end])
            if j > idx and total + block_tokens > target_tokens:
                break
            total += block_tokens
            j += 1
            if total >= target_tokens:
                break
        start_edge = blocks[idx].start
        end = blocks[j - 1].end
        while estimate_tokens(text[start_edge:end]) > max_tokens and end - start_edge > 1:
            cut = text.rfind(" ", start_edge, end - 1)
            new_end = (cut + 1) if cut > start_edge else max(start_edge + 1, end - 1)
            if new_end >= end:
                break
            end = new_end
        if end <= prev_end:
            # Fully contained in the previous chunk (coarse block granularity
            # can backtrack past new content near the corpus tail): adds no
            # new text, so stop instead of emitting redundant windows.
            break
        spans.append((start_edge, end))
        prev_end = end

        # Overlap: walk back through whole blocks (~overlap_tokens)...
        acc = 0
        next_idx = None
        k = j - 1
        while k >= idx:
            acc += estimate_tokens(text[blocks[k].start : blocks[k].end])
            if acc >= overlap_tokens:
                next_idx = k
                break
            k -= 1
        if next_idx is not None and blocks[next_idx].start > start_edge:
            idx = next_idx
            continue
        # ...or backtrack at character level, snapped forward to a sentence
        # boundary so the overlap starts on clean text.
        target_start = max(start_edge, end - overlap_tokens * _CHARS_PER_TOKEN)
        snapped = None
        for match in _SENTENCE_END_RE.finditer(text[target_start:end]):
            candidate = target_start + match.end()
            while candidate < end and text[candidate] in " \t":
                candidate += 1
            snapped = candidate
            if estimate_tokens(text[snapped:end]) <= overlap_tokens + 20:
                break
        following = [
            (bidx, block)
            for bidx, block in enumerate(blocks)
            if snapped is not None and block.start >= snapped and block.start < end
        ]
        if following and following[0][1].start > start_edge:
            idx = following[0][0]
            continue
        # No overlap possible here (e.g. one huge block): resume at the first
        # block at/after this chunk's end (strict progress), else stop.
        after = [bidx for bidx, block in enumerate(blocks) if block.start >= end]
        if not after:
            break
        idx = after[0]
    return spans


# ---------------------------------------------------------------------------
# Strategy A
# ---------------------------------------------------------------------------


class FixedWindowChunker:
    """Fixed ~400-token windows, ~80-token overlap, paragraph-preferring."""

    STRATEGY_ID = "fixed"
    STRATEGY_VERSION = "fixed-1"

    def __init__(
        self,
        *,
        target_tokens: int = FIXED_TARGET_TOKENS,
        overlap_tokens: int = FIXED_OVERLAP_TOKENS,
        max_tokens: int = FIXED_MAX_TOKENS,
    ) -> None:
        self.target_tokens = target_tokens
        self.overlap_tokens = overlap_tokens
        self.max_tokens = max_tokens

    def chunk(
        self, document_text: str, sections: list[SectionRef], document_meta: DocumentMeta
    ) -> list[ChunkDraft]:
        del document_meta  # identity is completed by the indexer, not the strategy
        if not document_text.strip():
            return []
        blocks = _atomic_blocks(document_text, self.max_tokens)
        spans = _pack_chunks(
            document_text,
            blocks,
            target_tokens=self.target_tokens,
            max_tokens=self.max_tokens,
            overlap_tokens=self.overlap_tokens,
        )
        drafts: list[ChunkDraft] = []
        for index, (start, end) in enumerate(spans):
            chunk_text = document_text[start:end]
            section = _containing_section(sections, start, end)
            drafts.append(
                ChunkDraft(
                    key=f"{self.STRATEGY_ID}:{index}",
                    role="chunk",
                    text=chunk_text,
                    start_offset=start,
                    end_offset=end,
                    token_count=estimate_tokens(chunk_text),
                    text_hash=text_hash(chunk_text),
                    section_id=section.section_id if section else None,
                    section_type=section.section_type if section else None,
                    page_start=section.page_start if section else None,
                    page_end=section.page_end if section else None,
                )
            )
        return drafts


def _containing_section(sections: list[SectionRef], start: int, end: int) -> SectionRef | None:
    """Section whose span best contains the chunk (midpoint containment)."""
    mid = (start + end) / 2
    for section in sections:
        if section.start_offset <= mid < section.end_offset:
            return section
    return None


__all__ = [
    "FIXED_MAX_TOKENS",
    "FIXED_OVERLAP_TOKENS",
    "FIXED_TARGET_TOKENS",
    "ChunkStrategy",
    "FixedWindowChunker",
    "SectionRef",
    "estimate_tokens",
]
