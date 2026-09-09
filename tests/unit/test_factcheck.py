"""Fact-check tests: numeric normalization, semantic matching, metric-mention
expansion (SPEC §17 "Safer numerical output design", §18 numeric validation).

All expected strings are computed with the same Decimal rules as the module —
the assertions check Decimal-exact rendering against the card, never floats.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from quarterline.core.factcheck import (
    check_statement_numbers,
    expand_metric_mentions,
    extract_numeric_claims,
    template_valid_for,
)
from quarterline.core.models import FactCard, MetricProvenance, MetricStatus, MetricValue
from quarterline.llm.schemas import MetricMention


def _metric(
    metric_id: str, value: Decimal | None, unit: str | None, status: MetricStatus = MetricStatus.ok
) -> MetricValue:
    return MetricValue(
        metric_id=metric_id,
        value=value,
        unit=unit,
        status=status,
        provenance=MetricProvenance(formula_version="test"),
    )


def _card(metrics: list[MetricValue]) -> FactCard:
    now = datetime.now(UTC)
    return FactCard(
        ticker="TEST", cik="0000000000", period_end=now.date(), generated_at=now, metrics=metrics
    )


CARD = _card(
    [
        _metric("revenue", Decimal(84200000000), "USD"),
        _metric("revenue_yoy", Decimal("0.095"), "ratio"),
        _metric("operating_margin", Decimal("0.273"), "ratio"),
        _metric("operating_margin_change_pp", Decimal("1.2"), "percentage_points"),
        _metric("diluted_eps", Decimal("1.55"), "USD/shares"),
        _metric("shares_diluted", Decimal(15200000000), "shares"),
        _metric("current_ratio", Decimal("1.24"), "ratio"),
        _metric("cfo", Decimal(-2000000), "USD"),
        _metric("quarter_label", None, None),
    ]
)
CARD.metrics[-1].provenance.derived_from = ["label:strong", "rule:test"]


# ---------------------------------------------------------------------------
# Extraction / normalization
# ---------------------------------------------------------------------------


def test_currency_and_thousands_separators() -> None:
    claims = extract_numeric_claims("Revenue was $84,200 million for the period.")
    money = [c for c in claims if c.kind == "money"]
    assert len(money) == 1
    assert money[0].value == Decimal(84200000000)  # 84,200 million normalized


def test_k_m_b_suffixes() -> None:
    claims = extract_numeric_claims("outflows of $2.4b and 1.5m units and 900k items")
    values = sorted(c.value for c in claims if c.kind == "money" or c.kind == "plain")
    assert Decimal(2400000000) in values
    assert Decimal(1500000) in values
    assert Decimal(900000) in values


def test_accounting_parentheses_negative() -> None:
    claims = extract_numeric_claims("The loss was $(2.0) million for the period.")
    assert any(c.value == Decimal(-2000000) for c in claims)


def test_negative_sign() -> None:
    claims = extract_numeric_claims("a change of -3.2% year over year")
    assert any(c.value == Decimal("-3.2") and c.kind == "percent" for c in claims)


def test_pp_versus_percent() -> None:
    claims = extract_numeric_claims("margin rose 1.2 pp while revenue rose 9.5%")
    kinds = {c.value: c.kind for c in claims}
    assert kinds[Decimal("1.2")] == "pp"
    assert kinds[Decimal("9.5")] == "percent"


def test_metadata_digits_are_not_financial_claims() -> None:
    text = (
        "Filed 2026-07-24 (June 27, 2026 period, FY2026, Form 10-Q, Item 2, "
        "evidence [ev-0f0259cc182a], accession 0000320193-26-000201)."
    )
    assert extract_numeric_claims(text) == []


def test_bare_year_is_masked() -> None:
    assert extract_numeric_claims("In 2024 the segment changed.") == []


# ---------------------------------------------------------------------------
# Semantic matching + tolerance
# ---------------------------------------------------------------------------


def test_matching_percent_claim_passes() -> None:
    assert check_statement_numbers("Revenue grew 9.5% year over year.", CARD) == []


def test_rounded_display_claim_passes() -> None:
    # 9.5 rendered with one decimal; a display-rounded 9.5% is accepted, and
    # so is a value within the 0.5% relative tolerance.
    assert check_statement_numbers("Operating margin was 27.3%.", CARD) == []


def test_unsupported_number_rejected() -> None:
    reasons = check_statement_numbers("Revenue grew 44.7% year over year.", CARD)
    assert reasons and "numeric_consistency" in reasons[0]


def test_same_digits_wrong_unit_class_rejected() -> None:
    # 9.5 exists as revenue_yoy in percent — but as a PP claim there is no
    # 9.5 pp metric, so unit-class semantics reject it.
    reasons = check_statement_numbers("Operating margin changed 9.5 pp.", CARD)
    assert reasons


def test_valid_number_used_with_wrong_sign_rejected() -> None:
    reasons = check_statement_numbers("Revenue declined -9.5% year over year.", CARD)
    assert reasons


def test_zero_claim_matches_only_zero_metric() -> None:
    zero_card = _card([_metric("revenue_yoy", Decimal(0), "ratio")])
    assert check_statement_numbers("Revenue growth was 0%.", zero_card) == []
    reasons = check_statement_numbers("Growth was 0%.", CARD)
    assert reasons


def test_tolerance_boundary() -> None:
    # 0.5% relative tolerance AFTER semantic matching: 9.5 * 1.005 = 9.5475.
    assert check_statement_numbers("Revenue grew 9.5475% year over year.", CARD) == []
    reasons = check_statement_numbers("Revenue grew 9.6% year over year.", CARD)
    assert reasons


def test_money_scale_semantics() -> None:
    assert check_statement_numbers("Revenue was $84.2 billion.", CARD) == []
    reasons = check_statement_numbers("Revenue was $8.4 billion.", CARD)
    assert reasons


def test_eps_and_shares_claims() -> None:
    assert check_statement_numbers("Diluted EPS was $1.55 per share.", CARD) == []
    assert check_statement_numbers("Diluted shares were 15.2 billion.", CARD) == []


# ---------------------------------------------------------------------------
# Metric-mention expansion (Python renders numbers; Decimal-exact)
# ---------------------------------------------------------------------------


def test_reported_change_expansion_matches_card_value() -> None:
    result = expand_metric_mentions(
        [MetricMention(metric_id="revenue_yoy", template="reported_change")], CARD
    )
    assert not result.issues
    (fact,) = result.rendered
    assert fact.text == "Revenue changed +9.5% year over year."


def test_reported_change_pp_expansion() -> None:
    result = expand_metric_mentions(
        [MetricMention(metric_id="operating_margin_change_pp", template="reported_change")], CARD
    )
    (fact,) = result.rendered
    assert fact.text == "Operating margin changed +1.2 pp versus the prior-year quarter."


def test_reported_value_expansion_billion_scale() -> None:
    result = expand_metric_mentions(
        [MetricMention(metric_id="revenue", template="reported_value")], CARD
    )
    (fact,) = result.rendered
    assert fact.text == "Revenue was $84.2 billion for the reported period."


def test_reported_value_eps_expansion() -> None:
    result = expand_metric_mentions(
        [MetricMention(metric_id="diluted_eps", template="reported_value")], CARD
    )
    assert result.rendered[0].text == "Diluted EPS was $1.55 per share for the reported period."


def test_reported_level_expansion_margin() -> None:
    result = expand_metric_mentions(
        [MetricMention(metric_id="operating_margin", template="reported_level")], CARD
    )
    # 27.3% sits in the documented moderate band (10-30% scale anchors).
    assert "moderate level" in result.rendered[0].text


def test_reported_level_quarter_label_echoes_card() -> None:
    result = expand_metric_mentions(
        [MetricMention(metric_id="quarter_label", template="reported_level")], CARD
    )
    assert "strong" in result.rendered[0].text


def test_structurally_invalid_template_rejected() -> None:
    # A level metric used with the change template: structurally wrong (SPEC §26).
    result = expand_metric_mentions(
        [MetricMention(metric_id="revenue", template="reported_change")], CARD
    )
    assert not result.rendered
    assert result.issues and "not structurally valid" in result.issues[0]
    assert template_valid_for("revenue_yoy", "reported_change")
    assert not template_valid_for("revenue_yoy", "reported_value")


def test_missing_metric_expansion_records_issue() -> None:
    missing = _card([_metric("revenue_yoy", None, "ratio", MetricStatus.missing)])
    result = expand_metric_mentions(
        [MetricMention(metric_id="revenue_yoy", template="reported_change")], missing
    )
    assert not result.rendered
    assert result.issues and "unavailable" in result.issues[0]
