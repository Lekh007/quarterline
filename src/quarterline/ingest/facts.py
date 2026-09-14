"""Facts ingestion pipeline (SPEC 10, Milestones 1-2).

``ingest_facts`` runs, per watchlist ticker: SecClient companyfacts fetch
(cache-first, identity-gated) -> observation drafts -> observation insert-or-skip
(by ``observation_hash``) -> normalized-fact upsert (period identity + scope
key) -> lineage rows -> derived-metric upsert. The whole pipeline is idempotent:
re-running inserts no duplicate observations and leaves values unchanged.

CLI handlers register into ``quarterline.cli.SUBCOMMAND_REGISTRY`` at import
time (``ingest facts``, ``verify facts``). NOTE: ``quarterline.cli.main``
imports nothing from this package, so the registering import must be wired by
the entry point (e.g. ``import quarterline.ingest.facts`` in
``quarterline/ingest/__init__.py``) - reported to the orchestrator as the CLI
wiring gap.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from quarterline.cli import register_subcommand
from quarterline.config import get_settings
from quarterline.core.normalization import (
    NORMALIZATION_VERSION,
    SELECTION_LATEST,
    ObservationRecord,
    SelectedFact,
    build_observation_drafts,
    derive_missing_quarters,
    infer_fiscal_year_end_month,
    record_observation_hash,
    select_facts,
)
from quarterline.core.provenance import (
    PERSISTED_METRIC_IDS,
    build_fact_cards,
)
from quarterline.core.quality import CORE_CONCEPTS
from quarterline.sources.sec.companyfacts import companyfacts_url
from quarterline.store.db import session_scope
from quarterline.store.models import (
    FactObservation,
    NormalizedFact,
    SourceArtifact,
    decimal_to_text,
)
from quarterline.store.repositories.companies import CompaniesRepo
from quarterline.store.repositories.facts import (
    ROLE_ANNUAL_TOTAL,
    ROLE_DIRECT_SOURCE,
    ROLE_PRIOR_YTD,
    FactsRepo,
)

logger = logging.getLogger(__name__)

#: Default watchlist path (relative to the repository root).
DEFAULT_WATCHLIST = "data/watchlist_us.csv"


@dataclass
class IngestReport:
    """Summary of one pipeline run (per-company counts + logs)."""

    tickers: list[str] = field(default_factory=list)
    observations_inserted: int = 0
    observations_skipped: int = 0
    facts_upserted: int = 0
    facts_created: int = 0
    lineage_rows: int = 0
    derived_metrics_upserted: int = 0
    missing_concepts: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            f"tickers: {', '.join(self.tickers) or '(none)'}",
            f"observations: {self.observations_inserted} inserted, {self.observations_skipped} skipped",
            f"normalized facts: {self.facts_created} created, {self.facts_upserted - self.facts_created} updated",
            f"lineage rows: {self.lineage_rows}",
            f"derived metrics: {self.derived_metrics_upserted}",
        ]
        for ticker, missing in self.missing_concepts.items():
            if missing:
                lines.append(
                    f"{ticker} missing concepts (logged, never filled): {', '.join(missing)}"
                )
        for ticker, error in self.errors.items():
            lines.append(f"{ticker}: ERROR {error}")
        return "\n".join(lines)


def _record_artifact(session: Session, cik: str, payload: dict, source_url: str) -> SourceArtifact:
    content = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    artifact = SourceArtifact(
        source="sec",
        source_url=source_url,
        fetched_at=datetime.now(UTC),
        content_hash=hashlib.sha256(content).hexdigest(),
        content_type="application/json",
        parser_version=NORMALIZATION_VERSION,
    )
    session.add(artifact)
    session.flush()
    return artifact


def _insert_observations(
    session: Session,
    repo: FactsRepo,
    company_id: int,
    artifact_id: int | None,
    drafts: list[ObservationRecord],
) -> tuple[dict[str, int], int, int]:
    """Insert-or-skip observation drafts; returns (hash->id map, inserted, skipped)."""
    hash_to_id: dict[str, int] = {}
    inserted = skipped = 0
    for draft in drafts:
        digest = record_observation_hash(draft)
        existing = repo.get_observation_by_hash(digest)
        if existing is not None:
            hash_to_id[digest] = existing.id
            skipped += 1
            continue
        row = FactObservation(
            company_id=company_id,
            source_artifact_id=artifact_id,
            accession=draft.accession,
            form=draft.form,
            filed_at=draft.filed_at,
            taxonomy=draft.taxonomy,
            original_tag=draft.original_tag,
            canonical_concept=draft.canonical_concept,
            value_decimal=decimal_to_text(draft.value),
            unit=draft.unit,
            currency=draft.unit if draft.unit in ("USD", "EUR") else None,
            period_start=draft.period_start,
            period_end=draft.period_end,
            period_kind=draft.period_kind.value,
            source_fy=draft.source_fy,
            source_fp=draft.source_fp,
            reporting_scope=draft.reporting_scope,
            context_metadata_json=draft.context_metadata_json,
            observation_hash=digest,
        )
        row, created = repo.insert_observation_skip_duplicate(row)
        hash_to_id[digest] = row.id
        if created:
            inserted += 1
        else:
            skipped += 1
    session.flush()
    return hash_to_id, inserted, skipped


def _observation_id_for(source: ObservationRecord, hash_to_id: dict[str, int]) -> int | None:
    return hash_to_id.get(record_observation_hash(source))


def _upsert_facts(
    session: Session,
    repo: FactsRepo,
    company_id: int,
    facts: list[SelectedFact],
    hash_to_id: dict[str, int],
) -> tuple[int, int, int]:
    """Upsert normalized facts and write lineage; returns (upserts, created, lineage)."""
    now = datetime.now(UTC)
    upserts = created = lineage_rows = 0
    for fact in facts:
        row = NormalizedFact(
            company_id=company_id,
            concept=fact.concept,
            period_start=fact.period_start,
            period_end=fact.period_end,
            fiscal_year=fact.fiscal_year,
            fiscal_quarter=fact.fiscal_quarter,
            period_kind=fact.period_kind.value,
            reporting_scope=fact.reporting_scope,
            value_decimal=decimal_to_text(fact.value),
            unit=fact.unit,
            selection_policy=SELECTION_LATEST,
            normalization_version=NORMALIZATION_VERSION,
            is_derived=fact.is_derived,
            derivation_method=fact.derivation_method,
            available_at=now,
            data_quality_status="ok",
        )
        row, was_created = repo.upsert_fact(row)
        upserts += 1
        if was_created:
            created += 1
        for source in fact.sources:
            observation_id = source.observation.observation_id or _observation_id_for(
                source.observation, hash_to_id
            )
            if observation_id is None:
                logger.warning("lineage source not found for %s; skipping", fact.concept)
                continue
            role = source.role
            if role not in (ROLE_DIRECT_SOURCE, ROLE_ANNUAL_TOTAL, ROLE_PRIOR_YTD):
                role = ROLE_DIRECT_SOURCE
            repo.add_lineage(row.id, observation_id, role)
            lineage_rows += 1
    session.flush()
    return upserts, created, lineage_rows


def _persist_derived_metrics(
    session: Session, repo: FactsRepo, ticker: str, company_id: int
) -> int:
    """Recompute fact cards and persist the numeric derived metrics."""
    cards = build_fact_cards(session, ticker)
    count = 0
    for card in cards:
        for metric in card.metrics:
            if metric.metric_id not in PERSISTED_METRIC_IDS:
                continue  # fact metrics live on normalized_facts; labels stay card-only
            repo.upsert_derived_metric(
                company_id=company_id,
                period_end=card.period_end,
                metric=metric.metric_id,
                value=metric.value,
                unit=metric.unit,
                formula_version=metric.provenance.formula_version,
                status=metric.status.value,
                input_fact_ids=[],
                available_at=datetime.now(UTC),
            )
            count += 1
    session.flush()
    return count


def ingest_facts(
    tickers: list[str] | None = None,
    watchlist: str | Path = DEFAULT_WATCHLIST,
    client=None,
    database_url: str | None = None,
    session: Session | None = None,
) -> IngestReport:
    """Ingest SEC company facts for the given tickers (default: whole watchlist).

    ``client`` may be any object with ``get_json(url) -> dict`` (tests inject a
    fixture-backed fake; production uses the identity-gated, throttled and
    cached ``SecClient``). Fully idempotent on re-run.
    """
    report = IngestReport(tickers=list(tickers) if tickers else [])
    own_session = session is None

    def _run(sess: Session) -> None:
        companies_repo = CompaniesRepo(sess)
        watchlist_companies = companies_repo.upsert_from_watchlist_csv(
            _resolve_watchlist(watchlist)
        )
        wanted = [t.strip().upper() for t in (tickers or [])]
        if wanted:
            # Resolve requested tickers against the store (watchlist members and
            # any previously registered company).
            targets = []
            missing = []
            for ticker in wanted:
                company = companies_repo.get_by_ticker(ticker)
                if company is None:
                    missing.append(ticker)
                else:
                    targets.append(company)
            if missing:
                report.errors["(unknown tickers)"] = ", ".join(sorted(missing))
        else:
            # Default: the watchlist rows only — never every company in the
            # store (the store also holds India issuers, which do not file
            # with the SEC, and any locally registered test rows).
            targets = list(watchlist_companies)
        if not report.tickers:
            report.tickers = [c.ticker for c in targets]
        settings = get_settings()
        active_client = client
        repo = FactsRepo(sess)
        for company in targets:
            try:
                if active_client is None:
                    # Deferred so tests (and offline tooling) never touch the live path.
                    from quarterline.sources.sec.client import SecClient

                    active_client = SecClient(settings)
                url = companyfacts_url(company.cik)
                payload = active_client.get_json(url)
                artifact = _record_artifact(sess, company.cik, payload, url)
                drafts, build_notes = build_observation_drafts(
                    company.id, company.cik, payload, _load_tagmap()
                )
                fye_month = infer_fiscal_year_end_month(drafts)
                company.fiscal_year_end = f"{fye_month:02d}"  # inferred FYE month
                result = select_facts(drafts, fye_month, policy=SELECTION_LATEST)
                direct = [f for f in result.facts if not f.is_derived]
                derived, derive_notes = derive_missing_quarters(direct, fye_month)
                hash_to_id, inserted, skipped = _insert_observations(
                    sess, repo, company.id, artifact.id, drafts
                )
                report.observations_inserted += inserted
                report.observations_skipped += skipped
                upserts, created, lineage = _upsert_facts(
                    sess, repo, company.id, direct + derived, hash_to_id
                )
                report.facts_upserted += upserts
                report.facts_created += created
                report.lineage_rows += lineage
                report.derived_metrics_upserted += _persist_derived_metrics(
                    sess, repo, company.ticker, company.id
                )
                report.missing_concepts[company.ticker] = [note for note in result.missing_concepts]
                for concept in CORE_CONCEPTS:
                    if not any(f.concept == concept for f in direct + derived):
                        report.missing_concepts.setdefault(company.ticker, [])
                        if concept not in report.missing_concepts[company.ticker]:
                            report.missing_concepts[company.ticker].append(concept)
                report.notes.extend(
                    f"{company.ticker}: {note}" for note in build_notes + derive_notes
                )
            except Exception as exc:  # per-company isolation; other tickers continue
                report.errors[company.ticker] = f"{type(exc).__name__}: {exc}"
                logger.exception("ingestion failed for %s", company.ticker)

    if own_session:
        with session_scope(database_url) as sess:
            _run(sess)
    else:
        _run(session)
    return report


def _resolve_watchlist(watchlist: str | Path) -> Path:
    path = Path(watchlist)
    if path.is_file():
        return path
    # Fall back to the repository root (src/quarterline/ingest/facts.py -> root).
    candidate = Path(__file__).resolve().parents[3] / watchlist
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(f"watchlist CSV not found: {watchlist}")


def _load_tagmap() -> dict[str, list[str]]:
    from quarterline.sources.sec.tagmap import load_tagmap

    return load_tagmap()


# ---------------------------------------------------------------------------
# CLI (registered into quarterline.cli.SUBCOMMAND_REGISTRY at import time)
# ---------------------------------------------------------------------------


def _handle_ingest_facts(args: argparse.Namespace) -> int:
    report = ingest_facts(tickers=args.tickers, watchlist=args.watchlist)
    print(report.summary())
    return 0 if not report.errors else 1


def verify_facts(
    ticker: str, database_url: str | None = None, session: Session | None = None
) -> str:
    """Build the per-concept latest-8-quarter verification table as text.

    Each row shows the normalized value, direct/derived flag and the
    accession/form/filed_at of the contributing source filing(s) (SPEC 26
    provenance reconciliation).
    """
    lines: list[str] = []

    def _run(sess: Session) -> None:
        companies_repo = CompaniesRepo(sess)
        company = companies_repo.get_by_ticker(ticker)
        if company is None:
            raise LookupError(f"unknown ticker {ticker!r}")
        repo = FactsRepo(sess)
        quarters = repo.quarters_available(company.id)[-8:]
        header = (
            f"verify facts: {company.ticker} (CIK {company.cik}, FYE {company.fiscal_year_end}) "
            f"- latest {len(quarters)} fiscal quarters"
        )
        lines.append(header)
        columns = " | ".join(
            f"{q.fiscal_year} {q.fiscal_quarter} ({q.period_end})" for q in quarters
        )
        lines.append(f"concept            | {columns}")
        for concept in CORE_CONCEPTS:
            cells: list[str] = []
            for q in quarters:
                fact = repo.find_fact(
                    company.id,
                    concept,
                    q.period_start,
                    q.period_end,
                    "quarter",
                    "consolidated",
                )
                if fact is None:
                    instant = repo.find_fact(
                        company.id,
                        concept,
                        None,
                        q.period_end,
                        "instant",
                        "consolidated",
                    )
                    fact = instant
                if fact is None:
                    cells.append("missing")
                    continue
                provenance_bits: list[str] = []
                for obs, lin in repo.lineage_for_fact(fact.id):
                    flag = "D" if fact.is_derived else ""
                    provenance_bits.append(
                        f"{lin.role[0]}{flag}:{obs.form}@{obs.filed_at}:{obs.accession}"
                    )
                cells.append(f"{fact.value_decimal} [{'; '.join(provenance_bits) or 'no lineage'}]")
            lines.append(f"{concept:<18} | " + " | ".join(cells))
        lines.append(
            "legend: role prefix d=direct_source/a=annual_total/p=prior_ytd; "
            "D=derived fact; values are canonical decimal strings"
        )

    if session is not None:
        _run(session)
    else:
        with session_scope(database_url) as sess:
            _run(sess)
    return "\n".join(lines)


def _handle_verify_facts(args: argparse.Namespace) -> int:
    try:
        print(verify_facts(args.ticker))
    except LookupError as exc:
        print(str(exc))
        return 1
    return 0


register_subcommand("ingest:facts", _handle_ingest_facts)
register_subcommand("verify:facts", _handle_verify_facts)
