"""India fact-observation pipeline (IND-2/IND-3): cached XBRL artifacts -> fact_observations.

``ingest_observations(issuer_id)`` runs, per issuer: every registered India XBRL
artifact (``source="india"``, matched via its ``.filing.json`` sidecar) is parsed
deterministically, undimensioned numeric facts are mapped through
``data/tagmap_india.yml``, and each mapped fact becomes one ``fact_observations``
row written through the US-wave idempotent insert-or-skip
(``FactsRepo.insert_observation_skip_duplicate``):

- ``taxonomy="in-capmkt"``, ``original_tag`` = raw local tag;
- ``canonical_concept`` from the India map (US concept ids never appear);
- ``value_decimal`` = exact full rupees from the instance (Decimal text);
- ``unit`` = ``INR`` (money) / ``INR/share`` (per-share concepts — EPS never
  inherits a crore/lakh multiplier). Since IND-3 the unit is VERIFIED against
  the concept class, not assumed: a money concept in a non-INR unit (or a
  per-share concept in a non per-share unit) is skipped and counted as
  ``skipped_unknown_unit`` — an unknown unit semantic is review, never a guess;
- rounding trait, scope, revision and audit status live in
  ``context_metadata_json``; ``source_fy``/``source_fp`` from the filing
  metadata and the date-derived fiscal calendar;
- ``observation_hash`` mirrors the US identity (issuer ISIN, taxonomy, tag,
  unit, period, value) with the reporting scope in the frame slot, so re-runs
  are idempotent (0 new rows) and genuinely revised values hash differently and
  are preserved alongside the original (SPEC 2.1.10).

Cash-flow frequency policy (IND-3, reviewer correction A — supersedes the IND-2
"annual instances only" restriction, which was too strict): cash-flow concepts
are accepted from ANY identified official document at its ACTUAL reported
duration (quarter, half-year, 9M YTD, annual, other known duration — see
``cash_flow.reporting_duration``). What is forbidden is FABRICATION: no dividing
annual figures by four, no half/2, no H2-as-Q4 relabeling, no annualizing from
incomplete coverage. Derived interval observations exist only through the
checked ``cash_flow.derive_by_subtraction`` (annual − H1 = H2; 9M − H1 = Q3).
When cash flow is absent for an issuer/period it is recorded as a distinct
missing-data status (``data_status.MissingDataStatus``), never zero — e.g.
"quarterly CF not present in the ingested sources" for HUL Q1 FY27.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from quarterline.core.normalization import observation_hash
from quarterline.sources.india.concept_map import IN_CAPMKT_PREFIX, PER_SHARE_CONCEPTS, map_tag
from quarterline.sources.india.ir_documents import (
    INDIA_XBRL_PARSER_VERSION,
    SOURCE_INDIA,
    read_filing_sidecar,
)
from quarterline.sources.india.issuers import require_verified
from quarterline.sources.india.periods import fiscal_year, source_fp
from quarterline.sources.india.revisions import FilingMeta
from quarterline.sources.india.xbrl_parse import IndiaFact, IndiaInstance, parse_instance
from quarterline.store.db import session_scope
from quarterline.store.models import FactObservation, SourceArtifact, decimal_to_text
from quarterline.store.repositories.facts import FactsRepo

#: fact_observations.form marker (column is String(16)).
FORM_INTEGRATED_FILING = "IFIndAs"


@dataclass
class IndiaIngestReport:
    """Summary of one ``ingest_observations`` run."""

    issuer_id: str
    artifacts_parsed: int = 0
    observations_inserted: int = 0
    observations_skipped: int = 0
    skipped_dimensioned: int = 0
    skipped_non_numeric: int = 0
    skipped_unknown_unit: int = 0
    unmapped_tags: dict[str, int] = field(default_factory=dict)
    by_concept: dict[str, int] = field(default_factory=dict)

    def note_unmapped(self, tag: str) -> None:
        self.unmapped_tags[tag] = self.unmapped_tags.get(tag, 0 + 1)


#: Declared XBRL unit semantics per concept class. Anything else is review,
#: never guessed into INR (IND-3 units rule). The parser maps the instances'
#: divided unit ``INRPerShare`` (iso4217:INR / xbrli:shares) to the logical
#: name ``INR/shares``; the STORED normalized unit keeps the existing
#: ``INR/share`` contract.
_MONEY_UNIT = "INR"
_PER_SHARE_DECLARED = "INR/shares"
_PER_SHARE_UNIT = "INR/share"


def _unit_for(fact: IndiaFact, concept: str) -> str | None:
    """Verified unit for a mapped fact, or ``None`` when the unit is not a
    known semantic for the concept class (unknown -> review, never a guess)."""
    if concept in PER_SHARE_CONCEPTS:
        return _PER_SHARE_UNIT if fact.unit_ref == _PER_SHARE_DECLARED else None
    return _MONEY_UNIT if fact.unit_ref == _MONEY_UNIT else None


def _india_artifacts(
    session, issuer_id: str, isin: str
) -> list[tuple[SourceArtifact, FilingMeta | None]]:
    """India XBRL artifacts belonging to this issuer (sidecar match, ISIN fallback)."""
    rows = list(
        session.scalars(
            select(SourceArtifact)
            .where(SourceArtifact.source == SOURCE_INDIA)
            .where(SourceArtifact.content_type == "xbrl")
            .where(SourceArtifact.parser_version == INDIA_XBRL_PARSER_VERSION)
            .order_by(SourceArtifact.id)
        )
    )
    matched: list[tuple[SourceArtifact, FilingMeta | None]] = []
    for artifact in rows:
        if not artifact.local_path:
            continue
        meta = read_filing_sidecar(Path(artifact.local_path))
        if meta is not None:
            if meta.issuer_id == issuer_id:
                matched.append((artifact, meta))
            continue
        # No sidecar (fixture-sized import without metadata): fall back to the
        # instance's own ISIN qualifier — identity from the document, not a guess.
        instance = parse_instance(Path(artifact.local_path).read_bytes())
        if instance.isin == isin:
            matched.append((artifact, None))
    return matched


def _observation_for(
    issuer_isin: str,
    fact: IndiaFact,
    concept: str,
    meta: FilingMeta | None,
    instance: IndiaInstance,
) -> FactObservation | None:
    """Build one FactObservation row (or None for non-mappable instants)."""
    period_end_text = fact.context.instant or fact.context.period_end
    if period_end_text is None:
        return None
    period_end = date.fromisoformat(period_end_text)
    period_start = (
        date.fromisoformat(fact.context.period_start) if fact.context.period_start else None
    )
    kind = fact.context.period_kind
    if kind == "instant":
        return None  # mapped concepts are flows/per-share; instants are not ingested

    value = fact.value_decimal
    if value is None:
        return None

    scope = (meta.scope if meta else None) or instance.declared_scope or "consolidated"
    revision_status = meta.revision_status if meta else None
    audited = instance.audited_status or (meta.audited_status if meta else None)
    unit = _unit_for(fact, concept)
    if unit is None:  # caller counts this as skipped_unknown_unit
        return None

    context_metadata = {
        "namespace": "in-capmkt",
        "rounding_trait": instance.rounding_trait,
        "declared_scope": instance.declared_scope,
        "reporting_scope": scope,
        "revision_status": revision_status,
        "audited_status": audited,
        # Source's OWN period labels (IND-3: source labels vs application labels
        # — carried as metadata, never trusted as classification; the
        # application label derives from the exact dates only).
        "source_period_labels": {
            "reporting_quarter": instance.qualifier("ReportingQuarter"),
            "type_of_reporting_period": instance.qualifier("TypeOfReportingPeriod"),
        },
        "audit_qualification_declaration": instance.audit_qualification_declaration,
        "taxonomy_version": instance.taxonomy_version,
        "unit_ref": fact.unit_ref,
        "decimals": fact.decimals,
        "exchange": meta.exchange if meta else None,
        "seq_id": meta.seq_id if meta else None,
        "doc_type": meta.doc_type if meta else None,
        "source_url": meta.source_url if meta else None,
    }
    digest = observation_hash(
        issuer_isin,
        "in-capmkt",
        fact.tag,
        unit,
        period_start,
        period_end,
        value,
        scope,  # frame slot: scope makes consolidated/standalone distinct identities
    )
    return FactObservation(
        company_id=-1,  # replaced by caller with the resolved company id
        source_artifact_id=None,  # set by caller
        accession=(meta.seq_id if meta else None),
        form=FORM_INTEGRATED_FILING,
        filed_at=(meta.published_at if meta else None),
        taxonomy="in-capmkt",
        original_tag=fact.tag,
        canonical_concept=concept,
        value_decimal=decimal_to_text(value),
        unit=unit,
        currency="INR",
        period_start=period_start,
        period_end=period_end,
        period_kind=kind,
        source_fy=fiscal_year(period_end),
        source_fp=source_fp(period_start, period_end, kind),
        reporting_scope=scope,
        context_metadata_json=json.dumps(context_metadata, sort_keys=True),
        observation_hash=digest,
    )


def ingest_observations(issuer_id: str, database_url: str | None = None) -> IndiaIngestReport:
    """Ingest every cached India XBRL artifact for one issuer. Idempotent."""
    issuer = require_verified(issuer_id)
    report = IndiaIngestReport(issuer_id=issuer.issuer_id)
    with session_scope(database_url) as session:
        companies_repo = _companies_repo(session)
        company = companies_repo.get_by_ticker(issuer.ticker_nse or issuer.issuer_id)
        if company is None:
            raise LookupError(
                f"no company for {issuer.issuer_id} — import a document first "
                "(`quarterline ingest india-document`)"
            )
        facts_repo = FactsRepo(session)
        for artifact, meta in _india_artifacts(session, issuer.issuer_id, issuer.isin):
            assert artifact.local_path is not None
            instance = parse_instance(Path(artifact.local_path).read_bytes(), filing_meta=meta)
            report.artifacts_parsed += 1
            for fact in instance.facts:
                if not fact.is_numeric:
                    report.skipped_non_numeric += 1
                    continue
                if fact.is_dimensioned:
                    report.skipped_dimensioned += 1
                    continue
                result = map_tag(fact.tag)
                if not result.mapped:
                    report.note_unmapped(fact.tag)
                    continue
                if _unit_for(fact, result.concept or "") is None:
                    # Unknown unit semantics for this concept class: surface as
                    # a counted skip (review), never silently ingest as INR.
                    report.skipped_unknown_unit += 1
                    continue
                observation = _observation_for(
                    issuer.isin, fact, result.concept or "", meta, instance
                )
                if observation is None:
                    continue
                observation.company_id = company.id
                observation.source_artifact_id = artifact.id
                _, inserted = facts_repo.insert_observation_skip_duplicate(observation)
                if inserted:
                    report.observations_inserted += 1
                    report.by_concept[observation.canonical_concept] = (
                        report.by_concept.get(observation.canonical_concept, 0) + 1
                    )
                else:
                    report.observations_skipped += 1
    return report


def _companies_repo(session):  # narrow import helper kept local for testability
    from quarterline.store.repositories.companies import CompaniesRepo

    return CompaniesRepo(session)


# ---------------------------------------------------------------------------
# Reported PDF cash flow (IND-3 correction A): INFY's quarterly CFO from the
# IR condensed-FS PDF, carried as an observation with full provenance.
# ---------------------------------------------------------------------------

#: form marker for PDF-sourced observations (column is String(16)).
FORM_PDF = "PDF"

#: The reported quarterly CFO row verified in IND-3's reconciliation
#: (reconcile.REVIEWED_PDF_CASH_FLOW[0]): consol-fy27-q1-finstatement.pdf p.6,
#: "Net cash generated by operating activities 9,330" (₹ crore), period
#: 2026-04-01..2026-06-30, review status agent_checked_against_document.
_PDF_CFO_TAG = "NetCashGeneratedByOperatingActivitiesRenderedLine"


def ingest_reviewed_pdf_cash_flow(
    issuer_id: str, database_url: str | None = None
) -> IndiaIngestReport:
    """Carry the agent-verified REPORTED quarterly CFO (IR PDF) as an observation.

    IND-3 correction A admits reported cash flow at its actual frequency when
    full provenance is preserved. Only the reconciliation's agent-checked row is
    ingested (extraction method ``pdf_text``, page reference, displayed value,
    review status); when the cached PDF is present the recorded value is
    re-extracted live and any drift REFUSES the ingest (never a stale constant).

    Idempotent via the observation hash (a distinct tag keeps it separate from
    the XBRL annual observation of the same concept).
    """
    from quarterline.sources.india.reconcile import (
        REF_INFY_CONSO_Q1,
        RENDERED_DOCUMENT_STORAGE,
        REVIEW_AGENT_CHECKED,
        REVIEWED_PDF_CASH_FLOW,
    )

    reviewed = REVIEWED_PDF_CASH_FLOW[0]
    reference_document_id = reviewed["reference_document_id"]
    if reference_document_id != REF_INFY_CONSO_Q1:  # pragma: no cover - guard
        raise ValueError("reviewed PDF cash-flow constant drifted; refusing to ingest")
    issuer = require_verified(issuer_id)
    report = IndiaIngestReport(issuer_id=issuer.issuer_id)

    storage_relative = RENDERED_DOCUMENT_STORAGE[reference_document_id]
    cached_pdf = (
        Path(__file__).resolve().parents[4] / "storage" / "raw" / "india" / storage_relative
    )
    sha256: str | None = None
    if cached_pdf.is_file():
        # Live drift guard: the cached document must still display the recorded
        # value on the recorded page, or nothing is ingested.
        import hashlib

        content = cached_pdf.read_bytes()
        sha256 = hashlib.sha256(content).hexdigest()
        from quarterline.sources.india.pdf_results import extract_cash_flow_from_pdf

        extractions = extract_cash_flow_from_pdf(content, reference_document_id)
        page = int(reviewed["page"].split("p.")[1].split(" ")[0])
        live = next(
            (e for e in extractions if e.page_number == page and e.ok),
            None,
        )
        if live is None or live.value != Decimal(reviewed["value"]):
            raise ValueError(
                "reviewed PDF cash-flow constant no longer matches the cached "
                f"document ({reference_document_id} p.{page}); refusing to ingest "
                "a stale value — re-run the IND-3 reconciliation"
            )
    # When the cache is absent the recorded constant stands (it is
    # agent-checked against the document; provenance below records that).

    with session_scope(database_url) as session:
        companies_repo = _companies_repo(session)
        company = companies_repo.get_by_ticker(issuer.ticker_nse or issuer.issuer_id)
        if company is None:
            raise LookupError(
                f"no company for {issuer.issuer_id} — import a document first "
                "(`quarterline ingest india-document`)"
            )
        facts_repo = FactsRepo(session)
        period_start = date.fromisoformat(reviewed["period_start"])
        period_end = date.fromisoformat(reviewed["period_end"])
        value = Decimal(reviewed["value"])
        metadata = {
            "namespace": "in-capmkt",
            "extraction_method": "pdf_text",
            "reference_document_id": reference_document_id,
            "source_document": reviewed["document"],
            "source_document_sha256": sha256,
            "page": reviewed["page"],
            "display_value": reviewed["display"],
            "declared_scale": "crore (page header; presentation only)",
            "period_source": "document_title",
            "review_status": REVIEW_AGENT_CHECKED,
            "reporting_scope": "consolidated",
            "revision_status": None,
            "audited_status": None,
            "note": reviewed["note"],
        }
        digest = observation_hash(
            issuer.isin,
            "in-capmkt",
            _PDF_CFO_TAG,
            "INR",
            period_start,
            period_end,
            value,
            "consolidated",
        )
        observation = FactObservation(
            company_id=company.id,
            source_artifact_id=None,
            accession=None,
            form=FORM_PDF,
            filed_at=date.fromisoformat("2026-07-23"),  # document date (with NSE seq 177385)
            taxonomy="in-capmkt",
            original_tag=_PDF_CFO_TAG,
            canonical_concept="cash_flow_operations",
            value_decimal=decimal_to_text(value),
            unit="INR",
            currency="INR",
            period_start=period_start,
            period_end=period_end,
            period_kind="quarter",
            source_fy=fiscal_year(period_end),
            source_fp=source_fp(period_start, period_end, "quarter"),
            reporting_scope="consolidated",
            context_metadata_json=json.dumps(metadata, sort_keys=True),
            observation_hash=digest,
        )
        _, inserted = facts_repo.insert_observation_skip_duplicate(observation)
        if inserted:
            report.observations_inserted += 1
            report.by_concept["cash_flow_operations"] = 1
        else:
            report.observations_skipped += 1
    return report


__all__ = [
    "FORM_INTEGRATED_FILING",
    "FORM_PDF",
    "IN_CAPMKT_PREFIX",
    "IndiaIngestReport",
    "ingest_observations",
    "ingest_reviewed_pdf_cash_flow",
]
