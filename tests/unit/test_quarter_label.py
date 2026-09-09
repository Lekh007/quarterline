"""Unit tests for the quarter label (SPEC 12.1) and experimental fundamental
score (SPEC 12.2): veto precedence, unknown signals, insufficient data, weight
rescaling, clamping and formula-version persistence."""

from __future__ import annotations

from decimal import Decimal

import pytest

from quarterline.core.models import QuarterLabel, SignalState
from quarterline.core.quarter_label import (
    FORMULA_VERSION_LABEL,
    FORMULA_VERSION_SCORE,
    SCORE_MIN_AVAILABLE_WEIGHT,
    SCORE_TOTAL_WEIGHT,
    SignalEvaluation,
    _signal,
    compute_fundamental_score,
    compute_quarter_label,
    evaluate_signals,
    scale,
)

D = Decimal


def signals(**overrides) -> list[SignalEvaluation]:
    """A fully-available baseline: YoY +10%, margin +1pp, cfo_ni 1.2, fcf +ve,
    shares -1% -> all five signals true."""
    values = {
        "revenue_yoy": D("0.10"),
        "revenue_yoy_valid": True,
        "operating_margin_change_pp": D(1),
        "cfo_to_net_income": D("1.2"),
        "cfo_to_net_income_valid": True,
        "fcf": D(1000),
        "shares_yoy": D("-0.01"),
        "shares_yoy_valid": True,
    }
    values.update(overrides)
    return evaluate_signals(**values)


# ---------------------------------------------------------------------------
# Signal evaluation: unknown stays unknown (SPEC 2.1.7)
# ---------------------------------------------------------------------------


def test_missing_signals_stay_unknown() -> None:
    result = signals(
        revenue_yoy=None,
        revenue_yoy_valid=False,
        operating_margin_change_pp=None,
        fcf=None,
    )
    by_id = {s.signal_id: s for s in result}
    assert by_id["revenue_yoy_positive"].state is SignalState.unknown
    assert by_id["operating_margin_change_non_negative"].state is SignalState.unknown
    assert by_id["fcf_positive"].state is SignalState.unknown
    assert by_id["cash_conversion_at_least_0_8"].state is SignalState.true
    # invalid growth inputs are unknown, never false
    invalid = signals(revenue_yoy=None, revenue_yoy_valid=False)
    assert invalid[0].state is SignalState.unknown


def test_signal_boundaries() -> None:
    # revenue YoY == 0 is false (strictly > 0 required)
    assert signals(revenue_yoy=D(0))[0].state is SignalState.false
    # margin change exactly 0pp is true (>= 0)
    assert signals(operating_margin_change_pp=D(0))[1].state is SignalState.true
    # cfo_ni exactly 0.8 is true (>= 0.8)
    assert signals(cfo_to_net_income=D("0.8"))[2].state is SignalState.true
    # cfo_ni valid but negative net income path: valid=False -> unknown
    assert (
        signals(cfo_to_net_income=D("1.5"), cfo_to_net_income_valid=False)[2].state
        is SignalState.unknown
    )
    # fcf exactly 0 is false (strictly > 0)
    assert signals(fcf=D(0))[3].state is SignalState.false
    # shares YoY exactly 2% is true (<= 2%)
    assert signals(shares_yoy=D("0.02"))[4].state is SignalState.true
    assert signals(shares_yoy=D("0.020001"))[4].state is SignalState.false


# ---------------------------------------------------------------------------
# Label precedence (SPEC 12.1)
# ---------------------------------------------------------------------------


def test_strong_with_three_or_more_true() -> None:
    # exactly 3 true, 1 false, 1 unknown
    result = signals(cfo_to_net_income=D("0.5"), fcf=None)
    label = compute_quarter_label(
        result, cfo_to_net_income_current=D("0.5"), cfo_to_net_income_prior_quarter=D(2)
    )
    assert label.label is QuarterLabel.strong
    assert label.rule_applied == "at_least_three_signals_true"


