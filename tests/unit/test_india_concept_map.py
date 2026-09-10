"""India concept mapping tests (IND-2): fixture tags resolve; unknown tags stay explicit.

The US tagmap/concept ids are deliberately NOT consulted — India facts must never
normalize under a us-gaap-derived concept name (SPEC 27).
"""

from __future__ import annotations

from quarterline.sources.india.concept_map import (
    INDIA_CONCEPTS,
    PER_SHARE_CONCEPTS,
    UNCERTAIN_TAGS,
    load_tagmap,
    map_tag,
)
from quarterline.sources.india.units import normalize_amount


class TestFixtureTagMapping:
    """Every canonical concept must resolve from tags actually in the fixtures."""

    def test_primary_tags(self):
        expected = {
            "RevenueFromOperations": "revenue_from_operations",
            "Income": "total_income",
            "ProfitBeforeTax": "profit_before_tax",
            "ProfitLossForPeriod": "profit_after_tax",
            "ProfitOrLossAttributableToOwnersOfParent": "profit_attributable_to_owners",
            "ExceptionalItemsBeforeTax": "exceptional_items",
            "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "eps_basic",
            "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations": "eps_diluted",
            "CashFlowsFromUsedInOperatingActivities": "cash_flow_operations",
            "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": "capex",
        }
        for tag, concept in expected.items():
            result = map_tag(tag)
            assert result.mapped, f"{tag} must map"
            assert result.concept == concept
            assert result.priority == 0

    def test_fallback_tags_have_higher_priority_index(self):
        result = map_tag("ProfitBeforeExceptionalItemsAndTax")
        assert result.mapped and result.concept == "profit_before_tax"
        assert result.priority == 1

    def test_qualified_tag_names_map_identically(self):
        assert map_tag("in-capmkt:RevenueFromOperations").concept == "revenue_from_operations"

    def test_per_share_concepts_flagged(self):
        for tag in (
            "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
            "DilutedEarningsLossPerShareFromContinuingOperations",
        ):
            assert map_tag(tag).per_share
        assert not map_tag("RevenueFromOperations").per_share

    def test_uncertain_fallbacks_flagged(self):
        for tag in (
            "ProfitLossForPeriodFromContinuingOperations",
            "BasicEarningsLossPerShareFromContinuingOperations",
            "PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities",
        ):
            assert tag in UNCERTAIN_TAGS
            assert map_tag(tag).uncertain
        assert not map_tag("ProfitBeforeTax").uncertain

    def test_unknown_tag_is_explicitly_unmapped(self):
        result = map_tag("SegmentRevenue")  # real in-capmkt tag, deliberately unmapped
        assert result.mapped is False
        assert result.concept is None
        result = map_tag("TotallyMadeUpTag")
        assert result.mapped is False
        assert result.concept is None


class TestTagmapIntegrity:
    def test_loads_all_concepts(self):
        tagmap = load_tagmap()
        assert set(tagmap) == set(INDIA_CONCEPTS)

    def test_us_concept_ids_rejected(self):
        # The India loader must never accept US tagmap concept names.
        assert "revenue" not in load_tagmap()
        assert "net_income" not in load_tagmap()
        assert "diluted_eps" not in load_tagmap()

    def test_per_share_set(self):
        assert PER_SHARE_CONCEPTS == frozenset({"eps_basic", "eps_diluted"})


class TestPerShareNeverScaled:
    def test_eps_value_passthrough(self):
        # A mapping's per_share flag must force the never-scale contract.
        from decimal import Decimal

        result = map_tag("BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations")
        assert result.per_share
        assert normalize_amount("19.19", per_share=result.per_share) == Decimal("19.19")
