"""India company pages and indicator panels (IND-5) — indicators, never a score.

Routes (all deterministic over the IND-4 fact-card layer; no LLM import or
network call anywhere — SPEC §25 graceful degradation):

- ``GET /in`` — the India watchlist (``data/watchlist_india.csv`` via
  ``sources.india.issuers``) with per-issuer verification status. Only
  verified issuers link onward; the eight ``proposed`` rows say so explicitly
  and never link to pages that would pretend facts exist.
- ``GET /in/{issuer_id}`` — the indicator page for one verified issuer:
  period-identity strip (application label AND source label; dates primary),
  quarterly/annual reported-fact panel, metrics panel (formula captions, YoY
  missing statuses shown as text), cash-flow panel at reported frequencies
  only, coverage panel with typed missing-data statuses, and the fixed
  no-score note. ``?scope=standalone`` currently renders an explicit
  "no standalone facts ingested" state — never consolidated data silently.
- ``GET /in/{issuer_id}/coverage`` — JSON ``coverage_report`` (IND-7).
- ``GET /in/review-packet`` — the review packet document the amber
  ``requires_manual_review`` badges link to (plain text; repo doc, not
  filing content, served verbatim).

IND-8 generation routes (lazy LLM imports; degraded modes per SPEC §25):

- ``POST /in/{issuer_id}/brief`` — grounded India brief (validated prose,
  page-level citations, India metric-reference allowlist, honest
  insufficient-evidence, advice refusal; no scores, no recommendations).
- ``POST /api/india-memos`` / ``GET /api/india-memos/{run_id}`` /
  ``POST /api/india-memos/{run_id}/approve-export`` — the US memo workflow
  with the India tool bindings (identical budgets and gates).

Honest display: missing is never zero (typed statuses render as text),
review-pending facts are flagged and link to the review packet, every money
value carries ₹-crore display plus the exact full-rupee amount, both PAT
variants are always co-labeled, and all filing-derived content goes through
Jinja autoescape (no ``|safe`` anywhere).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from quarterline.api.routers.companies import _parse_period_end, render_error
from quarterline.api.routers.memo import _parse_body  # same JSON/urlencoded body parser
from quarterline.sources.india.concept_map import INDIA_CONCEPTS, PER_SHARE_CONCEPTS
from quarterline.sources.india.factcard import (
    FORMULA_VERSION_INDIA_METRICS,
    NO_SCORE_NOTE,
    IndiaCanonicalFactEntry,
    IndiaCoverageCell,
    IndiaFactCard,
    IndiaFactProvenance,
    build_india_fact_card,
    coverage_report,
)
from quarterline.sources.india.issuers import (
    STATUS_VERIFIED,
    Issuer,
    get_issuer,
    load_issuers,
)
from quarterline.sources.india.metrics import IndiaMetricResult
from quarterline.sources.india.normalization import (
    DQ_AGENT_CHECKED,
    DQ_REQUIRES_MANUAL_REVIEW,
)
from quarterline.sources.india.periods import KIND_QUARTER
from quarterline.sources.india.units import format_crores
from quarterline.store.db import session_scope
from quarterline.store.models import FactObservation

API_DIR = Path(__file__).resolve().parents[1]

router = APIRouter()
templates = Jinja2Templates(directory=str(API_DIR / "templates"))

# ---------------------------------------------------------------------------
# Display constants
# ---------------------------------------------------------------------------

#: India-specific disclaimer (complements the base-template research-only line).
INDIA_DISCLAIMER = (
    "Quarter labels/scores are not provided for India issuers in this release; indicators only."
)

RESEARCH_DISCLAIMER = "Research and education only. Not investment advice."

#: Landing-page coverage caveat.
COVERAGE_CAVEAT = (
    "Coverage caveat: an issuer page exists only where canonical facts have been "
    "ingested and normalized, and shows only the periods actually present in the "
    "ingested sources. Missing data carries its exact typed status "
    "(e.g. not_present_in_ingested_sources) — a statement about our corpus, never "
    "a zero and never a guess."
)

#: Proposed-registry rows: explicit status text, never a page link.
PROPOSED_TEXT = "proposed — identifiers not yet verified"

SCOPE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("consolidated", "Consolidated"),
    ("standalone", "Standalone"),
)

REVIEW_PACKET_URL = "/in/review-packet"
REVIEW_PACKET_FILENAME = "india_reconciliation_review.md"
REVIEW_PACKET_SECTION_NOTE = (
    "see the review packet §9 checklist (a human must read the cited document)"
)

#: Presentation note carried in every page header.
CURRENCY_NOTE = (
    "Reporting currency: INR. Money values display in ₹ crore (the exchange "
    "presentation scale) with the exact filed full-rupee amount alongside; "
    "per-share values are ₹ per share and never inherit a crore multiplier."
)

CASH_FLOW_CONCEPTS: tuple[str, ...] = ("cash_flow_operations", "capex")
PANDL_CONCEPTS: tuple[str, ...] = tuple(
    concept for concept in INDIA_CONCEPTS if concept not in CASH_FLOW_CONCEPTS
)

CONCEPT_LABELS: dict[str, str] = {
    "revenue_from_operations": "Revenue from operations",
    "total_income": "Total income (separate line — includes other income)",
    "profit_before_tax": "Profit before tax",
    "profit_after_tax": "Profit after tax (group)",
    "profit_attributable_to_owners": "Profit attributable to owners (parent)",
    "exceptional_items": "Exceptional items (stored profit impact)",
    "eps_basic": "Basic EPS",
    "eps_diluted": "Diluted EPS",
    "cash_flow_operations": "Cash flow from operations (CFO)",
    "capex": "Capital expenditure (capex)",
}

#: Metric rows in fixed display order; captions state the exact inputs.
METRIC_ORDER: tuple[str, ...] = (
    "india_revenue_yoy",
    "india_revenue_qoq",
    "india_eps_growth_yoy",
    "india_pat_margin_owners",
    "india_pat_margin_group",
    "india_exceptional_impact_pbt",
)

METRIC_LABELS: dict[str, str] = {
    "india_revenue_yoy": "Revenue growth YoY",
    "india_revenue_qoq": "Revenue growth QoQ",
    "india_eps_growth_yoy": "Diluted EPS growth YoY",
    "india_pat_margin_owners": "PAT margin — attributable to owners",
    "india_pat_margin_group": "PAT margin — group",
    "india_exceptional_impact_pbt": "Exceptional items impact on PBT",
}

METRIC_CAPTIONS: dict[str, str] = {
    "india_revenue_yoy": (
        "revenue from operations vs the matching prior-year quarter (ingested facts only)"
    ),
    "india_revenue_qoq": (
        "revenue from operations, current vs immediately preceding fiscal quarter"
    ),
    "india_eps_growth_yoy": (
        "diluted EPS vs the matching prior-year quarter (ingested facts only)"
    ),
    "india_pat_margin_owners": "profit attributable to owners / revenue from operations",
    "india_pat_margin_group": "profit after tax (group) / revenue from operations",
    "india_exceptional_impact_pbt": (
        "exceptional items impact on profit before tax "
        "(impact = profit before tax − profit before exceptional items and tax)"
    ),
}

#: Growth metrics are defined per quarter only (never annualized).
QUARTER_ONLY_METRICS = frozenset({"india_revenue_yoy", "india_revenue_qoq", "india_eps_growth_yoy"})

PERCENT = Decimal("0.01")


# ---------------------------------------------------------------------------
# View models (plain display structures; Decimal-exact, never float)
# ---------------------------------------------------------------------------


@dataclass
class PeriodColumnVM:
    """One period identity as a dated column/strip entry."""

    key: str
    label: str  # "Q4 FY2025-26" / "FY2025-26"
    kind_label: str  # quarter | annual
    dates: str
    application_label: str
    source_label: str | None
    filing_identifier: str | None
    published_at: str | None
    audited_status: str | None
    revision_status: str | None


@dataclass
class FactCellVM:
    """One reported fact (or typed-missing cell) in an indicator table."""

    missing: bool = False
    missing_status: str | None = None
    note: str | None = None
    display: str | None = None
    full: str | None = None
    provenance: str = ""
    tag: str | None = None
    source_url: str | None = None
    filing_url: str | None = None
    review_status: str | None = None
    review_kind: str = "none"  # checked | review | none
    extraction: str | None = None


@dataclass
class FactRowVM:
    concept: str
    label: str
    cells: list[FactCellVM] = field(default_factory=list)


@dataclass
class CashFlowRowVM:
    """One cash-flow row at its actually-reported frequency."""

    concept: str
    concept_label: str
    period_label: str
    kind_label: str
    dates: str
    cell: FactCellVM = field(default_factory=FactCellVM)


@dataclass
class MetricCellVM:
    """One metric value (or typed-missing / not-defined cell)."""

    not_defined: bool = False
    status: str = "missing"
    status_kind: str = "missing"  # ok | missing | invalid
    value_text: str | None = None
    full: str | None = None
    explanation: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class MetricRowVM:
    metric_id: str
    label: str
    formula: str
    cells: list[MetricCellVM] = field(default_factory=list)


@dataclass
class CoverageCellVM:
    concept: str
    status: str
    missing_status: str | None = None
    note: str | None = None
    value_display: str | None = None


@dataclass
class CoverageBlockVM:
    label: str
    kind_label: str
    dates: str
    cells: list[CoverageCellVM] = field(default_factory=list)


@dataclass
class DerivationVM:
    concept: str
    target_label: str
    method: str
    status: str
    note: str


@dataclass
class IssuerListRowVM:
    """One landing-page row: verified rows link onward, proposed never do."""

    issuer_id: str
    name: str | None
    sector: str | None
    ids_text: str
    verified: bool
    status_text: str
    page_url: str | None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _full_rupees(value: Decimal) -> str:
    """Exact filed amount: 482110000000 -> "₹ 482,110,000,000"."""
    return f"₹ {value:,.0f}"


def _percent_text(value: Decimal) -> str:
    return f"{(value * 100).quantize(PERCENT, rounding=ROUND_HALF_UP):,.2f}%"


def _review_kind(status: str | None) -> str:
    if status == DQ_AGENT_CHECKED:
        return "checked"
    if status == DQ_REQUIRES_MANUAL_REVIEW:
        return "review"
    return "none"


def _dates_text(start: date | None, end: date) -> str:
    return f"{start.isoformat()}..{end.isoformat()}" if start else f"as at {end.isoformat()}"


def _short_label(application_label: str) -> str:
    label = application_label.split(" (", 1)[0]
    # period_label spells annual/cumulative out in the label itself; the kind
    # is shown separately, so the short label stays just the period id.
    return label.removesuffix(" annual").removesuffix(" cumulative").removesuffix(" cumulative")


def _column_from_identity(identity) -> PeriodColumnVM:
    return PeriodColumnVM(
        key=f"{identity.period_end.isoformat()}|{identity.period_kind}",
        label=_short_label(identity.application_label),
        kind_label=identity.period_kind,
        dates=_dates_text(identity.period_start, identity.period_end),
        application_label=identity.application_label,
        source_label=identity.source_label,
        filing_identifier=identity.filing_identifier,
        published_at=identity.published_at.isoformat() if identity.published_at else None,
        audited_status=identity.audited_status,
        revision_status=identity.revision_status,
    )


def _provenance_line(provenance: IndiaFactProvenance) -> str:
    bits: list[str] = []
    if provenance.filing_identifier:
        bits.append(provenance.filing_identifier)
    if provenance.published_at:
        bits.append(f"published {provenance.published_at.isoformat()}")
    if provenance.observation_ids:
        bits.append("obs " + ",".join(str(oid) for oid in provenance.observation_ids))
    return " · ".join(bits)


def _extraction_note(session, provenance: IndiaFactProvenance) -> str | None:
    """Display-level provenance for PDF-extracted cells (extraction method + page).

    Reads the winning observation's carried metadata — the same display-level
    enrichment pattern as the US company page — never a source-module call.
    """
    if not (provenance.filing_identifier or "").startswith("company-IR PDF"):
        return None
    observations = [
        obs
        for obs in (session.get(FactObservation, oid) for oid in provenance.observation_ids)
        if obs is not None
    ]
    if not observations:
        return None
    winner = max(observations, key=lambda obs: (obs.filed_at or date.min, obs.id))
    try:
        metadata = json.loads(winner.context_metadata_json or "{}")
    except json.JSONDecodeError:  # pragma: no cover - defensive
        return None
    bits = [str(metadata[key]) for key in ("extraction_method", "page") if metadata.get(key)]
    return " · ".join(bits) or None


def _fact_cell(session, fact: IndiaCanonicalFactEntry) -> FactCellVM:
    provenance = fact.provenance
    per_share = fact.concept in PER_SHARE_CONCEPTS or fact.unit == "INR/share"
    return FactCellVM(
        display=fact.display,
        full=None if per_share else _full_rupees(fact.value) if fact.value is not None else None,
        provenance=_provenance_line(provenance),
        tag=provenance.original_tag,
        source_url=provenance.source_url,
        filing_url=f"/filings/{provenance.artifact_id}" if provenance.artifact_id else None,
        review_status=fact.data_quality_status,
        review_kind=_review_kind(fact.data_quality_status),
        extraction=_extraction_note(session, provenance),
    )


def _missing_cell(cell: IndiaCoverageCell) -> FactCellVM:
    return FactCellVM(
        missing=True,
        missing_status=cell.missing_status,
        note=cell.note,
    )


def _metric_cell(result: IndiaMetricResult | None, column: PeriodColumnVM) -> MetricCellVM:
    if result is None:
        # Growth metrics are quarter-only by construction (never annualized).
        not_defined = column.kind_label != KIND_QUARTER
        return MetricCellVM(
            not_defined=not_defined,
            explanation=None if not_defined else "no metric result for this period identity",
        )
    if result.status == "ok" and result.value is not None:
        if result.unit == "ratio":
            return MetricCellVM(
                status="ok",
                status_kind="ok",
                value_text=_percent_text(result.value),
                notes=list(result.notes),
            )
        return MetricCellVM(
            status="ok",
            status_kind="ok",
            value_text=format_crores(result.value),
            full=_full_rupees(result.value),
            notes=list(result.notes),
        )
    return MetricCellVM(
        status=result.status,
        status_kind="invalid" if result.status in {"invalid", "unsuitable"} else "missing",
        explanation="; ".join(result.notes) or None,
    )


# ---------------------------------------------------------------------------
# Context assembly (public fact-card API only)
# ---------------------------------------------------------------------------


def _issuer_row(issuer: Issuer) -> IssuerListRowVM:
    verified = issuer.verification_status == STATUS_VERIFIED
    ids = [
        part
        for part in (
            f"NSE {issuer.ticker_nse}" if issuer.ticker_nse else None,
            f"BSE {issuer.bse_code}" if issuer.bse_code else None,
            f"ISIN {issuer.isin}" if issuer.isin else None,
        )
        if part
    ]
    return IssuerListRowVM(
        issuer_id=issuer.issuer_id,
        name=issuer.name,
        sector=issuer.sector,
        ids_text=" · ".join(ids) if ids else "identifiers not yet verified",
        verified=verified,
        status_text="verified" if verified else PROPOSED_TEXT,
        page_url=f"/in/{issuer.issuer_id}" if verified else None,
    )


def _empty_context(issuer: Issuer, scope: str, reason: str) -> dict:
    return {
        "issuer": issuer,
        "scope": scope,
        "scope_options": SCOPE_OPTIONS,
        "has_data": False,
        "empty_reason": reason,
        "review_packet_url": REVIEW_PACKET_URL,
        "no_score_note": NO_SCORE_NOTE,
        "formula_version": FORMULA_VERSION_INDIA_METRICS,
        "india_disclaimer": INDIA_DISCLAIMER,
        "research_disclaimer": RESEARCH_DISCLAIMER,
        "currency_note": CURRENCY_NOTE,
    }


def _company_context(session, issuer: Issuer, scope: str) -> dict:
    try:
        report = coverage_report(session, issuer.issuer_id, scope)
    except LookupError:
        report = None
    if report is None or not report.identities:
        if scope == "standalone":
            reason = (
                f"No standalone facts ingested for {issuer.issuer_id}: no standalone "
                "instance is part of the ingested corpus, so this view has nothing "
                "to show. Standalone values are never substituted into the "
                "consolidated series (and vice versa); consolidated data is "
                "deliberately NOT shown here."
            )
        else:
            reason = (
                f"No canonical India facts ingested for {issuer.issuer_id} yet — "
                "import a document first (quarterline ingest india-document). "
                "Nothing is shown rather than placeholder values."
            )
        return _empty_context(issuer, scope, reason)

    ends = sorted({identity.period_end for identity in report.identities})
    cards: dict[date, IndiaFactCard] = {}
    for end in ends:
        cards[end] = build_india_fact_card(session, issuer.issuer_id, period_end=end, scope=scope)

    # Columns come from the cards' period identities (they carry BOTH labels
    # and the filing provenance); the coverage report supplies per-concept
    # present/missing cells for the same (end|kind) keys.
    card_identities = sorted(
        (identity for card in cards.values() for identity in card.period_identities),
        key=lambda identity: (identity.period_end, identity.period_kind),
    )
    columns = [_column_from_identity(identity) for identity in card_identities]

    # facts and metrics keyed by column key (end|kind)
    facts_by_key: dict[str, dict[str, IndiaCanonicalFactEntry]] = {}
    for card in cards.values():
        for fact in card.canonical_facts:
            key = f"{fact.period_end.isoformat()}|{fact.period_kind}"
            facts_by_key.setdefault(key, {})[fact.concept] = fact
    metrics_by_key: dict[str, dict[str, IndiaMetricResult]] = {}
    for card in cards.values():
        for result in card.metrics:
            if result.period_end is None:  # pragma: no cover - defensive
                continue
            key = f"{result.period_end.isoformat()}|{result.period_kind}"
            metrics_by_key.setdefault(key, {})[result.metric_id] = result

    coverage_by_key: dict[tuple[str, str], IndiaCoverageCell] = {}
    for identity in report.identities:
        key = f"{identity.period_end.isoformat()}|{identity.period_kind}"
        for cell in identity.cells:
            coverage_by_key[(key, cell.concept)] = cell

    def indicator_rows(concepts: tuple[str, ...]) -> list[FactRowVM]:
        rows = []
        for concept in concepts:
            cells = []
            for column in columns:
                fact = facts_by_key.get(column.key, {}).get(concept)
                if fact is not None:
                    cells.append(_fact_cell(session, fact))
                else:
                    cells.append(_missing_cell(coverage_by_key[(column.key, concept)]))
            rows.append(FactRowVM(concept=concept, label=CONCEPT_LABELS[concept], cells=cells))
        return rows

    indicator_table = indicator_rows(PANDL_CONCEPTS)

    metric_rows = []
    for metric_id in METRIC_ORDER:
        cells = [
            _metric_cell(metrics_by_key.get(column.key, {}).get(metric_id), column)
            for column in columns
        ]
        metric_rows.append(
            MetricRowVM(
                metric_id=metric_id,
                label=METRIC_LABELS[metric_id],
                formula=METRIC_CAPTIONS[metric_id],
                cells=cells,
            )
        )

    cashflow_rows = []
    for concept in CASH_FLOW_CONCEPTS:
        for column in columns:
            fact = facts_by_key.get(column.key, {}).get(concept)
            cell = (
                _fact_cell(session, fact)
                if fact is not None
                else _missing_cell(coverage_by_key[(column.key, concept)])
            )
            cashflow_rows.append(
                CashFlowRowVM(
                    concept=concept,
                    concept_label=CONCEPT_LABELS[concept],
                    period_label=column.label,
                    kind_label=column.kind_label,
                    dates=column.dates,
                    cell=cell,
                )
            )

    coverage_by_column: dict[str, list[IndiaCoverageCell]] = {}
    for identity in report.identities:
        key = f"{identity.period_end.isoformat()}|{identity.period_kind}"
        coverage_by_column[key] = identity.cells

    coverage_blocks = [
        CoverageBlockVM(
            label=column.label,
            kind_label=column.kind_label,
            dates=column.dates,
            cells=[
                CoverageCellVM(
                    concept=cell.concept,
                    status=cell.status,
                    missing_status=cell.missing_status,
                    note=cell.note,
                    value_display=format_crores(cell.value)
                    if cell.status == "present" and cell.value is not None
                    else None,
                )
                for cell in coverage_by_column[column.key]
            ],
        )
        for column in columns
    ]

    derivations = [
        DerivationVM(
            concept=entry.concept,
            target_label=entry.target_label,
            method=entry.method,
            status=entry.status,
            note=entry.note,
        )
        for entry in report.derivations
    ]

    return {
        "issuer": issuer,
        "scope": scope,
        "scope_options": SCOPE_OPTIONS,
        "has_data": True,
        "columns": columns,
        "period_strip": columns,
        "indicator_rows": indicator_table,
        "metric_rows": metric_rows,
        "cashflow_rows": cashflow_rows,
        "coverage_blocks": coverage_blocks,
        "derivations": derivations,
        "review_packet_url": REVIEW_PACKET_URL,
        "review_packet_section_note": REVIEW_PACKET_SECTION_NOTE,
        "no_score_note": NO_SCORE_NOTE,
        "formula_version": FORMULA_VERSION_INDIA_METRICS,
        "cashflow_caption": (
            "Cash flow is shown only at the frequencies actually reported; values "
            "are never divided into artificial quarters."
        ),
        "india_disclaimer": INDIA_DISCLAIMER,
        "research_disclaimer": RESEARCH_DISCLAIMER,
        "currency_note": CURRENCY_NOTE,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/in")
async def india_landing(request: Request):
    rows = [_issuer_row(issuer) for issuer in load_issuers()]
    context = {
        "rows": rows,
        "verified_count": sum(1 for row in rows if row.verified),
        "india_disclaimer": INDIA_DISCLAIMER,
        "research_disclaimer": RESEARCH_DISCLAIMER,
        "coverage_caveat": COVERAGE_CAVEAT,
    }
    return templates.TemplateResponse(request=request, name="india_index.html", context=context)


@router.get("/in/review-packet")
async def india_review_packet():
    """The review packet the amber badges link to (plain text, verbatim doc)."""
    path = Path(__file__).resolve().parents[4] / "docs" / REVIEW_PACKET_FILENAME
    if path.is_file():
        return FileResponse(path, media_type="text/plain; charset=utf-8")
    return PlainTextResponse(
        "The India reconciliation review packet "
        f"({REVIEW_PACKET_FILENAME}) is not present in this deployment. "
        "Rows flagged requires_manual_review await a human reading of the cited "
        "document (§9 checklist); values are carried as ingested, never resolved.",
        media_type="text/plain; charset=utf-8",
    )


@router.get("/in/{issuer_id}")
async def india_company_page(request: Request, issuer_id: str, scope: str = "consolidated"):
    if scope not in {"consolidated", "standalone"}:
        return render_error(
            request, 404, f"Unknown scope {scope!r} (use consolidated or standalone)."
        )
    try:
        issuer = get_issuer(issuer_id.strip().upper())
    except LookupError as exc:
        return render_error(request, 404, f"Unknown India issuer {issuer_id!r}. {exc}")
    if issuer.verification_status != STATUS_VERIFIED:
        return render_error(
            request,
            404,
            f"{issuer.issuer_id} is listed as {issuer.verification_status!r} — "
            f"{PROPOSED_TEXT}, so no India indicators page exists for it. Pages "
            "appear only after the issuer's identifiers are verified against NSE, "
            "BSE and company sources (docs/india_source_audit.md §1).",
        )
    with session_scope() as session:
        context = _company_context(session, issuer, scope)
    if request.headers.get("HX-Request", "").lower() == "true":
        return templates.TemplateResponse(
            request=request, name="partials/india_panels.html", context=context
        )
    return templates.TemplateResponse(request=request, name="india_company.html", context=context)


@router.get("/in/{issuer_id}/coverage")
async def india_coverage_json(issuer_id: str, scope: str = "consolidated"):
    """Coverage report JSON (programmatic / IND-7 consumption)."""
    if scope not in {"consolidated", "standalone"}:
        return JSONResponse(
            status_code=404,
            content={"error": f"unknown scope {scope!r} (use consolidated or standalone)"},
        )
    try:
        issuer = get_issuer(issuer_id.strip().upper())
    except LookupError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})
    if issuer.verification_status != STATUS_VERIFIED:
        return JSONResponse(
            status_code=404,
            content={
                "error": (
                    f"{issuer.issuer_id} is {issuer.verification_status!r} "
                    f"({PROPOSED_TEXT}); no coverage exists"
                )
            },
        )
    try:
        with session_scope() as session:
            report = coverage_report(session, issuer.issuer_id, scope)
    except LookupError as exc:
        return JSONResponse(status_code=404, content={"error": str(exc)})
    if not report.identities:
        return JSONResponse(
            status_code=404,
            content={
                "error": (
                    f"no canonical India facts for {issuer.issuer_id} ({scope}) — "
                    "nothing has been ingested under this reporting scope"
                )
            },
        )
    # model_dump(mode="json"): Decimal -> canonical string, dates -> ISO.
    return JSONResponse(content=report.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# IND-8: India brief + memo generation routes
#
# Same guardrails as the US side: official facts + retrieved evidence in,
# constrained validated prose out, page-level citations, honest
# insufficient-evidence, advice refusal; no scores, no recommendations. The
# generation modules are imported LAZILY so the deterministic India pages keep
# working with the provider down (SPEC §25); degraded responses carry facts
# and evidence with a banner — never unvalidated prose.
# ---------------------------------------------------------------------------

_BANNER_DEGRADED = "generation unavailable — showing facts and evidence, not generated prose"

RESEARCH_DISCLAIMER_BRIEF = "Research and education only. Not investment advice."


def _wants_html(request: Request) -> bool:
    if request.headers.get("HX-Request", "").lower() == "true":
        return True
    accept = request.headers.get("accept", "")
    return "text/html" in accept and "application/json" not in accept.split(",")[0]


@router.post("/in/{issuer_id}/brief")
async def generate_issuer_brief(request: Request, issuer_id: str):
    """POST /in/{issuer_id}/brief — grounded India brief (IND-8).

    JSON/urlencoded body ``{"period_end"?: YYYY-MM-DD, "scope"?: ...
    "focus"?: str}``; HTML (HTMX) requests get the
    :template:`partials/india_brief.html` partial, others JSON.
    """
    from quarterline.sources.india.brief import generate_india_brief  # lazy: LLM stack

    try:
        issuer = get_issuer(issuer_id.strip().upper())
    except LookupError as exc:
        return _error(request, 404, f"Unknown India issuer {issuer_id!r}. {exc}")
    if issuer.verification_status != STATUS_VERIFIED:
        return _error(
            request,
            404,
            f"{issuer.issuer_id} is listed as {issuer.verification_status!r} — "
            f"{PROPOSED_TEXT}, so no brief can be generated for it.",
        )

    payload, parse_error = await _parse_body(request)
    if parse_error:
        return _error(request, 400, parse_error)
    unknown = sorted(set(payload) - {"period_end", "scope", "focus"})
    if unknown:
        return _error(request, 400, f"unknown field(s): {', '.join(unknown)}")
    scope = payload.get("scope") or "consolidated"
    if scope not in {"consolidated", "standalone"}:
        return _error(request, 400, f"unknown scope {scope!r} (use consolidated or standalone)")
    focus = payload.get("focus")
    if focus is not None and not isinstance(focus, str):
        return _error(request, 400, "focus must be a string")
    raw_period = payload.get("period_end")
    wanted = _parse_period_end(raw_period)
    if raw_period and wanted is None:
        return _error(request, 400, f"invalid period_end {raw_period!r} (use YYYY-MM-DD)")

    try:
        outcome = generate_india_brief(issuer.issuer_id, wanted, scope=scope, focus=focus)
    except LookupError as exc:
        return _error(request, 404, str(exc))

    if _wants_html(request):
        return templates.TemplateResponse(
            request=request,
            name="partials/india_brief.html",
            context={
                "outcome": outcome,
                "degraded_banner": _BANNER_DEGRADED
                if outcome.status == "provider_unavailable"
                else None,
                "disclaimer": RESEARCH_DISCLAIMER_BRIEF,
            },
        )
    return JSONResponse(content=outcome.model_dump(mode="json"))


@router.post("/api/india-memos")
async def create_india_memo_run(request: Request) -> JSONResponse:
    """POST /api/india-memos — one synchronous bounded India memo run (IND-8).

    Identical budgets, gates, checkpoints and approval-gated export as the US
    ``POST /api/memos``; the request is pinned to ``market="india"`` and the
    issuer must be verified in the India registry.
    """
    from quarterline.agent.graph import run_memo_workflow  # lazy: LLM stack

    payload, parse_error = await _parse_body(request)
    if parse_error:
        return _error(request, 400, parse_error)
    unknown = sorted(
        set(payload)
        - {"issuer_id", "memo_type", "question", "topic", "period_end", "market_context"}
    )
    if unknown:
        return _error(request, 400, f"unknown field(s): {', '.join(unknown)}")
    raw_issuer = payload.get("issuer_id")
    if not raw_issuer:
        return _error(request, 400, "issuer_id is required")
    try:
        issuer = get_issuer(str(raw_issuer).strip().upper())
    except LookupError as exc:
        return _error(request, 404, f"Unknown India issuer {raw_issuer!r}. {exc}")
    if issuer.verification_status != STATUS_VERIFIED:
        return _error(
            request,
            404,
            f"{issuer.issuer_id} is listed as {issuer.verification_status!r} — "
            f"{PROPOSED_TEXT}, so no memo can be generated for it.",
        )
    if payload.get("memo_type") not in ("quarter_review", "risk_review"):
        return _error(request, 400, "memo_type must be quarter_review or risk_review")
    if payload.get("period_end"):
        parsed_period = _parse_period_end(payload.get("period_end"))
        if parsed_period is None:
            return _error(
                request,
                400,
                f"invalid period_end {payload.get('period_end')!r} (use YYYY-MM-DD)",
            )
        payload["period_end"] = parsed_period.isoformat()
    if payload.get("market_context") not in (None, "true", "false", True, False):
        return _error(request, 400, "market_context must be a boolean")

    workflow_payload = {
        "ticker": issuer.issuer_id,
        "memo_type": payload["memo_type"],
        "market": "india",
    }
    for name in ("question", "topic", "period_end", "market_context"):
        if payload.get(name) not in (None, ""):
            workflow_payload[name] = payload[name]
    result = run_memo_workflow(workflow_payload)
    if _wants_html(request):
        return _render_india_review(request, result)
    return JSONResponse(status_code=200, content=result.model_dump(mode="json"))


@router.get("/api/india-memos/{run_id}")
async def get_india_memo_run(run_id: str, request: Request):
    from quarterline.agent.checkpoints import load_result  # lazy
    from quarterline.store.db import session_scope

    with session_scope() as session:
        result = load_result(session, run_id)
    if result is None:
        return _error(request, 404, f"no agent run {run_id!r}")
    if _wants_html(request):
        return _render_india_review(request, result)
    return JSONResponse(status_code=200, content=result.model_dump(mode="json"))


@router.post("/api/india-memos/{run_id}/approve-export")
async def approve_and_export_india_memo(run_id: str, request: Request):
    from quarterline.agent.checkpoints import load_state  # lazy
    from quarterline.agent.graph import export_approved_memo  # lazy
    from quarterline.store.db import session_scope

    payload, parse_error = await _parse_body(request)
    if parse_error:
        return _error(request, 400, parse_error)
    unknown = sorted(set(payload) - {"approval_request_id", "export_type", "memo_content"})
    if unknown:
        return _error(request, 400, f"unknown field(s): {', '.join(unknown)}")
    export_type = payload.get("export_type") or "md"
    if export_type not in ("md", "json"):
        return _error(request, 400, "export_type must be 'md' or 'json'")
    raw_request_id = payload.get("approval_request_id")
    try:
        request_id = int(raw_request_id)
    except (TypeError, ValueError):
        return _error(request, 400, "approval_request_id must be an integer")

    with session_scope() as session:
        state = load_state(session, run_id)
        if state is None:
            return _error(request, 404, f"no agent run {run_id!r}")
        if state.get("status") != "awaiting_approval":
            return _error(
                request,
                409,
                f"run status is {state.get('status')!r}; export requires "
                "awaiting_approval with a validated memo",
            )
        result, decision = export_approved_memo(
            run_id,
            request_id,
            payload.get("memo_content") or state.get("memo_content") or "",
            export_type,
            session=session,
        )
    if not decision.approved:
        return _error(request, 409, f"export not approved: {decision.reason}")
    assert result is not None
    if _wants_html(request):
        return _render_india_review(request, result, exported=True)
    return JSONResponse(
        status_code=200,
        content={
            "status": "exported",
            "exported": result.exported,
            "run": result.model_dump(mode="json"),
        },
    )


def _render_india_review(request: Request, result, *, exported: bool = False):
    """The US memo_review rendering reused verbatim (same page, same gate)."""
    from quarterline.api.routers.memo import _render_review

    return _render_review(request, result, exported=exported)


def _error(request: Request, status: int, detail: str):
    from quarterline.api.routers.memo import _wants_html as _memo_wants_html

    if _memo_wants_html(request):
        return render_error(request, status, detail)
    return JSONResponse(status_code=status, content={"error": detail})