def test_weak_with_three_or_more_false() -> None:
    result = signals(
        revenue_yoy=D("-0.05"),
        operating_margin_change_pp=D(-2),
        cfo_to_net_income=D("0.6"),
        shares_yoy=D("0.04"),
    )
    label = compute_quarter_label(
        result, cfo_to_net_income_current=D("0.6"), cfo_to_net_income_prior_quarter=D(2)
    )
    assert label.label is QuarterLabel.weak
    assert label.rule_applied == "at_least_three_signals_false"


def test_veto_precedence_weak_even_when_other_signals_strong() -> None:
    """cfo_ni < 0.4 for two consecutive quarters -> Weak despite 4 true signals."""
    result = signals(cfo_to_net_income=D("0.3"))
    assert sum(1 for s in result if s.state is SignalState.true) == 4
    label = compute_quarter_label(
        result,
        cfo_to_net_income_current=D("0.3"),
        cfo_to_net_income_prior_quarter=D("0.39"),
    )
    assert label.label is QuarterLabel.weak
    assert label.rule_applied == "veto_cash_conversion_below_0_4_two_consecutive_quarters"
    assert any("veto" in note for note in label.notes)


def test_veto_requires_two_consecutive_low_quarters() -> None:
    # prior quarter above 0.4 -> no veto; 4 true + 1 false -> Strong
    result = signals(cfo_to_net_income=D("0.3"))
    label = compute_quarter_label(
        result,
        cfo_to_net_income_current=D("0.3"),
        cfo_to_net_income_prior_quarter=D("0.9"),
    )
    assert label.label is QuarterLabel.strong
    assert label.rule_applied == "at_least_three_signals_true"
    # invalid (net income <= 0) ratios do not trigger the veto
    invalid = signals(cfo_to_net_income=None, cfo_to_net_income_valid=False)
    label2 = compute_quarter_label(
        invalid, cfo_to_net_income_current=None, cfo_to_net_income_prior_quarter=D("0.2")
    )
    assert label2.rule_applied != "veto_cash_conversion_below_0_4_two_consecutive_quarters"


def test_insufficient_data_when_fewer_than_three_signals_available() -> None:
    result = signals(
        revenue_yoy=None,
        revenue_yoy_valid=False,
        operating_margin_change_pp=None,
        cfo_to_net_income=None,
        cfo_to_net_income_valid=False,
        fcf=None,
    )
    label = compute_quarter_label(
        result, cfo_to_net_income_current=None, cfo_to_net_income_prior_quarter=None
    )
    assert label.label is QuarterLabel.insufficient_data
    assert label.rule_applied == "fewer_than_three_signals_available"


def test_mixed_when_neither_three_true_nor_three_false() -> None:
    # 2 true (revenue YoY, margin change), 2 false (cash conversion, shares),
    # 1 unknown (FCF missing) -> neither >=3 true nor >=3 false
    result = signals(
        cfo_to_net_income=D("0.5"),
        shares_yoy=D("0.06"),
        fcf=None,
    )
    label = compute_quarter_label(
        result, cfo_to_net_income_current=D("0.5"), cfo_to_net_income_prior_quarter=D(2)
    )
    assert label.label is QuarterLabel.mixed
    assert label.rule_applied == "otherwise_mixed"


def test_label_result_carries_every_signal_input_and_rule() -> None:
    result = signals()
    label = compute_quarter_label(
        result, cfo_to_net_income_current=D("1.2"), cfo_to_net_income_prior_quarter=D("1.1")
    )
    assert len(label.signals) == 5
    for signal in label.signals:
        assert signal.description
        assert signal.detail  # input values shown
    assert label.formula_version == FORMULA_VERSION_LABEL


def test_signal_helper_requires_known_signal_id() -> None:
    with pytest.raises(KeyError):
        _signal("not_a_signal", SignalState.unknown, "x")


# ---------------------------------------------------------------------------
# scale() and the experimental fundamental score (SPEC 12.2)
# ---------------------------------------------------------------------------


