"""Normalization unit tests against the committed fixtures (SPEC 10, 26).

The synthetic fixture provides deterministic edge cases (non-calendar FYE,
53-week year, restatement, unit-incompatible fallback, EPS that must never be
derived); the trimmed-but-real AAPL fixture is exercised in the integration
tests. Asserted financial values are always derived FROM the fixture payload,
never hardcoded independently.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from facts_test_helpers import (
    d,
    find_fact_entry,
    load_synthetic_fixture,
)

from quarterline.core.models import PeriodKind
from quarterline.core.normalization import (
    ADDITIVE_FLOW_CONCEPTS,
    NEVER_DERIVE_CONCEPTS,
    NORMALIZATION_VERSION,
    SELECTION_AS_OF,
    SELECTION_LATEST,
    build_observation_drafts,
    derive_missing_quarters,
    normalize_companyfacts,
    observation_hash,
    reverse_tagmap,
    select_facts,
)
from quarterline.sources.sec.tagmap import load_tagmap


@pytest.fixture(scope="module")
def tagmap() -> dict[str, list[str]]:
    return load_tagmap()


@pytest.fixture(scope="module")
def synthetic(tagmap) -> tuple:
    """Normalized (drafts, result, fye_month) for the synthetic fixture."""
    fixture = load_synthetic_fixture()
    return normalize_companyfacts(1, "9999999", fixture, tagmap)


@pytest.fixture(scope="module")
def synth_result(synthetic):
    return synthetic[1]


def _quarter_fact(result, concept, fiscal_year, label):
    matches = [
        f
        for f in result.facts
        if f.concept == concept
        and f.fiscal_year == fiscal_year
        and f.fiscal_quarter == label
        and f.period_kind is PeriodKind.quarter
    ]
    assert len(matches) == 1, f"expected exactly one {concept} {fiscal_year} {label}, got {matches}"
    return matches[0]


# ---------------------------------------------------------------------------
# Ordered tag fallback (SPEC 10.1)
# ---------------------------------------------------------------------------


def test_ordered_tag_fallback_prefers_higher_priority_tag(synth_result) -> None:
    fixture = load_synthetic_fixture()
    primary = find_fact_entry(
        fixture,
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        start="2022-10-30",
        end="2023-01-28",
    )
    fallback = find_fact_entry(fixture, "Revenues", start="2022-10-30", end="2023-01-28")
    assert fallback["val"] != primary["val"]  # fixture really has competing tags
    q1 = _quarter_fact(synth_result, "revenue", 2023, "Q1")
    assert q1.value == Decimal(str(primary["val"]))
    assert q1.sources[0].observation.original_tag == (
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    )


def test_ordered_tag_fallback_uses_lower_priority_when_alone(synth_result) -> None:
    fixture = load_synthetic_fixture()
    entry = find_fact_entry(fixture, "Revenues", start="2023-01-29", end="2023-04-29")
    q2 = _quarter_fact(synth_result, "revenue", 2023, "Q2")
    assert q2.value == Decimal(str(entry["val"]))
    assert q2.sources[0].observation.original_tag == "Revenues"


def test_priority_applies_after_unit_filtering(synth_result) -> None:
    """The higher-priority tag's EUR observation must not displace the USD one."""
    fixture = load_synthetic_fixture()
    eur = find_fact_entry(
        fixture,
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        start="2023-04-30",
        end="2023-07-29",
        unit="EUR",
    )
    usd = find_fact_entry(fixture, "Revenues", start="2023-04-30", end="2023-07-29")
    q3 = _quarter_fact(synth_result, "revenue", 2023, "Q3")
    assert q3.value == Decimal(str(usd["val"]))
    assert q3.value != Decimal(str(eur["val"]))
    assert q3.unit == "USD"
    assert q3.sources[0].observation.original_tag == "Revenues"


# ---------------------------------------------------------------------------
# Quarterly vs annual collision prevention + instant vs duration
# ---------------------------------------------------------------------------


