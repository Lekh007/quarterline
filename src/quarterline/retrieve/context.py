"""Bounded context assembly (SPEC §16).

Rules enforced here:
- at most :data:`MAX_PASSAGES` (6) evidence passages;
- at most :data:`MAX_PASSAGE_TOKENS` (~600) tokens per passage — evidence
  items are never truncated, because a supplied window must stay exactly
  resolvable via its evidence id (contract C8); oversized items are skipped;
- overlapping spans from the same document are deduplicated;
- source diversity: when several documents are available, a second document
  is preferred for the second passage;
- total estimated tokens stay within the caller's budget, minus a
  conservative ~10% headroom because :func:`estimate_tokens` is approximate.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from quarterline.config import Settings
from quarterline.retrieve.chunk_fixed import estimate_tokens
from quarterline.retrieve.models import EvidenceItem

#: Context limits (SPEC §16).
MAX_PASSAGES = 6
MAX_PASSAGE_TOKENS = 600

#: Conservative headroom against the approximate token estimator (SPEC §14).
_BUDGET_HEADROOM_FRACTION = 0.10


class ContextPassage(BaseModel):
    """One supplied evidence window; ``evidence_id`` resolves its exact text."""

    evidence_id: str
    document_id: int
    section: str | None = None
    start_offset: int
    end_offset: int
    text: str
    token_count: int
    scores: dict[str, float] = Field(default_factory=dict)


class ContextBundle(BaseModel):
    passages: list[ContextPassage] = Field(default_factory=list)
    total_tokens: int = 0
    budget_tokens: int
    effective_budget_tokens: int
    max_passages: int = MAX_PASSAGES
    max_passage_tokens: int = MAX_PASSAGE_TOKENS
    dropped_for_overlap: int = 0
    dropped_for_budget: int = 0
    skipped_oversized: int = 0
    policy_note: str = (
        "Token counts are estimates (chars/4); headroom reserved. Similarity "
        "scores on passages are not calibrated probabilities."
    )


def context_budget_from_settings(settings: Settings) -> int:
    """Prompt budget = MAX_PROMPT_TOKENS - reserved output - headroom.

    Conservative by design (SPEC §14/§16): token counts are approximate, so
    the estimator headroom is applied on top of the reserved output capacity.
    """
    usable = settings.max_prompt_tokens - settings.max_output_tokens
    return max(1, int(usable * (1.0 - _BUDGET_HEADROOM_FRACTION)))


def assemble_context(items: list[EvidenceItem], budget_tokens: int) -> ContextBundle:
    """Greedy, deterministic assembly of the prompt evidence bundle.

    Selection order: ranked input order, except that when choosing the second
    passage a *different* document is preferred when one is available (source
    diversity, SPEC §16). Rejected items (overlap / over budget) are removed
    permanently; items skipped only by the diversity rule stay eligible.
    """
    effective_budget = max(1, int(budget_tokens * (1.0 - _BUDGET_HEADROOM_FRACTION)))
    bundle = ContextBundle(budget_tokens=budget_tokens, effective_budget_tokens=effective_budget)

    candidates: list[tuple[EvidenceItem, int]] = []
    for item in items:
        tokens = estimate_tokens(item.text)
        if tokens > MAX_PASSAGE_TOKENS:
            # Windows are never truncated: a supplied window must stay exactly
            # resolvable (contract C8), so oversized items are skipped whole.
            bundle.skipped_oversized += 1
            continue
        candidates.append((item, tokens))

    selected: list[ContextPassage] = []
    spans_by_document: dict[int, list[tuple[int, int]]] = {}
    documents_seen: set[int] = set()
    rejected: set[int] = set()  # id() of already-rejected entries
    total = 0

    def overlaps(item: EvidenceItem) -> bool:
        return any(
            item.start_offset < other_end and other_start < item.end_offset
            for other_start, other_end in spans_by_document.get(item.document_id, [])
        )

    while len(selected) < MAX_PASSAGES and candidates:
        pick: tuple[EvidenceItem, int] | None = None
        for allow_same_document in (False, True):
            for entry in candidates:
                item, tokens = entry
                if id(entry) in rejected:
                    continue
                if overlaps(item):
                    bundle.dropped_for_overlap += 1
                    rejected.add(id(entry))
                    continue
                if (
                    not allow_same_document
                    and len(selected) == 1
                    and item.document_id in documents_seen
                ):
                    continue  # diversity preference; still eligible on pass 2
                if total + tokens > effective_budget:
                    bundle.dropped_for_budget += 1
                    rejected.add(id(entry))
                    continue
                pick = entry
                break
            if pick is not None:
                break
        if pick is None:
            break
        item, tokens = pick
        selected.append(
            ContextPassage(
                evidence_id=item.evidence_id,
                document_id=item.document_id,
                section=item.section,
                start_offset=item.start_offset,
                end_offset=item.end_offset,
                text=item.text,
                token_count=tokens,
                scores=dict(item.scores),
            )
        )
        spans_by_document.setdefault(item.document_id, []).append(
            (item.start_offset, item.end_offset)
        )
        documents_seen.add(item.document_id)
        total += tokens
        candidates.remove(pick)

    bundle.passages = selected
    bundle.total_tokens = total
    return bundle


__all__ = [
    "MAX_PASSAGES",
    "MAX_PASSAGE_TOKENS",
    "ContextBundle",
    "ContextPassage",
    "assemble_context",
    "context_budget_from_settings",
]
