"""Typed missing-data statuses for India facts (IND-3, reviewer correction A).

When an expected value is absent, the pipeline must record WHY it is absent —
distinctly, and never as a zero. "Quarterly cash flow not present in the
ingested sources" is a statement about OUR corpus, not about the company
(docs/india_source_audit.md §10, claim-audit correction of the IND-1/IND-2
phrasing "has no quarterly cash flow").

The five statuses are mutually exclusive and never coerce to ``0``:

- ``not_present_in_ingested_sources`` — the issuer/period is in scope, every
  eligible ingested document was examined, and the concept simply is not
  reported in any of them (e.g. HUL Q1 FY27 cash flow: no CF in the exchange
  instance, none in the results PDF, no CF sheets in the Q1 workbook);
- ``source_not_ingested`` — a document that plausibly reports the concept is
  known to exist but is not in the corpus (e.g. BSE copies, older periods);
- ``extraction_failed`` — the document IS ingested but extraction produced no
  trustworthy value (scan needing OCR, parse error);
- ``requires_manual_review`` — extraction produced a candidate value with
  table ambiguity; a human must read the document, we never guess;
- ``not_applicable`` — the concept cannot exist for this issuer/period by
  rule (e.g. a quarterly cash-flow DERIVATION for a company with no cumulative
  CF observations to derive from is not "zero", it is not applicable).
"""

from __future__ import annotations

import enum


class MissingDataStatus(str, enum.Enum):
    """Why an expected India value is absent. Distinct, never zero (IND-3)."""

    NOT_PRESENT_IN_INGESTED_SOURCES = "not_present_in_ingested_sources"
    SOURCE_NOT_INGESTED = "source_not_ingested"
    EXTRACTION_FAILED = "extraction_failed"
    REQUIRES_MANUAL_REVIEW = "requires_manual_review"
    NOT_APPLICABLE = "not_applicable"

    def __str__(self) -> str:  # pragma: no cover - display convenience
        return self.value


#: Every status value (for validation of external inputs).
MISSING_DATA_STATUSES: frozenset[str] = frozenset(s.value for s in MissingDataStatus)


def require_status(value: str) -> MissingDataStatus:
    """Parse a status string into the enum; unknown strings raise (never guess)."""
    for status in MissingDataStatus:
        if status.value == value:
            return status
    raise ValueError(
        f"unknown missing-data status {value!r}; expected one of {sorted(MISSING_DATA_STATUSES)}"
    )
