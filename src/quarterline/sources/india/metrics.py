"""India metric definitions, ``india-metrics-v1`` (IND-4). Decimal only.

Every metric is formula-registered (``INDIA_METRIC_IDS``), versioned
(``FORMULA_VERSION_INDIA_METRICS``), computed from canonical India facts
(``normalized_facts`` written by ``normalization.normalize_canonical_facts``),
and returns an :class:`IndiaMetricResult` carrying

``{metric_id, value, unit, formula_version, status, input_fact_ids, notes}``

Metric ids are prefixed ``india_`` and never collide with the US ``METRIC_IDS``
allowlist (asserted in tests). Persisted rows (``derived_metrics``) are keyed by
(company, period_end, metric, formula_version); because an annual identity and a
Q4 quarter identity can SHARE a period_end (31 March), non-quarter rows are
persisted under a period-kind suffix (``@annual`` / ``@ytd`` / ``@other``) so
the two never overwrite each other — the store-key analogue of the facts'
period-identity rule.

Formulas (exact numerator/denominator; growth per US SPEC 11.2 conventions):

- ``india_revenue_yoy`` = revenue_from_operations(current quarter) /
  revenue_from_operations(matching prior-year quarter) − 1, from ingested facts
  ONLY. Prior-year quarters are not ingested for either issuer, so the metric
  reports its typed missing status with the explanation — never a fabricated
  comparative.
- ``india_revenue_qoq`` = revenue_from_operations(current quarter) /
  revenue_from_operations(immediately preceding fiscal quarter) − 1, when both
  exist (Q4 FY26 → Q1 FY27 works).
- ``india_pat_margin_owners`` = profit_attributable_to_owners /
  revenue_from_operations (NEVER total_income; the group PAT margin is the
  separate ``india_pat_margin_group`` = profit_after_tax /
  revenue_from_operations, so owners' and group margins are never conflated).
- ``india_exceptional_impact_pbt`` = exceptional impact on profit before tax,
  from the canonical ``exceptional_items`` and ``profit_before_tax`` facts; when
  the instance also reports the before-exceptional variant
  (``ProfitBeforeExceptionalItemsAndTax``) the identity impact == PBT − PBIT is
  cross-checked and never forced. ``value`` = impact in INR; the share of PBT
  travels in notes. Source sign semantics are PRESERVED per issuer: INFY FY26
  stores −1,289 Cr (an expense reducing PBT; the rendered statement shows it as
  a positive 1,289 deducted from PBIT), HUL Q4 FY26 stores +247 Cr (a demerger
  gain increasing PBT). Signs are never normalized across issuers.
- ``india_eps_growth_yoy`` = eps_diluted(current quarter) /
  eps_diluted(matching prior-year quarter) − 1, only where the prior-year fact
  is an ingested observation — the SAME rule as revenue YoY, applied
  consistently to both issuers (no computing one issuer's YoY while silently
  reporting the other's as equivalent without its missing status).

Missing-data semantics: a metric whose inputs are absent returns
``status="missing"`` with ``value=None`` and a note stating what is absent and
what would be needed. Zero/negative denominators follow the US convention:
``value=None``, ``status="invalid"`` (zero) / ``"unsuitable"`` (negative), with
a note. No aggregate score exists anywhere — IND-5 renders indicators only.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from pydantic import BaseModel, Field
from sqlalchemy import select

from quarterline.sources.india.issuers import require_verified
from quarterline.sources.india.normalization import (
    DQ_REQUIRES_MANUAL_REVIEW,
    NORMALIZATION_VERSION,
)
from quarterline.sources.india.periods import (
    KIND_ANNUAL,
    KIND_QUARTER,
    prior_year_quarter,
)
from quarterline.store.db import session_scope
from quarterline.store.models import FactObservation, NormalizedFact, text_to_decimal
from quarterline.store.repositories.companies import CompaniesRepo
from quarterline.store.repositories.facts import FactsRepo

#: Bumped when any formula below changes; persisted on every derived_metrics row.
FORMULA_VERSION_INDIA_METRICS = "india-metrics-v1"

#: Registered India metric ids (mission-defined set; never in US METRIC_IDS).
INDIA_METRIC_IDS: tuple[str, ...] = (
    "india_revenue_yoy",
    "india_revenue_qoq",
    "india_pat_margin_owners",
    "india_pat_margin_group",
    "india_exceptional_impact_pbt",
    "india_eps_growth_yoy",
)

UNIT_RATIO = "ratio"
UNIT_INR = "INR"

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_INVALID = "invalid"
STATUS_UNSUITABLE = "unsuitable"

#: The before-exceptional P&L variant (tagmap fallback observation), used ONLY
#: as an identity cross-check / fallback for the exceptional-impact metric.
_PBIT_TAG = "ProfitBeforeExceptionalItemsAndTax"

#: Store-key suffixes so period identities sharing a period_end (e.g. the Q4
#: quarter and the annual, both ending 31 March) never overwrite each other in
#: derived_metrics (which keys on period_end alone).
_PERIOD_KIND_SUFFIX = {
    KIND_QUARTER: "",
    KIND_ANNUAL: "@annual",
    "year_to_date": "@ytd",
    "other_duration": "@other",
}

#: Shared sign-semantics note (source conventions per issuer, IND-3 §6).
_SIGN_SEMANTICS_NOTE = (
    "stored sign is the PROFIT IMPACT (XBRL convention): negative = expense/loss "
    "reducing PBT, positive = gain increasing PBT; rendered presentation differs "
    "by issuer and is never normalized — INFY prints a negative impact as a "
    "positive expense deducted from 'Profit before exceptional item and tax' "
    "(41,284 - 1,289 = 39,995), HUL prints gains/losses with the same sign as "
    "stored (FY26 'a loss of Rs. 235 crores' = -235 Cr; Q4 +247 Cr demerger gain)"
)


def store_metric_id(metric_id: str, period_kind: str) -> str:
    """Persisted metric id: quarter metrics keep the plain id; other period
    kinds carry a kind suffix (``india_pat_margin_owners@annual``)."""
    return f"{metric_id}{_PERIOD_KIND_SUFFIX.get(period_kind, '@other')}"


class IndiaMetricResult(BaseModel):
    """One computed (or typed-missing) India metric value."""

    metric_id: str
    value: Decimal | None = None
    unit: str | None = None
    formula_version: str = FORMULA_VERSION_INDIA_METRICS
    status: str = STATUS_MISSING
    input_fact_ids: list[int] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    period_start: date | None = None
    period_end: date | None = None
    period_kind: str | None = None

    @property
    def persisted_metric_id(self) -> str:
        """Id under which this result is stored (kind-suffixed, never colliding)."""
        return store_metric_id(self.metric_id, self.period_kind or "")


class _FactView(NamedTuple):
    """The inputs a metric may consume from one canonical fact."""

    fact_id: int
    concept: str
    value: Decimal | None
    period_start: date | None
    period_end: date
    period_kind: str
    data_quality_status: str | None


def _view(fact: NormalizedFact) -> _FactView:
    return _FactView(
        fact_id=fact.id,
        concept=fact.concept,
        value=text_to_decimal(fact.value_decimal),
        period_start=fact.period_start,
        period_end=fact.period_end,
        period_kind=fact.period_kind,
        data_quality_status=fact.data_quality_status,
    )


def _review_note(view: _FactView) -> list[str]:
    if view.data_quality_status == DQ_REQUIRES_MANUAL_REVIEW:
        return [
            (
                f"input fact {view.concept} (fact id {view.fact_id}) carries "
                f"data_quality_status={DQ_REQUIRES_MANUAL_REVIEW} "
                "(IND-3 human-review-pending; value used as ingested, never resolved)"
            )
        ]
    return []


def _quarter_months_earlier(start: date, end: date, months: int) -> tuple[date, date]:
    """The fiscal quarter ``months`` earlier (calendar-identity shift).

    Q1 FY27 (2026-04-01..2026-06-30) minus 3 months -> Q4 FY26
    (2026-01-01..2026-03-31). Indian fiscal quarter identities start on day 1
    and end on the LAST day of their closing month, so the shifted start keeps
    day 1 and the shifted end takes the shifted month's last day (a plain
    day-of-month shift would turn Jun 30 into Mar 30).
    """

    def month_index(value: date, shift: int) -> tuple[int, int]:
        index = value.year * 12 + (value.month - 1) - shift
        return index // 12, index % 12 + 1

    start_year, start_month = month_index(start, months)
    shifted_start = date(
        start_year, start_month, min(start.day, calendar.monthrange(start_year, start_month)[1])
    )
    end_year, end_month = month_index(end, months)
    shifted_end = date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])
    return shifted_start, shifted_end


# ---------------------------------------------------------------------------
# Growth metrics (SPEC 11.2 conventions: Decimal only; positive prior required)
# ---------------------------------------------------------------------------

_GROWTH_UNIT = UNIT_RATIO


def _growth_metric(
    metric_id: str,
    identity: tuple[date | None, date, str],
    current: _FactView | None,
    prior: _FactView | None,
    *,
    missing_current_note: str,
    missing_prior_note: str,
) -> IndiaMetricResult:
    start, end, kind = identity

    def result(value, status, notes, input_ids):
        return IndiaMetricResult(
            metric_id=metric_id,
            value=value,
            unit=_GROWTH_UNIT,
            formula_version=FORMULA_VERSION_INDIA_METRICS,
            status=status,
            input_fact_ids=input_ids,
            notes=notes,
            period_start=start,
            period_end=end,
            period_kind=kind,
        )

    if current is None:
        return result(None, STATUS_MISSING, [missing_current_note], [])
    refs = [current.fact_id] + ([prior.fact_id] if prior else [])
    if prior is None:
        return result(None, STATUS_MISSING, [missing_prior_note], refs)
    if prior.value is None or current.value is None:
        return result(None, STATUS_MISSING, ["input fact has no numeric value"], refs)
    notes = [
        (
            f"current {current.period_start}..{current.period_end} = {current.value}; "
            f"prior {prior.period_start}..{prior.period_end} = {prior.value}"
        )
    ]
    notes.extend(_review_note(current))
    notes.extend(_review_note(prior))
    if prior.value < Decimal(0):
        return result(
            None,
            STATUS_UNSUITABLE,
            notes
            + [
                "negative prior value; percentage growth not meaningful",
                f"absolute change: {current.value - prior.value}",
            ],
            refs,
        )
    if prior.value == Decimal(0):
        return result(
            None,
            STATUS_INVALID,
            notes
            + [
                "zero prior value; percentage growth undefined",
                f"absolute change: {current.value - prior.value}",
            ],
            refs,
        )
    return result(
        current.value / prior.value - Decimal(1),
        STATUS_OK,
        notes,
        refs,
    )


def revenue_yoy_metric(
    identity: tuple[date | None, date, str],
    current: _FactView | None,
    prior: _FactView | None,
) -> IndiaMetricResult:
    """``india_revenue_yoy`` — ingested facts only; an un-ingested prior year is
    a typed missing status with an explanation, never a fabricated comparative."""
    return _growth_metric(
        "india_revenue_yoy",
        identity,
        current,
        prior,
        missing_current_note=(
            "current-quarter revenue_from_operations fact not found among the "
            "issuer's canonical facts"
        ),
        missing_prior_note=(
            "matching prior-year quarter not present in ingested sources: the "
            "prior-year consolidated observation is not ingested (exchange "
            "instances carry no prior-year duration contexts) and is available "
            "only in the issuer's IR PDF comparative column — ingest it as an "
            "observation with pdf_text provenance to enable this metric"
        ),
    )


def revenue_qoq_metric(
    identity: tuple[date | None, date, str],
    current: _FactView | None,
    prior: _FactView | None,
) -> IndiaMetricResult:
    """``india_revenue_qoq`` — vs the immediately preceding fiscal quarter."""
    return _growth_metric(
        "india_revenue_qoq",
        identity,
        current,
        prior,
        missing_current_note=(
            "current-quarter revenue_from_operations fact not found among the "
            "issuer's canonical facts"
        ),
        missing_prior_note=(
            "immediately preceding fiscal quarter not present in ingested "
            "sources; growth is never annualized, interpolated, or annualized "
            "from incomplete coverage"
        ),
    )


def eps_growth_yoy_metric(
    identity: tuple[date | None, date, str],
    current: _FactView | None,
    prior: _FactView | None,
) -> IndiaMetricResult:
    """``india_eps_growth_yoy`` — the same ingested-facts rule as revenue YoY,
    applied consistently to every issuer."""
    return _growth_metric(
        "india_eps_growth_yoy",
        identity,
        current,
        prior,
        missing_current_note=(
            "current-quarter eps_diluted fact not found among the issuer's canonical facts"
        ),
        missing_prior_note=(
            "matching prior-year quarter not present in ingested sources: the "
            "prior-year diluted-EPS observation is not ingested (available only "
            "in the issuer's IR PDF comparative column, which would carry "
            "pdf_text provenance) — the SAME missing status is reported for "
            "every issuer until such observations are ingested"
        ),
    )


# ---------------------------------------------------------------------------
# Margins (exact numerator/denominator in the name and the notes)
# ---------------------------------------------------------------------------


def _margin_metric(
    metric_id: str,
    identity: tuple[date | None, date, str],
    numerator: _FactView | None,
    denominator: _FactView | None,
    *,
    numerator_concept: str,
    denominator_concept: str,
) -> IndiaMetricResult:
    start, end, kind = identity
    label = f"{numerator_concept} / {denominator_concept}"

    def result(value, status, notes, input_ids):
        return IndiaMetricResult(
            metric_id=metric_id,
            value=value,
            unit=UNIT_RATIO,
            formula_version=FORMULA_VERSION_INDIA_METRICS,
            status=status,
            input_fact_ids=input_ids,
            notes=notes,
            period_start=start,
            period_end=end,
            period_kind=kind,
        )

    if numerator is None or denominator is None:
        missing = [
            concept
            for concept, view in (
                (numerator_concept, numerator),
                (denominator_concept, denominator),
            )
            if view is None
        ]
        return result(None, STATUS_MISSING, [f"missing input fact(s): {', '.join(missing)}"], [])
    refs = [numerator.fact_id, denominator.fact_id]
    notes = [label]
    notes.extend(_review_note(numerator))
    notes.extend(_review_note(denominator))
    if numerator.value is None or denominator.value is None:
        return result(None, STATUS_MISSING, notes + ["input fact has no numeric value"], refs)
    if denominator.value == Decimal(0):
        return result(None, STATUS_INVALID, notes + ["zero denominator; ratio undefined"], refs)
    if denominator.value < Decimal(0):
        return result(
            None,
            STATUS_UNSUITABLE,
            notes + ["negative denominator; ratio not meaningful"],
            refs,
        )
    return result(numerator.value / denominator.value, STATUS_OK, notes, refs)


def pat_margin_owners_metric(
    identity: tuple[date | None, date, str],
    owners: _FactView | None,
    revenue: _FactView | None,
) -> IndiaMetricResult:
    """``india_pat_margin_owners`` = profit_attributable_to_owners /
    revenue_from_operations (never total_income; never group PAT)."""
    return _margin_metric(
        "india_pat_margin_owners",
        identity,
        owners,
        revenue,
        numerator_concept="profit_attributable_to_owners",
        denominator_concept="revenue_from_operations",
    )


def pat_margin_group_metric(
    identity: tuple[date | None, date, str],
    pat: _FactView | None,
    revenue: _FactView | None,
) -> IndiaMetricResult:
    """``india_pat_margin_group`` = profit_after_tax /
    revenue_from_operations — deliberately SEPARATE from the owners' margin."""
    return _margin_metric(
        "india_pat_margin_group",
        identity,
        pat,
        revenue,
        numerator_concept="profit_after_tax",
        denominator_concept="revenue_from_operations",
    )


