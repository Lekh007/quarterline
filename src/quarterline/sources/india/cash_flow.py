"""Cash-flow eligibility and derivation policy (IND-3, reviewer correction A).

The IND-2 rule "CFO/capex map only from annual instances" was WRONG — too
restrictive. The actual requirement is: **do not invent quarterly cash flow
when the source does not report it.** That is not the same as restricting
extraction to annual documents. The corrected policy has exactly two ways a
cash-flow value may exist:

1. **Reported observations** — accepted from an identified official document
   whenever the concept is established, the exact reporting duration is known,
   scope/units are known, and extraction provenance is preserved. Eligible
   durations: ``quarter, half_year, nine_month_ytd, annual, other_duration``.
   Exact start/end dates are stored regardless of label: a quarterly CFO in an
   Infosys IR condensed-FS PDF is legitimate and extractable (text-level
   extraction with page provenance; table ambiguity is
   ``requires_manual_review``, never a guess).

2. **Derived observations** — subtraction ONLY when the concept is additive,
   the cumulative periods share the correct starting boundary, units/scope/
   reporting basis match, revisions are compatible, and both source
   observations are preserved. Examples: ``annual − H1 = H2`` (labeled **H2**,
   never Q4); ``nine_month_ytd − H1 = Q3``. Permitted only when the underlying
   observations actually exist and pass compatibility checks.

NEVER: divide annual CFO by four, divide half-year by two, treat H2 as Q4,
assume a 6-month observation is H1 without checking dates, or annualize/
trailing-figure from incomplete coverage. There is no division anywhere in
this module by construction.

Missing cash flow is recorded with a :class:`~quarterline.sources.india.data_status.MissingDataStatus`
— distinct, and never zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from quarterline.sources.india.data_status import MissingDataStatus

#: Cash-flow concepts governed by this policy.
CF_CONCEPTS: frozenset[str] = frozenset({"cash_flow_operations", "capex"})

#: Cash-flow concepts that are additive over time (a flow may be derived as
#: later-cumulative − earlier-cumulative). All current CF concepts qualify;
#: the set exists so a future non-additive concept cannot silently subtract.
CF_ADDITIVE_CONCEPTS: frozenset[str] = frozenset(CF_CONCEPTS)

# -- reporting-duration vocabulary (correction A) ---------------------------
#: Eligible duration kinds for REPORTED cash-flow observations.
DURATION_QUARTER = "quarter"
DURATION_HALF_YEAR = "half_year"
DURATION_NINE_MONTH_YTD = "nine_month_ytd"
DURATION_ANNUAL = "annual"
DURATION_OTHER = "other_duration"

REPORTED_CF_DURATIONS: frozenset[str] = frozenset(
    {DURATION_QUARTER, DURATION_HALF_YEAR, DURATION_NINE_MONTH_YTD, DURATION_ANNUAL, DURATION_OTHER}
)


def reporting_duration(start: date, end: date) -> str:
    """Classify a reporting duration from exact dates (never from a label).

    Unlike ``periods.classify_period`` (which raises for unusual spans and
    buckets 6/9-month periods as ``year_to_date``), this vocabulary is the
    cash-flow eligibility contract: every span maps to a duration and exact
    dates are stored regardless of label. 3 -> quarter, 6 -> half_year,
    9 -> nine_month_ytd, 12 ending 31 March -> annual, everything else
    (including 12-month non-Indian-FY periods) -> ``other_duration``.
    """
    if end < start:
        raise ValueError(f"period end before start: {start}..{end}")
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day >= start.day:
        months += 1
    if months == 3:
        return DURATION_QUARTER
    if months == 6:
        return DURATION_HALF_YEAR
    if months == 9:
        return DURATION_NINE_MONTH_YTD
    if months == 12 and (end.month, end.day) == (3, 31):
        return DURATION_ANNUAL
    return DURATION_OTHER


@dataclass(frozen=True)
class ReportedCashFlow:
    """One reported cash-flow observation with full provenance."""

    concept: str
    value: Decimal
    unit: str  # "INR" for money CF facts
    scope: str  # "consolidated" | "standalone"
    period_start: date
    period_end: date
    source_document_id: str
    revision_status: str | None = None  # "Original"/"Revised"; None = unknown

    @property
    def duration(self) -> str:
        return reporting_duration(self.period_start, self.period_end)


class IncompatibleDerivation(ValueError):
    """A cash-flow subtraction failed a compatibility check (never forced)."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise IncompatibleDerivation(reason)


