"""India unit/scale handling tests (IND-2): lakh/crore, EPS exemption, display strings."""

from __future__ import annotations

from decimal import Decimal

import pytest

from quarterline.sources.india.units import (
    CRORE,
    LAKH,
    AmbiguousScaleError,
    format_crores,
    normalize_amount,
    parse_display_amount,
    scale_for_rounding_trait,
)

CRORE_AS_DECIMAL = Decimal(10000000)


class TestConstants:
    def test_lakh_and_crore(self):
        assert LAKH == Decimal(100000)
        assert CRORE == Decimal(10000000)

    def test_rounding_trait_scales(self):
        assert scale_for_rounding_trait("Crores") == CRORE
        assert scale_for_rounding_trait("lakhs") == LAKH
        assert scale_for_rounding_trait("Units") == Decimal(1)
        assert scale_for_rounding_trait(None) is None
        assert scale_for_rounding_trait("Hundred Crores") is None  # unknown trait: not guessed


class TestNormalizeAmount:
    """in-capmkt values are FULL rupees; the LevelOfRounding trait is display-only."""

    def test_full_rupee_passthrough(self):
        assert normalize_amount(Decimal(339860000000)) == Decimal(339860000000)

    def test_trait_does_not_rescale(self):
        # LevelOfRounding=Crores must NOT multiply the value by a crore.
        value = normalize_amount(Decimal(339860000000))
        assert value / CRORE == Decimal(33986)

    def test_eps_never_scaled(self):
        # per_share=True is the explicit never-scale contract for EPS concepts.
        assert normalize_amount(Decimal("19.19"), per_share=True) == Decimal("19.19")
        assert normalize_amount("64", per_share=True) == Decimal(64)

    def test_string_input(self):
        assert normalize_amount("-12890000000") == Decimal(-12890000000)


class TestParseDisplayAmount:
    def test_explicit_crore_suffix(self):
        assert parse_display_amount("₹1,234 Cr") == 1234 * CRORE_AS_DECIMAL

    def test_explicit_crore_decimal(self):
        assert parse_display_amount("₹ 1,234.5 crore") == Decimal(12345000000)

    def test_rs_without_suffix_is_ambiguous(self):
        # "Rs 9,330" carries no scale suffix: it needs a declared header scale.
        with pytest.raises(AmbiguousScaleError):
            parse_display_amount("Rs 9,330")

    def test_declared_scale(self):
        assert parse_display_amount("1,234.5", declared_scale=CRORE) == Decimal(12345000000)

    def test_lakh_suffix_and_declared(self):
        assert parse_display_amount("2,50,000 L") == 250000 * LAKH
        assert parse_display_amount("12345", declared_scale=LAKH) == Decimal(1234500000)

    def test_indian_digit_grouping(self):
        assert parse_display_amount("₹1,78,650 Cr") == Decimal(1786500000000)

    def test_parenthesised_negative(self):
        # Declared-scale column: "(1,234)" is an accounting negative.
        assert parse_display_amount("(1,234)", declared_scale=CRORE) == -1234 * CRORE_AS_DECIMAL
        # Suffix inside the parentheses also reads as negative.
        assert parse_display_amount("(₹1,234 Cr)") == -1234 * CRORE_AS_DECIMAL

    def test_thousands_suffix(self):
        assert parse_display_amount("5 thousand") == Decimal(5000)

    def test_ambiguous_raises(self):
        with pytest.raises(AmbiguousScaleError):
            parse_display_amount("1,234.5")

    def test_no_number_raises(self):
        with pytest.raises(ValueError):
            parse_display_amount("₹ Cr", declared_scale=CRORE)


class TestFormatCrores:
    def test_infosys_annual_cfo(self):
        assert format_crores(Decimal(339860000000)) == "₹33,986 Cr"

    def test_indian_grouping(self):
        assert format_crores(Decimal(1786500000000)) == "₹1,78,650 Cr"

    def test_negative_and_small(self):
        assert format_crores(Decimal(-750000000)) == "-₹75 Cr"
        assert format_crores(Decimal(0)) == "₹0 Cr"


