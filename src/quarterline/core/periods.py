"""Period kind inference and fiscal-calendar math (SPEC 9.3, 10.2).

All functions are pure. Classification of a reported fact's *economic* period
is duration-based, never taken from the source ``fy``/``fp`` labels alone
(SPEC 9.3: "Do not assume source fy and fp always directly identify the
economic period of a comparative observation").

Duration bands intentionally do NOT assume a 90-day quarter: 13-week quarters
are 91/90 days, 14-week quarters (extra week before a 53-week year end) are 98
days, 52-week years are 364 days and 53-week years 371 days. Bands are spans
of ``end - start`` in days.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from pydantic import BaseModel

from quarterline.core.models import PeriodKind

#: Inclusive day-count spans (``end - start``) used to recognise durations.
QUARTER_DAYS = (80, 105)  # ~3 months; covers 13- and 14-week quarters
SIX_MONTH_DAYS = (168, 195)  # ~6 months
NINE_MONTH_DAYS = (255, 290)  # ~9 months
ANNUAL_DAYS = (350, 380)  # 52-week (364), 365/366, 53-week (371)

FY_QUARTERS = ("Q1", "Q2", "Q3", "Q4")
ANNUAL_LABEL = "FY"


class QuarterPeriod(BaseModel):
    """Fiscal-period identity of one economic period (SPEC 9.4 labels)."""

    start: date | None = None
    end: date
    fiscal_year: int
    #: "Q1".."Q4" for quarters and year-to-date (cumulative through), "FY" for annual.
    fiscal_quarter: str
    kind: PeriodKind


def add_months(day: date, months: int) -> date:
    """Add ``months`` calendar months, clamping the day to the target month end."""
    month_index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(month_index, 12)
    month += 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def days_between(start: date, end: date) -> int:
    """XBRL-style duration length: ``end - start`` in days."""
    return (end - start).days


def _in_band(days: int, band: tuple[int, int]) -> bool:
    return band[0] <= days <= band[1]


def period_kind(
    period_start: date | None,
    period_end: date | None,
    *,
    instant: bool = False,
) -> PeriodKind:
    """Infer the economic period kind (SPEC 9.3).

    ``instant`` facts have no duration. Durations are classified by length:
    ~3 months -> quarter, ~6/~9 months from a fiscal-year start -> year_to_date,
    ~12 months (incl. 52/53-week years) -> annual, anything else other_duration.
    """
    if instant or (period_start is None and period_end is not None):
        return PeriodKind.instant
    if period_start is None or period_end is None:
        return PeriodKind.other_duration
    days = days_between(period_start, period_end)
    if _in_band(days, ANNUAL_DAYS):
        return PeriodKind.annual
    if _in_band(days, QUARTER_DAYS):
        return PeriodKind.quarter
    if _in_band(days, SIX_MONTH_DAYS) or _in_band(days, NINE_MONTH_DAYS):
        return PeriodKind.year_to_date
    return PeriodKind.other_duration


def fiscal_year_of(day: date, fiscal_year_end_month: int) -> int:
    """Calendar year of the fiscal year that contains ``day``.

    A fiscal year is named for the calendar year in which it *ends*. E.g. with a
    September FYE, 2024-06-30 belongs to FY2024 and 2024-12-31 to FY2025.
    """
    if day.month > fiscal_year_end_month:
        return day.year + 1
    return day.year


#: A 53-week year can push the actual year end up to ~5 weeks past the nominal
#: FYE month (e.g. an "October" FYE landing on 2024-11-02). End dates within
#: this many days of the nominal FYE of their own calendar year still belong to
#: that calendar year's fiscal year.
FYE_SPILL_TOLERANCE_DAYS = 45


def fiscal_year_of_duration_end(end: date, fiscal_year_end_month: int) -> int:
    """Fiscal-year number for the end date of an *annual* duration.

    Handles 52/53-week spill: an annual period ending 2024-11-02 under an
    October FYE is FY2024 (the year that just ended), not FY2025.
    """
    nominal = fiscal_year_end(end.year, fiscal_year_end_month)
    if abs((end - nominal).days) <= FYE_SPILL_TOLERANCE_DAYS:
        return end.year
    return fiscal_year_of(end, fiscal_year_end_month)


#: Average length of one fiscal quarter in days (365.25 / 4), used to place a
#: quarter end within its fiscal year without assuming exact 90-day quarters.
DAYS_PER_QUARTER = 91.3125


def quarter_index_from_days_into_year(days_into_year: int) -> int:
    """Fiscal quarter index (1..4) from days elapsed inside the fiscal year.

    Works for 13-week (90/91) and 14-week (97/98) quarter ends alike; the
    rounded ratio is stable because real period ends sit at quarter boundaries.
    """
    index = round(days_into_year / DAYS_PER_QUARTER)
    return max(1, min(4, index))


def fiscal_year_end(fiscal_year: int, fiscal_year_end_month: int) -> date:
    """Fiscal year-end date (last day of the FYE month) for ``fiscal_year``."""
    last = calendar.monthrange(fiscal_year, fiscal_year_end_month)[1]
    return date(fiscal_year, fiscal_year_end_month, last)


def fiscal_year_start(fiscal_year: int, fiscal_year_end_month: int) -> date:
    """First day of the fiscal year ``fiscal_year`` (day after the prior FYE)."""
    return fiscal_year_end(fiscal_year - 1, fiscal_year_end_month) + timedelta(days=1)


def quarter_index_in_year(day: date, fiscal_year_end_month: int) -> int:
    """Which fiscal quarter (1..4) of its fiscal year contains ``day``.

    Month-based, so a 52/53-week quarter end such as 2024-06-29 (AAPL, Sep FYE)
    maps to Q3 exactly like the calendar-aligned 2024-06-30 would.
    """
    months_after_fye = (day.month - fiscal_year_end_month) % 12
    if months_after_fye == 0:
        months_after_fye = 12
    return (months_after_fye - 1) // 3 + 1


def fiscal_labels_for_duration(
    period_start: date,
    period_end: date,
    fiscal_year_end_month: int,
) -> tuple[int, str, PeriodKind]:
    """Return ``(fiscal_year, fiscal_quarter_label, kind)`` for a duration fact.

    The label is the fiscal-quarter position of ``period_end`` inside its fiscal
    year: Q1..Q3 single quarters and year-to-date periods (YTD is cumulative
    *through* that quarter), FY for annual periods.
    """
    kind = period_kind(period_start, period_end)
    fiscal_year = fiscal_year_of(period_end, fiscal_year_end_month)
    index = quarter_index_in_year(period_end, fiscal_year_end_month)
    if kind is PeriodKind.annual:
        return fiscal_year, ANNUAL_LABEL, kind
    return fiscal_year, FY_QUARTERS[index - 1], kind


def prior_quarter(period: QuarterPeriod, fiscal_year_end_month: int) -> QuarterPeriod:
    """The fiscal quarter immediately before ``period``.

    Quarter boundaries are contiguous, so the previous quarter ends one day
    before this one starts; its fiscal identity follows the fiscal sequence
    (Q1 wraps to Q4 of the previous fiscal year).
    """
    if period.start is None:
        raise ValueError("prior_quarter requires a duration period with a start")
    index = FY_QUARTERS.index(period.fiscal_quarter) if period.fiscal_quarter in FY_QUARTERS else 3
    if index == 0:
        prev_label, prev_fy = "Q4", period.fiscal_year - 1
    else:
        prev_label, prev_fy = FY_QUARTERS[index - 1], period.fiscal_year
    prev_end = period.start - timedelta(days=1)
    # Step back the current quarter's own span ((end - start).days, e.g. 90 for
    # a 13-week quarter) so the previous quarter has the same length and the
    # boundaries stay contiguous (prev.end + 1 day == period.start).
    length = max((period.end - period.start).days, 1)
    prev_start = prev_end - timedelta(days=length)
    return QuarterPeriod(
        start=prev_start,
        end=prev_end,
        fiscal_year=prev_fy,
        fiscal_quarter=prev_label,
        kind=PeriodKind.quarter,
    )


def matching_prior_year_quarter(period: QuarterPeriod, fiscal_year_end_month: int) -> QuarterPeriod:
    """Same fiscal quarter, previous fiscal year (the correct YoY comparator).

    The date estimate steps back the actual fiscal-year length when possible
    (52 vs 53 weeks), falling back to 364 days; YoY comparisons must key on the
    returned fiscal identity, not on row offsets (SPEC 11.2).
    """
    if period.start is not None and period.fiscal_quarter in FY_QUARTERS:
        target_fy = period.fiscal_year - 1
        this_start = fiscal_year_start(period.fiscal_year, fiscal_year_end_month)
        prior_start = fiscal_year_start(target_fy, fiscal_year_end_month)
        offset = this_start - prior_start  # 364 or 371 days
        est_start = period.start - offset
        est_end = period.end - offset
    else:
        est_start, est_end = None, period.end.replace(year=period.end.year - 1)
    return QuarterPeriod(
        start=est_start,
        end=est_end,
        fiscal_year=period.fiscal_year - 1,
        fiscal_quarter=period.fiscal_quarter,
        kind=period.kind,
    )
