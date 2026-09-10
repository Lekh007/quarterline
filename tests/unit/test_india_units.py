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