def derive_by_subtraction(
    minuend: ReportedCashFlow, subtrahend: ReportedCashFlow
) -> ReportedCashFlow:
    """Derive the interval between two cumulative reported cash flows.

    ``annual − H1 = H2`` (labeled H2, never Q4); ``nine_month_ytd − H1 = Q3``;
    ``annual − nine_month_ytd = Q4``. The derived period starts the day after
    the subtrahend ends and ends with the minuend. Both source observations
    are preserved by the caller (this returns a NEW derived record and never
    mutates or replaces evidence).
    """
    _require(
        minuend.concept == subtrahend.concept,
        f"concept mismatch: {minuend.concept} vs {subtrahend.concept}",
    )
    _require(minuend.concept in CF_ADDITIVE_CONCEPTS, f"{minuend.concept} is not additive")
    _require(minuend.unit == subtrahend.unit, f"unit mismatch: {minuend.unit} vs {subtrahend.unit}")
    _require(
        minuend.scope == subtrahend.scope, f"scope mismatch: {minuend.scope} vs {subtrahend.scope}"
    )
    _require(
        minuend.period_start == subtrahend.period_start,
        f"cumulative periods must share the starting boundary "
        f"({minuend.period_start} vs {subtrahend.period_start})",
    )
    _require(
        minuend.period_end > subtrahend.period_end,
        f"minuend must end after subtrahend ({minuend.period_end} <= {subtrahend.period_end})",
    )
    _require(
        minuend.revision_status == subtrahend.revision_status,
        f"revision status mismatch: {minuend.revision_status!r} vs {subtrahend.revision_status!r}",
    )
    derived_start = subtrahend.period_end + timedelta(days=1)
    return ReportedCashFlow(
        concept=minuend.concept,
        value=minuend.value - subtrahend.value,
        unit=minuend.unit,
        scope=minuend.scope,
        period_start=derived_start,
        period_end=minuend.period_end,
        source_document_id=f"{minuend.source_document_id}+{subtrahend.source_document_id}",
        revision_status=minuend.revision_status,
    )


def derived_interval_label(derived: ReportedCashFlow) -> str:
    """Human label for a derived interval, from its EXACT boundaries only.

    ``annual − H1`` -> ``"H2"`` (never "Q4": H2 covers Oct..Mar, Q4 covers
    Jan..Mar). ``nine_month_ytd − H1`` -> ``"Q3"``. ``annual − nine_month_ytd``
    -> ``"Q4"``. Anything else is labeled from the dates, never from the
    minuend's label.
    """
    start = (derived.period_start.month, derived.period_start.day)
    end = (derived.period_end.month, derived.period_end.day)
    if start == (10, 1) and end == (3, 31):
        return "H2"
    if derived.duration == DURATION_QUARTER:
        from quarterline.sources.india.periods import fiscal_quarter

        quarter = fiscal_quarter(derived.period_end)
        if quarter is not None:
            return quarter
    return f"interval {derived.period_start.isoformat()}..{derived.period_end.isoformat()}"


# -- availability -----------------------------------------------------------

#: Convenience alias so callers do not import the enum from two places.
CF_MISSING = MissingDataStatus


def reported_from_observation_rows(rows) -> list[ReportedCashFlow]:
    """Convert stored observation rows (duck-typed: canonical_concept, value_decimal,
    unit, reporting_scope, period_start, period_end) into :class:`ReportedCashFlow`.

    Only cash-flow concepts are converted; everything else is ignored. provenance
    falls back to the row's accession/tag when source-document ids are absent.
    """
    reported: list[ReportedCashFlow] = []
    for row in rows:
        concept = getattr(row, "canonical_concept", None)
        if concept not in CF_CONCEPTS:
            continue
        value = getattr(row, "value_decimal", None)
        if value is None:
            continue
        reported.append(
            ReportedCashFlow(
                concept=concept,
                value=Decimal(str(value)),
                unit=getattr(row, "unit", "") or "",
                scope=getattr(row, "reporting_scope", "") or "",
                period_start=row.period_start,
                period_end=row.period_end,
                source_document_id=getattr(row, "accession", None) or "",
                revision_status=None,  # unknown from a bare row: never inferred
            )
        )
    return reported


def cash_flow_availability(
    concept: str,
    reported: list[ReportedCashFlow],
    *,
    scope: str,
    period_start: date,
    period_end: date,
) -> tuple[str, ReportedCashFlow | None]:
    """Classify whether a cash-flow value exists for one scope+period.

    Returns ``(status_value, observation_or_None)`` where status is a
    :class:`MissingDataStatus` value or the literal ``"reported"`` when an
    exact-boundary reported observation exists. Missing is NEVER zero: callers
    must render the status, not a 0.
    """
    for observation in reported:
        if (
            observation.concept == concept
            and observation.scope == scope
            and observation.period_start == period_start
            and observation.period_end == period_end
        ):
            return "reported", observation
    return MissingDataStatus.NOT_PRESENT_IN_INGESTED_SOURCES.value, None