# ---------------------------------------------------------------------------
# Exceptional impact on PBT (source sign semantics preserved per issuer)
# ---------------------------------------------------------------------------


def exceptional_impact_metric(
    identity: tuple[date | None, date, str],
    pbt: _FactView | None,
    exceptional: _FactView | None,
    pbit_observation: tuple[int, Decimal] | None = None,
) -> IndiaMetricResult:
    """``india_exceptional_impact_pbt`` — impact and its share of PBT.

    ``value`` = impact in INR (source sign preserved per issuer); the share of
    PBT and the sign-semantics note travel in ``notes``. When the instance also
    reports ``ProfitBeforeExceptionalItemsAndTax``, the identity
    impact == PBT − PBIT is cross-checked; a failed identity is ``invalid``
    (never forced). Without the canonical exceptional fact the impact may still
    be computed as PBT − PBIT from the observation, referenced in the notes.
    """
    start, end, kind = identity

    def result(value, status, notes, input_ids):
        return IndiaMetricResult(
            metric_id="india_exceptional_impact_pbt",
            value=value,
            unit=UNIT_INR,
            formula_version=FORMULA_VERSION_INDIA_METRICS,
            status=status,
            input_fact_ids=input_ids,
            notes=notes,
            period_start=start,
            period_end=end,
            period_kind=kind,
        )

    if pbt is None:
        return result(None, STATUS_MISSING, ["missing input fact(s): profit_before_tax"], [])
    refs = [pbt.fact_id] + ([exceptional.fact_id] if exceptional else [])
    notes: list[str] = ["impact = exceptional_items = profit_before_tax − PBIT"]
    notes.extend(_review_note(pbt))
    pbt_value = pbt.value
    if pbt_value is None:
        return result(None, STATUS_MISSING, notes + ["profit_before_tax fact has no value"], refs)

    if exceptional is not None and exceptional.value is not None:
        notes.extend(_review_note(exceptional))
        impact = exceptional.value
        if pbit_observation is not None:
            obs_id, pbit_value = pbit_observation
            identity_value = pbt_value - pbit_value
            if identity_value != impact:
                return result(
                    None,
                    STATUS_INVALID,
                    notes
                    + [
                        (
                            "identity check failed: exceptional_items "
                            f"({impact}) != profit_before_tax − PBIT ({identity_value}); "
                            "never forced — needs manual review"
                        ),
                        f"PBIT observation id {obs_id}",
                    ],
                    refs,
                )
            notes.append(
                f"identity verified against observation {obs_id}: "
                f"PBT {pbt_value} − PBIT {pbit_value} = {impact}"
            )
    elif pbit_observation is not None:
        obs_id, pbit_value = pbit_observation
        impact = pbt_value - pbit_value
        notes.append(
            f"computed as PBT − PBIT ({pbt_value} − {pbit_value}) from observation "
            f"{obs_id} (no canonical exceptional_items fact for this identity)"
        )
    else:
        return result(
            None,
            STATUS_MISSING,
            notes
            + [
                (
                    "no exceptional_items fact and no ProfitBeforeExceptionalItemsAndTax "
                    "observation for this period identity — impact not computable from "
                    "ingested sources"
                )
            ],
            refs,
        )

    if pbt_value > Decimal(0):
        share = impact / pbt_value
        share_note = f"share of PBT: {share} (impact / profit_before_tax = {impact} / {pbt_value})"
    elif pbt_value == Decimal(0):
        return result(
            impact, STATUS_OK, notes + ["share of PBT: zero denominator; undefined"], refs
        )
    else:
        return result(
            impact, STATUS_OK, notes + ["share of PBT: negative PBT; not meaningful"], refs
        )

    if impact < Decimal(0):
        direction = "impact reduces profit before tax as stored (negative = expense/loss)"
    elif impact > Decimal(0):
        direction = "impact increases profit before tax as stored (positive = gain)"
    else:
        direction = "exceptional items nil for this period (impact 0)"
    return result(impact, STATUS_OK, notes + [direction, share_note, _SIGN_SEMANTICS_NOTE], refs)


