"""Strategy B — section-aware parent-child chunking (SPEC §14).

Per input section (one ``sections`` row, typically passed one at a time by the
indexer):

- **children** (~200 tokens, ~40 overlap): the retrieval units — embedded and
  FTS-indexed; ``role="chunk"`` with ``parent_key`` pointing at the parent;
- **parent**: the FULL section text, stored for provenance/expansion
  (``role="parent"``, never embedded, never FTS-indexed);
- **window**: the bounded expansion of each child inside its parent
  (``role="window"``, never embedded, never FTS-indexed). At query time a
  retrieved child is *presented as its window*, so the model never sees a
  whole long MD&A section and evidence stays C8-resolvable.

Window size: :data:`PARENT_WINDOW_TOKENS` = 560 estimated tokens — chosen at
or below the ~600-token per-passage context cap (SPEC §16) so an expansion
window can always be supplied whole. This keeps every supplied window exactly
resolvable (its text is a stored ``chunks`` row); a larger window (~1200
tokens, the task's example) would force query-time truncation and break the
exact-text resolvability contract. Deviation documented in the wave report.

Chunk-row conventions (schema is W0-owned; encoded here and in the indexer,
not in ``store/models.py``): one strategy build writes three row families
distinguished by ``strategy_version`` suffix — children/base rows keep the
bare version (``section-1``), parents use ``section-1-parent``, windows use
``section-1-window``. Retrieval filters match the bare version only, so
parents/windows are never retrieved directly; ``parent_id`` links child ->
parent and window -> child. Because the ``chunks`` unique key is
``(document_id, strategy, strategy_version, text_hash)``, drafts whose text
hash was already emitted are skipped — tiny sections collapse to a single
child row (the section itself) instead of three identical-text rows.
"""

from __future__ import annotations

from quarterline.retrieve.chunk_fixed import (
    _CHARS_PER_TOKEN,
    SectionRef,
    _atomic_blocks,
    _pack_chunks,
    estimate_tokens,
)
from quarterline.retrieve.models import ChunkDraft, DocumentMeta, text_hash

#: Strategy B defaults (SPEC §14).
CHILD_TARGET_TOKENS = 200
CHILD_OVERLAP_TOKENS = 40
#: Bounded parent expansion window (see module docstring for the 560 choice).
PARENT_WINDOW_TOKENS = 560


