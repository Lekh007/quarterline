"""Domain models: enums, the metric allowlist, and pydantic DTOs.

Contracts implemented here:
- C6: ``MetricValue`` / ``FactCard``.
- C7: ``METRIC_IDS`` frozenset (single source of truth for the metric allowlist).
- C8 (partial): ``EvidenceItem`` skeleton; ID construction lives in the
  retrieval wave, resolvable from the ``chunks`` table.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Enums (SPEC §9.3, §11, §12.1)
# ---------------------------------------------------------------------------


class PeriodKind(str, Enum):
    """Economic period identity of a fact. Never infer from source fy/fp alone."""

    instant = "instant"
    quarter = "quarter"
    year_to_date = "year_to_date"
    annual = "annual"
    other_duration = "other_duration"


class MetricStatus(str, Enum):
    """Status of a computed or reported metric value."""

    ok = "ok"
    missing = "missing"
    invalid = "invalid"
    unsuitable = "unsuitable"


class SignalState(str, Enum):
    """Unknown signals are not false signals (SPEC §2.1.7)."""

    true = "true"
    false = "false"
    unknown = "unknown"


class QuarterLabel(str, Enum):
    """Rule-based quarterly performance label (SPEC §12.1). Never a recommendation."""

    strong = "strong"
    mixed = "mixed"
    weak = "weak"
    insufficient_data = "insufficient_data"


# ---------------------------------------------------------------------------
# Metric allowlist (PLAN contract C7 — single source of truth)
# ---------------------------------------------------------------------------

METRIC_IDS: frozenset[str] = frozenset(
    {
        "revenue",
        "revenue_yoy",
        "gross_profit",
        "gross_margin",
        "operating_income",
        "operating_margin",
        "operating_margin_change_pp",
        "net_income",
        "net_margin",
        "diluted_eps",
        "shares_diluted",
        "shares_yoy",
        "cfo",
        "capex_outflow",
        "fcf",
        "fcf_margin",
        "current_ratio",
        "cash",
        "total_assets",
        "total_liabilities",
        "long_term_debt",
        "long_term_net_debt_proxy",
        "cfo_to_net_income",
        "quarter_label",
        "fundamental_score",
        "data_coverage",
    }
)


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class MetricProvenance(BaseModel):
    """Where a displayed metric value came from (SPEC §2.1.8: inspectable provenance)."""

    observation_ids: list[int] = Field(default_factory=list)
    derived_from: list[str] = Field(default_factory=list)
    formula_version: str | None = None


class MetricValue(BaseModel):
    """A single fact-card metric (contract C6)."""

    metric_id: str
    value: Decimal | None = None
    unit: str | None = None
    status: MetricStatus = MetricStatus.missing
    provenance: MetricProvenance = Field(default_factory=MetricProvenance)
    notes: list[str] = Field(default_factory=list)

    @field_validator("metric_id")
    @classmethod
    def _metric_id_must_be_allowlisted(cls, value: str) -> str:
        if value not in METRIC_IDS:
            raise ValueError(f"metric_id {value!r} is not in the METRIC_IDS allowlist")
        return value


class FactCard(BaseModel):
    """Code-generated, source-linked card of one company-period (contract C6)."""

    ticker: str
    cik: str
    period_start: date | None = None
    period_end: date
    fiscal_year: int | None = None
    fiscal_quarter: str | None = None
    currency: str | None = None
    metrics: list[MetricValue] = Field(default_factory=list)
    generated_at: datetime


class EvidenceItem(BaseModel):
    """Retrieved evidence window (contract C8 skeleton).

    ``evidence_id`` = ``ev-`` + first 12 hex chars of
    ``sha1("{document_id}|{strategy}|{start}|{end}|{text_hash}")``; resolvable
    from the ``chunks`` table.
    """

    evidence_id: str
    document_id: int
    section: str | None = None
    start_offset: int
    end_offset: int
    text: str
    scores: dict[str, float] = Field(default_factory=dict)


__all__ = [
    "METRIC_IDS",
    "EvidenceItem",
    "FactCard",
    "MetricProvenance",
    "MetricStatus",
    "MetricValue",
    "PeriodKind",
    "QuarterLabel",
    "SignalState",
]