# ---------------------------------------------------------------------------
# Session-level computation + persistence
# ---------------------------------------------------------------------------


@dataclass
class IndiaMetricsReport:
    """Summary of one ``compute_india_metrics`` run (idempotent upserts)."""

    issuer_id: str
    normalization_version: str = NORMALIZATION_VERSION
    formula_version: str = FORMULA_VERSION_INDIA_METRICS
    metric_rows_written: int = 0
    metric_rows_created: int = 0
    by_status: dict[str, int] = field(default_factory=dict)


class _PeriodFacts(NamedTuple):
    """Canonical facts for one period identity, by concept."""

    identity: tuple  # (period_start, period_end, period_kind, scope)
    by_concept: dict[str, NormalizedFact]


def _company_id(session, issuer_id: str) -> int:
    issuer = require_verified(issuer_id)
    company = CompaniesRepo(session).get_by_ticker(issuer.ticker_nse or issuer.issuer_id)
    if company is None:
        raise LookupError(
            f"no company for {issuer.issuer_id} — import a document first "
            "(`quarterline ingest india-document`)"
        )
    return company.id


def _load_period_facts(
    session, company_id: int, scope: str, period_end: date | None = None
) -> list[_PeriodFacts]:
    stmt = (
        select(NormalizedFact)
        .where(
            NormalizedFact.company_id == company_id,
            NormalizedFact.reporting_scope == scope,
        )
        .order_by(NormalizedFact.period_end, NormalizedFact.period_kind)
    )
    if period_end is not None:
        stmt = stmt.where(NormalizedFact.period_end == period_end)
    grouped: dict[tuple, dict[str, NormalizedFact]] = {}
    for row in session.scalars(stmt):
        key = (row.period_start, row.period_end, row.period_kind, row.reporting_scope)
        grouped.setdefault(key, {})[row.concept] = row
    return [_PeriodFacts(k, v) for k, v in sorted(grouped.items(), key=lambda item: item[0][1])]


