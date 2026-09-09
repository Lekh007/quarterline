"""Company pages, watchlist and JSON fact APIs (SPEC §19 non-LLM routes).

Everything here is deterministic over the store: no LLM/module imports, no
network. Missing data renders as ``n/a`` (watchlist) or ``missing`` (company
page) -- never a zero substitute (SPEC §2.1.5/2.1.6). All JSON numbers are
Decimal-precision strings (never floats), via pydantic ``model_dump(mode=
"json")`` which serializes ``Decimal`` to its canonical string form.

Display helpers in this module are reused by the screener router.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from quarterline.core.models import (
    METRIC_IDS,
    FactCard,
    MetricStatus,
    MetricValue,
    QuarterLabel,
)
from quarterline.core.provenance import (
    FLOW_FACT_METRICS,
    FORMULA_TEXT,
    INSTANT_FACT_METRICS,
    QUARTER_LABEL_DISCLAIMER,
    ProvenanceSource,
    build_fact_cards,
    build_quarter_data,
    explain_metric,
)
from quarterline.store.db import session_scope
from quarterline.store.models import Company, DerivedMetric
from quarterline.store.repositories.companies import CompaniesRepo
from quarterline.store.repositories.facts import ROLE_DIRECT_SOURCE, FactsRepo

API_DIR = Path(__file__).resolve().parents[1]

router = APIRouter()
templates = Jinja2Templates(directory=str(API_DIR / "templates"))

MILLION = Decimal(1_000_000)

#: Reported-fact concepts shown on the fact card (see provenance module).
FACT_CONCEPTS = frozenset(FLOW_FACT_METRICS) | frozenset(INSTANT_FACT_METRICS) | {"capex_outflow"}

#: Ratio metrics rendered as percentages (stored as unit ``ratio``).
PERCENT_METRICS = frozenset(
    {
        "revenue_yoy",
        "shares_yoy",
        "gross_margin",
        "operating_margin",
        "net_margin",
        "fcf_margin",
        "data_coverage",
    }
)

LABEL_DISPLAY: dict[str, str] = {
    QuarterLabel.strong.value: "Strong",
    QuarterLabel.mixed.value: "Mixed",
    QuarterLabel.weak.value: "Weak",
    QuarterLabel.insufficient_data.value: "Insufficient data",
}


# ---------------------------------------------------------------------------
# Formatting helpers (Decimal-exact; never float)
# ---------------------------------------------------------------------------


def format_decimal(value: Decimal, unit: str | None, metric_id: str) -> str:
    """Deterministic human rendering; the underlying value stays a Decimal."""
    unit = unit or ""
    if unit == "USD":
        return f"{value / MILLION:,.1f}"  # column headers carry the USD M unit
    if unit == "shares":
        return f"{value / MILLION:,.1f}M"
    if unit == "USD/shares":
        return f"{value:.2f}"
    if unit == "percentage_points":
        return f"{value:+.1f} pp"
    if unit == "score":
        return f"{value:.1f}"
    if unit == "ratio":
        if metric_id in PERCENT_METRICS:
            return f"{value * 100:.1f}%"
        return f"{value:.2f}"
    return str(value)


def format_metric(metric: MetricValue) -> str:
    """Watchlist-style rendering: anything not ``ok`` shows ``n/a`` (SPEC §25:
    tables keep working; missing stays missing, never zero)."""
    if metric.status is not MetricStatus.ok or metric.value is None:
        return "n/a"
    return format_decimal(metric.value, metric.unit, metric.metric_id)


def metric_of(card: FactCard, metric_id: str) -> MetricValue:
    """The card's metric, or an explicit missing one (allowlist-checked ids)."""
    for metric in card.metrics:
        if metric.metric_id == metric_id:
            return metric
    return MetricValue(metric_id=metric_id)


def quarter_text(card: FactCard) -> str:
    """``FY2026 Q3 (2026-06-27)``-style label; period end always shown."""
    parts = card.period_end.isoformat()
    if card.fiscal_year and card.fiscal_quarter:
        return f"FY{card.fiscal_year} {card.fiscal_quarter} ({parts})"
    return parts


def latest_card_for(session, ticker: str) -> FactCard | None:
    """Latest complete quarter's fact card, or None when nothing ingested."""
    try:
        cards = build_fact_cards(session, ticker, n_quarters=1)
    except LookupError:
        return None
    return cards[0] if cards else None


def last_ingested_at(session, company: Company) -> datetime | None:
    """Last ingestion timestamp: newest derived-metric write, else company upsert."""
    stamp = session.scalar(
        select(func.max(DerivedMetric.available_at)).where(DerivedMetric.company_id == company.id)
    )
    return stamp if stamp is not None else company.updated_at


