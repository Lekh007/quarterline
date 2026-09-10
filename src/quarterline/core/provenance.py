"""Fact cards and metric provenance (contracts C6/C7, SPEC 2.1.8, 10.5).

``build_fact_card`` assembles, for one company-quarter, the normalized facts
plus all derived metrics allowed by ``core.models.METRIC_IDS``, each carrying
inspectable provenance (observation ids or derived-from lineage plus a
formula version). ``build_fact_cards`` returns the latest eight complete
fiscal quarters (SPEC 10.5 retention window) and ``build_latest_annual_card``
the latest annual period.

``explain_metric`` produces the full lineage report for the provenance viewer:
which accession/form/filed_at each contributing observation came from, whether
the value is direct, derived or computed, and the formula text.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from quarterline.core.models import (
    FactCard,
    MetricProvenance,
    MetricStatus,
    MetricValue,
    PeriodKind,
)
from quarterline.core.normalization import NORMALIZATION_VERSION
from quarterline.core.periods import FY_QUARTERS
from quarterline.core.quality import COVERAGE_DERIVED, COVERAGE_DIRECT, quarter_coverage
from quarterline.core.quarter_label import (
    QuarterLabelResult,
    compute_fundamental_score,
    compute_quarter_label,
    evaluate_signals,
)
from quarterline.core.ratios import (
    FORMULA_VERSION_RATIOS,
    UNIT_RATIO,
    capex_outflow_metric,
    cfo_to_net_income,
    current_ratio,
    free_cash_flow,
    long_term_net_debt_proxy,
    margin,
    margin_change_pp,
    yoy_growth,
)
from quarterline.store.models import Company, NormalizedFact, text_to_decimal
from quarterline.store.repositories.companies import CompaniesRepo
from quarterline.store.repositories.facts import (
    ROLE_DIRECT_SOURCE,
    FactsRepo,
    QuarterRef,
)

#: Displayed disclaimer attached to every quarter label (SPEC 12.1).
QUARTER_LABEL_DISCLAIMER = (
    "Rule-based quarterly performance label. Not a recommendation or forecast."
)

#: Human-readable formula text for the provenance viewer.
FORMULA_TEXT: dict[str, str] = {
    "revenue_yoy": "revenue_yoy = current_quarter_revenue / prior_year_same_fiscal_quarter_revenue - 1",
    "shares_yoy": "shares_yoy = current_quarter_diluted_shares / prior_year_same_fiscal_quarter_diluted_shares - 1",
    "gross_margin": "gross_margin = gross_profit / revenue",
    "operating_margin": "operating_margin = operating_income / revenue",
    "operating_margin_change_pp": "operating_margin_change_pp = (operating_margin - prior_year_operating_margin) * 100",
    "net_margin": "net_margin = net_income / revenue",
    "fcf": "fcf = cfo - capex_outflow (capex reported negative; presented as positive outflow magnitude)",
    "fcf_margin": "fcf_margin = fcf / revenue",
    "current_ratio": "current_ratio = current_assets / current_liabilities",
    "long_term_net_debt_proxy": "long_term_net_debt_proxy = long_term_debt - cash (proxy, not total net debt)",
    "cfo_to_net_income": "cfo_to_net_income = cfo / net_income (valid only when net income > 0)",
    "quarter_label": "SPEC 12.1 signals + precedence (veto, availability, true/false counts)",
    "fundamental_score": "SPEC 12.2: weighted components scale(x, low, high), rescaled over available weights",
    "data_coverage": "available core concepts (direct or derived) / total core concepts",
}

#: Flow concepts shown as reported fact metrics on the card.
FLOW_FACT_METRICS = (
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "diluted_eps",
    "shares_diluted",
    "cfo",
)
#: Instant concepts shown as reported fact metrics. ``current_assets`` /
#: ``current_liabilities`` are also instant concepts but are NOT card metrics
#: (not in the C7 allowlist); they feed ``current_ratio`` and coverage only.
INSTANT_FACT_METRICS = (
    "cash",
    "total_assets",
    "total_liabilities",
    "long_term_debt",
)
#: Instant concepts consumed by ratios / data coverage but not card metrics.
INSTANT_INPUT_CONCEPTS = ("current_assets", "current_liabilities")

#: Numeric metrics persisted into ``derived_metrics`` (categorical labels stay
#: on the fact card only; the label is not a decimal value).
PERSISTED_METRIC_IDS = (
    "revenue_yoy",
    "shares_yoy",
    "gross_margin",
    "operating_margin",
    "operating_margin_change_pp",
    "net_margin",
    "fcf",
    "fcf_margin",
    "current_ratio",
    "long_term_net_debt_proxy",
    "cfo_to_net_income",
    "fundamental_score",
    "data_coverage",
)


class ProvenanceSource(BaseModel):
    """One contributing observation in a provenance report."""

    observation_id: int
    accession: str | None
    form: str | None
    filed_at: date | None
    original_tag: str
    canonical_concept: str
    unit: str | None
    value: str | None
    role: str
    direct: bool


class ProvenanceReport(BaseModel):
    """Full lineage for one metric of one company-period (C6/C7 viewer data)."""

    ticker: str
    cik: str
    metric_id: str
    period_end: date
    period_kind: str | None = None
    value: Decimal | None = None
    unit: str | None = None
    status: str
    derivation: str  # direct | derived | computed | missing
    formula_version: str | None = None
    formula_text: str | None = None
    sources: list[ProvenanceSource] = Field(default_factory=list)
    input_concepts: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class _QuarterData:
    """Facts and derived MetricValues for one quarter, plus input fact ids."""

    def __init__(self) -> None:
        self.metrics: list[MetricValue] = []
        self.input_fact_ids: dict[str, list[int]] = {}
        self.label_result: QuarterLabelResult | None = None


def _metric(
    metric_id: str,
    value: Decimal | None,
    unit: str | None,
    status: MetricStatus,
    fact: NormalizedFact | None,
    observation_ids: list[int],
    derived_from: list[str],
    formula_version: str,
    notes: list[str] | None = None,
) -> MetricValue:
    return MetricValue(
        metric_id=metric_id,
        value=value,
        unit=unit,
        status=status,
        provenance=MetricProvenance(
            observation_ids=observation_ids,
            derived_from=derived_from,
            formula_version=formula_version,
        ),
        notes=notes or [],
    )


def _load_quarter_facts(
    repo: FactsRepo,
    company_id: int,
    quarter: QuarterRef,
    as_of: date | None,
) -> dict[str, NormalizedFact]:
    return repo.facts_for_quarter(
        company_id,
        quarter.period_end,
        kinds=("quarter", "instant"),
        as_of=as_of,
    )


def _lineage_sources(
    repo: FactsRepo, fact: NormalizedFact | None
) -> tuple[list[int], list[str], list[tuple[object, str]]]:
    """Observation ids, derived-from strings and (observation, role) pairs."""
    if fact is None:
        return [], [], []
    pairs = repo.lineage_for_fact(fact.id)
    observation_ids = [obs.id for obs, _lin in pairs if obs.id is not None]
    derived_from = [f"{lin.role}:observation:{obs.id}" for obs, lin in pairs]
    return observation_ids, derived_from, pairs


def build_quarter_data(
    repo: FactsRepo,
    company: Company,
    quarter: QuarterRef,
    all_quarters: list[QuarterRef],
    as_of: date | None,
    *,
    is_quarter: bool,
) -> _QuarterData:
    """Compute every allowlisted metric for one company-period."""
    data = _QuarterData()
    cid = company.id
    facts = _load_quarter_facts(repo, cid, quarter, as_of)

    def available_status(concept: str) -> str | None:
        fact = facts.get(concept)
        if fact is None:
            return None
        return COVERAGE_DERIVED if fact.is_derived else COVERAGE_DIRECT

    def value_of(concept: str) -> Decimal | None:
        fact = facts.get(concept)
        return text_to_decimal(fact.value_decimal) if fact is not None else None

    def add_fact_metric(concept: str) -> None:
        fact = facts.get(concept)
        obs_ids, derived_from, _pairs = _lineage_sources(repo, fact)
        if fact is None:
            data.metrics.append(
                _metric(
                    concept,
                    None,
                    None,
                    MetricStatus.missing,
                    None,
                    [],
                    [],
                    NORMALIZATION_VERSION,
                    notes=[f"concept {concept!r} missing for this period; missing stays missing"],
                )
            )
            return
        unit = fact.unit
        notes = []
        if fact.is_derived:
            notes.append(
                f"derived by subtraction ({fact.derivation_method}); both inputs preserved in lineage"
            )
        data.metrics.append(
            _metric(
                concept,
                text_to_decimal(fact.value_decimal),
                unit,
                MetricStatus.ok,
                fact,
                obs_ids,
                derived_from,
                NORMALIZATION_VERSION,
                notes=notes,
            )
        )
        data.input_fact_ids[concept] = [fact.id]

    # -- reported facts ------------------------------------------------------
    for concept in FLOW_FACT_METRICS:
        add_fact_metric(concept)

    # capex: preserve reported sign upstream, present positive outflow magnitude
    reported_capex = value_of("capex_outflow")
    capex_metric = capex_outflow_metric(reported_capex)
    capex_fact = facts.get("capex_outflow")
    obs_ids, derived_from, _pairs = _lineage_sources(repo, capex_fact)
    capex_metric.provenance.observation_ids = obs_ids
    capex_metric.provenance.derived_from = derived_from + capex_metric.provenance.derived_from
    data.metrics.append(capex_metric)
    data.input_fact_ids["capex_outflow"] = [capex_fact.id] if capex_fact else []

    for concept in INSTANT_FACT_METRICS:
        add_fact_metric(concept)

    revenue = value_of("revenue")
    gross_profit = value_of("gross_profit")
    operating_income = value_of("operating_income")
    net_income = value_of("net_income")
    cfo = value_of("cfo")
    current_assets = value_of("current_assets")
    current_liabilities = value_of("current_liabilities")
    long_term_debt = value_of("long_term_debt")
    cash = value_of("cash")
    # prior-year matching fiscal quarter (never an arbitrary row offset)
    prior_year_quarter = next(
        (
            q
            for q in all_quarters
            if q.fiscal_quarter == quarter.fiscal_quarter
            and q.fiscal_year is not None
            and quarter.fiscal_year is not None
            and q.fiscal_year == quarter.fiscal_year - 1
        ),
        None,
    )
    py_facts = (
        _load_quarter_facts(repo, cid, prior_year_quarter, as_of) if prior_year_quarter else {}
    )

    def py_value(concept: str) -> Decimal | None:
        fact = py_facts.get(concept)
        return text_to_decimal(fact.value_decimal) if fact is not None else None

    # -- computed ratios (SPEC 11) --------------------------------------------
    gross_margin_metric = margin("gross_margin", gross_profit, revenue)
    operating_margin_metric = margin("operating_margin", operating_income, revenue)
    net_margin_metric = margin("net_margin", net_income, revenue)
    fcf_metric = free_cash_flow(cfo, reported_capex)
    fcf_margin_metric = margin("fcf_margin", fcf_metric.value, revenue)
    current_ratio_metric = current_ratio(current_assets, current_liabilities)
    debt_proxy_metric = long_term_net_debt_proxy(long_term_debt, cash)
    cash_conversion_metric = cfo_to_net_income(cfo, net_income)

    revenue_py = py_value("revenue")
    shares = value_of("shares_diluted")
    shares_py = py_value("shares_diluted")
    operating_margin_py = (
        margin("operating_margin", py_value("operating_income"), revenue_py).value
        if "operating_income" in py_facts or "revenue" in py_facts
        else None
    )
    revenue_yoy_metric = yoy_growth("revenue_yoy", "revenue", revenue, revenue_py, UNIT_RATIO)
    shares_yoy_metric = yoy_growth("shares_yoy", "shares_diluted", shares, shares_py, UNIT_RATIO)
    op_margin_change_metric = margin_change_pp(
        "operating_margin_change_pp",
        "operating_margin",
        operating_margin_metric.value,
        operating_margin_py,
    )

    for metric in (
        gross_margin_metric,
        operating_margin_metric,
        net_margin_metric,
        fcf_metric,
        fcf_margin_metric,
        current_ratio_metric,
        debt_proxy_metric,
        cash_conversion_metric,
        revenue_yoy_metric,
        shares_yoy_metric,
        op_margin_change_metric,
    ):
        data.metrics.append(metric)

    # -- data coverage ---------------------------------------------------------
    availability = {
        concept: status
        for concept in (
            "revenue",
            "gross_profit",
            "operating_income",
            "net_income",
            "diluted_eps",
            "shares_diluted",
            "cfo",
            "capex_outflow",
            "cash",
            "total_assets",
            "total_liabilities",
            "long_term_debt",
            "current_assets",
            "current_liabilities",
        )
        if (status := available_status(concept)) is not None
    }
    coverage = quarter_coverage(availability)
    data.metrics.append(
        _metric(
            "data_coverage",
            coverage.fraction,
            UNIT_RATIO,
            MetricStatus.ok,
            None,
            [],
            [f"fact:{concept}" for concept in availability],
            FORMULA_VERSION_RATIOS,
            notes=[f"{coverage.available_count}/{coverage.total_concepts} core concepts available"]
            + ([f"derived: {coverage.derived_concepts}"] if coverage.derived_concepts else [])
            + ([f"missing: {coverage.missing_concepts}"] if coverage.missing_concepts else []),
        )
    )

    # -- quarter label (SPEC 12.1) and experimental score (SPEC 12.2) ----------
    if is_quarter:
        # The veto needs the immediately preceding fiscal quarter's cash conversion.
        prior_quarter = _prior_fiscal_quarter(quarter, all_quarters)
        pq_facts = _load_quarter_facts(repo, cid, prior_quarter, as_of) if prior_quarter else {}
        prior_cash = cfo_to_net_income(
            text_to_decimal(pq_facts["cfo"].value_decimal) if "cfo" in pq_facts else None,
            text_to_decimal(pq_facts["net_income"].value_decimal)
            if "net_income" in pq_facts
            else None,
        )
        signals = evaluate_signals(
            revenue_yoy=revenue_yoy_metric.value,
            revenue_yoy_valid=revenue_yoy_metric.status is MetricStatus.ok,
            operating_margin_change_pp=op_margin_change_metric.value,
            cfo_to_net_income=cash_conversion_metric.value,
            cfo_to_net_income_valid=cash_conversion_metric.status is MetricStatus.ok,
            fcf=fcf_metric.value,
            shares_yoy=shares_yoy_metric.value,
            shares_yoy_valid=shares_yoy_metric.status is MetricStatus.ok,
        )
        label_result = compute_quarter_label(
            signals,
            cfo_to_net_income_current=(
                cash_conversion_metric.value
                if cash_conversion_metric.status is MetricStatus.ok
                else None
            ),
            cfo_to_net_income_prior_quarter=(
                prior_cash.value if prior_cash.status is MetricStatus.ok else None
            ),
        )
        data.label_result = label_result
        data.metrics.append(
            _metric(
                "quarter_label",
                None,
                None,
                MetricStatus.ok,
                None,
                [],
                [f"label:{label_result.label.value}", f"rule:{label_result.rule_applied}"],
                label_result.formula_version,
                notes=[QUARTER_LABEL_DISCLAIMER]
                + [f"{s.signal_id}: {s.state.value} ({s.detail})" for s in label_result.signals]
                + [f"rule applied: {label_result.rule_applied}"]
                + label_result.notes,
            )
        )

    score_result = compute_fundamental_score(
        operating_margin=operating_margin_metric.value,
        cfo_to_net_income=cash_conversion_metric.value,
        cfo_to_net_income_valid=cash_conversion_metric.status is MetricStatus.ok,
        revenue_yoy=revenue_yoy_metric.value,
        revenue_yoy_valid=revenue_yoy_metric.status is MetricStatus.ok,
        current_ratio=current_ratio_metric.value,
        fcf=fcf_metric.value,
        revenue=revenue,
        shares_yoy=shares_yoy_metric.value,
        shares_yoy_valid=shares_yoy_metric.status is MetricStatus.ok,
    )
    data.metrics.append(
        _metric(
            "fundamental_score",
            score_result.score,
            "score",
            MetricStatus.ok if score_result.status == "ok" else MetricStatus.missing,
            None,
            [],
            [f"available_weight:{score_result.available_weight}/{score_result.total_weight}"],
            score_result.formula_version,
            notes=score_result.notes
            + [
                f"{c.name}: {c.value if c.value is not None else 'n/a'} ({c.detail})"
                for c in score_result.components
            ],
        )
    )
    return data


def _prior_fiscal_quarter(quarter: QuarterRef, all_quarters: list[QuarterRef]) -> QuarterRef | None:
    """The fiscal quarter immediately before ``quarter`` (for the label veto)."""
    if quarter.fiscal_quarter not in FY_QUARTERS:
        return None
    index = FY_QUARTERS.index(quarter.fiscal_quarter)
    if index == 0:
        target_fy = quarter.fiscal_year - 1 if quarter.fiscal_year is not None else None
        target_label = "Q4"
    else:
        target_fy = quarter.fiscal_year
        target_label = FY_QUARTERS[index - 1]
    return next(
        (
            q
            for q in all_quarters
            if q.fiscal_quarter == target_label
            and target_fy is not None
            and q.fiscal_year == target_fy
        ),
        None,
    )


def _select_quarters(
    quarters: list[QuarterRef], period_end: date | None, n_quarters: int
) -> list[QuarterRef]:
    """Latest ``n_quarters`` quarters, or the one matching ``period_end``."""
    ordered = sorted(quarters, key=lambda q: q.period_end)
    if period_end is not None:
        return [q for q in ordered if q.period_end == period_end]
    return ordered[-n_quarters:]


def build_fact_card(
    session: Session,
    ticker: str,
    period_end: date | None = None,
    as_of: date | None = None,
) -> FactCard:
    """Fact card for one fiscal quarter (latest when ``period_end`` is None)."""
    cards = build_fact_cards(session, ticker, period_end=period_end, as_of=as_of, n_quarters=1)
    if not cards:
        raise LookupError(
            f"no normalized quarters for ticker {ticker!r}"
            + (f" at period_end {period_end}" if period_end else "")
        )
    return cards[0]


def build_fact_cards(
    session: Session,
    ticker: str,
    period_end: date | None = None,
    as_of: date | None = None,
    n_quarters: int = 8,
) -> list[FactCard]:
    """Fact cards for the latest eight complete fiscal quarters (SPEC 10.5)."""
    company = CompaniesRepo(session).get_by_ticker(ticker)
    if company is None:
        raise LookupError(f"unknown ticker {ticker!r}")
    repo = FactsRepo(session)
    quarters = repo.quarters_available(company.id)
    selected = _select_quarters(quarters, period_end, n_quarters)
    cards: list[FactCard] = []
    for quarter in selected:
        data = build_quarter_data(repo, company, quarter, quarters, as_of, is_quarter=True)
        currency = None
        revenue_fact = repo.facts_for_quarter(
            company.id, quarter.period_end, kinds=("quarter",)
        ).get("revenue")
        if revenue_fact is not None and revenue_fact.unit:
            currency = revenue_fact.unit
        elif company.reporting_currency:
            currency = company.reporting_currency
        cards.append(
            FactCard(
                ticker=company.ticker,
                cik=company.cik,
                period_start=quarter.period_start,
                period_end=quarter.period_end,
                fiscal_year=quarter.fiscal_year,
                fiscal_quarter=quarter.fiscal_quarter,
                currency=currency,
                metrics=data.metrics,
                generated_at=datetime.now(UTC),
            )
        )
    return cards


def build_latest_annual_card(
    session: Session,
    ticker: str,
    as_of: date | None = None,
) -> FactCard | None:
    """Fact card for the latest annual period (SPEC 10.5: latest annual)."""
    from sqlalchemy import select

    company = CompaniesRepo(session).get_by_ticker(ticker)
    if company is None:
        raise LookupError(f"unknown ticker {ticker!r}")
    repo = FactsRepo(session)
    annual_end = session.scalar(
        select(NormalizedFact.period_end)
        .where(
            NormalizedFact.company_id == company.id,
            NormalizedFact.period_kind == PeriodKind.annual.value,
        )
        .order_by(NormalizedFact.period_end.desc())
        .limit(1)
    )
    if annual_end is None:
        return None
    facts = repo.facts_for_quarter(company.id, annual_end, kinds=("annual",), as_of=as_of)
    metrics: list[MetricValue] = []
    for concept in FLOW_FACT_METRICS:
        fact = facts.get(concept)
        obs_ids, derived_from, _p = _lineage_sources(repo, fact)
        metrics.append(
            _metric(
                concept,
                text_to_decimal(fact.value_decimal) if fact else None,
                fact.unit if fact else None,
                MetricStatus.ok if fact else MetricStatus.missing,
                fact,
                obs_ids,
                derived_from,
                NORMALIZATION_VERSION,
                notes=["annual period"] if fact else [f"concept {concept!r} missing"],
            )
        )
    revenue = text_to_decimal(facts["revenue"].value_decimal) if "revenue" in facts else None
    metrics.append(
        free_cash_flow(
            text_to_decimal(facts["cfo"].value_decimal) if "cfo" in facts else None,
            text_to_decimal(facts["capex_outflow"].value_decimal)
            if "capex_outflow" in facts
            else None,
        )
    )
    for metric_id, numerator in (
        ("gross_margin", "gross_profit"),
        ("operating_margin", "operating_income"),
        ("net_margin", "net_income"),
    ):
        numerator_value = (
            text_to_decimal(facts[numerator].value_decimal) if numerator in facts else None
        )
        metrics.append(margin(metric_id, numerator_value, revenue))
    metrics.append(
        current_ratio(
            text_to_decimal(facts["current_assets"].value_decimal)
            if "current_assets" in facts
            else None,
            text_to_decimal(facts["current_liabilities"].value_decimal)
            if "current_liabilities" in facts
            else None,
        )
    )
    annual = next(iter(facts.values()), None)
    return FactCard(
        ticker=company.ticker,
        cik=company.cik,
        period_start=annual.period_start if annual else None,
        period_end=annual_end,
        fiscal_year=annual.fiscal_year if annual else None,
        fiscal_quarter="FY",
        currency=facts["revenue"].unit if "revenue" in facts else company.reporting_currency,
        metrics=metrics,
        generated_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Provenance viewer (explain_metric)
# ---------------------------------------------------------------------------


def explain_metric(
    session: Session,
    ticker: str,
    metric_id: str,
    period_end: date,
    as_of: date | None = None,
) -> ProvenanceReport:
    """Full lineage for one metric: accessions, filing dates, direct/derived."""

    company = CompaniesRepo(session).get_by_ticker(ticker)
    if company is None:
        raise LookupError(f"unknown ticker {ticker!r}")
    repo = FactsRepo(session)
    quarter = next(
        (q for q in repo.quarters_available(company.id) if q.period_end == period_end),
        None,
    )
    if quarter is None:
        raise LookupError(f"no quarter with period_end {period_end} for {ticker!r}")

    facts = _load_quarter_facts(repo, company.id, quarter, as_of)
    data = build_quarter_data(
        repo, company, quarter, repo.quarters_available(company.id), as_of, is_quarter=True
    )
    metric = next((m for m in data.metrics if m.metric_id == metric_id), None)
    if metric is None:
        raise LookupError(f"metric {metric_id!r} is not part of the fact card")

    sources: list[ProvenanceSource] = []
    input_concepts: list[str] = []
    derivation = "computed"

    fact = facts.get(metric_id)
    if fact is not None and metric.provenance.observation_ids:
        derivation = "derived" if fact.is_derived else "direct"
        for obs, lin in repo.lineage_for_fact(fact.id):
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
                    role=lin.role,
                    direct=lin.role == ROLE_DIRECT_SOURCE,
                )
            )
    else:
        # Computed metric: resolve its input concepts and their observations.
        input_concepts = [
            ref.split(":")[0]
            for ref in metric.provenance.derived_from
            if not ref.startswith(("label:", "rule:", "available_weight:"))
        ]
        for concept in list(dict.fromkeys(input_concepts)):
            input_fact = facts.get(concept)
            if input_fact is None:
                # inputs may live on the prior-year quarter (growth metrics)
                prior_year = next(
                    (
                        q
                        for q in repo.quarters_available(company.id)
                        if q.fiscal_quarter == quarter.fiscal_quarter
                        and q.fiscal_year is not None
                        and quarter.fiscal_year is not None
                        and q.fiscal_year == quarter.fiscal_year - 1
                    ),
                    None,
                )
                if prior_year is not None:
                    input_fact = _load_quarter_facts(repo, company.id, prior_year, as_of).get(
                        concept
                    )
            if input_fact is None:
                continue
            for obs, lin in repo.lineage_for_fact(input_fact.id):
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
                        role=f"{lin.role}:{concept}",
                        direct=lin.role == ROLE_DIRECT_SOURCE,
                    )
                )
    if derivation == "computed" and not sources and metric.status is MetricStatus.missing:
        derivation = "missing"

    period_kind = None
    if fact is not None:
        period_kind = fact.period_kind
    return ProvenanceReport(
        ticker=company.ticker,
        cik=company.cik,
        metric_id=metric_id,
        period_end=period_end,
        period_kind=period_kind,
        value=metric.value,
        unit=metric.unit,
        status=metric.status.value,
        derivation=derivation,
        formula_version=metric.provenance.formula_version,
        formula_text=FORMULA_TEXT.get(metric_id),
        sources=sources,
        input_concepts=input_concepts,
        notes=metric.notes,
    )
