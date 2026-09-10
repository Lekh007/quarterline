"""Indian fiscal calendar and period classification (IND-2).

Rules (docs/india_financial_methodology.md):

- An Indian financial year (FY) runs **1 April -> 31 March**. ``FY2026-27`` ends
  2027-03-31; the numeric ``fiscal_year`` is the *ending* calendar year (2027).
- Period kind is derived ONLY from the context (start, end) dates — never from
  column labels or from instance-declared strings like ``ReportingQuarter``
  ("First quarter") / ``TypeOfReportingPeriod`` ("Quarterly"), which are carried
  as metadata but never trusted as classification.
- ``quarter`` ≈ 3 months; ``year_to_date`` = 6 / 9 / 12-month cumulative periods
  (a 12-month period NOT ending 31 March stays cumulative ``year_to_date``);
  ``annual`` = 12 months ending 31 March; ``instant`` = no start date.
- Q1 ends 30 June, Q2 30 September, Q3 31 December, Q4/annual 31 March.
- A 9-month cumulative period is NOT Q3 and an annual value is never treated as
  a Q4 quarter value (SPEC 2.1.9 — never mix periods that share an end date).
"""

from __future__ import annotations

import calendar
from datetime import date

#: Period kinds produced by :func:`classify_period`.
KIND_QUARTER = "quarter"
KIND_YEAR_TO_DATE = "year_to_date"
KIND_ANNUAL = "annual"
KIND_INSTANT = "instant"

#: Quarter-end (month, day) in filing order.
QUARTER_ENDS: tuple[tuple[int, int], ...] = ((6, 30), (9, 30), (12, 31), (3, 31))

_FY_END = (3, 31)


def _month_span(start: date, end: date) -> int:
    """Whole calendar months covered by ``start..end`` inclusive of both endpoint months.

    2026-04-01..2026-06-30 -> 3; 2025-04-01..2026-03-31 -> 12.
    """
    if end < start:
        raise ValueError(f"period end before start: {start}..{end}")
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day >= start.day:
        months += 1
    # A span ending mid-starting-month (e.g. 15 Apr..30 Jun) is not a clean
    # whole-month count; treat sub-month remnants conservatively as the floor.
    return max(months, 1)


def classify_period(start: date | None, end: date | None) -> str:
    """Classify a context period purely from its dates.

    Raises ``ValueError`` for durations that are not ~3 / 6 / 9 / 12 months —
    an unclassifiable period must fail loudly, not be silently bucketed.
    """
    if end is None:
        raise ValueError(f"period has no end date: start={start!r}")
    if start is None:
        return KIND_INSTANT
    span = _month_span(start, end)
    if span == 3:
        return KIND_QUARTER
    if span in (6, 9):
        return KIND_YEAR_TO_DATE
    if span == 12:
        # Full year: annual only when it is the Indian FY (ends 31 March).
        return KIND_ANNUAL if (end.month, end.day) == _FY_END else KIND_YEAR_TO_DATE
    raise ValueError(
        f"period {start}..{end} spans {span} months; expected ~3/6/9/12 — not classifiable"
    )


def fiscal_year(end: date) -> int:
    """Numeric fiscal year of a period ending on ``end`` (the FY *ending* year).

    2026-06-30 -> 2027 (FY2026-27); 2026-03-31 -> 2026 (FY2025-26).
    """
    return end.year if (end.month, end.day) == _FY_END else end.year + 1


def fiscal_year_label(fiscal_year_end: int) -> str:
    """``2027`` -> ``"FY2026-27"``, ``2026`` -> ``"FY2025-26"``."""
    return f"FY{fiscal_year_end - 1}-{str(fiscal_year_end)[-2:]}"


def fiscal_quarter(end: date) -> str | None:
    """``"Q1".."Q4"`` for Indian quarter-end dates, else ``None`` (not a quarter end)."""
    for index, (month, day) in enumerate(QUARTER_ENDS, start=1):
        if (end.month, end.day) == (month, day):
            return f"Q{index}"
    return None


def source_fp(start: date | None, end: date, kind: str) -> str:
    """Filing-style period label for observations (``Q1..Q4``, ``FY``, ``YTD``)."""
    if kind == KIND_INSTANT:
        quarter = fiscal_quarter(end)
        return quarter or "FY"
    if kind == KIND_QUARTER:
        return fiscal_quarter(end) or "QTR"
    if kind == KIND_ANNUAL:
        return "FY"
    return "YTD"


def prior_year_quarter(start: date, end: date) -> tuple[date, date]:
    """The comparison quarter one year earlier (2026-04-01..2026-06-30 -> 2025-04-01..2025-06-30).

    Leap-day endings clamp to 28 February of the prior year.
    """

    def minus_one_year(value: date) -> date:
        try:
            return value.replace(year=value.year - 1)
        except ValueError:  # 29 February
            return value.replace(
                year=value.year - 1, day=calendar.monthrange(value.year - 1, value.month)[1]
            )

    return minus_one_year(start), minus_one_year(end)


def period_label(start: date | None, end: date, kind: str) -> str:
    """Human label for reconciliation output, derived from dates only."""
    fy = fiscal_year_label(fiscal_year(end))
    if kind == KIND_INSTANT:
        return f"as at {end.isoformat()} ({fy})"
    if kind == KIND_QUARTER:
        return f"{fiscal_quarter(end)} {fy} ({start.isoformat()}..{end.isoformat()})"
    if kind == KIND_ANNUAL:
        return f"{fy} annual ({start.isoformat()}..{end.isoformat()})"
    return f"{fy} cumulative {start.isoformat()}..{end.isoformat()}"
