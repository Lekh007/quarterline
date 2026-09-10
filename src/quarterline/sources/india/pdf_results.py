"""Company-IR results PDFs: header/scale detection + cash-flow extraction (IND-3).

Header detection (IND-2, unchanged):

- scale-header detection ("₹ in Crores", "Rs in Lakhs", ...) over extracted page
  text (text itself comes from ``quarterline.ingest.pdf_extract``);
- scope-header detection (Consolidated/Standalone) with an honest ambiguity rule:
  company IR PDFs often bundle both scopes in one document — when both (or
  neither) are detected the result is ``review_required``, never a guess;
- a typed :class:`PdfHeaderDetection` carrying ``review_required`` so downstream
  code cannot silently consume an ambiguous extraction.

Cash-flow statement extraction (IND-3, reviewer correction A): a REPORTED
cash-flow observation may be extracted from an identified official document at
its ACTUAL reported frequency — e.g. the quarterly CFO in Infosys' Q1 condensed
Ind AS FS PDF. Extraction is text-level with page provenance; every ambiguity
(scale header missing, statement period not parseable, net-CFO line absent,
column order not verifiable) yields ``requires_manual_review`` — never a guess,
never a fabricated value.

No LLM-assisted extraction anywhere (SPEC 2.1.3; SPEC 27).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
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


# ---------------------------------------------------------------------------
# Cash-flow statement extraction (IND-3, reviewer correction A)
# ---------------------------------------------------------------------------

STATUS_EXTRACTED = "extracted"
STATUS_MANUAL_REVIEW = "requires_manual_review"

#: Canonical "net cash from operating activities" line labels observed in the
#: acquired company-IR PDFs (Infosys condensed FS: "Net cash generated by
#: operating activities"; HUL results letter: "Net cash flows generated from
#: operating activities").
_NET_CFO_LABELS: tuple[re.Pattern[str], ...] = (
    re.compile(r"net cash (?:flows? )?generated (?:by|from) operating activities", re.IGNORECASE),
    re.compile(r"net cash (?:flow|flows) from operating activities", re.IGNORECASE),
    re.compile(r"net cash used in operating activities", re.IGNORECASE),
)

#: Statement-period headers: ("three months ended June 30, 2026", "Year ended
#: 31st March, 2026"). Only canonical Indian quarter/FY end dates are accepted;
#: anything else is table ambiguity -> manual review.
_PERIOD_HEADER_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"three\s+months\s+ended\s+([A-Z][a-z]+\s+\d{1,2},?\s+(\d{4}))", re.IGNORECASE), 3),
    (re.compile(r"six\s+months\s+ended\s+([A-Z][a-z]+\s+\d{1,2},?\s+(\d{4}))", re.IGNORECASE), 6),
    (re.compile(r"nine\s+months\s+ended\s+([A-Z][a-z]+\s+\d{1,2},?\s+(\d{4}))", re.IGNORECASE), 9),
    (
        re.compile(
            r"year\s+ended\s+(\d{1,2}(?:st|nd|rd|th)?\s+[A-Z][a-z]+,?\s*(\d{4}))", re.IGNORECASE
        ),
        12,
    ),
    (re.compile(r"year\s+ended\s+([A-Z][a-z]+\s+\d{1,2},?\s+(\d{4}))", re.IGNORECASE), 12),
)

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

_MONTH_END_DAY = {
    1: 31,
    2: 29,
    3: 31,
    4: 30,
    5: 31,
    6: 30,
    7: 31,
    8: 31,
    9: 30,
    10: 31,
    11: 30,
    12: 31,
}

_NUMBER_TOKEN = re.compile(r"\(?-?[\d,]+(?:\.\d+)?\)?")

_YEAR_TOKEN = re.compile(r"\b(20\d{2})\b")


@dataclass(frozen=True)
class CashFlowStatementExtraction:
    """Typed result of one cash-flow statement extraction attempt.

    ``status`` is ``extracted`` only when scale, statement period, net-CFO
    line, and current-period column order were ALL verified from the page
    text. Any other outcome is ``requires_manual_review`` with ``value=None``
    — ambiguity surfaces, it is never resolved by guessing.
    """

    source_document_id: str
    page_number: int
    status: str
    reason: str | None
    concept: str = "cash_flow_operations"
    value: Decimal | None = None  # full rupees when status == extracted
    declared_scale: Decimal | None = None
    scale_label: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    matched_line: str | None = None
    period_source: str | None = None  # "page" | "document_title" (provenance)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_EXTRACTED


def _parse_period_header(text: str) -> tuple[date, date] | None:
    """Exact (start, end) from a statement-period header, else ``None``.

    Accepts only canonical period ends (last day of a month, matching the
    stated span) — e.g. "three months ended June 30, 2026" -> 2026-04-01..2026-06-30;
    "Year ended 31st March, 2026" -> 2025-04-01..2026-03-31.
    """
    for pattern, months in _PERIOD_HEADER_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        end = _parse_display_date(match.group(1))
        if end is None or end.day != _MONTH_END_DAY.get(end.month):
            return None
        # Start = first day of the month `months-1` before the end month
        # (the end day must be the last day of its month, checked above).
        start_month_index = end.year * 12 + (end.month - 1) - (months - 1)
        start = date(start_month_index // 12, start_month_index % 12 + 1, 1)
        return start, end
    return None


def _parse_display_date(fragment: str) -> date | None:
    """``June 30, 2026`` / ``31st March, 2026`` -> date; ``None`` if unclear."""
    fragment = fragment.replace(",", " ")
    lowered = fragment.lower()
    month = next(
        (number for name, number in _MONTHS.items() if name in lowered),
        None,
    )
    numbers = re.findall(r"\d{1,4}", fragment)
    if month is None or len(numbers) < 2:
        return None
    day, year = int(numbers[0]), int(numbers[1])
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _current_column_is_first(text: str, period_end: date) -> bool:
    """True when the page's year-header order puts the statement year first.

    The condensed statements print the period column heads before the rows
    (Infosys: "Particulars / Note No. / 2026 / 2025"; HUL: "Year ended
    31st March, 2026 / Year ended 31st March, 2025"). If the statement's
    end-year is not the FIRST year token on the page, column order is
    unverified and the caller must record manual review instead of picking.
    """
    years = [int(y) for y in _YEAR_TOKEN.findall(text[:1200])]
    if not years:
        return False
    return years[0] == period_end.year


def extract_cash_flow_statement(
    page_number: int,
    text: str,
    source_document_id: str,
    period_context: str | None = None,
) -> CashFlowStatementExtraction:
    """Extract one reported net-operating-cash-flow figure from one page.

    Text-level extraction with page provenance (IND-3 correction A). The
    statement period is resolved from the page first, then from
    ``period_context`` (e.g. the document's title page text — Infosys prints
    "for the three months ended June 30, 2026" on the cover, not on the
    statement page); the provenance of the period is recorded in
    ``period_source``. Every unresolved ambiguity returns
    ``requires_manual_review`` with no value.
    """
    scale_detection = detect_headers(text)
    period = _parse_period_header(text)
    period_source = "page"
    if period is None and period_context:
        period = _parse_period_header(period_context)
        period_source = "document_title"
    if period is None:
        return CashFlowStatementExtraction(
            source_document_id=source_document_id,
            page_number=page_number,
            status=STATUS_MANUAL_REVIEW,
            reason="no parseable cash-flow statement period header on page or in document title",
        )
    if scale_detection.scale is None:
        return CashFlowStatementExtraction(
            source_document_id=source_document_id,
            page_number=page_number,
            status=STATUS_MANUAL_REVIEW,
            reason=f"no declared scale header on page ({scale_detection.reason})",
            period_start=period[0],
            period_end=period[1],
            period_source=period_source,
        )

    lines = text.splitlines()
    label: re.Pattern[str]
    for label in _NET_CFO_LABELS:
        for index, line in enumerate(lines):
            if not label.search(line):
                continue
            # Number tokens on the label line, then continuation lines until a
            # section header or alphabetic line intervenes.
            tokens: list[str] = _NUMBER_TOKEN.findall(line)
            lookahead = index + 1
            while len(tokens) < 2 and lookahead < len(lines):
                candidate = lines[lookahead].strip()
                if not candidate or re.search(r"[A-Za-z]{4,}", candidate):
                    break
                tokens.extend(_NUMBER_TOKEN.findall(candidate))
                lookahead += 1
            if not tokens:
                continue
            if not _current_column_is_first(text, period[1]):
                return CashFlowStatementExtraction(
                    source_document_id=source_document_id,
                    page_number=page_number,
                    status=STATUS_MANUAL_REVIEW,
                    reason="current-period column order not verifiable from page text",
                    declared_scale=scale_detection.scale,
                    scale_label=scale_detection.scale_label,
                    period_start=period[0],
                    period_end=period[1],
                    matched_line=line.strip(),
                    period_source=period_source,
                )
            first = tokens[0]
            negative = first.startswith("(") and first.endswith(")")
            digits = first.strip("()").replace(",", "")
            try:
                value = Decimal(digits) * scale_detection.scale
            except ArithmeticError:  # pragma: no cover - malformed number token
                break
            return CashFlowStatementExtraction(
                source_document_id=source_document_id,
                page_number=page_number,
                status=STATUS_EXTRACTED,
                reason=None,
                value=-value if negative else value,
                declared_scale=scale_detection.scale,
                scale_label=scale_detection.scale_label,
                period_start=period[0],
                period_end=period[1],
                matched_line=line.strip(),
                period_source=period_source,
            )
    return CashFlowStatementExtraction(
        source_document_id=source_document_id,
        page_number=page_number,
        status=STATUS_MANUAL_REVIEW,
        reason="no net operating cash flow line found on page",
        period_start=period[0],
        period_end=period[1],
        period_source=period_source,
    )


def extract_cash_flow_from_pdf(
    content: bytes,
    source_document_id: str,
) -> list[CashFlowStatementExtraction]:
    """Run :func:`extract_cash_flow_statement` over every page of a PDF.

    The document's own title pages (first two pages) provide the
    ``period_context`` so a statement page without an inline period header can
    still be resolved with recorded provenance.
    """
    pages = page_texts(content)
    title_context = "\n".join(text for _, text in pages[:2])
    return [
        extract_cash_flow_statement(page_number, text, source_document_id, title_context)
        for page_number, text in pages
    ]