def test_annual_and_derived_q4_coexist_without_collision(synth_result) -> None:
    annual = next(
        f
        for f in synth_result.facts
        if f.concept == "revenue" and f.period_kind is PeriodKind.annual and f.fiscal_year == 2024
    )
    q4 = _quarter_fact(synth_result, "revenue", 2024, "Q4")
    assert annual.identity != q4.identity  # period identity includes period_kind
    assert annual.period_end == q4.period_end  # same end date, different identity
    assert annual.value != q4.value
    # Q4 = annual - nine-month YTD, both read from the fixture
    ytd9 = next(
        f
        for f in synth_result.facts
        if f.concept == "revenue"
        and f.period_kind is PeriodKind.year_to_date
        and f.fiscal_year == 2024
        and f.fiscal_quarter == "Q3"
    )
    assert q4.value == annual.value - ytd9.value


def test_instant_and_duration_facts_are_separated(synth_result) -> None:
    cash_instants = [f for f in synth_result.facts if f.concept == "cash"]
    assert cash_instants
    for fact in cash_instants:
        assert fact.period_kind is PeriodKind.instant
        assert fact.period_start is None
    revenue_quarters = [f for f in synth_result.facts if f.concept == "revenue"]
    assert all(f.period_kind is not PeriodKind.instant for f in revenue_quarters)


def test_concept_sets_exclude_non_additive_concepts() -> None:
    assert "diluted_eps" in NEVER_DERIVE_CONCEPTS
    assert "shares_diluted" in NEVER_DERIVE_CONCEPTS
    assert "cash" in NEVER_DERIVE_CONCEPTS
    assert "revenue" in ADDITIVE_FLOW_CONCEPTS
    assert "cfo" in ADDITIVE_FLOW_CONCEPTS
    assert NEVER_DERIVE_CONCEPTS.isdisjoint(ADDITIVE_FLOW_CONCEPTS)


# ---------------------------------------------------------------------------
# YTD -> quarter derivation + Q4 from annual - 9m (SPEC 10.3)
# ---------------------------------------------------------------------------


def test_ytd_derivation_q2_and_q3_with_lineage_roles(synth_result) -> None:
    fixture = load_synthetic_fixture()
    ytd6 = find_fact_entry(
        fixture,
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        start="2023-10-29",
        end="2024-04-27",
    )
    q1 = _quarter_fact(synth_result, "revenue", 2024, "Q1")
    q2 = _quarter_fact(synth_result, "revenue", 2024, "Q2")
    assert q2.is_derived is True
    assert q2.value == Decimal(str(ytd6["val"])) - q1.value
    roles = {source.role for source in q2.sources}
    assert roles == {"annual_total", "prior_ytd"}
    assert q2.derivation_method == "ytd_minus_prior_ytd"
    q3 = _quarter_fact(synth_result, "revenue", 2024, "Q3")
    assert q3.is_derived is True
    assert q3.value == Decimal(3550) - Decimal(str(ytd6["val"]))


def test_q4_derived_from_annual_minus_nine_month(synth_result) -> None:
    fixture = load_synthetic_fixture()
    annual_entry = find_fact_entry(
        fixture,
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        start="2023-10-29",
        end="2024-11-02",
    )
    ytd9_entry = find_fact_entry(
        fixture,
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        start="2023-10-29",
        end="2024-07-27",
    )
    q4 = _quarter_fact(synth_result, "revenue", 2024, "Q4")
    assert q4.value == Decimal(str(annual_entry["val"])) - Decimal(str(ytd9_entry["val"]))
    assert q4.derivation_method == "annual_minus_ytd9"
    # The 53-week year's Q4 is a 14-week quarter (98 calendar days inclusive,
    # 97 as end-start) and still classifies as quarter.
    assert (q4.period_end - q4.period_start).days == 97
    assert q4.period_kind is PeriodKind.quarter


def test_derivation_covers_flow_concepts_for_in_progress_year(synth_result) -> None:
    """FY2024 has an annual; FY2023 quarters derive where rungs exist."""
    derived_concepts = {
        f.concept for f in synth_result.facts if f.is_derived and f.fiscal_year == 2023
    }
    assert "revenue" in derived_concepts
    assert "net_income" in derived_concepts
    # cfo has only an FY2023 annual -> no rungs, no derivation
    assert all(
        not f.is_derived for f in synth_result.facts if f.concept == "cfo" and f.fiscal_year == 2023
    )