class TestFixturePrecisionReconciliation:
    """Task 5: precision semantics proven on the REAL committed fixtures.

    ``decimals="-7"`` is PRECISION (the source's own rounding), ``LevelOfRounding
    ="Crores"`` is PRESENTATION metadata — raw rupee values must not be
    multiplied, and the displayed crore values must reconcile exactly with that
    declared precision.
    """

    def _parse(self, file_name: str):
        from india_test_helpers import INDIA_FIXTURES_DIR

        from quarterline.sources.india.xbrl_parse import parse_instance

        return parse_instance((INDIA_FIXTURES_DIR / file_name).read_bytes())

    def test_raw_rupees_not_multiplied_twice(self):
        instance = self._parse("INFY-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml")
        fact = next(f for f in instance.facts if f.tag == "RevenueFromOperations")
        raw = fact.value_decimal
        assert raw == Decimal(482110000000)
        # normalization is a passthrough: the trait is never applied, not once
        # and not twice; the display reconciliation is a clean division.
        assert normalize_amount(raw) == raw
        assert raw / CRORE == Decimal(48211)
        assert raw / CRORE / CRORE != Decimal(48211)

    def test_displayed_crore_value_reconciles_exactly_with_declared_precision(self):
        instance = self._parse("INFY-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml")
        fact = next(f for f in instance.facts if f.tag == "RevenueFromOperations")
        assert fact.decimals == "-7"  # source precision: rounded to crores
        value = fact.value_decimal
        crores = value / CRORE
        assert crores == crores.to_integral_value()  # exact at the declared precision
        assert format_crores(value) == "₹48,211 Cr"  # matches the rendered "48,211"

    def test_annual_cfo_display_reconciles_both_issuers(self):
        infy = self._parse("INFY-Q4FY26-consolidated-nse-integrated-filing-xbrl.xml")
        hul = self._parse("HUL-Q4FY26-consolidated-nse-integrated-filing-xbrl.xml")
        infy_cfo = next(f for f in infy.facts if f.tag == "CashFlowsFromUsedInOperatingActivities")
        hul_cfo = next(f for f in hul.facts if f.tag == "CashFlowsFromUsedInOperatingActivities")
        assert format_crores(infy_cfo.value_decimal) == "₹33,986 Cr"
        assert format_crores(hul_cfo.value_decimal) == "₹10,999 Cr"
        assert infy_cfo.decimals == "-7" and hul_cfo.decimals == "-7"

    def test_presentation_rounding_is_not_the_stored_scale(self):
        instance = self._parse("INFY-Q4FY26-consolidated-nse-integrated-filing-xbrl.xml")
        assert instance.rounding_trait == "Crores"
        fact = next(f for f in instance.facts if f.tag == "Income")
        stored = normalize_amount(fact.value_decimal)
        assert stored == fact.value_decimal  # unchanged
        assert stored != stored / CRORE  # the stored value is NOT in crores

    def test_eps_stays_per_share_in_real_fixture(self):
        instance = self._parse("HUL-Q4FY26-consolidated-nse-integrated-filing-xbrl.xml")
        eps = next(
            f
            for f in instance.facts
            if f.tag == "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations"
        )
        # the parser maps the instance's divided unit INRPerShare to "INR/shares"
        assert eps.unit_ref == "INR/shares"
        assert eps.decimals == "INF"  # per-share: full precision, no crore rounding
        assert normalize_amount(eps.value_decimal, per_share=True) == Decimal("12.73")

    def test_parenthesized_negatives_in_rendered_columns(self):
        # HUL rendered cash-flow column: "(1,258)" under "(Rs in Crores)" is the
        # capex outflow whose XBRL magnitude is 12580000000.
        assert parse_display_amount("(1,258)", declared_scale=CRORE) == Decimal(-12580000000)
        # Infosys condensed-FS style: "(2,111)" income-tax paid, "In ₹ crore".
        assert parse_display_amount("(2,111)", declared_scale=CRORE) == Decimal(-21110000000)

    def test_share_counts_and_percentages_are_not_currency(self):
        # A pure share-count or percentage unit must never pass the money-unit
        # gate: the pipeline's verified-unit rule accepts only INR / INR/shares
        # for money / per-share concepts (see test_india_pipeline).
        from quarterline.sources.india.pipeline import _unit_for
        from quarterline.sources.india.xbrl_parse import IndiaContext, IndiaFact

        def fact_with_unit(unit: str) -> IndiaFact:
            return IndiaFact(
                tag="RevenueFromOperations",
                value_text="100",
                context_ref="OneD",
                unit_ref=unit,
                decimals="-7",
                precision=None,
                is_numeric=True,
                context=IndiaContext(context_id="OneD"),
            )

        assert _unit_for(fact_with_unit("INR"), "revenue_from_operations") == "INR"
        assert _unit_for(fact_with_unit("shares"), "revenue_from_operations") is None
        assert _unit_for(fact_with_unit("percent"), "revenue_from_operations") is None

    def test_face_value_per_share_never_scaled(self):
        instance = self._parse("INFY-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml")
        face = next(f for f in instance.facts if f.tag == "FaceValueOfEquityShareCapital")
        assert face.value_decimal == Decimal(5)  # ₹5 face value, INRPerShare
        assert normalize_amount(face.value_decimal, per_share=True) == Decimal(5)