# ---------------------------------------------------------------------------
# Error pages (shared with the other wave-2 routers)
# ---------------------------------------------------------------------------


def render_error(request: Request, status: int, detail: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="partials/error.html",
        context={"status": status, "detail": detail},
        status_code=status,
    )


def json_error(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": detail})


# ---------------------------------------------------------------------------
# Watchlist (GET /)
# ---------------------------------------------------------------------------


@dataclass
class WatchlistRow:
    """Display view of one watchlist company's latest complete quarter."""

    ticker: str
    name: str | None
    sector: str | None
    has_data: bool
    quarter: str = "n/a"
    revenue_yoy: str = "n/a"
    operating_margin: str = "n/a"
    cfo_to_net_income: str = "n/a"
    fcf: str = "n/a"
    label: str = "n/a"
    label_value: str = ""
    fundamental_score: str = "n/a"
    coverage: str = "n/a"
    ingested_at: str = "n/a"


def watchlist_row_for(session, company: Company) -> WatchlistRow:
    card = latest_card_for(session, company.ticker)
    return watchlist_row_for_card(session, company, card)


def watchlist_row_for_card(session, company: Company, card: FactCard | None) -> WatchlistRow:
    """Display row for a company from an already-built latest-quarter card."""
    row = WatchlistRow(
        ticker=company.ticker, name=company.name, sector=company.sector, has_data=False
    )
    if card is None:
        return row  # no ingested data: graceful empty state, no invented values
    row.has_data = True
    row.quarter = quarter_text(card)
    row.revenue_yoy = format_metric(metric_of(card, "revenue_yoy"))
    row.operating_margin = format_metric(metric_of(card, "operating_margin"))
    row.cfo_to_net_income = format_metric(metric_of(card, "cfo_to_net_income"))
    row.fcf = format_metric(metric_of(card, "fcf"))
    row.fundamental_score = format_metric(metric_of(card, "fundamental_score"))
    row.coverage = format_metric(metric_of(card, "data_coverage"))
    row.label = label_display_of(card)
    row.label_value = label_value_of(card)
    stamp = last_ingested_at(session, company)
    row.ingested_at = stamp.isoformat() if stamp else "n/a"
    return row


def label_value_of(card: FactCard) -> str:
    label_metric = metric_of(card, "quarter_label")
    for ref in label_metric.provenance.derived_from:
        if ref.startswith("label:"):
            return ref.split(":", 1)[1]
    return ""


def label_display_of(card: FactCard) -> str:
    value = label_value_of(card)
    return LABEL_DISPLAY.get(value, "n/a") if value else "n/a"


def _watchlist_rows(session) -> list[WatchlistRow]:
    companies = CompaniesRepo(session).all()
    return [watchlist_row_for(session, company) for company in companies]


@router.get("/")
async def watchlist(request: Request):
    with session_scope() as session:
        rows = _watchlist_rows(session)
    context = {"rows": rows, "label_caption": QUARTER_LABEL_DISCLAIMER}
    if request.headers.get("HX-Request", "").lower() == "true":
        return templates.TemplateResponse(
            request=request, name="partials/watchlist_rows.html", context=context
        )
    return templates.TemplateResponse(request=request, name="index.html", context=context)


# ---------------------------------------------------------------------------
# Company page (GET /c/{ticker})
# ---------------------------------------------------------------------------


@dataclass
class FactRow:
    """One row of the company-page fact table."""

    metric_id: str
    value: str
    unit: str
    badge: str
    badge_kind: str  # direct | derived | missing | rule
    formula_text: str
    formula_version: str | None
    provenance_url: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class SummaryRow:
    """One row of the eight-quarter summary table (no-JS chart fallback)."""

    quarter: str
    revenue: str
    net_income: str
    operating_margin: str
    net_margin: str
    fcf: str


def _badge_for(metric: MetricValue) -> tuple[str, str]:
    if metric.metric_id == "quarter_label":
        return ("rule-based", "rule")
    if metric.metric_id in FACT_CONCEPTS:
        if metric.status is not MetricStatus.ok:
            return (metric.status.value, "missing")
        derived_fact = any(n.startswith("derived by subtraction") for n in metric.notes)
        if derived_fact:
            return ("derived (subtraction)", "derived")
        return ("direct", "direct")
    if metric.status is not MetricStatus.ok:
        return (metric.status.value, "missing")
    return ("derived (formula)", "derived")


def _fact_rows(card: FactCard) -> list[FactRow]:
    rows: list[FactRow] = []
    provenance_base = f"/c/{card.ticker}/provenance"
    period = card.period_end.isoformat()
    for metric in card.metrics:
        badge, kind = _badge_for(metric)
        if metric.status is MetricStatus.ok and metric.value is not None:
            value = format_decimal(metric.value, metric.unit, metric.metric_id)
        elif metric.status is MetricStatus.missing:
            value = "missing"
        else:
            value = metric.status.value
        formula = FORMULA_TEXT.get(metric.metric_id)
        if formula is None:
            formula = (
                "Reported fact, normalized from filed XBRL observations."
                if metric.metric_id in FACT_CONCEPTS
                else None
            )
        warnings = (
            list(metric.notes)
            if metric.status is not MetricStatus.ok
            else [note for note in metric.notes if "review" in note.lower()]
        )
        rows.append(
            FactRow(
                metric_id=metric.metric_id,
                value=value,
                unit=metric.unit or "",
                badge=badge,
                badge_kind=kind,
                formula_text=formula or "n/a",
                formula_version=metric.provenance.formula_version,
                provenance_url=f"{provenance_base}/{metric.metric_id}?period_end={period}",
                warnings=warnings,
            )
        )
    return rows


def _summary_rows(cards: list[FactCard]) -> list[SummaryRow]:
    rows = []
    for card in cards:
        rows.append(
            SummaryRow(
                quarter=quarter_text(card),
                revenue=format_metric(metric_of(card, "revenue")),
                net_income=format_metric(metric_of(card, "net_income")),
                operating_margin=format_metric(metric_of(card, "operating_margin")),
                net_margin=format_metric(metric_of(card, "net_margin")),
                fcf=format_metric(metric_of(card, "fcf")),
            )
        )
    return rows


def _chart_payload(cards: list[FactCard]) -> dict:
    """Series for Chart.js; values stay Decimal strings (JS converts for plots)."""
    quarters = []
    for card in cards:
        values = {
            metric.metric_id: {
                "value": str(metric.value) if metric.value is not None else None,
                "unit": metric.unit,
                "status": metric.status.value,
            }
            for metric in card.metrics
        }
        quarters.append(
            {
                "period_end": card.period_end.isoformat(),
                "label": quarter_text(card),
                "values": values,
            }
        )
    return {"ticker": cards[0].ticker, "quarters": quarters}


def _label_panel(session, ticker: str, period_end: date) -> dict | None:
    """Structured SPEC §12.1 label result: every input and rule for the UI."""
    company = CompaniesRepo(session).get_by_ticker(ticker)
    if company is None:
        return None
    repo = FactsRepo(session)
    quarters = repo.quarters_available(company.id)
    quarter = next((q for q in quarters if q.period_end == period_end), None)
    if quarter is None:
        return None
    data = build_quarter_data(repo, company, quarter, quarters, None, is_quarter=True)
    if data.label_result is None:
        return None
    result = data.label_result
    return {
        "label": LABEL_DISPLAY.get(result.label.value, result.label.value),
        "label_value": result.label.value,
        "rule_applied": result.rule_applied,
        "formula_version": result.formula_version,
        "signals": [
            {
                "signal_id": s.signal_id,
                "description": s.description,
                "state": s.state.value,
                "detail": s.detail,
            }
            for s in result.signals
        ],
        "notes": result.notes,
        "caption": QUARTER_LABEL_DISCLAIMER,
    }


def _parse_period_end(raw: str | None) -> date | None:
    if raw is None or raw == "":
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


@router.get("/c/{ticker}")
async def company_page(request: Request, ticker: str, period_end: str | None = None):
    wanted = _parse_period_end(period_end)
    if period_end and wanted is None:
        return render_error(request, 404, f"Invalid period_end {period_end!r} (use YYYY-MM-DD).")
    with session_scope() as session:
        company = CompaniesRepo(session).get_by_ticker(ticker)
        if company is None:
            return render_error(request, 404, f"Unknown ticker {ticker.upper()!r}.")
        cards = build_fact_cards(session, ticker)
        if not cards:
            # Never assume facts exist: explicit empty state, no zero filling.
            return templates.TemplateResponse(
                request=request,
                name="company.html",
                context={
                    "company": {
                        "ticker": company.ticker,
                        "name": company.name,
                        "sector": company.sector,
                        "cik": company.cik,
                    },
                    "has_data": False,
                    "label_caption": QUARTER_LABEL_DISCLAIMER,
                },
            )
        selected = cards[-1]
        if wanted is not None:
            matches = [card for card in cards if card.period_end == wanted]
            if not matches:
                return render_error(
                    request,
                    404,
                    f"No quarter with period_end {wanted.isoformat()} for {ticker.upper()!r}.",
                )
            selected = matches[0]
        context = {
            "company": {
                "ticker": company.ticker,
                "name": company.name,
                "sector": company.sector,
                "cik": company.cik,
            },
            "has_data": True,
            "cards": cards,
            "selected": selected,
            "selected_quarter": quarter_text(selected),
            "quarter_texts": [quarter_text(card) for card in cards],
            "fact_rows": _fact_rows(selected),
            "summary_rows": _summary_rows(cards),
            "chart_payload": _chart_payload(cards),
            "label_panel": _label_panel(session, company.ticker, selected.period_end),
            "label_caption": QUARTER_LABEL_DISCLAIMER,
            "coverage": format_metric(metric_of(selected, "data_coverage")),
        }
    return templates.TemplateResponse(request=request, name="company.html", context=context)


# ---------------------------------------------------------------------------
# Provenance page (GET /c/{ticker}/provenance/{metric_id}?period_end=...)
# ---------------------------------------------------------------------------

#: Growth-style metrics whose upstream ``derived_from`` refs are
#: ``"<metric_id>:<role>"`` rather than concept names, so ``explain_metric``
#: cannot resolve their input lineage on its own. This display-level map
#: (mirroring the published FORMULA_TEXT) restores the full lineage for the
#: viewer; the authoritative fix belongs in core (reported to the orchestrator).
GROWTH_INPUT_CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue_yoy": ("revenue",),
    "shares_yoy": ("shares_diluted",),
    "operating_margin_change_pp": ("operating_income", "revenue"),
}


