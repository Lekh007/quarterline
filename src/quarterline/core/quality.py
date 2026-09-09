"""Per company-quarter data coverage (feeds the watchlist "data completeness"
column and the screener ``minimum_data_coverage`` filter).

Coverage counts the fraction of core concepts available for a quarter, whether
each came from a direct or derived fact, and which are missing. Missing stays
missing - coverage only *describes* availability, it never fills it
(SPEC 10.1 / 2.1.5).
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

#: Core concepts defining full data coverage for one company-quarter.
CORE_CONCEPTS: tuple[str, ...] = (
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "diluted_eps",
    "shares_diluted",
    "cfo",
    "capex_outflow",
    "cash",
    "total_assets",
    "total_liabilities",
    "long_term_debt",
    "current_assets",
    "current_liabilities",
)

COVERAGE_DIRECT = "direct"
COVERAGE_DERIVED = "derived"
COVERAGE_MISSING = "missing"


class ConceptCoverage(BaseModel):
    concept: str
    status: str  # direct | derived | missing


class CoverageResult(BaseModel):
    """Data coverage for one company-quarter."""

    fraction: Decimal  # available (direct or derived) / core concepts
    available_count: int
    total_concepts: int
    derived_concepts: list[str] = Field(default_factory=list)
    missing_concepts: list[str] = Field(default_factory=list)
    concepts: list[ConceptCoverage] = Field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.missing_concepts


def quarter_coverage(availability: dict[str, str]) -> CoverageResult:
    """Build a coverage report from ``{concept: direct|derived}`` availability.

    Concepts absent from the mapping are missing. Unknown status strings are
    treated as missing rather than guessed.
    """
    concepts: list[ConceptCoverage] = []
    derived: list[str] = []
    missing: list[str] = []
    available = 0
    for concept in CORE_CONCEPTS:
        status = availability.get(concept)
        if status == COVERAGE_DIRECT or status == COVERAGE_DERIVED:
            available += 1
            if status == COVERAGE_DERIVED:
                derived.append(concept)
            concepts.append(ConceptCoverage(concept=concept, status=status))
        else:
            if status is not None:
                # A provided status we do not recognize is not silently trusted.
                missing.append(concept)
            else:
                missing.append(concept)
            concepts.append(ConceptCoverage(concept=concept, status=COVERAGE_MISSING))
    total = len(CORE_CONCEPTS)
    return CoverageResult(
        fraction=Decimal(available) / Decimal(total),
        available_count=available,
        total_concepts=total,
        derived_concepts=derived,
        missing_concepts=missing,
        concepts=concepts,
    )
