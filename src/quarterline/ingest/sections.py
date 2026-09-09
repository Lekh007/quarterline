"""Section mapping with honesty (SPEC §13.2).

Mapping rules:

- 10-Q MD&A: Part I, Item 2.  10-Q risk factors: Part II, Item 1A.
- 10-K MD&A: Item 7 ("Item 7A" is a different item and never matches).
- 10-K risk factors: Item 1A.
- Earnings-release exhibits (8-K Item 2.02): the whole exhibit is one
  ``earnings_release`` section (see :func:`earnings_release_candidate`).

Anti-Table-of-Contents strategy: candidate headings come from real heading
blocks (and, only with section-title confirmation, from paragraphs that start
with an item label); ToC listings rendered as tables are never headings at all.
Each match is scored by the amount of body text between it and the next
item/part-level boundary — a ToC line has almost no body, the real section has
a large one. Among multiple matches for one target, the largest-body match
wins.

Honesty: section_type is one of ``mda|risk_factors|earnings_release|other``.
When the evidence is weak (item-number-only match, or only table-of-contents-
scale matches found) the candidate is demoted to ``other`` with low confidence
and an explanatory note. Sections are never invented: a filing without a
detectable MD&A gets no ``mda`` candidate — never a whole-filing mislabel.

Note: the ``sections`` table (W0 schema) has no confidence/notes columns; those
live on :class:`SectionCandidate` and surface in the ingestion report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from quarterline.ingest.html_clean import CleanedDocument, TextBlock

#: Allowed section_type values (SPEC §13.2).
SECTION_TYPES = ("mda", "risk_factors", "earnings_release", "other")

#: Body length (chars) at/above which a match is accepted as the real section.
DEFAULT_MIN_SUBSTANTIAL_BODY_CHARS = 120
#: Below this, a match is ToC-scale and yields nothing (or an ``other`` stub).
DEFAULT_MIN_BODY_CHARS = 40


@dataclass(frozen=True)
class SectionCandidate:
    """A detected section span with offsets into the cleaned document text."""

    section_type: str  # mda | risk_factors | earnings_release | other
    heading: str
    start_offset: int
    end_offset: int
    confidence: float  # 0.0-1.0
    notes: str = ""


_ITEM_HEADING_RE = re.compile(
    r"^item\s+(?P<num>\d{1,2})(?P<letter>[a-z])?(?![a-z0-9])[\s.,:;\u2013\u2014-]*(?P<title>.*)$",
    re.IGNORECASE,
)
_PART_HEADING_RE = re.compile(
    r"^part\s+(?P<part>[ivx]+)(?![a-z0-9])[\s.,:;\u2013\u2014-]*(?P<rest>.*)$",
    re.IGNORECASE,
)
_MDA_TITLE_RE = re.compile(r"management'?s?\s+discussion", re.IGNORECASE)
_RISK_TITLE_RE = re.compile(r"risk\s+facto?rs", re.IGNORECASE)

_SIGNALS_TITLE_RE = {
    "mda": _MDA_TITLE_RE,
    "risk_factors": _RISK_TITLE_RE,
}


@dataclass(frozen=True)
class _Target:
    section_type: str
    item_num: str
    item_letter: str | None
    part: str | None  # required part context for 10-Q targets, None for 10-K


@dataclass(frozen=True)
class _Hit:
    block: TextBlock
    item_num: str | None
    item_letter: str | None
    part: str | None  # set for "Part I"-style headers
    from_paragraph: bool


def _targets_for_form(form: str) -> dict[str, _Target]:
    normalized = (form or "").strip().upper()
    if normalized == "10-Q":
        return {
            "mda": _Target("mda", "2", None, "i"),
            "risk_factors": _Target("risk_factors", "1", "a", "ii"),
        }
    if normalized == "10-K":
        return {
            "mda": _Target("mda", "7", None, None),
            "risk_factors": _Target("risk_factors", "1", "a", None),
        }
    return {}


def _scan_item_level_blocks(blocks: list[TextBlock]) -> list[_Hit]:
    """Collect item/part-level heading candidates in document order.

    Heading blocks qualify from their item/part label alone; paragraph blocks
    qualify only when the item label is followed by a known section title
    (inline headings embedded in a big paragraph), which keeps ToC-like stray
    paragraph text out of the candidate pool.
    """
    hits: list[_Hit] = []
    for block in blocks:
        if block.kind not in ("heading", "paragraph"):
            continue
        item_match = _ITEM_HEADING_RE.match(block.text)
        part_match = _PART_HEADING_RE.match(block.text)
        if item_match:
            title = item_match.group("title") or ""
            if block.kind == "paragraph" and not (
                _MDA_TITLE_RE.search(title) or _RISK_TITLE_RE.search(title)
            ):
                continue  # paragraph starting with an item number but no title: too weak
            letter = item_match.group("letter")
            hits.append(
                _Hit(
                    block=block,
                    item_num=item_match.group("num"),
                    item_letter=letter.lower() if letter else None,
                    part=None,
                    from_paragraph=block.kind == "paragraph",
                )
            )
        elif part_match and block.kind == "heading":
            hits.append(
                _Hit(
                    block=block,
                    item_num=None,
                    item_letter=None,
                    part=(part_match.group("part") or "").lower(),
                    from_paragraph=False,
                )
            )
    return hits


def _match_confidence(target: _Target, hit: _Hit, part_at_hit: str | None) -> tuple[float, str]:
    """(confidence, note) for a hit against a target, ignoring body size."""
    title_ok = bool(_SIGNALS_TITLE_RE[target.section_type].search(hit.block.text))
    part_ok = target.part is not None and part_at_hit == target.part
    if title_ok and part_ok:
        return 0.95, "item heading with section title in expected part"
    if title_ok:
        return 0.90, "item heading with section title"
    if part_ok:
        return 0.75, "item number in expected part (section title absent from heading)"
    return 0.55, "item number match without title or part context"


def _body_extent(doc_text: str, hit: _Hit, hits: list[_Hit]) -> tuple[int, int, int]:
    """Section span plus its body-size measure.

    For heading matches the body that matters is the text *after* the heading
    (a table-of-contents line is followed almost immediately by the next
    line); for inline paragraph matches the paragraph itself is the body.
    """
    start = hit.block.start_offset
    next_starts = [h.block.start_offset for h in hits if h.block.start_offset > start]
    end = min(next_starts) if next_starts else len(doc_text)
    end = start + len(doc_text[start:end].rstrip())
    body_len = (end - start) if hit.from_paragraph else (end - hit.block.end_offset)
    return start, end, body_len


def _heading_label(block: TextBlock, limit: int = 120) -> str:
    return block.text if len(block.text) <= limit else block.text[: limit - 1] + "\u2026"


def _candidate_for_target(
    target: _Target,
    hits: list[_Hit],
    doc: CleanedDocument,
    *,
    min_substantial_body_chars: int,
    min_body_chars: int,
) -> SectionCandidate | None:
    matches: list[tuple[_Hit, float, str, int, int, int]] = []  # hit, conf, note, start, end, body
    part_at = None
    for hit in hits:
        if hit.part is not None:
            part_at = hit.part
        if hit.item_num is None or hit.item_num != target.item_num:
            continue
        if hit.item_letter != target.item_letter:
            continue
        confidence, note = _match_confidence(target, hit, part_at)
        start, end, body_len = _body_extent(doc.text, hit, hits)
        matches.append((hit, confidence, note, start, end, body_len))

    if not matches:
        return None

    # ToC avoidance: prefer the match with the most body text.
    best = max(matches, key=lambda m: (m[5], m[3]))
    hit, confidence, note, start, end, body_len = best
    heading = _heading_label(hit.block)

    substantial = [m for m in matches if m[5] >= min_substantial_body_chars]
    if len(substantial) > 1:
        note += (
            f"; chose largest of {len(substantial)} substantial matches "
            "(others looked like table-of-contents entries)"
        )

    if body_len >= min_substantial_body_chars:
        return SectionCandidate(
            section_type=target.section_type,
            heading=heading,
            start_offset=start,
            end_offset=end,
            confidence=confidence,
            notes=note,
        )
    if body_len >= min_body_chars:
        return SectionCandidate(
            section_type=target.section_type,
            heading=heading,
            start_offset=start,
            end_offset=end,
            confidence=0.5,
            notes=(
                f"{note}; short body ({body_len} chars) — may be a brief update or a "
                "table-of-contents artifact; verify before relying on this section"
            ),
        )
    # ToC-scale or no body: record the uncertainty as 'other', never mislabel.
    return SectionCandidate(
        section_type="other",
        heading=heading,
        start_offset=start,
        end_offset=end,
        confidence=0.2,
        notes=(
            f"{note}; only a table-of-contents-scale match ({body_len} chars of body) was "
            f"found for the {target.section_type} target — not labelled {target.section_type}"
        ),
    )


def detect_sections(
    form: str,
    doc: CleanedDocument,
    *,
    min_substantial_body_chars: int = DEFAULT_MIN_SUBSTANTIAL_BODY_CHARS,
    min_body_chars: int = DEFAULT_MIN_BODY_CHARS,
) -> list[SectionCandidate]:
    """Detect MD&A / risk-factor sections for a 10-Q or 10-K cleaned document.

    Returns candidates sorted by ``start_offset``. Unknown forms (8-K etc.)
    produce no item-based candidates; use :func:`earnings_release_candidate`
    for those. Uncertain matches are demoted to ``other`` with low confidence —
    a missing section stays missing (never a whole-filing mislabel).
    """
    targets = _targets_for_form(form)
    if not targets:
        return []
    hits = _scan_item_level_blocks(doc.blocks)
    candidates = [
        candidate
        for target in targets.values()
        if (
            candidate := _candidate_for_target(
                target,
                hits,
                doc,
                min_substantial_body_chars=min_substantial_body_chars,
                min_body_chars=min_body_chars,
            )
        )
        is not None
    ]
    candidates.sort(key=lambda c: c.start_offset)
    return candidates


def earnings_release_candidate(doc: CleanedDocument) -> SectionCandidate:
    """Whole-exhibit section for an 8-K Item 2.02 earnings release exhibit."""
    text = doc.text
    head = text[:2000].lower()
    signals = ("report", "results", "announce", "earnings", "quarter", "revenue", "fiscal")
    signal_hits = sum(1 for signal in signals if signal in head)
    if signal_hits >= 3:
        confidence = 0.85
    elif signal_hits >= 1:
        confidence = 0.7
    else:
        confidence = 0.4
    heading = next((b.text for b in doc.blocks if b.kind == "heading"), "")
    heading = heading or str(doc.metadata.get("title") or "") or "Earnings release"
    return SectionCandidate(
        section_type="earnings_release",
        heading=_heading_label(TextBlock("heading", heading, 0, len(heading))),
        start_offset=0,
        end_offset=len(text),
        confidence=confidence,
        notes=f"whole-exhibit earnings release; {signal_hits}/{len(signals)} title signals",
    )