def _enrich_growth_sources(session, report):
    """Fill in source observations for growth metrics (UI display only)."""
    concepts = GROWTH_INPUT_CONCEPTS.get(report.metric_id)
    if report.derivation != "computed" or report.sources or not concepts:
        return report
    company = CompaniesRepo(session).get_by_ticker(report.ticker)
    if company is None:
        return report
    repo = FactsRepo(session)
    quarters = repo.quarters_available(company.id)
    quarter = next((q for q in quarters if q.period_end == report.period_end), None)
    if quarter is None:
        return report
    prior_year = next(
        (
            q
            for q in quarters
            if q.fiscal_quarter == quarter.fiscal_quarter
            and q.fiscal_year is not None
            and quarter.fiscal_year is not None
            and q.fiscal_year == quarter.fiscal_year - 1
        ),
        None,
    )
    periods = [(report.period_end, "current")]
    if prior_year is not None:
        periods.append((prior_year.period_end, "prior_year_quarter"))
    sources = list(report.sources)
    input_concepts = list(report.input_concepts)
    for concept in concepts:
        if concept not in input_concepts:
            input_concepts.append(concept)
        for period_end, tag in periods:
            fact = repo.facts_for_quarter(company.id, period_end).get(concept)
            if fact is None:
                continue
            for obs, lineage in repo.lineage_for_fact(fact.id):
                sources.append(
                    ProvenanceSource(
                        observation_id=obs.id or 0,
                        accession=obs.accession,
                        form=obs.form,
                        filed_at=obs.filed_at,
                        original_tag=obs.original_tag,
                        canonical_concept=obs.canonical_concept,
                        unit=obs.unit,
                        value=obs.value_decimal,
                        role=f"{lineage.role}:{concept}@{tag}",
                        direct=lineage.role == ROLE_DIRECT_SOURCE,
                    )
                )
    report.sources = sources
    report.input_concepts = input_concepts
    return report


