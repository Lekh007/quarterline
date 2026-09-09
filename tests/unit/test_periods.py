"""Unit tests for fiscal-calendar math and period-kind inference (SPEC 9.3, 10.2).

Calendar math is exercised with inline dates (pure calendar data, not company
data); fiscal-calendar handling for non-calendar FYEs and 52/53-week years is
additionally covered against the synthetic fixture in the normalization tests.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from quarterline.core.models import PeriodKind
from quarterline.core.periods import (
    QuarterPeriod,
    add_months,
    fiscal_labels_for_duration,
    fiscal_year_end,
    fiscal_year_of,
    fiscal_year_of_duration_end,
    matching_prior_year_quarter,
    period_kind,
    prior_quarter,
    quarter_index_from_days_into_year,
    quarter_index_in_year,
)

# ---------------------------------------------------------------------------
# period_kind: duration-based, never assuming 90-day quarters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        # 13-week quarter: 2023-12-31 -> 2024-03-30 (91 days)
        (date(2023, 12, 31), date(2024, 3, 30), PeriodKind.quarter),
        # calendar-aligned quarter: Jan 1 -> Mar 31 (90/91 days)
        (date(2024, 1, 1), date(2024, 3, 31), PeriodKind.quarter),
        # a ~3-month duration that is NOT a calendar quarter still classifies
        (date(2024, 1, 15), date(2024, 4, 14), PeriodKind.quarter),
        # 14-week quarter (extra week before a 53-week year end): 98 days
        (date(2024, 7, 28), date(2024, 11, 2), PeriodKind.quarter),
        # six-month YTD
        (date(2023, 9, 24), date(2024, 3, 30), PeriodKind.year_to_date),
        # nine-month YTD
        (date(2023, 9, 24), date(2024, 6, 29), PeriodKind.year_to_date),
        # 52-week fiscal year (364 days, late-September FYE like AAPL)
        (date(2023, 10, 1), date(2024, 9, 28), PeriodKind.annual),
        # 53-week fiscal year (371 days)
        (date(2023, 10, 29), date(2024, 11, 2), PeriodKind.annual),
        # calendar year incl. leap year
        (date(2024, 1, 1), date(2024, 12, 31), PeriodKind.annual),
        # five-month transition period -> other_duration
        (date(2024, 1, 1), date(2024, 5, 31), PeriodKind.other_duration),
    ],
)
def test_period_kind_duration_bands(start: date, end: date, expected: PeriodKind) -> None:
    assert period_kind(start, end) is expected


def test_period_kind_instant() -> None:
    assert period_kind(None, date(2024, 6, 29)) is PeriodKind.instant
    assert period_kind(None, date(2024, 6, 29), instant=True) is PeriodKind.instant
    assert period_kind(date(2024, 1, 1), None) is PeriodKind.other_duration


def test_period_kind_other_duration_outside_all_bands() -> None:
    # ~10.5 months is neither a clean 6m/9m rung nor annual
    assert period_kind(date(2024, 1, 1), date(2024, 11, 15)) is PeriodKind.other_duration
    # ~4.5 months
    assert period_kind(date(2024, 1, 1), date(2024, 5, 15)) is PeriodKind.other_duration


# ---------------------------------------------------------------------------
# Fiscal calendar math (non-calendar FYE, 52/53-week)
# ---------------------------------------------------------------------------


def test_fiscal_year_of_september_fye() -> None:
    # AAPL-style late-September FYE
    assert fiscal_year_of(date(2024, 6, 30), 9) == 2024  # inside FY2024
    assert fiscal_year_of(date(2024, 12, 31), 9) == 2025  # Q1 of FY2025
    assert fiscal_year_of(date(2023, 9, 30), 9) == 2023


def test_fiscal_year_of_duration_end_handles_53_week_spill() -> None:
    # October FYE but the 53-week year actually ends 2024-11-02: still FY2024.
    assert fiscal_year_of_duration_end(date(2024, 11, 2), 10) == 2024
    assert fiscal_year_of_duration_end(date(2023, 10, 28), 10) == 2023
    # A genuinely different-year end follows the month rule.
    assert fiscal_year_of_duration_end(date(2025, 2, 1), 10) == 2025


def test_quarter_index_in_year_for_september_fye() -> None:
    assert quarter_index_in_year(date(2023, 12, 30), 9) == 1  # Q1 FY2024
    assert quarter_index_in_year(date(2024, 3, 30), 9) == 2
    assert quarter_index_in_year(date(2024, 6, 29), 9) == 3
    assert quarter_index_in_year(date(2024, 9, 28), 9) == 4
    assert quarter_index_in_year(date(2024, 8, 15), 9) == 4


def test_quarter_index_in_year_calendar_fye() -> None:
    assert quarter_index_in_year(date(2024, 3, 31), 12) == 1
    assert quarter_index_in_year(date(2024, 12, 31), 12) == 4


def test_quarter_index_from_days_into_year_handles_14_week_quarters() -> None:
    # 53-week year: Q4 ends 370 days in (14-week quarter)
    assert quarter_index_from_days_into_year(370) == 4
    assert quarter_index_from_days_into_year(90) == 1
    assert quarter_index_from_days_into_year(98) == 1  # 14-week Q1 is still Q1
    assert quarter_index_from_days_into_year(272) == 3


def test_fiscal_labels_for_duration() -> None:
    # Dec 31 -> Mar 30 under a September FYE is Q2 (Dec-Mar quarter)
    fy, label, kind = fiscal_labels_for_duration(date(2023, 12, 31), date(2024, 3, 30), 9)
    assert (fy, label, kind) == (2024, "Q2", PeriodKind.quarter)
    # Sep 25 -> Dec 30 under a September FYE is Q1
    fy, label, kind = fiscal_labels_for_duration(date(2023, 9, 25), date(2023, 12, 30), 9)
    assert (fy, label, kind) == (2024, "Q1", PeriodKind.quarter)
    fy, label, kind = fiscal_labels_for_duration(date(2023, 9, 24), date(2024, 6, 29), 9)
    assert (fy, label, kind) == (2024, "Q3", PeriodKind.year_to_date)
    fy, label, kind = fiscal_labels_for_duration(date(2023, 10, 1), date(2024, 9, 28), 9)
    assert (fy, label, kind) == (2024, "FY", PeriodKind.annual)


def test_fiscal_year_end_and_start() -> None:
    assert fiscal_year_end(2024, 9) == date(2024, 9, 30)
    assert fiscal_year_end(2023, 9) == date(2023, 9, 30)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)  # month-end clamp


# ---------------------------------------------------------------------------
# Quarter sequence helpers
# ---------------------------------------------------------------------------


def test_prior_quarter_wraps_fiscal_year() -> None:
    q = QuarterPeriod(
        start=date(2023, 12, 31),
        end=date(2024, 3, 30),
        fiscal_year=2024,
        fiscal_quarter="Q1",
        kind=PeriodKind.quarter,
    )
    prev = prior_quarter(q, 9)
    assert (prev.fiscal_year, prev.fiscal_quarter) == (2023, "Q4")
    assert prev.end == date(2023, 12, 30)  # contiguous boundaries
    assert prev.start is not None and (prev.end - prev.start).days == 90
    assert prev.end + timedelta(days=1) == q.start

    q2 = QuarterPeriod(
        start=date(2024, 3, 31),
        end=date(2024, 6, 29),
        fiscal_year=2024,
        fiscal_quarter="Q2",
        kind=PeriodKind.quarter,
    )
    prev2 = prior_quarter(q2, 9)
    assert (prev2.fiscal_year, prev2.fiscal_quarter) == (2024, "Q1")


def test_matching_prior_year_quarter_keeps_fiscal_identity() -> None:
    q = QuarterPeriod(
        start=date(2024, 3, 31),
        end=date(2024, 6, 29),
        fiscal_year=2024,
        fiscal_quarter="Q3",
        kind=PeriodKind.quarter,
    )
    py = matching_prior_year_quarter(q, 9)
    assert (py.fiscal_year, py.fiscal_quarter) == (2023, "Q3")
    # The date estimate steps back one fiscal-year length (364-371 days),
    # never a naive calendar-year subtraction nor a row offset.
    assert 360 <= (q.end - py.end).days <= 375