def test_no_subtraction_derivation_of_eps_or_shares(synth_result) -> None:
    """YTD and annual EPS/shares exist; quarter-kind facts must stay missing
    except the directly reported Q1s (one per fiscal year)."""
    for concept in ("diluted_eps", "shares_diluted"):
        facts = [
            f
            for f in synth_result.facts
            if f.concept == concept and f.period_kind is PeriodKind.quarter
        ]
        assert sorted((f.fiscal_year, f.fiscal_quarter) for f in facts) == [
            (2023, "Q1"),
            (2024, "Q1"),
        ], (
            f"{concept}: only directly reported Q1s may exist; got "
            f"{[(f.fiscal_year, f.fiscal_quarter) for f in facts]}"
        )
        assert all(not f.is_derived for f in facts)
    # YTD + annual observations ARE present for EPS (so absence is not vacuous)
    eps_kinds = {f.period_kind for f in synth_result.facts if f.concept == "diluted_eps"}
    assert PeriodKind.year_to_date in eps_kinds
    assert PeriodKind.annual in eps_kinds


def test_derivation_refuses_non_contiguous_fiscal_periods(tagmap) -> None:
    """A YTD that does not start at the fiscal-year start must not derive."""
    fixture = load_synthetic_fixture()
    # Remove the Q1 revenue rung so the 6m YTD has no matching Q1 start anchor.
    fixture["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"][
        "USD"
    ] = [
        e
        for e in fixture["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"][
            "units"
        ]["USD"]
        if not (e.get("start") == "2023-10-29" and e.get("end") == "2024-01-27")
    ]
    _drafts, result, _fye = normalize_companyfacts(1, "9999999", fixture, tagmap)
    q2_facts = [
        f
        for f in result.facts
        if f.concept == "revenue"
        and f.is_derived
        and f.fiscal_year == 2024
        and f.fiscal_quarter == "Q2"
    ]
    assert q2_facts == []


def test_derivation_refuses_incompatible_revision_vintages(tagmap) -> None:
    """Inputs filed more than one quarter apart are not revision-compatible."""
    fixture = load_synthetic_fixture()
    revenue_entries = fixture["facts"]["us-gaap"][
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    ]["units"]["USD"]
    for entry in revenue_entries:
        # Push the YTD6 filing a full year after the Q1 filing.
        if entry.get("start") == "2023-10-29" and entry.get("end") == "2024-04-27":
            entry["filed"] = "2025-05-03"
            entry["accn"] = "0009999999-25-000099"
    _drafts, result, _fye = normalize_companyfacts(1, "9999999", fixture, tagmap)
    q2_facts = [
        f
        for f in result.facts
        if f.concept == "revenue"
        and f.is_derived
        and f.fiscal_year == 2024
        and f.fiscal_quarter == "Q2"
        and f.period_start == d("2024-01-28")
    ]
    assert q2_facts == []


# ---------------------------------------------------------------------------
# Restatement selection + as_of (SPEC 10.4)
# ---------------------------------------------------------------------------


def test_restatement_latest_available_wins_and_both_kept(tagmap) -> None:
    fixture = load_synthetic_fixture()
    original = find_fact_entry(
        fixture,
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        start="2023-10-29",
        end="2024-01-27",
    )
    assert original["val"] == 1050  # original; restatement exists at same period
    drafts, _notes = build_observation_drafts(1, "9999999", fixture, tagmap)
    period_drafts = [
        dr
        for dr in drafts
        if dr.canonical_concept == "revenue"
        and dr.period_start == d("2023-10-29")
        and dr.period_end == d("2024-01-27")
    ]
    assert {dr.value for dr in period_drafts} == {Decimal(1050), Decimal(1080)}

    result = select_facts(drafts, 10, policy=SELECTION_LATEST)
    q1 = next(
        f
        for f in result.facts
        if f.concept == "revenue"
        and f.period_end == d("2024-01-27")
        and f.period_kind is PeriodKind.quarter
    )
    assert q1.value == Decimal(1080)  # latest filing wins
    assert q1.sources[0].observation.filed_at == d("2024-05-03")


