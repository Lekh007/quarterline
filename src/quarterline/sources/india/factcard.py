"""India fact card (IND-4): canonical facts + metrics + coverage, with lineage.

``build_india_fact_card(session, issuer_id, period_end=None, scope="consolidated")``
assembles everything IND-5's indicator panels consume for one issuer-period:

- **issuer identity** — the verified registry fields (ISIN, NSE symbol, BSE
  code, verification source);
- **period** — the exact dates plus BOTH labels: the application label
  (Quarterline's own, derived from the dates only) and the source label
  (the issuer/instance's own qualifiers, carried not trusted);
- **canonical facts** — each with full provenance back to ``fact_observations``
  (observation ids via ``fact_lineage``, artifact, accession/URL, published
  date, audit + revision status, and the IND-3 review status);
- **metrics** — every registered ``india-metrics-v1`` value with its formula
  version, status, input fact ids and notes;
- **data coverage** — per concept: present / missing plus the EXACT typed
  missing-data status (``not_present_in_ingested_sources`` etc.), and the
  blocked cash-flow derivation statuses (H2 = annual − H1 with no H1 ingested).

There is NO aggregate score anywhere on the card — IND-5 renders indicators
only. Missing data is never zero and never guessed.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import select

from quarterline.cli import register_subcommand
from quarterline.sources.india.concept_map import INDIA_CONCEPTS, PER_SHARE_CONCEPTS
from quarterline.sources.india.data_status import MissingDataStatus
from quarterline.sources.india.issuers import require_verified
from quarterline.sources.india.metrics import (
    FORMULA_VERSION_INDIA_METRICS,
    IndiaMetricResult,
    build_metric_results,
)
from quarterline.sources.india.normalization import (
    NORMALIZATION_VERSION,
    SELECTION_POLICY,
    cash_flow_derivation_statuses,
)
from quarterline.sources.india.periods import period_label
from quarterline.sources.india.reconcile import review_status_for
from quarterline.sources.india.units import format_crores
from quarterline.store.models import FactObservation, NormalizedFact, text_to_decimal
from quarterline.store.repositories.companies import CompaniesRepo
from quarterline.store.repositories.facts import FactsRepo

#: Fixed card footer: the India data layer carries indicators, never a score.
NO_SCORE_NOTE = (
    "No aggregate score exists for India issuers (no scores, no recommendations); "
    "downstream panels render these indicators and their typed missing statuses "
    "as-is."
)


class IndiaIssuerIdentity(BaseModel):
    """Verified registry identity of one issuer."""

    issuer_id: str
    name: str | None = None
    isin: str | None = None
    ticker_nse: str | None = None
    bse_code: str | None = None
    verification_status: str | None = None
    verified_source: str | None = None


class IndiaPeriodIdentity(BaseModel):
    """One period identity at the card's period end (quarter and annual
    identities coexist at 31 March without collision)."""

    period_start: date | None
    period_end: date
    period_kind: str
    application_label: str  # Quarterline's own label, from dates only
    source_label: str | None  # the issuer/instance's own labels, carried
    filing_identifier: str | None
    published_at: date | None
    audited_status: str | None
    revision_status: str | None


class IndiaFactProvenance(BaseModel):
    """Provenance of one canonical fact back to its observations."""

    observation_ids: list[int] = Field(default_factory=list)
    artifact_id: int | None = None
    accession: str | None = None
    filing_identifier: str | None = None
    source_url: str | None = None
    published_at: date | None = None
    revision_status: str | None = None
    audited_status: str | None = None
    original_tag: str | None = None  # the tag that won selection
    review_status: str | None = None  # IND-3 packet status


class IndiaCanonicalFactEntry(BaseModel):
    """One canonical fact with its provenance."""

    concept: str
    value: Decimal | None = None
    unit: str | None = None
    display: str | None = None
    period_start: date | None
    period_end: date
    period_kind: str
    fiscal_year: int | None = None
    fiscal_quarter: str | None = None
    reporting_scope: str | None = None
    data_quality_status: str | None = None
    normalization_version: str = NORMALIZATION_VERSION
    selection_policy: str = SELECTION_POLICY
    provenance: IndiaFactProvenance = Field(default_factory=IndiaFactProvenance)


class IndiaCoverageCell(BaseModel):
    """Per-concept coverage: present / missing + the exact missing status."""

    concept: str
    period_start: date | None
    period_end: date
    period_kind: str
    status: str  # "present" | "missing"
    value: Decimal | None = None
    missing_status: str | None = None
    note: str | None = None


class IndiaDerivationStatusEntry(BaseModel):
    """One blocked cash-flow derivation (never a fabricated number)."""

    concept: str
    target_label: str
    target_start: date
    target_end: date
    method: str
    status: str
    note: str


class IndiaFactCard(BaseModel):
    """The IND-4 canonical fact card for one issuer/scope/period-end."""

    issuer: IndiaIssuerIdentity
    scope: str
    generated_at: datetime
    period_identities: list[IndiaPeriodIdentity] = Field(default_factory=list)
    canonical_facts: list[IndiaCanonicalFactEntry] = Field(default_factory=list)
    metrics: list[IndiaMetricResult] = Field(default_factory=list)
    coverage: list[IndiaCoverageCell] = Field(default_factory=list)
    derivations: list[IndiaDerivationStatusEntry] = Field(default_factory=list)
    no_score_note: str = NO_SCORE_NOTE


class IndiaCoverageIdentity(BaseModel):
    """Coverage block for one period identity (all concepts)."""

    period_start: date | None
    period_end: date
    period_kind: str
    application_label: str
    cells: list[IndiaCoverageCell] = Field(default_factory=list)


class IndiaCoverageReport(BaseModel):
    """Coverage across every ingested period of one issuer/scope."""

    issuer_id: str
    scope: str
    generated_at: datetime
    identities: list[IndiaCoverageIdentity] = Field(default_factory=list)
    derivations: list[IndiaDerivationStatusEntry] = Field(default_factory=list)


def _observation_metadata(observation: FactObservation) -> dict:
    try:
        return json.loads(observation.context_metadata_json or "{}")
    except json.JSONDecodeError:  # pragma: no cover - defensive
        return {}


def _filing_identifier(observation: FactObservation, metadata: dict) -> str | None:
    if metadata.get("seq_id"):
        return f"NSE seq {metadata['seq_id']}"
    if observation.form == "PDF" or metadata.get("extraction_method") == "pdf_text":
        return f"company-IR PDF ({metadata.get('source_document') or 'cached document'})"
    return observation.accession or None


def _source_label(metadata: dict) -> str | None:
    labels = metadata.get("source_period_labels") or {}
    parts = [
        f'{key}="{value}"'
        for key, value in (
            ("ReportingQuarter", labels.get("reporting_quarter")),
            ("TypeOfReportingPeriod", labels.get("type_of_reporting_period")),
        )
        if value
    ]
    return "; ".join(parts) if parts else None


def _display(value: Decimal | None, concept: str, unit: str | None) -> str | None:
    if value is None:
        return None
    if concept in PER_SHARE_CONCEPTS or unit == "INR/share":
        return f"₹{value}/share"
    return format_crores(value)


def _provenance_for(
    session, fact: NormalizedFact, issuer_id: str
) -> tuple[IndiaFactProvenance, dict]:
    """(provenance, winner-metadata) for one canonical fact."""
    facts_repo = FactsRepo(session)
    pairs = facts_repo.lineage_for_fact(fact.id)
    observations = [obs for obs, _lineage in pairs]
    winner = max(
        observations,
        key=lambda obs: (obs.filed_at or date.min, obs.id),
        default=None,
    )
    if winner is None:
        return IndiaFactProvenance(), {}
    metadata = _observation_metadata(winner)
    carried_review = metadata.get("review_status")
    document_id = ""
    if winner.source_artifact_id is not None:
        from quarterline.store.models import SourceArtifact

        artifact = session.get(SourceArtifact, winner.source_artifact_id)
        if artifact is not None and artifact.local_path:
            document_id = artifact.local_path.replace("\\", "/").rsplit("/", 1)[-1]
    packet_review = (
        review_status_for(issuer_id, document_id, winner.period_kind, winner.original_tag)
        if document_id
        else None
    )
    review = carried_review or packet_review
    provenance = IndiaFactProvenance(
        observation_ids=[obs.id for obs in observations],
        artifact_id=winner.source_artifact_id,
        accession=winner.accession,
        filing_identifier=_filing_identifier(winner, metadata),
        source_url=metadata.get("source_url"),
        published_at=winner.filed_at,
        revision_status=metadata.get("revision_status"),
        audited_status=metadata.get("audited_status"),
        original_tag=winner.original_tag,
        review_status=review,
    )
    return provenance, metadata


def _identity_rows(
    session, company_id: int, scope: str, identity: tuple
) -> dict[str, NormalizedFact]:
    """Canonical facts of one period identity, {concept: fact}."""
    start, fact_end, kind, _scope = identity
    rows = list(
        session.scalars(
            select(NormalizedFact).where(
                NormalizedFact.company_id == company_id,
                NormalizedFact.reporting_scope == scope,
                NormalizedFact.period_end == fact_end,
                NormalizedFact.period_kind == kind,
            )
        )
    )
    return {row.concept: row for row in rows if row.period_start == start}


def _identities(session, company_id: int, scope: str, period_end: date | None) -> list[tuple]:
    stmt = (
        select(NormalizedFact)
        .where(NormalizedFact.company_id == company_id, NormalizedFact.reporting_scope == scope)
        .order_by(NormalizedFact.period_end, NormalizedFact.period_kind)
    )
    if period_end is not None:
        stmt = stmt.where(NormalizedFact.period_end == period_end)
    seen: dict[tuple, NormalizedFact] = {}
    for row in session.scalars(stmt):
        key = (row.period_start, row.period_end, row.period_kind, row.reporting_scope)
        seen.setdefault(key, row)
    return list(seen.keys())


def _coverage_cell(
    facts_by_identity: dict[tuple, dict[str, NormalizedFact]],
    identity: tuple,
    concept: str,
) -> IndiaCoverageCell:
    start, end, kind, _scope = identity
    fact = facts_by_identity.get(identity, {}).get(concept)
    if fact is not None:
        return IndiaCoverageCell(
            concept=concept,
            period_start=start,
            period_end=end,
            period_kind=kind,
            status="present",
            value=text_to_decimal(fact.value_decimal),
        )
    return IndiaCoverageCell(
        concept=concept,
        period_start=start,
        period_end=end,
        period_kind=kind,
        status="missing",
        missing_status=MissingDataStatus.NOT_PRESENT_IN_INGESTED_SOURCES.value,
        note=(
            f"{concept} for this period identity is not reported in any ingested "
            "source (a statement about our corpus, not about the company); never "
            "rendered as 0"
        ),
    )


def _issuer_identity(issuer_id: str) -> IndiaIssuerIdentity:
    issuer = require_verified(issuer_id)
    return IndiaIssuerIdentity(
        issuer_id=issuer.issuer_id,
        name=issuer.name,
        isin=issuer.isin,
        ticker_nse=issuer.ticker_nse,
        bse_code=issuer.bse_code,
        verification_status=issuer.verification_status,
        verified_source=issuer.verified_source,
    )


def _company(session, issuer_id: str):
    issuer = require_verified(issuer_id)
    company = CompaniesRepo(session).get_by_ticker(issuer.ticker_nse or issuer.issuer_id)
    if company is None:
        raise LookupError(
            f"no company for {issuer.issuer_id} — import a document first "
            "(`quarterline ingest india-document`)"
        )
    if not _identities(session, company.id, "consolidated", None) and not _identities(
        session, company.id, "standalone", None
    ):
        raise LookupError(
            f"no canonical India facts for {issuer.issuer_id} — run "
            "normalize_canonical_facts() first (IND-4 normalization layer)"
        )
    return issuer, company


def build_india_fact_card(
    session,
    issuer_id: str,
    period_end: date | str | None = None,
    scope: str = "consolidated",
) -> IndiaFactCard:
    """Build the canonical fact card for one issuer/scope(/period-end).

    ``period_end=None`` selects the LATEST period end in the scope. When several
    period identities share the end date (Q4 quarter + annual at 31 March) all
    of them appear — facts and metrics carry their exact identities.
    """
    if scope not in ("consolidated", "standalone"):
        raise ValueError(f"scope must be 'consolidated' or 'standalone', got {scope!r}")
    end = date.fromisoformat(period_end) if isinstance(period_end, str) else period_end
    issuer, company = _company(session, issuer_id)
    identities = _identities(session, company.id, scope, end)
    if not identities:
        raise LookupError(
            f"no canonical India facts for {issuer.issuer_id} ({scope}"
            f"{f', period_end={end.isoformat()}' if end else ''}) — run "
            "normalize_canonical_facts() first"
        )
    if end is None:
        # Default card = the LATEST period end in the scope. Identities sharing
        # that end (Q4 quarter + annual at 31 March) all appear.
        end = max(identity[1] for identity in identities)
        identities = _identities(session, company.id, scope, end)

    facts_by_identity: dict[tuple, dict[str, NormalizedFact]] = {}
    for identity in identities:
        facts_by_identity[identity] = _identity_rows(session, company.id, scope, identity)

    period_blocks: list[IndiaPeriodIdentity] = []
    fact_entries: list[IndiaCanonicalFactEntry] = []
    for identity in sorted(identities, key=lambda i: (i[1], i[2])):
        start, fact_end, kind, _ = identity
        rows = facts_by_identity[identity]
        # The period block's provenance comes from a P&L fact when available
        # (deterministic), else the identity's first fact.
        any_fact = rows.get("revenue_from_operations") or next(iter(rows.values()), None)
        provenance = None
        metadata: dict = {}
        if any_fact is not None:
            provenance, metadata = _provenance_for(session, any_fact, issuer.issuer_id)
        period_blocks.append(
            IndiaPeriodIdentity(
                period_start=start,
                period_end=fact_end,
                period_kind=kind,
                application_label=period_label(start, fact_end, kind),
                source_label=_source_label(metadata) if metadata else None,
                filing_identifier=provenance.filing_identifier if provenance else None,
                published_at=provenance.published_at if provenance else None,
                audited_status=provenance.audited_status if provenance else None,
                revision_status=provenance.revision_status if provenance else None,
            )
        )
        for concept in INDIA_CONCEPTS:
            fact = rows.get(concept)
            if fact is None:
                continue
            fact_provenance, _meta = _provenance_for(session, fact, issuer.issuer_id)
            fact_entries.append(
                IndiaCanonicalFactEntry(
                    concept=concept,
                    value=text_to_decimal(fact.value_decimal),
                    unit=fact.unit,
                    display=_display(text_to_decimal(fact.value_decimal), concept, fact.unit),
                    period_start=fact.period_start,
                    period_end=fact.period_end,
                    period_kind=fact.period_kind,
                    fiscal_year=fact.fiscal_year,
                    fiscal_quarter=fact.fiscal_quarter,
                    reporting_scope=fact.reporting_scope,
                    data_quality_status=fact.data_quality_status,
                    provenance=fact_provenance,
                )
            )

    metric_results = build_metric_results(session, issuer.issuer_id, scope=scope, period_end=end)

    coverage = [
        _coverage_cell(facts_by_identity, identity, concept)
        for identity in sorted(identities, key=lambda i: (i[1], i[2]))
        for concept in INDIA_CONCEPTS
    ]
    derivations = [
        IndiaDerivationStatusEntry(
            concept=entry.concept,
            target_label=entry.target_label,
            target_start=entry.target_start,
            target_end=entry.target_end,
            method=entry.method,
            status=entry.status,
            note=entry.note,
        )
        for entry in cash_flow_derivation_statuses(session, company.id, scope=scope)
    ]
    return IndiaFactCard(
        issuer=_issuer_identity(issuer.issuer_id),
        scope=scope,
        generated_at=datetime.now(UTC),
        period_identities=period_blocks,
        canonical_facts=fact_entries,
        metrics=metric_results,
        coverage=coverage,
        derivations=derivations,
    )


def coverage_report(
    session,
    issuer_id: str,
    scope: str = "consolidated",
) -> IndiaCoverageReport:
    """Coverage across ALL ingested periods of one issuer/scope (IND-5 panels)."""
    issuer, company = _company(session, issuer_id)
    identities = _identities(session, company.id, scope, None)
    facts_by_identity: dict[tuple, dict[str, NormalizedFact]] = {}
    for identity in identities:
        facts_by_identity[identity] = _identity_rows(session, company.id, scope, identity)
    blocks: list[IndiaCoverageIdentity] = []
    for identity in sorted(identities, key=lambda i: (i[1], i[2])):
        start, fact_end, kind, _scope = identity
        blocks.append(
            IndiaCoverageIdentity(
                period_start=start,
                period_end=fact_end,
                period_kind=kind,
                application_label=period_label(start, fact_end, kind),
                cells=[
                    _coverage_cell(facts_by_identity, identity, concept)
                    for concept in INDIA_CONCEPTS
                ],
            )
        )
    derivations = [
        IndiaDerivationStatusEntry(
            concept=entry.concept,
            target_label=entry.target_label,
            target_start=entry.target_start,
            target_end=entry.target_end,
            method=entry.method,
            status=entry.status,
            note=entry.note,
        )
        for entry in cash_flow_derivation_statuses(session, company.id, scope=scope)
    ]
    return IndiaCoverageReport(
        issuer_id=issuer.issuer_id,
        scope=scope,
        generated_at=datetime.now(UTC),
        identities=blocks,
        derivations=derivations,
    )


# ---------------------------------------------------------------------------
# Rendering + CLI handler (registry key "verify:india-facts"; the argparse
# subparser is an orchestrator task — cli.py is not owned by this wave).
# ---------------------------------------------------------------------------


def render_india_fact_card(card: IndiaFactCard) -> str:
    """Plain-text rendering of the fact card (CLI output)."""
    issuer = card.issuer
    lines = [
        f"India fact card — {issuer.issuer_id} ({issuer.name}) [{card.scope}]",
        (
            f"  ISIN {issuer.isin} | NSE {issuer.ticker_nse} | BSE {issuer.bse_code} "
            f"| {issuer.verification_status} (verified via {issuer.verified_source})"
        ),
        "  periods:",
    ]
    for period in card.period_identities:
        lines.append(
            f"    {period.application_label} | source: {period.source_label or 'n/a'} "
            f"| {period.filing_identifier or 'n/a'} published {period.published_at or 'n/a'} "
            f"| {period.audited_status or 'audit n/a'} / {period.revision_status or 'revision n/a'}"
        )
    lines.append(f"  canonical facts (selection {SELECTION_POLICY}, {NORMALIZATION_VERSION}):")
    if not card.canonical_facts:
        lines.append("    (none)")
    for fact in card.canonical_facts:
        label = period_label(fact.period_start, fact.period_end, fact.period_kind)
        provenance = fact.provenance
        lines.append(
            f"    {fact.concept:<32} {label:<46} {fact.display or 'n/a':>14} "
            f"[{fact.data_quality_status or 'no review status'}]"
        )
        lines.append(
            f"        provenance: obs {provenance.observation_ids} | tag {provenance.original_tag} "
            f"| {provenance.filing_identifier or 'n/a'} | published {provenance.published_at or 'n/a'} "
            f"| review {provenance.review_status or 'n/a'}"
            + (f" | {provenance.source_url}" if provenance.source_url else "")
        )
    lines.append(f"  metrics (formula version {FORMULA_VERSION_INDIA_METRICS}):")
    for metric in card.metrics:
        persisted = metric.persisted_metric_id
        value = "n/a" if metric.value is None else str(metric.value)
        label = period_label(metric.period_start, metric.period_end, metric.period_kind)
        lines.append(f"    {persisted:<42} {label:<46} {value:>32} [{metric.status}]")
        for note in metric.notes:
            lines.append(f"        note: {note}")
    lines.append("  coverage:")
    for cell in card.coverage:
        label = period_label(cell.period_start, cell.period_end, cell.period_kind)
        if cell.status == "present":
            lines.append(f"    {cell.concept:<32} {label:<46} present")
        else:
            lines.append(f"    {cell.concept:<32} {label:<46} MISSING: {cell.missing_status}")
    if card.derivations:
        lines.append("  cash-flow derivations:")
        for derivation in card.derivations:
            lines.append(
                f"    {derivation.concept:<24} {derivation.target_label} "
                f"({derivation.target_start}..{derivation.target_end}, {derivation.method}) "
                f"-> {derivation.status}"
            )
            lines.append(f"        {derivation.note}")
    lines.append(f"  {card.no_score_note}")
    return "\n".join(lines)


def _handle_verify_india_facts(args: argparse.Namespace) -> int:
    from quarterline.store.db import session_scope

    try:
        with session_scope() as session:
            card = build_india_fact_card(
                session,
                args.issuer,
                period_end=getattr(args, "period_end", None),
                scope=getattr(args, "scope", None) or "consolidated",
            )
        print(render_india_fact_card(card))
        return 0
    except (LookupError, ValueError) as exc:
        print(str(exc))
        return 1


register_subcommand("verify:india-facts", _handle_verify_india_facts)


__all__ = [
    "FORMULA_VERSION_INDIA_METRICS",
    "NO_SCORE_NOTE",
    "IndiaCanonicalFactEntry",
    "IndiaCoverageCell",
    "IndiaCoverageIdentity",
    "IndiaCoverageReport",
    "IndiaDerivationStatusEntry",
    "IndiaFactCard",
    "IndiaFactProvenance",
    "IndiaIssuerIdentity",
    "IndiaPeriodIdentity",
    "build_india_fact_card",
    "coverage_report",
    "render_india_fact_card",
]
