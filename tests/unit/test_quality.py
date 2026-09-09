"""Unit tests for per company-quarter data coverage (quality.py)."""

from __future__ import annotations

from decimal import Decimal

from quarterline.core.quality import (
    CORE_CONCEPTS,
    COVERAGE_DERIVED,
    COVERAGE_DIRECT,
    COVERAGE_MISSING,
    CoverageResult,
    quarter_coverage,
)


def test_full_coverage() -> None:
    availability = {concept: COVERAGE_DIRECT for concept in CORE_CONCEPTS}
    result = quarter_coverage(availability)
    assert isinstance(result, CoverageResult)
    assert result.fraction == Decimal(1)
    assert result.available_count == len(CORE_CONCEPTS)
    assert result.missing_concepts == []
    assert result.is_complete


def test_partial_coverage_counts_direct_and_derived() -> None:
    availability = {
        "revenue": COVERAGE_DIRECT,
        "net_income": COVERAGE_DERIVED,
        "cfo": COVERAGE_DERIVED,
    }
    result = quarter_coverage(availability)
    assert result.available_count == 3
    assert result.fraction == Decimal(3) / Decimal(len(CORE_CONCEPTS))
    assert sorted(result.derived_concepts) == ["cfo", "net_income"]
    assert "revenue" not in result.derived_concepts
    assert "diluted_eps" in result.missing_concepts
    assert not result.is_complete


def test_missing_stays_missing_and_unknown_status_not_trusted() -> None:
    result = quarter_coverage({"revenue": "unknown-status", "cash": COVERAGE_DIRECT})
    assert result.available_count == 1
    assert "revenue" in result.missing_concepts  # unrecognized status = missing
    by_concept = {c.concept: c.status for c in result.concepts}
    assert by_concept["revenue"] == COVERAGE_MISSING
    assert by_concept["cash"] == COVERAGE_DIRECT


def test_empty_coverage() -> None:
    result = quarter_coverage({})
    assert result.fraction == Decimal(0)
    assert result.available_count == 0
    assert len(result.missing_concepts) == len(CORE_CONCEPTS)
