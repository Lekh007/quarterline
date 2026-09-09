"""Citation validation (SPEC §2.2, §18 checks 4–7).

Given generated statements and the map of evidence that was actually supplied
to the model (evidence_id -> document identity + company + period + text),
every citation must:

4. exist in the supplied evidence map;
5. be part of the supplied context (the map is built FROM the supplied
   context, so existence and membership are enforced together — recorded as
   separate check results);
6. belong to the correct company AND the requested reporting period — a
   citation from a document of a different fiscal period is rejected, and a
   document whose period is unknown cannot be verified, so it is rejected
   ("cannot be validated reliably" -> drop, SPEC §18);
7. appear in sentence-level format: ``[ev-xxxxxxxxxxxx]`` attached at the end
   of the sentence making the assertion.

An invalid citation drops the WHOLE statement — never just the stripped id
(SPEC §2.2.5). Reasons are recorded per statement for observability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

#: Contract C8 evidence-id shape.
EVIDENCE_ID_RE = re.compile(r"ev-[0-9a-f]{12}")
_CITED_RE = re.compile(r"\[(ev-[0-9a-f]{12})\]")

#: Check ids (SPEC §18 order, 4–7).
CHECK_EXISTENCE = "citation_existence"
CHECK_MEMBERSHIP = "citation_supplied_context"
CHECK_CONTEXT = "citation_company_period"
CHECK_FORMAT = "citation_sentence_format"


@dataclass(frozen=True)
class EvidenceRef:
    """Identity of one supplied evidence window (the only citable material)."""

    evidence_id: str
    document_id: int
    ticker: str
    period_end: date | None
    text: str


@dataclass
class StatementCitationResult:
    """Outcome of validating one statement's citations."""

    statement_index: int
    valid: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class CitationReport:
    """Aggregate citation validation results (observable + testable)."""

    checked: int = 0
    valid_count: int = 0
    invalid_count: int = 0
    results: list[StatementCitationResult] = field(default_factory=list)

    @property
    def citation_valid(self) -> bool:
        """True when every statement's citations passed (run-event field)."""
        return self.invalid_count == 0

    def reasons_by_check(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for result in self.results:
            for reason in result.reasons:
                grouped.setdefault(reason.split(":", 1)[0], []).append(reason)
        return grouped


def _ids_cited_in_text(text: str) -> list[str]:
    return _CITED_RE.findall(text or "")


def _sentence_format_reasons(text: str) -> list[str]:
    """Check 7: every [ev-...] must close a sentence.

    Valid: the citation is followed only by whitespace and/or end of text, or
    by sentence-ending punctuation. Invalid: a citation pasted mid-sentence.
    """
    reasons: list[str] = []
    for match in _CITED_RE.finditer(text or ""):
        rest = text[match.end() :].lstrip()
        if rest and rest[0] not in ".!?":
            reasons.append(
                f"{CHECK_FORMAT}: citation [{match.group(1)}] is not attached to "
                "the end of the sentence making the assertion"
            )
    return reasons


def validate_statement_citations(
    text: str,
    evidence_ids: list[str],
    evidence_map: dict[str, EvidenceRef],
    *,
    expected_ticker: str,
    expected_period_end: date | None,
) -> list[str]:
    """All SPEC §18 citation checks (4–7) for one statement.

    Returns a list of rejection reasons; empty list means the statement's
    citations are fully valid. The statement index is not needed here and is
    attached by the caller.
    """
    reasons: list[str] = []
    cited_in_text = _ids_cited_in_text(text)
    declared = list(dict.fromkeys(evidence_ids or []))

    # Check 4 — existence: a listed id must exist in the supplied map.
    for evidence_id in declared:
        if evidence_id not in evidence_map:
            reasons.append(
                f"{CHECK_EXISTENCE}: cited evidence {evidence_id!r} was not supplied "
                "(invented or out-of-context citation)"
            )

    # Check 5 — supplied-context membership: an id used in the text must be
    # declared AND supplied; an undeclared [ev-...] tag in the text is dropped.
    for evidence_id in cited_in_text:
        if evidence_id not in declared:
            reasons.append(
                f"{CHECK_MEMBERSHIP}: citation [{evidence_id}] appears in the text but "
                "is not declared in evidence_ids"
            )
        elif evidence_id not in evidence_map:
            reasons.append(
                f"{CHECK_MEMBERSHIP}: citation [{evidence_id}] is not part of the "
                "supplied evidence context"
            )
    for evidence_id in declared:
        if evidence_id in evidence_map and evidence_id not in cited_in_text:
            reasons.append(
                f"{CHECK_MEMBERSHIP}: declared evidence {evidence_id!r} never appears "
                "in the statement text as [evidence_id]"
            )

    # Check 6 — company and period compatibility (SPEC §2.2.3).
    for evidence_id in declared:
        ref = evidence_map.get(evidence_id)
        if ref is None:
            continue  # already reported by the existence check
        if ref.ticker != expected_ticker:
            reasons.append(
                f"{CHECK_CONTEXT}: citation {evidence_id!r} belongs to company "
                f"{ref.ticker!r}, not {expected_ticker!r}"
            )
        if expected_period_end is not None:
            if ref.period_end is None:
                reasons.append(
                    f"{CHECK_CONTEXT}: citation {evidence_id!r} has no verifiable reporting "
                    "period; cannot be validated reliably"
                )
            elif ref.period_end != expected_period_end:
                reasons.append(
                    f"{CHECK_CONTEXT}: citation {evidence_id!r} comes from reporting period "
                    f"{ref.period_end.isoformat()}, requested {expected_period_end.isoformat()}"
                )

    # Check 7 — sentence-level citation format.
    reasons.extend(_sentence_format_reasons(text))
    return reasons


def filter_statements(
    statements: list[tuple[int, str, list[str]]],
    evidence_map: dict[str, EvidenceRef],
    *,
    expected_ticker: str,
    expected_period_end: date | None,
) -> tuple[list[tuple[int, str, list[str]]], CitationReport]:
    """Validate every statement; keep only fully valid ones.

    ``statements`` are (index, text, evidence_ids) triples. Invalid statements
    are dropped WHOLE (SPEC §2.2.5) with their reasons recorded in the report.
    Statements with no declared citations pass the citation checks (they are
    still subject to the numeric and advice checks in the gate).
    """
    report = CitationReport()
    kept: list[tuple[int, str, list[str]]] = []
    for index, text, evidence_ids in statements:
        report.checked += 1
        reasons = validate_statement_citations(
            text,
            evidence_ids,
            evidence_map,
            expected_ticker=expected_ticker,
            expected_period_end=expected_period_end,
        )
        if reasons:
            report.invalid_count += 1
            report.results.append(
                StatementCitationResult(statement_index=index, valid=False, reasons=reasons)
            )
        else:
            report.valid_count += 1
            report.results.append(StatementCitationResult(statement_index=index, valid=True))
            kept.append((index, text, evidence_ids))
    return kept, report


__all__ = [
    "CHECK_CONTEXT",
    "CHECK_EXISTENCE",
    "CHECK_FORMAT",
    "CHECK_MEMBERSHIP",
    "EVIDENCE_ID_RE",
    "CitationReport",
    "EvidenceRef",
    "StatementCitationResult",
    "filter_statements",
    "validate_statement_citations",
]
