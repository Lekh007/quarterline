"""Quarter label (SPEC 12.1) and experimental fundamental score (SPEC 12.2).

Both are deterministic, versioned heuristics over already-computed facts and
ratios. They are rule-based research signals, never recommendations or
forecasts, and every input and rule is exposed for display (SPEC 12.1: "Show
every input and rule in the UI").

Money math uses ``Decimal``; percentages are Decimals too. ``scale`` follows
SPEC 12.2 exactly: ``100 x clamp((x - low) / (high - low), 0, 1)``.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from quarterline.core.models import QuarterLabel, SignalState

FORMULA_VERSION_LABEL = "quarter-label-v1"
FORMULA_VERSION_SCORE = "fundamental-score-v1"

ZERO = Decimal(0)
ONE_HUNDRED = Decimal(100)

#: SPEC 12.1 signal identifiers (display order).
SIGNAL_IDS = (
    "revenue_yoy_positive",
    "operating_margin_change_non_negative",
    "cash_conversion_at_least_0_8",
    "fcf_positive",
    "share_dilution_at_most_2pct",
)

SIGNAL_DESCRIPTIONS = {
    "revenue_yoy_positive": "Revenue YoY > 0",
    "operating_margin_change_non_negative": "Operating-margin YoY change >= 0 percentage points",
    "cash_conversion_at_least_0_8": "CFO/net income >= 0.8 (when net income > 0)",
    "fcf_positive": "Free cash flow > 0",
    "share_dilution_at_most_2pct": "Diluted shares YoY <= 2%",
}

#: SPEC 12.1 thresholds.
CASH_CONVERSION_STRONG = Decimal("0.8")
CASH_CONVERSION_VETO = Decimal("0.4")
SHARE_DILUTION_LIMIT = Decimal("0.02")

#: SPEC 12.2 component weights.
SCORE_COMPONENT_WEIGHTS: dict[str, Decimal] = {
    "profitability": Decimal(25),
    "cash_conversion": Decimal(20),
    "growth": Decimal(20),
    "liquidity": Decimal(15),
    "capital_discipline": Decimal(10),
}
SCORE_TOTAL_WEIGHT = sum(SCORE_COMPONENT_WEIGHTS.values(), ZERO)  # 90
#: Below 60% of the defined total weight, no aggregate score is returned.
SCORE_MIN_AVAILABLE_WEIGHT = SCORE_TOTAL_WEIGHT * Decimal("0.6")  # 54


class SignalEvaluation(BaseModel):
    """One signal with its state, the inputs used, and why."""

    signal_id: str
    description: str
    state: SignalState
    detail: str


class QuarterLabelResult(BaseModel):
    """Code-generated label plus every input and the rule applied (SPEC 12.1)."""

    label: QuarterLabel
    rule_applied: str
    signals: list[SignalEvaluation]
    notes: list[str] = Field(default_factory=list)
    formula_version: str = FORMULA_VERSION_LABEL

    @property
    def available_signals(self) -> int:
        return sum(1 for s in self.signals if s.state is not SignalState.unknown)

    @property
    def true_signals(self) -> int:
        return sum(1 for s in self.signals if s.state is SignalState.true)

    @property
    def false_signals(self) -> int:
        return sum(1 for s in self.signals if s.state is SignalState.false)


class ScoreComponent(BaseModel):
    name: str
    weight: Decimal
    value: Decimal | None  # raw component score 0..100, None when unavailable
    available: bool
    detail: str


class FundamentalScoreResult(BaseModel):
    """Experimental fundamental score (SPEC 12.2). Not a validated score."""

    score: Decimal | None
    status: str  # ok | insufficient_weight | missing_inputs
    components: list[ScoreComponent]
    available_weight: Decimal
    total_weight: Decimal
    rescaled: bool
    formula_version: str = FORMULA_VERSION_SCORE
    notes: list[str] = Field(default_factory=list)


def scale(x: Decimal, low: Decimal, high: Decimal) -> Decimal:
    """SPEC 12.2: ``100 * clamp((x - low) / (high - low), 0, 1)``."""
    if high == low:
        raise ValueError("scale() requires distinct low and high bounds")
    fraction = (x - low) / (high - low)
    if fraction < ZERO:
        fraction = ZERO
    elif fraction > Decimal(1):
        fraction = Decimal(1)
    return fraction * ONE_HUNDRED


# ---------------------------------------------------------------------------
# Quarter label (SPEC 12.1)
# ---------------------------------------------------------------------------


def _signal(signal_id: str, state: SignalState, detail: str) -> SignalEvaluation:
    return SignalEvaluation(
        signal_id=signal_id,
        description=SIGNAL_DESCRIPTIONS[signal_id],
        state=state,
        detail=detail,
    )


def evaluate_signals(
    *,
    revenue_yoy: Decimal | None,
    revenue_yoy_valid: bool,
    operating_margin_change_pp: Decimal | None,
    cfo_to_net_income: Decimal | None,
    cfo_to_net_income_valid: bool,
    fcf: Decimal | None,
    shares_yoy: Decimal | None,
    shares_yoy_valid: bool,
) -> list[SignalEvaluation]:
    """Evaluate the five SPEC 12.1 signals; unknowns stay unknown (SPEC 2.1.7)."""
    signals: list[SignalEvaluation] = []

    if not revenue_yoy_valid or revenue_yoy is None:
        signals.append(
            _signal(
                "revenue_yoy_positive",
                SignalState.unknown,
                "revenue YoY not computable (missing current or matching prior-year quarter, "
                "or prior <= 0)",
            )
        )
    else:
        signals.append(
            _signal(
                "revenue_yoy_positive",
                SignalState.true if revenue_yoy > ZERO else SignalState.false,
                f"revenue YoY = {revenue_yoy}",
            )
        )

    if operating_margin_change_pp is None:
        signals.append(
            _signal(
                "operating_margin_change_non_negative",
                SignalState.unknown,
                "operating-margin change not computable (missing current or prior-year margin)",
            )
        )
    else:
        signals.append(
            _signal(
                "operating_margin_change_non_negative",
                SignalState.true if operating_margin_change_pp >= ZERO else SignalState.false,
                f"operating-margin change = {operating_margin_change_pp} pp",
            )
        )

    if not cfo_to_net_income_valid or cfo_to_net_income is None:
        signals.append(
            _signal(
                "cash_conversion_at_least_0_8",
                SignalState.unknown,
                "CFO/net income not valid (missing inputs or net income <= 0)",
            )
        )
    else:
        signals.append(
            _signal(
                "cash_conversion_at_least_0_8",
                SignalState.true
                if cfo_to_net_income >= CASH_CONVERSION_STRONG
                else SignalState.false,
                f"cfo/net income = {cfo_to_net_income} (threshold {CASH_CONVERSION_STRONG})",
            )
        )

    if fcf is None:
        signals.append(
            _signal(
                "fcf_positive", SignalState.unknown, "FCF not computable (missing cfo or capex)"
            )
        )
    else:
        signals.append(
            _signal(
                "fcf_positive",
                SignalState.true if fcf > ZERO else SignalState.false,
                f"FCF = {fcf}",
            )
        )

    if not shares_yoy_valid or shares_yoy is None:
        signals.append(
            _signal(
                "share_dilution_at_most_2pct",
                SignalState.unknown,
                "diluted shares YoY not computable (missing current or matching prior-year "
                "quarter, or prior <= 0)",
            )
        )
    else:
        signals.append(
            _signal(
                "share_dilution_at_most_2pct",
                SignalState.true if shares_yoy <= SHARE_DILUTION_LIMIT else SignalState.false,
                f"diluted shares YoY = {shares_yoy} (limit {SHARE_DILUTION_LIMIT})",
            )
        )
    return signals


def compute_quarter_label(
    signals: list[SignalEvaluation],
    *,
    cfo_to_net_income_current: Decimal | None,
    cfo_to_net_income_prior_quarter: Decimal | None,
) -> QuarterLabelResult:
    """Apply the SPEC 12.1 precedence rules to evaluated signals.

    ``cfo_to_net_income_*`` values must already be validity-checked (non-None
    only when net income > 0): the veto needs two CONSECUTIVE quarters with a
    VALID ratio below 0.4.
    """
    notes: list[str] = []

    veto_applicable = (
        cfo_to_net_income_current is not None
        and cfo_to_net_income_prior_quarter is not None
        and cfo_to_net_income_current < CASH_CONVERSION_VETO
        and cfo_to_net_income_prior_quarter < CASH_CONVERSION_VETO
    )
    if veto_applicable:
        notes.append(
            f"veto: valid cfo/net income below {CASH_CONVERSION_VETO} for two consecutive "
            f"quarters (current {cfo_to_net_income_current}, prior "
            f"{cfo_to_net_income_prior_quarter})"
        )
        return QuarterLabelResult(
            label=QuarterLabel.weak,
            rule_applied="veto_cash_conversion_below_0_4_two_consecutive_quarters",
            signals=signals,
            notes=notes,
        )

    if signals_available(signals) < 3:
        notes.append("fewer than three signals available")
        return QuarterLabelResult(
            label=QuarterLabel.insufficient_data,
            rule_applied="fewer_than_three_signals_available",
            signals=signals,
            notes=notes,
        )

    true_count = sum(1 for s in signals if s.state is SignalState.true)
    false_count = sum(1 for s in signals if s.state is SignalState.false)
    if true_count >= 3:
        return QuarterLabelResult(
            label=QuarterLabel.strong,
            rule_applied="at_least_three_signals_true",
            signals=signals,
            notes=notes,
        )
    if false_count >= 3:
        return QuarterLabelResult(
            label=QuarterLabel.weak,
            rule_applied="at_least_three_signals_false",
            signals=signals,
            notes=notes,
        )
    return QuarterLabelResult(
        label=QuarterLabel.mixed,
        rule_applied="otherwise_mixed",
        signals=signals,
        notes=notes,
    )


def signals_available(signals: list[SignalEvaluation]) -> int:
    return sum(1 for s in signals if s.state is not SignalState.unknown)


# ---------------------------------------------------------------------------
# Experimental fundamental score (SPEC 12.2)
# ---------------------------------------------------------------------------


def compute_fundamental_score(
    *,
    operating_margin: Decimal | None,
    cfo_to_net_income: Decimal | None,
    cfo_to_net_income_valid: bool,
    revenue_yoy: Decimal | None,
    revenue_yoy_valid: bool,
    current_ratio: Decimal | None,
    fcf: Decimal | None,
    revenue: Decimal | None,
    shares_yoy: Decimal | None,
    shares_yoy_valid: bool,
) -> FundamentalScoreResult:
    """Versioned heuristic components per SPEC 12.2, rescaled over available
    weights. No aggregate when available weight < 60% of the defined total."""
    components: list[ScoreComponent] = []

    profitability = (
        scale(operating_margin, ZERO, Decimal("0.30")) if operating_margin is not None else None
    )
    components.append(
        ScoreComponent(
            name="profitability",
            weight=SCORE_COMPONENT_WEIGHTS["profitability"],
            value=profitability,
            available=profitability is not None,
            detail="scale(operating_margin, 0, 0.30)",
        )
    )

    cash_conversion = (
        scale(cfo_to_net_income, Decimal("0.4"), Decimal("1.2"))
        if cfo_to_net_income_valid and cfo_to_net_income is not None
        else None
    )
    components.append(
        ScoreComponent(
            name="cash_conversion",
            weight=SCORE_COMPONENT_WEIGHTS["cash_conversion"],
            value=cash_conversion,
            available=cash_conversion is not None,
            detail="scale(cfo_to_net_income, 0.4, 1.2) when valid (net income > 0)",
        )
    )

    growth = (
        scale(revenue_yoy, Decimal("-0.10"), Decimal("0.20"))
        if revenue_yoy_valid and revenue_yoy is not None
        else None
    )
    components.append(
        ScoreComponent(
            name="growth",
            weight=SCORE_COMPONENT_WEIGHTS["growth"],
            value=growth,
            available=growth is not None,
            detail="scale(revenue_yoy, -0.10, 0.20)",
        )
    )

    liquidity = (
        scale(current_ratio, Decimal("0.5"), Decimal("2.0")) if current_ratio is not None else None
    )
    components.append(
        ScoreComponent(
            name="liquidity",
            weight=SCORE_COMPONENT_WEIGHTS["liquidity"],
            value=liquidity,
            available=liquidity is not None,
            detail="scale(current_ratio, 0.5, 2.0)",
        )
    )

    # Capital discipline: FCF margin and share discipline, average of the
    # available subcomponents (SPEC 12.2).
    subcomponents: list[tuple[Decimal, str]] = []
    if fcf is not None and revenue is not None and revenue != ZERO:
        fcf_margin = fcf / revenue
        subcomponents.append(
            (
                scale(fcf_margin, Decimal("-0.05"), Decimal("0.15")),
                f"scale(fcf/revenue={fcf_margin}, -0.05, 0.15)",
            )
        )
    if shares_yoy_valid and shares_yoy is not None:
        share_component = ONE_HUNDRED - scale(shares_yoy, ZERO, Decimal("0.05"))
        subcomponents.append((share_component, f"100 - scale(shares_yoy={shares_yoy}, 0, 0.05)"))
    capital = (
        sum((v for v, _ in subcomponents), ZERO) / Decimal(len(subcomponents))
        if subcomponents
        else None
    )
    components.append(
        ScoreComponent(
            name="capital_discipline",
            weight=SCORE_COMPONENT_WEIGHTS["capital_discipline"],
            value=capital,
            available=capital is not None,
            detail="; ".join(d for _, d in subcomponents) or "no subcomponents available",
        )
    )

    available_weight = sum((c.weight for c in components if c.available), ZERO)
    rescaled = available_weight < SCORE_TOTAL_WEIGHT
    notes: list[str] = [
        (
            "Experimental fundamental score: illustrative product heuristic, not a validated "
            "investment score; thresholds are not empirically validated financial truths and "
            "sector limitations apply."
        )
    ]

    if available_weight < SCORE_MIN_AVAILABLE_WEIGHT:
        notes.append(
            f"available weight {available_weight} of {SCORE_TOTAL_WEIGHT} is below the 60% "
            "minimum; no aggregate score returned"
        )
        return FundamentalScoreResult(
            score=None,
            status="insufficient_weight",
            components=components,
            available_weight=available_weight,
            total_weight=SCORE_TOTAL_WEIGHT,
            rescaled=rescaled,
            notes=notes,
        )

    if available_weight == ZERO:
        notes.append("no components available")
        return FundamentalScoreResult(
            score=None,
            status="missing_inputs",
            components=components,
            available_weight=available_weight,
            total_weight=SCORE_TOTAL_WEIGHT,
            rescaled=rescaled,
            notes=notes,
        )

    weighted = sum(
        (c.value * c.weight for c in components if c.available and c.value is not None), ZERO
    )
    score = weighted / available_weight
    if score < ZERO:
        score = ZERO
    elif score > ONE_HUNDRED:
        score = ONE_HUNDRED
    if rescaled:
        notes.append(f"rescaled over available weight {available_weight} of {SCORE_TOTAL_WEIGHT}")
    return FundamentalScoreResult(
        score=score,
        status="ok",
        components=components,
        available_weight=available_weight,
        total_weight=SCORE_TOTAL_WEIGHT,
        rescaled=rescaled,
        notes=notes,
    )