def _provenance_report(session, ticker: str, metric_id: str, period_end: date):
    """explain_metric plus the display-level growth lineage enrichment."""
    return _enrich_growth_sources(session, explain_metric(session, ticker, metric_id, period_end))


def _latest_period_end(session, ticker: str) -> date | None:
    company = CompaniesRepo(session).get_by_ticker(ticker)
    if company is None:
        return None
    quarters = FactsRepo(session).quarters_available(company.id)
    return quarters[-1].period_end if quarters else None


@router.get("/c/{ticker}/provenance/{metric_id}")
async def provenance_page(
    request: Request, ticker: str, metric_id: str, period_end: str | None = None
):
    # METRIC_IDS allowlist gate (PLAN C7): bogus metric ids never reach the store.
    if metric_id not in METRIC_IDS:
        return render_error(request, 404, f"Unknown metric {metric_id!r}.")
    wanted = _parse_period_end(period_end)
    if period_end and wanted is None:
        return render_error(request, 404, f"Invalid period_end {period_end!r} (use YYYY-MM-DD).")
    with session_scope() as session:
        if wanted is None:
            resolved = _latest_period_end(session, ticker)
            if resolved is None:
                return render_error(
                    request, 404, f"Unknown ticker {ticker.upper()!r} or no quarters."
                )
            wanted = resolved
        try:
            report = _provenance_report(session, ticker, metric_id, wanted)
        except LookupError as exc:
            return render_error(request, 404, str(exc))
        sources = [
            {
                "observation_id": s.observation_id,
                "accession": _dashed_accession(s.accession),
                "form": s.form,
                "filed_at": s.filed_at.isoformat() if s.filed_at else None,
                "original_tag": s.original_tag,
                "canonical_concept": s.canonical_concept,
                "unit": s.unit,
                "value": s.value,
                "role": s.role,
                "direct": s.direct,
            }
            for s in report.sources
        ]
        context = {
            "report": report,
            "sources": sources,
            "period_end": report.period_end.isoformat(),
            "back_url": f"/c/{report.ticker}",
        }
    return templates.TemplateResponse(
        request=request, name="partials/provenance.html", context=context
    )