class SectionParentChildChunker:
    """Parent = identified section; children ≈200 tokens; bounded windows."""

    STRATEGY_ID = "section"
    STRATEGY_VERSION = "section-1"

    def __init__(
        self,
        *,
        child_target_tokens: int = CHILD_TARGET_TOKENS,
        child_overlap_tokens: int = CHILD_OVERLAP_TOKENS,
        window_tokens: int = PARENT_WINDOW_TOKENS,
    ) -> None:
        self.child_target_tokens = child_target_tokens
        self.child_overlap_tokens = child_overlap_tokens
        self.window_tokens = window_tokens

    def chunk(
        self, document_text: str, sections: list[SectionRef], document_meta: DocumentMeta
    ) -> list[ChunkDraft]:
        """Chunk ONE section (``document_text`` == the section text).

        ``sections[0]`` supplies the parent linkage (id, type, pages). The
        indexer calls this once per section row; offsets are relative to the
        passed text and shifted into document coordinates by the indexer.
        """
        if not document_text.strip() or not sections:
            return []
        section = sections[0]
        blocks = _atomic_blocks(document_text, self.child_target_tokens)
        child_spans = _pack_chunks(
            document_text,
            blocks,
            target_tokens=self.child_target_tokens,
            max_tokens=self.child_target_tokens + 60,
            overlap_tokens=self.child_overlap_tokens,
        )

        drafts: list[ChunkDraft] = []
        emitted_hashes: set[str] = set()
        parent_key: str | None = None

        for index, (start, end) in enumerate(child_spans):
            text = document_text[start:end]
            digest = text_hash(text)
            if digest not in emitted_hashes:
                emitted_hashes.add(digest)
                drafts.append(
                    ChunkDraft(
                        key=f"child:{index}",
                        role="chunk",
                        text=text,
                        start_offset=start,
                        end_offset=end,
                        token_count=estimate_tokens(text),
                        text_hash=digest,
                        section_id=section.section_id,
                        section_type=section.section_type,
                        page_start=section.page_start,
                        page_end=section.page_end,
                        parent_key=None,  # resolved below once the parent is known
                    )
                )

        # Parent: the FULL section. Skipped when identical text was already
        # emitted (tiny sections -> a single child row covers it).
        section_hash = text_hash(document_text)
        if section_hash not in emitted_hashes:
            parent_key = "parent:0"
            emitted_hashes.add(section_hash)
            drafts.append(
                ChunkDraft(
                    key=parent_key,
                    role="parent",
                    text=document_text,
                    start_offset=0,
                    end_offset=len(document_text),
                    token_count=estimate_tokens(document_text),
                    text_hash=section_hash,
                    section_id=section.section_id,
                    section_type=section.section_type,
                    page_start=section.page_start,
                    page_end=section.page_end,
                )
            )
        for draft in drafts:
            if draft.role == "chunk":
                draft.parent_key = parent_key

        # Windows: bounded expansion of each child inside the parent; skipped
        # when identical to already-emitted text (the child row itself then
        # serves as the evidence window).
        for index, (start, end) in enumerate(child_spans):
            w_start, w_end = self._expand_window(document_text, start, end)
            text = document_text[w_start:w_end]
            digest = text_hash(text)
            if digest in emitted_hashes:
                continue
            emitted_hashes.add(digest)
            drafts.append(
                ChunkDraft(
                    key=f"window:{index}",
                    role="window",
                    text=text,
                    start_offset=w_start,
                    end_offset=w_end,
                    token_count=estimate_tokens(text),
                    text_hash=digest,
                    section_id=section.section_id,
                    section_type=section.section_type,
                    page_start=section.page_start,
                    page_end=section.page_end,
                    parent_key=f"child:{index}",
                )
            )
        del document_meta
        return drafts

    # -- window expansion ----------------------------------------------------

    def _expand_window(self, text: str, start: int, end: int) -> tuple[int, int]:
        """Grow [start, end) to ≈window tokens centered on the child, snapped
        to paragraph/sentence starts, capped strictly at ``window_tokens``."""
        child_tokens = estimate_tokens(text[start:end])
        extra_chars = max(0, (self.window_tokens - child_tokens)) * _CHARS_PER_TOKEN
        back = extra_chars // 2
        w_start = max(0, start - back)
        w_end = min(len(text), end + (extra_chars - (start - w_start)))
        # Snap the left edge forward to a paragraph/sentence boundary.
        w_start = self._snap_forward(text, w_start, start)
        # Enforce the cap exactly (estimate-based): shrink whichever edge has
        # more slack, cutting at word boundaries; the child span is never cut.
        while estimate_tokens(text[w_start:w_end]) > self.window_tokens and w_end - w_start > 1:
            left_slack = start - w_start
            right_slack = w_end - end
            if right_slack >= left_slack and right_slack > 0:
                cut = text.rfind(" ", w_start, w_end - 1)
                new_end = cut if cut > w_start else w_end - 1
                if new_end >= w_end:
                    break
                w_end = new_end
            elif left_slack > 0:
                cut = text.find(" ", w_start + 1, w_end)
                new_start = cut + 1 if cut != -1 else w_start + 1
                if new_start > start:
                    break
                w_start = new_start
            else:
                break
        return w_start, w_end

    @staticmethod
    def _snap_forward(text: str, pos: int, limit: int) -> int:
        """Advance ``pos`` (never beyond ``limit``) to the next paragraph or
        sentence start so windows begin on clean text."""
        best = pos
        for pattern in ("\n\n", "\n"):
            found = text.find(pattern, pos, limit)
            if found != -1:
                candidate = found + len(pattern)
                if best == pos or candidate < best:
                    best = candidate
        if best == pos:
            for sep in (". ", "! ", "? "):
                found = text.find(sep, pos, limit)
                if found != -1:
                    candidate = found + len(sep)
                    if best == pos or candidate < best:
                        best = candidate
        return min(best, limit)


__all__ = [
    "CHILD_OVERLAP_TOKENS",
    "CHILD_TARGET_TOKENS",
    "PARENT_WINDOW_TOKENS",
    "SectionParentChildChunker",
]
