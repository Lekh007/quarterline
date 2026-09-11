"""Canonical fact selection: India observations -> normalized_facts (IND-4).

Policy ``india-normalization-v1`` (docs/india_financial_methodology.md,
"Canonical facts and metrics"):

- **Scope**: consolidated is the default research series; standalone
  observations are normalized into facts under their OWN scope and are never
  substituted into a consolidated series (or vice versa). The reporting scope is
  part of the fact identity (normalized_facts unique key), so the two coexist
  without ever being mixed.
- **Recency**: the latest publication (``filed_at``) wins per (issuer, scope,
  concept, period identity). Older filings' observations remain in
  ``fact_observations`` and lineage/history stays queryable — selection happens
  here, never by deleting evidence.
- **Period identity**: (period_start, period_end, period_kind,
  reporting_scope) — quarter, annual, and any future half_year/9M YTD facts
  coexist without collision (annual facts carry fiscal_quarter ``None`` even
  when they end on 31 March, which is also Q4's end date).
- **Tag fallbacks**: where a concept has ordered fallback tags
  (``data/tagmap_india.yml``), observations from the SAME publication are
  selected in tagmap-priority order and the lineage row records which tag won
  (via the winning observation's ``original_tag``). E.g. ``profit_before_tax``
  selects ``ProfitBeforeTax`` (39,995 Cr INFY FY26) over the fallback
  ``ProfitBeforeExceptionalItemsAndTax`` (41,284 Cr), and ``capex`` selects the
  PP&E purchase over the intangibles fallback.
- **Lineage**: every canonical fact writes ``fact_lineage`` rows with role
  ``direct_source`` pointing at the winning observation (derived facts would use
  the derivation roles; none exist in this milestone — cash-flow derivations
  report their blocked status instead of a fabricated number).
- **Data quality**: the fact carries the IND-3 reconciliation review status of
  its winning observation — ``agent_checked_against_document`` where the packet
  verified the rendered document, ``requires_manual_review`` where the packet
  left the row human-review-pending (HUL P&L scrambling; INFY FY26 intangible
  capex), ``None`` when the packet does not cover the observation. Review
  status is carried, never resolved.

India concept strings are exactly the ``data/tagmap_india.yml`` allowlist and
never collide with US ``METRIC_IDS`` (asserted in tests). Everything is Decimal;
nothing is guessed, averaged, or coerced.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import select

from quarterline.sources.india.concept_map import load_tagmap
from quarterline.sources.india.issuers import require_verified
from quarterline.sources.india.periods import fiscal_quarter, fiscal_year, period_label
from quarterline.sources.india.reconcile import (
    REVIEW_AGENT_CHECKED,
    REVIEW_HUMAN_PENDING,
    review_status_for,
)
from quarterline.store.db import session_scope
from quarterline.store.models import FactObservation, NormalizedFact, SourceArtifact
from quarterline.store.repositories.companies import CompaniesRepo
from quarterline.store.repositories.facts import (
    ROLE_DIRECT_SOURCE,
    FactsRepo,
    text_to_decimal,
)

#: Version stamped on every canonical India fact.
NORMALIZATION_VERSION = "india-normalization-v1"

#: Selection policy label stored on every canonical India fact.
SELECTION_POLICY = "india-latest-publication"

#: data_quality_status values carried onto canonical facts (IND-3 review
#: vocabulary; ``requires_manual_review`` is the canonical-fact rendering of the
#: packet's ``human_review_pending``).
DQ_AGENT_CHECKED = "agent_checked_against_document"
DQ_REQUIRES_MANUAL_REVIEW = "requires_manual_review"


@dataclass
class IndiaNormalizationReport:
    """Summary of one ``normalize_canonical_facts`` run (idempotent)."""

    issuer_id: str
    facts_created: int = 0
    facts_updated: int = 0
    lineage_rows_added: int = 0
    facts_by_scope: dict[str, int] = field(default_factory=dict)
    winning_tags: dict[str, int] = field(default_factory=dict)


def _tag_priorities() -> dict[str, int]:
    """{raw tag: tagmap priority} (lower wins) across every India concept."""
    priorities: dict[str, int] = {}
    for tags in load_tagmap().values():
        for index, tag in enumerate(tags):
            priorities.setdefault(tag, index)
    return priorities


def _carried_review_status(observation: FactObservation) -> str | None:
    """Review status carried in the observation's own metadata (PDF-sourced rows)."""
    try:
        metadata = json.loads(observation.context_metadata_json or "{}")
    except json.JSONDecodeError:  # pragma: no cover - defensive
        return None
    return metadata.get("review_status")