def test_scale_clamps_to_0_100() -> None:
    assert scale(D("0.15"), D(0), D("0.30")) == D(50)
    assert scale(D("-1"), D(0), D("0.30")) == D(0)
    assert scale(D("5"), D(0), D("0.30")) == D(100)
    assert scale(D("0.30"), D(0), D("0.30")) == D(100)
    with pytest.raises(ValueError):
        scale(D(1), D(1), D(1))


def full_score_inputs(**overrides):
    values = {
        "operating_margin": D("0.15"),  # -> 50
        "cfo_to_net_income": D("1.2"),  # -> 100
        "cfo_to_net_income_valid": True,
        "revenue_yoy": D("0.20"),  # -> 100
        "revenue_yoy_valid": True,
        "current_ratio": D("2.0"),  # -> 100
        "fcf": D(150),
        "revenue": D(1000),  # fcf margin 0.15 -> 100
        "shares_yoy": D(0),  # 100 - scale(0,0,0.05) = 100
        "shares_yoy_valid": True,
    }
    values.update(overrides)
    return values


def test_score_full_components_weighted_average() -> None:
    result = compute_fundamental_score(**full_score_inputs())
    assert result.status == "ok"
    assert result.available_weight == SCORE_TOTAL_WEIGHT
    assert result.rescaled is False
    # 25*50 + 20*100 + 20*100 + 15*100 + 10*100 all over 90
    expected = (D(25) * 50 + D(20) * 100 + D(20) * 100 + D(15) * 100 + D(10) * 100) / D(90)
    assert result.score == expected
    assert result.formula_version == FORMULA_VERSION_SCORE


def test_score_rescales_over_available_weights() -> None:
    result = compute_fundamental_score(
        **full_score_inputs(
            current_ratio=None, cfo_to_net_income_valid=False, cfo_to_net_income=None
        )
    )
    # available: profitability 25 + growth 20 + capital_discipline 10 = 55
    assert result.available_weight == D(55)
    assert result.rescaled is True
    assert result.status == "ok"
    expected = (D(25) * 50 + D(20) * 100 + D(10) * 100) / D(55)
    assert result.score == expected
    assert any("rescaled" in note for note in result.notes)


def test_score_no_aggregate_below_60_percent_weight() -> None:
    result = compute_fundamental_score(
        **full_score_inputs(
            operating_margin=None,
            cfo_to_net_income_valid=False,
            cfo_to_net_income=None,
            revenue_yoy_valid=False,
            revenue_yoy=None,
        )
    )
    # available: liquidity 15 + capital_discipline 10 = 25 < 54 (60% of 90)
    assert result.available_weight < SCORE_MIN_AVAILABLE_WEIGHT
    assert result.score is None
    assert result.status == "insufficient_weight"
    assert any("60%" in note for note in result.notes)


def test_score_cash_conversion_requires_valid_ratio() -> None:
    result = compute_fundamental_score(
        **full_score_inputs(cfo_to_net_income=D("1.2"), cfo_to_net_income_valid=False)
    )
    cash_component = next(c for c in result.components if c.name == "cash_conversion")
    assert cash_component.available is False
    assert cash_component.value is None


def test_score_capital_discipline_averages_available_subcomponents() -> None:
    result = compute_fundamental_score(
        **full_score_inputs(fcf=None, shares_yoy=None, shares_yoy_valid=False)
    )
    capital = next(c for c in result.components if c.name == "capital_discipline")
    assert capital.available is False  # both subcomponents missing
    result2 = compute_fundamental_score(
        **full_score_inputs(shares_yoy=None, shares_yoy_valid=False)
    )
    capital2 = next(c for c in result2.components if c.name == "capital_discipline")
    assert capital2.value == D(100)  # only FCF-margin subcomponent -> its own value


def test_score_components_clamped() -> None:
    result = compute_fundamental_score(**full_score_inputs(operating_margin=D("0.90")))
    profitability = next(c for c in result.components if c.name == "profitability")
    assert profitability.value == D(100)  # clamped at the top of the band
    result2 = compute_fundamental_score(**full_score_inputs(operating_margin=D("-0.9")))
    profitability2 = next(c for c in result2.components if c.name == "profitability")
    assert profitability2.value == D(0)