def _quarter_fact_at(
    session,
    company_id: int,
    concept: str,
    scope: str,
    period_start: date,
    period_end: date,
) -> NormalizedFact | None:
    """Quarter-kind canonical fact at an exact period identity (never an annual
    or YTD fact standing in for the quarter — SPEC 2.1.9)."""
    return session.scalar(
        select(NormalizedFact).where(
            NormalizedFact.company_id == company_id,
            NormalizedFact.concept == concept,
            NormalizedFact.reporting_scope == scope,
            NormalizedFact.period_start == period_start,
            NormalizedFact.period_end == period_end,
            NormalizedFact.period_kind == KIND_QUARTER,
        )
    )


def _prior_quarter_fact(
    session,
    company_id: int,
    concept: str,
    scope: str,
    start: date,
    end: date,
    *,
    months: int,
) -> _FactView | None:
    if months == 12:
        prior_start, prior_end = prior_year_quarter(start, end)
    else:
        prior_start, prior_end = _quarter_months_earlier(start, end, months)
    fact = _quarter_fact_at(session, company_id, concept, scope, prior_start, prior_end)
    return _view(fact) if fact else None


def _latest_pbit_observation(
    session, company_id: int, scope: str, identity: tuple
) -> tuple[int, Decimal] | None:
    start, end, kind, _ = identity
    row = session.scalar(
        select(FactObservation)
        .where(
            FactObservation.company_id == company_id,
            FactObservation.taxonomy == "in-capmkt",
            FactObservation.original_tag == _PBIT_TAG,
            FactObservation.reporting_scope == scope,
            FactObservation.period_start == start,
            FactObservation.period_end == end,
            FactObservation.period_kind == kind,
        )
        .order_by(FactObservation.id.desc())
        .limit(1)
    )
    if row is None or row.value_decimal is None:
        return None
    return row.id, text_to_decimal(row.value_decimal)


