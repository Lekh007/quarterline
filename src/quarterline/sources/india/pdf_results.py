"""Company-IR results PDFs: THIN header/scale detection helpers (IND-2).

Full table extraction is a later milestone. This module deliberately implements
only what the feasibility milestone must prove:

- scale-header detection ("₹ in Crores", "Rs in Lakhs", ...) over extracted page
  text (text itself comes from ``quarterline.ingest.pdf_extract``);
- scope-header detection (Consolidated/Standalone) with an honest ambiguity rule:
  company IR PDFs often bundle both scopes in one document — when both (or
  neither) are detected the result is ``review_required``, never a guess;
- a typed :class:`PdfHeaderDetection` carrying ``review_required`` so downstream
  code cannot silently consume an ambiguous extraction.

No LLM-assisted extraction anywhere (SPEC 2.1.3; SPEC 27).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from quarterline.sources.india.units import CRORE, LAKH

#: Scale headers observed in the acquired PDFs (docs/india_source_audit.md §6.6):
#: HUL prints "(Rs in Crores)" / "Rs in Lakhs"; Infosys prints "In ₹ crore".
_SCALE_HEADER_PATTERNS: tuple[tuple[re.Pattern[str], Decimal, str], ...] = (
    (re.compile(r"(?:₹|Rs\.?|INR)\s*(?:in|In)?\s*crores?\b", re.IGNORECASE), CRORE, "Crores"),
    (re.compile(r"\bin\s+₹?\s*crores?\b", re.IGNORECASE), CRORE, "Crores"),
    (re.compile(r"(?:₹|Rs\.?|INR)\s*(?:in|In)?\s*lakhs?\b", re.IGNORECASE), LAKH, "Lakhs"),
    (re.compile(r"\bin\s+₹?\s*lakhs?\b", re.IGNORECASE), LAKH, "Lakhs"),
)

_SCOPE_CONSOLIDATED = re.compile(r"\bconsolidated\b", re.IGNORECASE)
_SCOPE_STANDALONE = re.compile(r"\bstandalone\b", re.IGNORECASE)


@dataclass(frozen=True)
class PdfHeaderDetection:
    """Typed detection result; ambiguity is a flag, never a guessed value."""

    scale: Decimal | None  # declared display scale (multiplier to rupees)
    scale_label: str | None  # "Crores" / "Lakhs"
    scope: str | None  # "consolidated" / "standalone"
    review_required: bool
    reason: str | None

    @property
    def ok(self) -> bool:
        return not self.review_required


def _detect_scale(text: str) -> tuple[Decimal | None, str | None, str | None]:
    labels: list[tuple[Decimal, str]] = []
    for pattern, scale, label in _SCALE_HEADER_PATTERNS:
        if pattern.search(text) and (scale, label) not in labels:
            labels.append((scale, label))
    if not labels:
        return None, None, "no scale header found"
    distinct = {label for _, label in labels}
    if len(distinct) > 1:
        return None, None, f"conflicting scale headers: {sorted(distinct)}"
    return labels[0][0], labels[0][1], None


def _detect_scope(text: str) -> tuple[str | None, str | None]:
    has_consolidated = bool(_SCOPE_CONSOLIDATED.search(text))
    has_standalone = bool(_SCOPE_STANDALONE.search(text))
    if has_consolidated and has_standalone:
        return None, "both Consolidated and Standalone headers present — split by section first"
    if has_consolidated:
        return "consolidated", None
    if has_standalone:
        return "standalone", None
    return None, "no Consolidated/Standalone header found"


def detect_headers(text: str) -> PdfHeaderDetection:
    """Detect declared scale and scope in extracted page text.

    Any detection failure sets ``review_required`` with the reason; detection
    never invents a scale or scope.
    """
    scale, scale_label, scale_reason = _detect_scale(text)
    scope, scope_reason = _detect_scope(text)
    reasons = [r for r in (scale_reason, scope_reason) if r]
    return PdfHeaderDetection(
        scale=scale,
        scale_label=scale_label,
        scope=scope,
        review_required=bool(reasons),
        reason="; ".join(reasons) if reasons else None,
    )


def page_texts(content: bytes) -> list[tuple[int, str]]:
    """Extract (page_number, text) pairs via the shared PDF extractor (thin reuse)."""
    from quarterline.ingest.pdf_extract import extract_pdf

    extraction = extract_pdf(content)
    return [(page.page_number, page.text) for page in extraction.pages]


def detect_headers_in_pdf(content: bytes) -> list[tuple[int, PdfHeaderDetection]]:
    """Run header detection per page of a real PDF (used by later milestones)."""
    return [(page_number, detect_headers(text)) for page_number, text in page_texts(content)]
