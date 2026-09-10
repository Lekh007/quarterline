"""Indian fiscal calendar tests (IND-2): classification and FY mapping from dates only."""

from __future__ import annotations

from datetime import date

import pytest

from quarterline.sources.india.periods import (
    KIND_ANNUAL,
    KIND_INSTANT,
    KIND_QUARTER,
    KIND_YEAR_TO_DATE,
    classify_period,
    fiscal_quarter,
    fiscal_year,
    fiscal_year_label,
    period_label,
    prior_year_quarter,
    source_fp,
)


class TestClassifyPeriod:
    def test_quarter(self):
        assert classify_period(date(2026, 4, 1), date(2026, 6, 30)) == KIND_QUARTER
        assert classify_period(date(2026, 1, 1), date(2026, 3, 31)) == KIND_QUARTER

    def test_six_month_is_year_to_date(self):
        assert classify_period(date(2026, 4, 1), date(2026, 9, 30)) == KIND_YEAR_TO_DATE

    def test_nine_month_is_year_to_date_not_quarter(self):
        assert classify_period(date(2025, 4, 1), date(2025, 12, 31)) == KIND_YEAR_TO_DATE

    def test_annual_requires_march_end(self):
        assert classify_period(date(2025, 4, 1), date(2026, 3, 31)) == KIND_ANNUAL
        # A 12-month period NOT ending 31 March stays cumulative (e.g. calendar year).
        assert classify_period(date(2025, 1, 1), date(2025, 12, 31)) == KIND_YEAR_TO_DATE

    def test_instant(self):
        assert classify_period(None, date(2026, 3, 31)) == KIND_INSTANT

    def test_unclassifiable_duration_raises(self):
        with pytest.raises(ValueError):
            classify_period(date(2026, 4, 1), date(2026, 5, 31))  # 2 months
        with pytest.raises(ValueError):
            classify_period(date(2024, 4, 1), date(2026, 3, 31))  # 24 months

    def test_end_before_start_raises(self):
        with pytest.raises(ValueError):
            classify_period(date(2026, 6, 30), date(2026, 4, 1))


class TestFiscalMapping:
    def test_june_is_q1_of_next_fy(self):
        # Jun-2026 quarter belongs to FY2026-27 (fiscal_year = ending year 2027).
        assert fiscal_quarter(date(2026, 6, 30)) == "Q1"
        assert fiscal_year(date(2026, 6, 30)) == 2027
        assert fiscal_year_label(2027) == "FY2026-27"

    def test_march_is_q4_and_fy_boundary(self):
        # 31-Mar-2026 closes FY2025-26: Q4 of fiscal_year 2026.
        assert fiscal_quarter(date(2026, 3, 31)) == "Q4"
        assert fiscal_year(date(2026, 3, 31)) == 2026
        assert fiscal_year_label(2026) == "FY2025-26"

    def test_other_quarter_ends(self):
        assert fiscal_quarter(date(2026, 9, 30)) == "Q2"
        assert fiscal_quarter(date(2026, 12, 31)) == "Q3"

    def test_non_quarter_end_has_no_quarter(self):
        assert fiscal_quarter(date(2026, 5, 15)) is None

    def test_source_fp(self):
        assert source_fp(date(2026, 4, 1), date(2026, 6, 30), KIND_QUARTER) == "Q1"
        assert source_fp(date(2025, 4, 1), date(2026, 3, 31), KIND_ANNUAL) == "FY"
        assert source_fp(date(2025, 4, 1), date(2025, 12, 31), KIND_YEAR_TO_DATE) == "YTD"
        assert source_fp(None, date(2026, 3, 31), KIND_INSTANT) == "Q4"


class TestPriorYearQuarter:
    def test_q1_comparison(self):
        assert prior_year_quarter(date(2026, 4, 1), date(2026, 6, 30)) == (
            date(2025, 4, 1),
            date(2025, 6, 30),
        )

    def test_q4_comparison(self):
        assert prior_year_quarter(date(2026, 1, 1), date(2026, 3, 31)) == (
            date(2025, 1, 1),
            date(2025, 3, 31),
        )


class TestPeriodLabel:
    def test_quarter_label(self):
        label = period_label(date(2026, 4, 1), date(2026, 6, 30), KIND_QUARTER)
        assert "Q1" in label and "FY2026-27" in label

    def test_annual_label(self):
        label = period_label(date(2025, 4, 1), date(2026, 3, 31), KIND_ANNUAL)
        assert "FY2025-26" in label and "annual" in label

    def test_instant_label(self):
        assert "as at" in period_label(None, date(2026, 3, 31), KIND_INSTANT)
