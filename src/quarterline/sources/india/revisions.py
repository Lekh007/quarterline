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

Versions vs revisions (IND-3): :func:`classify_filing_pair` distinguishes a
duplicate copy of the same document (identical content), an original filing, an
explicitly revised filing (listing-declared), and a comparative value reported
in a later filing — which is NOT a revision. The latest-available selection
policy keeps history retained; a revised filing can never replace facts of a
different period/scope/unit/concept (the observation hash makes those distinct
identities).
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


# ---------------------------------------------------------------------------
# Filing VERSIONS vs REVISIONS (IND-3, Task 6)
# ---------------------------------------------------------------------------

#: A duplicate copy of the SAME document (identical content), however it was
#: named or however many times it was imported. Content-addressed storage makes
#: re-import idempotent, so duplicates never create new facts.
VERSION_DUPLICATE_COPY = "duplicate_copy"

#: An original filing: the first (or only) filing for one (scope, period) with
#: no revision declared for it by the exchange listing.
VERSION_ORIGINAL = "original_filing"

#: An explicitly revised filing: the exchange listing declares a revised
#: submission (``revised_Date``/``revision_Remark``, carried as
#: ``revision_status="Revised"``) for the SAME (scope, period). A revised filing
#: must not replace facts of a different period/scope/unit/concept — the
#: observation hash keeps those distinct identities, and both the original and
#: the revision are retained.
VERSION_REVISED = "revised_filing"

#: A comparative value reported in a LATER filing for a DIFFERENT period (e.g.
#: the June-2026 quarter filing re-reporting the March-2026 quarter as a
#: comparative column). This is NOT a revision of the earlier filing: the later
#: document merely repeats the earlier period's figures for context.
VERSION_LATER_COMPARATIVE = "later_comparative"

VERSION_KINDS: frozenset[str] = frozenset(
    {VERSION_DUPLICATE_COPY, VERSION_ORIGINAL, VERSION_REVISED, VERSION_LATER_COMPARATIVE}
)


@dataclass(frozen=True)
class FilingVersion:
    """Minimal identity of one filed document for version classification."""

    content_hash: str
    scope: str
    period_end: date
    published_at: date
    revision_status: str | None  # listing-declared "Original"/"Revised"; None = unknown

    @property
    def period_key(self) -> tuple[str, date]:
        return (self.scope, self.period_end)


def classify_filing_pair(first: FilingVersion, second: FilingVersion) -> str:
    """Classify the relationship between two filed documents.

    - identical content -> ``duplicate_copy`` (never a new revision);
    - same (scope, period): an explicitly declared revised submission is a
      ``revised_filing``; two originals for one period are NOT folded together —
      the relationship is reported as ``original_filing`` (both retained, the
      listing alone decides which is in force);
    - different (scope, period): the later document's repetition of the earlier
      period is a ``later_comparative``, never a revision.
    """
    if first.content_hash == second.content_hash:
        return VERSION_DUPLICATE_COPY
    if first.period_key == second.period_key:
        statuses = {first.revision_status, second.revision_status}
        if REVISION_REVISED in statuses:
            return VERSION_REVISED
        return VERSION_ORIGINAL
    return VERSION_LATER_COMPARATIVE