def _concept_view(facts: _PeriodFacts, concept: str) -> _FactView | None:
    fact = facts.by_concept.get(concept)
    return _view(fact) if fact else None


def build_metric_results(
    session,
    issuer_id: str,
    scope: str = "consolidated",
    period_end: date | None = None,
) -> list[IndiaMetricResult]:
    """Compute every registered metric for every (scope-filtered) period
    identity without persisting (the fact card and tests read this)."""
    company_id = _company_id(session, issuer_id)
    results: list[IndiaMetricResult] = []
    for facts in _load_period_facts(session, company_id, scope, period_end):
        start, end, kind, _scope = facts.identity
        identity = (start, end, kind)
        revenue = _concept_view(facts, "revenue_from_operations")
        if kind == KIND_QUARTER:
            results.append(
                revenue_yoy_metric(
                    identity,
                    revenue,
                    _prior_quarter_fact(
                        session, company_id, "revenue_from_operations", scope, start, end, months=12
                    ),
                )
            )
            results.append(
                revenue_qoq_metric(
                    identity,
                    revenue,
                    _prior_quarter_fact(
                        session, company_id, "revenue_from_operations", scope, start, end, months=3
                    ),
                )
            )
            results.append(
                eps_growth_yoy_metric(
                    identity,
                    _concept_view(facts, "eps_diluted"),
                    _prior_quarter_fact(
                        session, company_id, "eps_diluted", scope, start, end, months=12
                    ),
                )
            )
        results.append(
            pat_margin_owners_metric(
                identity, _concept_view(facts, "profit_attributable_to_owners"), revenue
            )
        )
        results.append(
            pat_margin_group_metric(identity, _concept_view(facts, "profit_after_tax"), revenue)
        )
        results.append(
            exceptional_impact_metric(
                identity,
                _concept_view(facts, "profit_before_tax"),
                _concept_view(facts, "exceptional_items"),
                _latest_pbit_observation(session, company_id, scope, facts.identity),
            )
        )
    return results