def data_quality_status_for(
    observation: FactObservation, issuer_id: str, document_id: str
) -> str | None:
    """Canonical data-quality status for the observation's review state.

    PDF-sourced observations carry their review status in their own metadata;
    XBRL observations are looked up in the IND-3 packet by (issuer, document,
    context kind, tag). Uncovered observations (synthetic tests) carry ``None``.
    """
    carried = _carried_review_status(observation)
    packet = review_status_for(
        issuer_id, document_id, observation.period_kind or "", observation.original_tag
    )
    status = carried or packet
    if status == REVIEW_AGENT_CHECKED:
        return DQ_AGENT_CHECKED
    if status == REVIEW_HUMAN_PENDING:
        return DQ_REQUIRES_MANUAL_REVIEW
    return None


def _document_id_for(session, observation: FactObservation) -> str:
    """Fixture document id of the observation's artifact (its file basename)."""
    if observation.source_artifact_id is None:
        return ""
    artifact = session.get(SourceArtifact, observation.source_artifact_id)
    if artifact is None or not artifact.local_path:
        return ""
    return Path(artifact.local_path).name


def _select_winner(group: list[FactObservation], priorities: dict[str, int]) -> FactObservation:
    """Latest publication wins; ties break by tagmap order, then observation id.

    ``filed_at`` is the publication date carried from the exchange listing (or
    the PDF document date). ``-priority`` turns "lowest tagmap index wins" into
    a max-key comparison.
    """

    def rank(observation: FactObservation) -> tuple:
        published = observation.filed_at or date.min
        priority = priorities.get(observation.original_tag, len(priorities) + 1)
        return (published, -priority, observation.id)

    return max(group, key=rank)


def _canonical_fact_from(
    winner: FactObservation,
    data_quality_status: str | None,
    available_at: datetime,
) -> NormalizedFact:
    kind = winner.period_kind
    return NormalizedFact(
        company_id=winner.company_id,
        concept=winner.canonical_concept,
        period_start=winner.period_start,
        period_end=winner.period_end,
        fiscal_year=fiscal_year(winner.period_end),
        # An annual fact ending 31 March shares Q4's end date but is NOT a
        # quarter: only quarter-kind facts carry a fiscal_quarter label.
        fiscal_quarter=fiscal_quarter(winner.period_end) if kind == "quarter" else None,
        period_kind=kind,
        reporting_scope=winner.reporting_scope or "consolidated",
        value_decimal=winner.value_decimal,
        unit=winner.unit,
        selection_policy=SELECTION_POLICY,
        normalization_version=NORMALIZATION_VERSION,
        is_derived=False,
        derivation_method=None,
        available_at=available_at,
        data_quality_status=data_quality_status,
    )


def normalize_canonical_facts(
    issuer_id: str,
    database_url: str | None = None,
    *,
    scope: str | None = None,
) -> IndiaNormalizationReport:
    """Normalize every mapped India observation into canonical facts. Idempotent.

    ``scope=None`` (default) normalizes every scope (standalone observations are
    retained as their own facts); pass ``"consolidated"``/``"standalone"`` to
    restrict the run. Re-runs create zero rows (upsert by the full period
    identity) and re-link lineage idempotently.
    """
    issuer = require_verified(issuer_id)
    report = IndiaNormalizationReport(issuer_id=issuer.issuer_id)
    priorities = _tag_priorities()
    with session_scope(database_url) as session:
        companies_repo = CompaniesRepo(session)
        company = companies_repo.get_by_ticker(issuer.ticker_nse or issuer.issuer_id)
        if company is None:
            raise LookupError(
                f"no company for {issuer.issuer_id} — import a document first "
                "(`quarterline ingest india-document`)"
            )
        facts_repo = FactsRepo(session)
        observations = list(
            session.scalars(
                select(FactObservation)
                .where(
                    FactObservation.company_id == company.id,
                    FactObservation.taxonomy == "in-capmkt",
                )
                .order_by(FactObservation.id)
            )
        )
        groups: dict[tuple, list[FactObservation]] = {}
        for observation in observations:
            if scope is not None and observation.reporting_scope != scope:
                continue
            identity = (
                observation.canonical_concept,
                observation.period_start,
                observation.period_end,
                observation.period_kind,
                observation.reporting_scope or "consolidated",
            )
            groups.setdefault(identity, []).append(observation)

        available_at = datetime.now(UTC)
        for identity, group in sorted(
            groups.items(),
            key=lambda item: (item[0][4], item[0][1] or date.min, item[0][0]),
        ):
            _concept, _start, _end, _kind, group_scope = identity
            winner = _select_winner(group, priorities)
            document_id = _document_id_for(session, winner)
            fact = _canonical_fact_from(
                winner,
                data_quality_status_for(winner, issuer.issuer_id, document_id),
                available_at,
            )
            fact_row, created = facts_repo.upsert_fact(fact)
            if created:
                report.facts_created += 1
                report.facts_by_scope[group_scope] = report.facts_by_scope.get(group_scope, 0) + 1
            else:
                report.facts_updated += 1
            existing_lineage = {
                lineage.source_observation_id
                for _obs, lineage in facts_repo.lineage_for_fact(fact_row.id)
            }
            facts_repo.add_lineage(fact_row.id, winner.id, ROLE_DIRECT_SOURCE)
            if winner.id not in existing_lineage:
                report.lineage_rows_added += 1
            if created:
                report.winning_tags[winner.original_tag] = (
                    report.winning_tags.get(winner.original_tag, 0) + 1
                )
    return report


