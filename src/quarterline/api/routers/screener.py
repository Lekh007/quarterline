"""Deterministic screener (SPEC §19) -- filters, never recommendations.

Filters exactly as specified: ``revenue_yoy_gt``, ``operating_margin_gt``,
``operating_margin_change_pp_gt``, ``fcf_positive``, ``quarter_label``,
``sector``, ``minimum_data_coverage``. Server-side filtering over fact cards
via the same code paths as the watchlist. Works with Ollama stopped: this
module imports no LLM code and touches no generation provider.

GET and POST both render the form with every submitted value echoed; HTMX
requests (``HX-Request: true``) receive only the result-rows partial.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from quarterline.api.routers.companies import (
    WatchlistRow,
    label_value_of,
    latest_card_for,
    metric_of,
    watchlist_row_for_card,
)
from quarterline.core.models import FactCard, MetricStatus
from quarterline.core.provenance import QUARTER_LABEL_DISCLAIMER
from quarterline.store.db import session_scope
from quarterline.store.models import Company
from quarterline.store.repositories.companies import CompaniesRepo

API_DIR = Path(__file__).resolve().parents[1]

router = APIRouter()
templates = Jinja2Templates(directory=str(API_DIR / "templates"))

#: Form field names (SPEC §19 screener filter list, verbatim).
FILTER_FIELDS = (
    "revenue_yoy_gt",
    "operating_margin_gt",
    "operating_margin_change_pp_gt",
    "fcf_positive",
    "quarter_label",
    "sector",
    "minimum_data_coverage",
)

LABEL_CHOICES = (
    ("strong", "Strong"),
    ("mixed", "Mixed"),
    ("weak", "Weak"),
    ("insufficient_data", "Insufficient data"),
)


@dataclass
class ScreenerFilters:
    """Parsed filter state plus the raw strings echoed back into the form."""

    revenue_yoy_gt: Decimal | None = None
    operating_margin_gt: Decimal | None = None
    operating_margin_change_pp_gt: Decimal | None = None
    fcf_positive: bool = False
    quarter_label: str = ""
    sector: str = ""
    minimum_data_coverage: Decimal | None = None
    raw: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(
            self.revenue_yoy_gt is not None
            or self.operating_margin_gt is not None
            or self.operating_margin_change_pp_gt is not None
            or self.fcf_positive
            or self.quarter_label
            or self.sector
            or self.minimum_data_coverage is not None
        )


def _parse_decimal(raw: str, name: str, errors: list[str]) -> Decimal | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        errors.append(f"{name}: {text!r} is not a valid decimal number")
        return None


def parse_filters(values: dict[str, str]) -> ScreenerFilters:
    """Parse submitted strings; validation failures become visible errors."""
    errors: list[str] = []
    raw = {name: str(values.get(name) or "") for name in FILTER_FIELDS}
    filters = ScreenerFilters(
        revenue_yoy_gt=_parse_decimal(raw["revenue_yoy_gt"], "revenue_yoy_gt", errors),
        operating_margin_gt=_parse_decimal(
            raw["operating_margin_gt"], "operating_margin_gt", errors
        ),
        operating_margin_change_pp_gt=_parse_decimal(
            raw["operating_margin_change_pp_gt"], "operating_margin_change_pp_gt", errors
        ),
        fcf_positive=raw["fcf_positive"].strip().lower() in {"on", "true", "1", "yes"},
        quarter_label=raw["quarter_label"].strip().lower(),
        sector=raw["sector"].strip(),
        minimum_data_coverage=_parse_decimal(
            raw["minimum_data_coverage"], "minimum_data_coverage", errors
        ),
        raw=raw,
        errors=errors,
    )
    if filters.quarter_label and filters.quarter_label not in {v for v, _ in LABEL_CHOICES}:
        errors.append(f"quarter_label: unknown label {filters.quarter_label!r}")
        filters.quarter_label = ""
    if filters.minimum_data_coverage is not None and not (
        Decimal(0) <= filters.minimum_data_coverage <= Decimal(1)
    ):
        errors.append("minimum_data_coverage: must be between 0 and 1")
        filters.minimum_data_coverage = None
    return filters


def _company_passes(company: Company, card: FactCard, filters: ScreenerFilters) -> bool:
    """Deterministic predicate over the latest complete quarter's fact card.

    Numeric filters are strict ``>`` (coverage is ``>=``); a metric that is
    missing or invalid never satisfies its filter (SPEC §2.1.5).
    """

    def passes(metric_id: str, threshold: Decimal, *, at_least: bool = False) -> bool:
        metric = metric_of(card, metric_id)
        if metric.status is not MetricStatus.ok or metric.value is None:
            return False  # missing data never satisfies a numeric filter
        return metric.value >= threshold if at_least else metric.value > threshold

    thresholds: list[tuple[Decimal, str, bool]] = [
        (filters.revenue_yoy_gt, "revenue_yoy", False),
        (filters.operating_margin_gt, "operating_margin", False),
        (filters.operating_margin_change_pp_gt, "operating_margin_change_pp", False),
        (filters.minimum_data_coverage, "data_coverage", True),
    ]
    if filters.fcf_positive:
        thresholds.append((Decimal(0), "fcf", False))
    sector_ok = not filters.sector or (
        (company.sector or "").strip().lower() == filters.sector.lower()
    )
    return sector_ok and all(
        passes(metric_id, threshold, at_least=at_least)
        for threshold, metric_id, at_least in thresholds
        if threshold is not None
    )


async def _submitted_values(request: Request) -> dict[str, str]:
    """Filter strings from the query string (GET) or urlencoded body (POST).

    Parsed directly from the body because the project does not depend on a
    multipart parser (browser forms without file uploads are urlencoded by
    default, and so is HTMX).
    """
    if request.method != "POST":
        return {name: request.query_params.get(name) or "" for name in FILTER_FIELDS}
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" not in content_type:
        return {}
    body = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(body, keep_blank_values=True)
    return {name: (parsed.get(name) or [""])[0] for name in FILTER_FIELDS}


async def _handle_screener(request: Request):
    submitted = await _submitted_values(request)
    filters = parse_filters(submitted)

    with session_scope() as session:
        companies = CompaniesRepo(session).all()
        sectors = sorted({(c.sector or "").strip() for c in companies if c.sector})
        rows: list[WatchlistRow] = []
        with_data = 0
        for company in companies:
            card = latest_card_for(session, company.ticker)
            if card is None:
                continue  # companies without ingested data never match a filter
            with_data += 1
            if filters.quarter_label and label_value_of(card) != filters.quarter_label:
                continue
            if not filters.active or _company_passes(company, card, filters):
                rows.append(watchlist_row_for_card(session, company, card))

    context = {
        "filters": filters,
        "rows": rows,
        "sectors": sectors,
        "label_choices": LABEL_CHOICES,
        "with_data": with_data,
        "label_caption": QUARTER_LABEL_DISCLAIMER,
        "result_count": len(rows),
    }
    if request.headers.get("HX-Request", "").lower() == "true":
        return templates.TemplateResponse(
            request=request, name="partials/screener_rows.html", context=context
        )
    return templates.TemplateResponse(request=request, name="screener.html", context=context)


@router.get("/screener")
async def screener_form(request: Request):
    return await _handle_screener(request)


@router.post("/screener")
async def screener_submit(request: Request):
    return await _handle_screener(request)