def compute_india_metrics(
    issuer_id: str,
    database_url: str | None = None,
    *,
    scope: str = "consolidated",
) -> IndiaMetricsReport:
    """Compute and persist every registered India metric. Idempotent upserts.

    Quarter identities persist under the plain metric id; annual and YTD
    identities persist under the kind-suffixed id (see ``store_metric_id``).
    Missing metrics persist with ``status="missing"`` and ``value=None`` — a
    typed status in the store, never a zero.
    """
    report = IndiaMetricsReport(issuer_id=issuer_id)
    with session_scope(database_url) as session:
        company_id = _company_id(session, issuer_id)
        facts_repo = FactsRepo(session)
        for result in build_metric_results(session, issuer_id, scope=scope):
            _row, created = facts_repo.upsert_derived_metric(
                company_id,
                result.period_end,
                result.persisted_metric_id,
                result.value,
                result.unit,
                result.formula_version,
                result.status,
                input_fact_ids=result.input_fact_ids,
            )
            report.metric_rows_written += 1
            if created:
                report.metric_rows_created += 1
            report.by_status[result.status] = report.by_status.get(result.status, 0) + 1
    return report


__all__ = [
    "FORMULA_VERSION_INDIA_METRICS",
    "INDIA_METRIC_IDS",
    "IndiaMetricResult",
    "IndiaMetricsReport",
    "build_metric_results",
    "compute_india_metrics",
    "eps_growth_yoy_metric",
    "exceptional_impact_metric",
    "pat_margin_group_metric",
    "pat_margin_owners_metric",
    "revenue_qoq_metric",
    "revenue_yoy_metric",
    "store_metric_id",
]