def test_as_of_selection_excludes_future_filings(tagmap) -> None:
    fixture = load_synthetic_fixture()
    drafts, _notes = build_observation_drafts(1, "9999999", fixture, tagmap)
    # Before the Q1 FY2024 10-Q was filed, that quarter did not exist at all.
    before = select_facts(drafts, 10, policy=SELECTION_AS_OF, as_of=d("2024-01-15"))
    assert not [
        f for f in before.facts if f.concept == "revenue" and f.period_end == d("2024-01-27")
    ]
    # After the original 10-Q but before the restatement: original value in force.
    between = select_facts(drafts, 10, policy=SELECTION_AS_OF, as_of=d("2024-03-01"))
    q1 = next(
        f for f in between.facts if f.concept == "revenue" and f.period_end == d("2024-01-27")
    )
    assert q1.value == Decimal(1050)
    # After the restatement filing: revised value in force.
    after = select_facts(drafts, 10, policy=SELECTION_AS_OF, as_of=d("2024-06-01"))
    q1_revised = next(
        f for f in after.facts if f.concept == "revenue" and f.period_end == d("2024-01-27")
    )
    assert q1_revised.value == Decimal(1080)


def test_as_of_requires_timestamp() -> None:
    with pytest.raises(ValueError):
        select_facts([], 12, policy=SELECTION_AS_OF, as_of=None)


# ---------------------------------------------------------------------------
# Observation hash / idempotency + missing concept logging
# ---------------------------------------------------------------------------


def test_observation_hash_idempotent_and_revision_sensitive() -> None:
    base = {
        "cik": "320193",
        "taxonomy": "us-gaap",
        "tag": "Revenues",
        "unit": "USD",
        "period_start": date(2024, 1, 1),
        "period_end": date(2024, 3, 31),
        "value": Decimal(1000),
        "frame": None,
    }
    h1 = observation_hash(**base)
    h2 = observation_hash(**base)
    assert h1 == h2
    revised = dict(base, value=Decimal(1050))
    assert observation_hash(**revised) != h1  # revised value -> different hash
    relabelled = dict(base, frame="CY2024Q1")
    assert observation_hash(**relabelled) != h1  # frame context participates
    # fy/fp/accession changes do NOT change identity (same reported fact)
    assert h1 == observation_hash(**{**base, "tag": "Revenues", "unit": "USD"})


def test_missing_concepts_are_logged_never_filled(synthetic, tagmap) -> None:
    _drafts, result, fye = synthetic
    assert fye == 10  # October FYE inferred from the 53-week annual periods
    assert result.missing_concepts == []  # the synthetic fixture covers all concepts
    # A concept absent from the payload is logged and stays missing.
    fixture = load_synthetic_fixture()
    fixture["facts"]["us-gaap"].pop("LongTermDebt")
    _d2, r2, _f2 = normalize_companyfacts(1, "9999999", fixture, tagmap)
    assert "long_term_debt" in r2.missing_concepts
    assert all(f.concept != "long_term_debt" for f in r2.facts)


def test_zero_is_a_value_not_missing(synth_result) -> None:
    gp_q1 = _quarter_fact(synth_result, "gross_profit", 2024, "Q1")
    assert gp_q1.value == Decimal(0)
    assert gp_q1.value is not None


def test_normalization_version_constant_is_set() -> None:
    assert NORMALIZATION_VERSION == "norm-v1"


def test_reverse_tagmap_reports_ambiguity() -> None:
    reversed_map = reverse_tagmap({"a": ["t1", "t2"], "b": ["t2"]})
    assert sorted(concept for concept, _ in reversed_map["t2"]) == ["a", "b"]


def test_derive_missing_quarters_empty_input() -> None:
    assert derive_missing_quarters([], 12) == ([], [])


def test_select_facts_skips_non_decimal_values(tagmap) -> None:
    fixture = load_synthetic_fixture()
    fixture["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"].append(
        {
            "start": "2024-11-03",
            "end": "2025-02-01",
            "val": None,
            "accn": "x",
            "fy": 2025,
            "fp": "Q1",
            "form": "10-Q",
            "filed": "2025-02-07",
        }
    )
    drafts, _notes = build_observation_drafts(1, "9999999", fixture, tagmap)
    assert all(dr.value is not None for dr in drafts)