# ---------------------------------------------------------------------------
# Cash-flow derivation status (IND-3 §4/§5): H2 = annual − H1 is permitted ONLY
# when both cumulative observations exist. No H1 cash flow is ingested for
# either issuer, so every H2 reports its blocked status — never a number.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DerivationStatus:
    """One blocked cash-flow derivation, with the typed reason it is blocked."""

    concept: str
    target_label: str  # "H2 FY2025-26" (never "Q4")
    target_start: date
    target_end: date
    method: str  # "annual - H1" (subtraction only; no division code path exists)
    status: str  # MissingDataStatus value
    note: str


def cash_flow_derivation_statuses(
    session,
    company_id: int,
    scope: str = "consolidated",
) -> list[DerivationStatus]:
    """Report blocked cash-flow derivations for every annual CF observation.

    For each cash-flow concept with an annual observation, the H2 interval
    (1 Oct..31 Mar) would be ``annual − H1``; when the required H1 cumulative
    observation (1 Apr..30 Sep of the same FY, same scope/unit) is not ingested
    the derivation is blocked with ``source_not_ingested``. A hypothetical
    "Q4 = annual − 9M" is ``not_applicable`` until a 9M CF document exists.
    """
    from quarterline.sources.india.cash_flow import CF_CONCEPTS
    from quarterline.sources.india.data_status import MissingDataStatus

    rows = list(
        session.scalars(
            select(FactObservation).where(
                FactObservation.company_id == company_id,
                FactObservation.taxonomy == "in-capmkt",
                FactObservation.reporting_scope == scope,
            )
        )
    )
    statuses: list[DerivationStatus] = []
    emitted: set[tuple[str, date, date]] = set()
    for concept in sorted(CF_CONCEPTS):
        annuals = [
            r
            for r in rows
            if r.canonical_concept == concept
            and r.period_kind == "annual"
            and r.period_start is not None
        ]
        for annual in sorted(annuals, key=lambda r: r.period_end, reverse=True):
            fy_start = annual.period_start
            fy_end = annual.period_end
            if (concept, fy_start, fy_end) in emitted:
                continue  # one report per concept+FY (several tags may be annual)
            emitted.add((concept, fy_start, fy_end))
            h1_end = date(fy_start.year, 9, 30)
            h2_start = date(fy_start.year, 10, 1)
            has_h1 = any(
                r.canonical_concept == concept
                and r.period_start == fy_start
                and r.period_end == h1_end
                for r in rows
            )
            has_9m = any(
                r.canonical_concept == concept
                and r.period_start == fy_start
                and r.period_end == date(fy_start.year, 12, 31)
                for r in rows
            )
            if has_h1:
                continue  # derivable — a future derivation wave's operation
            if has_9m:
                status = MissingDataStatus.NOT_APPLICABLE.value
                note = (
                    "Q4 = annual − 9M is not applicable: an annual and a 9M "
                    "cumulative observation exist but a 9M-derived quarter is a "
                    "derivation-layer operation and no Q4 CF derivation has been "
                    "requested for this milestone; nothing is divided"
                )
                target_label = "Q4 (from annual − 9M)"
                target_start = date(fy_start.year + 1, 1, 1)
            else:
                status = MissingDataStatus.SOURCE_NOT_INGESTED.value
                note = (
                    "H2 = annual − H1 requires a half-year cash-flow observation "
                    f"({fy_start.isoformat()}..{h1_end.isoformat()}); none is "
                    "ingested for this issuer — the annual figure is never "
                    "divided, halved, or annualized"
                )
                target_label = f"H2 {fy_start.year}-{str(fy_end.year)[-2:]}"
                target_start = h2_start
            statuses.append(
                DerivationStatus(
                    concept=concept,
                    target_label=target_label,
                    target_start=target_start,
                    target_end=fy_end,
                    method="annual - H1" if not has_9m else "annual - 9M",
                    status=status,
                    note=note,
                )
            )
    return statuses


def canonical_fact_label(fact: NormalizedFact) -> str:
    """Application label for a canonical fact, reproducible from its dates only."""
    return period_label(fact.period_start, fact.period_end, fact.period_kind)


def fact_value(fact: NormalizedFact) -> object:
    """Exact Decimal value of a canonical fact (Decimal passthrough)."""
    return text_to_decimal(fact.value_decimal)


__all__ = [
    "DQ_AGENT_CHECKED",
    "DQ_REQUIRES_MANUAL_REVIEW",
    "NORMALIZATION_VERSION",
    "SELECTION_POLICY",
    "DerivationStatus",
    "IndiaNormalizationReport",
    "canonical_fact_label",
    "cash_flow_derivation_statuses",
    "data_quality_status_for",
    "fact_value",
    "normalize_canonical_facts",
]
