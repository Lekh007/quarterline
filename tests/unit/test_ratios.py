"""Unit tests for financial ratios (SPEC 11) - all money math in Decimal."""

from __future__ import annotations

from decimal import Decimal

import pytest

from quarterline.core.models import MetricStatus
from quarterline.core.ratios import (
    FORMULA_VERSION_CAPEX_SEMANTICS,
    FORMULA_VERSION_RATIOS,
    capex_outflow_metric,
    cfo_to_net_income,
    current_ratio,
    free_cash_flow,
    long_term_net_debt_proxy,
    margin,
    margin_change_pp,
    yoy_growth,
)

D = Decimal


def test_gross_margin_exact_decimal() -> None:
    metric = margin("gross_margin", D(400), D(1000))
    assert metric.value == D("0.4")
    assert metric.unit == "ratio"
    assert metric.status is MetricStatus.ok
    assert metric.provenance.formula_version == FORMULA_VERSION_RATIOS
    assert metric.provenance.derived_from == ["gross_profit", "revenue"]


def test_margin_zero_revenue_is_invalid_not_zero() -> None:
    metric = margin("net_margin", D(50), D(0))
    assert metric.value is None
    assert metric.status is MetricStatus.invalid
    assert any("zero denominator" in note for note in metric.notes)


def test_margin_missing_input_stays_missing() -> None:
    metric = margin("operating_margin", None, D(1000))
    assert metric.status is MetricStatus.missing
    assert metric.value is None
    assert metric.value is not D(0)  # missing is not zero (SPEC 2.1.6)


def test_negative_revenue_denominator_flagged_invalid() -> None:
    metric = margin("operating_margin", D(100), D(-500))
    assert metric.status is MetricStatus.invalid
    assert any("negative denominator" in note for note in metric.notes)


def test_free_cash_flow_negative_reported_capex() -> None:
    """Capex reported as -100 (outflow): fcf = 300 - 100 = 200."""
    metric = free_cash_flow(D(300), D(-100))
    assert metric.value == D(200)
    assert metric.status is MetricStatus.ok
    assert any("positive outflow magnitude" in note for note in metric.notes)


def test_capex_positive_reported_sign_is_flagged_not_abs_hidden() -> None:
    """A positive reported capex (unexpected sign) is flagged for review."""
    capex = capex_outflow_metric(D(50))
    assert capex.value == D(50)
    assert capex.provenance.formula_version == FORMULA_VERSION_CAPEX_SEMANTICS
    assert any("unexpected sign" in note for note in capex.notes)
    assert any("abs()" in note for note in capex.notes)
    fcf = free_cash_flow(D(1200), D(50))
    assert fcf.value == D(1150)  # documented semantics: outflow magnitude
    assert any("unexpected sign" in note for note in fcf.notes)


def test_capex_zero_and_missing() -> None:
    assert capex_outflow_metric(D(0)).value == D(0)
    missing = capex_outflow_metric(None)
    assert missing.status is MetricStatus.missing
    assert missing.value is None


def test_current_ratio_zero_and_negative_denominator() -> None:
    ok = current_ratio(D(8000), D(4000))
    assert ok.value == D(2)
    zero = current_ratio(D(8000), D(0))
    assert zero.status is MetricStatus.invalid and zero.value is None
    negative = current_ratio(D(8000), D(-4000))
    assert negative.status is MetricStatus.invalid and negative.value is None


def test_long_term_net_debt_proxy_labelled_proxy() -> None:
    metric = long_term_net_debt_proxy(D(2800), D(5300))
    assert metric.value == D(-2500)
    assert any("not total net debt" in note for note in metric.notes)
    missing = long_term_net_debt_proxy(None, D(5300))
    assert missing.status is MetricStatus.missing


def test_growth_positive_prior() -> None:
    metric = yoy_growth("revenue_yoy", "revenue", D(1100), D(1000), "ratio")
    assert metric.value == D("0.1")
    assert metric.status is MetricStatus.ok
    assert any("absolute change: 100" in note for note in metric.notes)


def test_growth_zero_prior_null_percentage_with_turned_profitable_note() -> None:
    metric = yoy_growth("revenue_yoy", "revenue", D(110), D(0), "ratio")
    assert metric.value is None
    assert metric.status is MetricStatus.unsuitable
    assert any("turned profitable" in note for note in metric.notes)
    assert any("absolute change: 110" in note for note in metric.notes)


def test_growth_negative_prior_null_with_turned_profitable_note() -> None:
    metric = yoy_growth("revenue_yoy", "revenue", D(150), D(-50), "ratio")
    assert metric.value is None
    assert metric.status is MetricStatus.unsuitable
    assert any("turned profitable" in note for note in metric.notes)
    assert any("absolute change: 200" in note for note in metric.notes)


def test_growth_turned_loss_making_note() -> None:
    """A positive prior makes growth computable; the loss turn is a note."""
    metric = yoy_growth("revenue_yoy", "revenue", D(-30), D(200), "ratio")
    assert metric.status is MetricStatus.ok
    assert metric.value == D(-30) / D(200) - 1
    assert any("turned loss-making" in note for note in metric.notes)


def test_growth_missing_quarter_is_missing_never_zero() -> None:
    metric = yoy_growth("revenue_yoy", "revenue", D(100), None, "ratio")
    assert metric.status is MetricStatus.missing
    assert metric.value is None


def test_margin_change_in_percentage_points() -> None:
    metric = margin_change_pp(
        "operating_margin_change_pp", "operating_margin", D("0.20"), D("0.15")
    )
    assert metric.value == D("5.00")
    assert metric.unit == "percentage_points"
    decrease = margin_change_pp(
        "operating_margin_change_pp", "operating_margin", D("0.10"), D("0.25")
    )
    assert decrease.value == D("-15.00")


def test_cfo_to_net_income_validity_rules() -> None:
    ok = cfo_to_net_income(D(300), D(200))
    assert ok.value == D("1.5")
    assert ok.status is MetricStatus.ok
    zero_ni = cfo_to_net_income(D(300), D(0))
    assert zero_ni.status is MetricStatus.unsuitable
    assert zero_ni.value is None
    negative_ni = cfo_to_net_income(D(300), D(-10))
    assert negative_ni.status is MetricStatus.unsuitable
    assert any("cfo=300" in note and "net_income=-10" in note for note in negative_ni.notes)
    missing = cfo_to_net_income(None, D(200))
    assert missing.status is MetricStatus.missing


def test_all_ratios_carry_formula_version() -> None:
    for metric in (
        margin("net_margin", D(1), D(4)),
        free_cash_flow(D(1), D(-1)),
        current_ratio(D(2), D(1)),
        yoy_growth("shares_yoy", "shares_diluted", D(2), D(1), "shares"),
        cfo_to_net_income(D(2), D(1)),
    ):
        assert metric.provenance.formula_version == FORMULA_VERSION_RATIOS


@pytest.mark.parametrize("value", [D("0"), D("-1")])
def test_decimals_not_floats(value: Decimal) -> None:
    metric = margin("gross_margin", value, D(1))
    assert isinstance(metric.value, Decimal)
