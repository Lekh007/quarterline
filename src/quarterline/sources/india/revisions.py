"""Original-vs-revised filing handling (IND-2).

Where the data lives (observed, docs/india_source_audit.md §3 and §6.8):

- The XBRL instance itself carries **no revision field**. It does carry audit
  qualifiers (``WhetherResultsAreAuditedOrUnaudited``, the declaration of
  unmodified opinion), which the parser surfaces verbatim.
- The **exchange listing** (NSE Integrated Filing- Financials) declares
  ``Original`` plus revision fields (``revised_Date``, ``revision_Remark``) per
  filing. Those fields are carried here as :class:`FilingMeta` from the
  import/manifest metadata — never inferred from document content.

Selection policy (SPEC 2.1.10 — revised observations are preserved, never
silently overwritten):

- ``select_latest(revisions)`` picks the latest **publication** per
  (issuer, scope, period) for the "latest_available" view;
- ``select_latest(revisions, as_of=d)`` excludes filings published after ``d``
  (an as-of view must never use later information);
- all revisions stay in ``fact_observations`` — selection happens at the
  normalized/derived layer, not by deleting evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

#: Instance-declared revision vocabulary from the exchange listing.
REVISION_ORIGINAL = "Original"
REVISION_REVISED = "Revised"


@dataclass(frozen=True)
class FilingMeta:
    """Exchange-listing metadata for one filed document (carried, not inferred)."""

    issuer_id: str
    scope: str  # "consolidated" | "standalone"
    period_start: date | None
    period_end: date
    published_at: date  # broadcast date (IST listing timestamp -> date)
    revision_status: str | None  # "Original"/"Revised" from the listing; None = unknown
    audited_status: str | None  # listing label ("Audited"/"Un-Audited"); instance wins if present
    doc_type: str = "financial_results"
    exchange: str | None = None  # "NSE"
    seq_id: str | None = None  # NSE seq_Id (stable per filing broadcast)
    source_url: str | None = None
    notes: str | None = None

    @property
    def period_key(self) -> tuple[str, date]:
        """Revisions are comparable within one (scope, period-end)."""
        return (self.scope, self.period_end)


def select_latest(
    revisions: list[FilingMeta],
    as_of: date | None = None,
) -> FilingMeta | None:
    """Pick the in-force filing for one (scope, period): latest publication wins.

    ``as_of`` excludes filings published after that date (SPEC 10.4). Input order
    breaks publication-date ties deterministically (first listed wins). Returns
    ``None`` when nothing survives the ``as_of`` filter.
    """
    if not revisions:
        return None
    period_keys = {r.period_key for r in revisions}
    if len(period_keys) > 1:
        raise ValueError(
            "select_latest expects one (scope, period); got: "
            + ", ".join(sorted(str(k) for k in period_keys))
        )
    candidates = [r for r in revisions if as_of is None or r.published_at <= as_of]
    if not candidates:
        return None
    return max(candidates, key=lambda r: (r.published_at, -revisions.index(r)))