def _dashed_accession(accession: str | None) -> str | None:
    """Display accessions in the canonical 0000320193-26-000011 form."""
    if accession and len(accession) == 18 and "-" not in accession:
        return f"{accession[:10]}-{accession[10:12]}-{accession[12:]}"
    return accession


# ---------------------------------------------------------------------------
# JSON APIs (Decimal values serialized as strings; 404 JSON on unknowns)
# ---------------------------------------------------------------------------


@router.get("/api/c/{ticker}/facts")
async def api_facts(ticker: str, period_end: str | None = None):
    wanted = _parse_period_end(period_end)
    if period_end and wanted is None:
        return json_error(404, f"invalid period_end {period_end!r} (use YYYY-MM-DD)")
    with session_scope() as session:
        company = CompaniesRepo(session).get_by_ticker(ticker)
        if company is None:
            return json_error(404, f"unknown ticker {ticker.upper()!r}")
        try:
            cards = build_fact_cards(session, ticker, period_end=wanted, n_quarters=1)
        except LookupError as exc:
            return json_error(404, str(exc))
        if not cards:
            return json_error(404, f"no quarter found for {ticker.upper()!r}")
        card = cards[0]
        # model_dump(mode="json"): Decimal -> canonical string, dates -> ISO.
        return JSONResponse(content=card.model_dump(mode="json"))


@router.get("/api/c/{ticker}/metrics")
async def api_metrics(ticker: str):
    with session_scope() as session:
        company = CompaniesRepo(session).get_by_ticker(ticker)
        if company is None:
            return json_error(404, f"unknown ticker {ticker.upper()!r}")
        try:
            cards = build_fact_cards(session, ticker, n_quarters=8)
        except LookupError as exc:
            return json_error(404, str(exc))
        quarters = []
        for card in cards:
            quarters.append(
                {
                    "period_end": card.period_end.isoformat(),
                    "fiscal_year": card.fiscal_year,
                    "fiscal_quarter": card.fiscal_quarter,
                    "metrics": {
                        metric.metric_id: {
                            "value": str(metric.value) if metric.value is not None else None,
                            "unit": metric.unit,
                            "status": metric.status.value,
                            "formula_version": metric.provenance.formula_version,
                        }
                        for metric in card.metrics
                    },
                }
            )
        payload = {"ticker": company.ticker, "n_quarters": len(quarters), "quarters": quarters}
    return JSONResponse(content=payload)


@router.get("/api/c/{ticker}/provenance/{metric_id}")
async def api_provenance(ticker: str, metric_id: str, period_end: str | None = None):
    if metric_id not in METRIC_IDS:
        return json_error(404, f"unknown metric {metric_id!r} (not in the METRIC_IDS allowlist)")
    wanted = _parse_period_end(period_end)
    if period_end and wanted is None:
        return json_error(404, f"invalid period_end {period_end!r} (use YYYY-MM-DD)")
    with session_scope() as session:
        if wanted is None:
            resolved = _latest_period_end(session, ticker)
            if resolved is None:
                return json_error(404, f"unknown ticker {ticker.upper()!r} or no quarters")
            wanted = resolved
        try:
            report = _provenance_report(session, ticker, metric_id, wanted)
        except LookupError as exc:
            return json_error(404, str(exc))
        return JSONResponse(content=report.model_dump(mode="json"))
