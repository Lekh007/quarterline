"""XBRL parse tests on the REAL committed in-capmkt fixtures (IND-2, IND-6 corpus).

Offline: parses tests/fixtures/india/*.xml with stdlib ElementTree only. Asserts,
for ALL TEN issuers and BOTH periods, the declared scope, the captured
LevelOfRounding trait (Crores AND Millions — per issuer, IND-6) and the
per-instance taxonomy version, plus the IND-2-era extracted-value spot checks.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from india_test_helpers import (
    HUL_Q1,
    HUL_Q4,
    INDIA_FIXTURES_DIR,
    INFY_Q1,
    INFY_Q4,
    load_india_manifest,
)

from quarterline.sources.india.xbrl_parse import parse_instance

ALL_FIXTURES = tuple(entry["file"] for entry in load_india_manifest()["committed_fixtures"])


def parse(fixture: str):
    return parse_instance((INDIA_FIXTURES_DIR / fixture).read_bytes())


def manifest_expectations() -> dict[str, dict]:
    """{file: {taxonomy, rounding, scope}} declared per fixture by the manifest.

    The manifest is the provenance record (IND-1/IND-6a/IND-6b acquisition, all
    entries sha256-verified against the committed files), so the expected
    per-instance taxonomy version and LevelOfRounding trait come from it rather
    than being restated here.
    """
    expectations: dict[str, dict] = {}
    for entry in load_india_manifest()["committed_fixtures"]:
        # IND-1 entries predate the per-entry taxonomy field; the documented
        # corpus rule applies (July Q1 filings V2.1, April/May Q4 filings V2.0).
        default = "V2.0 (06-02-2026)" if entry["period"] == "q4_fy2025-26" else "V2.1 (26-06-2026)"
        taxonomy = entry.get("taxonomy", default)
        expectations[entry["file"]] = {
            # manifest stores the full comment ("IFIndAs V2.1 (...)"); the
            # parser returns the version fragment without the scheme name.
            "taxonomy": taxonomy.removeprefix("IFIndAs "),
            "scope": entry["scope"],
        }
    return expectations


def undim_value(instance, tag: str, context_id: str) -> Decimal:
    facts = [
        f
        for f in instance.facts_for_tag(tag)
        if f.context.context_id == context_id and not f.is_dimensioned
    ]
    assert len(facts) == 1, f"expected exactly one {tag}@{context_id}, got {len(facts)}"
    value = facts[0].value_decimal
    assert value is not None
    return value


#: FINDING (IND-2, corpus-wide since IND-6): the taxonomy version VARIES per
#: filing generation — July-filed Q1 FY27 instances carry IFIndAs V2.1
#: (26-06-2026), April/May-filed Q4+FY26 instances carry V2.0 (06-02-2026),
#: and the Asian Paints Q4 REVISION (re-filed 15-Jul-2026) carries V2.1 while
#: its superseded original is V2.0. The parser carries the per-instance
#: version; nothing may assume V2.1.
EXPECTED_TAXONOMY_VERSIONS = {
    INFY_Q1: "V2.1 (26-06-2026)",
    INFY_Q4: "V2.0 (06-02-2026)",
    HUL_Q1: "V2.1 (26-06-2026)",
    HUL_Q4: "V2.0 (06-02-2026)",
}

#: FINDING (IND-6, group B): LevelOfRounding varies BY ISSUER, not by period:
#: MARUTI and SUNPHARMA declare Millions; the other eight declare Crores.
#: Values are full rupees either way (presentation metadata only).
EXPECTED_ROUNDING_TRAITS = {
    "IN-MARUTI": "Millions",
    "IN-SUNPHARMA": "Millions",
}


class TestInstanceQualifiers:
    """Scope/audit/rounding qualifiers are declared IN the instance and surfaced."""

    @pytest.mark.parametrize("fixture", ALL_FIXTURES)
    def test_common_qualifiers(self, fixture):
        expectations = manifest_expectations()
        inst = parse(fixture)
        expected_tax = expectations[fixture]["taxonomy"]
        assert inst.taxonomy_version == expected_tax, fixture
        assert inst.declared_scope == expectations[fixture]["scope"] == "consolidated", fixture
        assert inst.schema_ref == "in-capmkt-ent-2026-01-31.xsd", fixture
        expected_scheme = (
            "http://www.sebi.gov.in/in-capmkt/Symbol"
            if fixture.startswith("ULTRACEMCO-Q4")
            else "http://www.sebi.gov.in/in-capmkt/ScripCode"
        )
        assert inst.entity_scheme == expected_scheme, fixture

    def test_rounding_trait_varies_by_issuer_and_is_carried(self):
        from india_test_helpers import fixture_entries

        entries = fixture_entries()
        for file, entry in entries.items():
            expected = EXPECTED_ROUNDING_TRAITS.get(entry["issuer_id"], "Crores")
            inst = parse(file)
            assert inst.rounding_trait == expected, file

    def test_ultracemco_q4_entity_scheme_variance_is_carried(self):
        """FINDING (IND-6): 19 of 20 committed instances identify the entity by
        BSE scrip code; ULTRACEMCO's Q4 instance uses the in-capmkt/Symbol
        scheme with the NSE symbol instead. The parser surfaces the scheme
        verbatim — identity joins must use the qualifier facts (ISIN), never
        assume the scrip-code scheme."""
        inst = parse("ULTRACEMCO-Q4FY26-consolidated-nse-integrated-filing-xbrl.xml")
        assert inst.entity_scheme == "http://www.sebi.gov.in/in-capmkt/Symbol"
        assert inst.entity_identifier == "ULTRACEMCO"
        assert inst.isin == "INE481G01011"  # qualifier facts remain the identity

    def test_entity_identifiers_are_bse_scrip_codes(self):
        assert parse(INFY_Q1).entity_identifier == "500209"
        assert parse(HUL_Q1).entity_identifier == "500696"

    def test_isin_and_symbol(self):
        infy = parse(INFY_Q1)
        assert infy.isin == "INE009A01021"
        assert infy.symbol == "INFY"
        hul = parse(HUL_Q1)
        assert hul.isin == "INE030A01027"
        assert hul.symbol == "HINDUNILVR"

    def test_audited_status_variance_is_carried_not_normalized(self):
        # Infosys labels Q1 "Audited"; HUL's limited review is declared "Unaudited".
        assert parse(INFY_Q1).audited_status == "Audited"
        assert parse(HUL_Q1).audited_status == "Unaudited"
        assert parse(INFY_Q4).audited_status == "Audited"
        assert parse(HUL_Q4).audited_status == "Audited"

    def test_audit_qualification_declaration(self):
        assert parse(INFY_Q1).audit_qualification_declaration == "Declaration of unmodified opinion"
        assert parse(HUL_Q1).audit_qualification_declaration == "Not applicable"


class TestPeriodClassificationFromContexts:
    def test_q1_filing_has_only_quarter_duration(self):
        inst = parse(INFY_Q1)
        kinds = {
            ctx.period_kind
            for ctx in inst.contexts.values()
            if not ctx.is_dimensioned and ctx.period_start is not None
        }
        assert kinds == {"quarter"}
        one_d = inst.contexts["OneD"]
        assert (one_d.period_start, one_d.period_end) == ("2026-04-01", "2026-06-30")
        assert one_d.period_kind == "quarter"

    def test_q4_filing_has_quarter_and_annual_durations(self):
        inst = parse(INFY_Q4)
        one_d = inst.contexts["OneD"]
        four_d = inst.contexts["FourD"]
        assert one_d.period_kind == "quarter"
        assert (one_d.period_start, one_d.period_end) == ("2026-01-01", "2026-03-31")
        assert four_d.period_kind == "annual"
        assert (four_d.period_start, four_d.period_end) == ("2025-04-01", "2026-03-31")

    def test_instant_and_prior_year_instant(self):
        inst = parse(HUL_Q4)
        assert inst.contexts["OneI"].is_instant
        assert inst.contexts["OneI"].instant == "2026-03-31"
        assert inst.contexts["PY_I"].instant == "2025-03-31"

    def test_no_6m_or_9m_contexts_in_these_fixtures(self):
        # Cumulative 6M/9M contexts are not present here; classification still
        # distinguishes them (see test_india_periods). Guard against surprises.
        from datetime import date

        from quarterline.sources.india.periods import _month_span

        for fixture in ALL_FIXTURES:
            inst = parse(fixture)
            for ctx in inst.contexts.values():
                if ctx.is_dimensioned or ctx.period_start is None:
                    continue
                span = _month_span(
                    date.fromisoformat(ctx.period_start),
                    date.fromisoformat(ctx.instant or ctx.period_end),
                )
                assert span in (3, 12)

    def test_segment_fact_on_quarter_context_is_undimensioned(self):
        # INFY Q1 carries a SegmentRevenue total on the undimensioned quarter
        # context (segment-disclosure total). It must never be treated as P&L
        # revenue: it stays unmapped (asserted in the pipeline tests).
        inst = parse(INFY_Q1)
        undimensioned_segment = [
            f for f in inst.facts_for_tag("SegmentRevenue") if not f.is_dimensioned
        ]
        assert undimensioned_segment
        assert "SegmentRevenue" in inst.unmapped_tags


class TestConsolidatedValues:
    """Extracted values for both issuers and both periods (full rupees, exact)."""

    def test_infy_q1_fy27(self):
        inst = parse(INFY_Q1)
        assert undim_value(inst, "RevenueFromOperations", "OneD") == Decimal(482110000000)
        assert undim_value(inst, "ProfitBeforeTax", "OneD") == Decimal(110280000000)
        assert undim_value(inst, "ProfitLossForPeriod", "OneD") == Decimal(77750000000)
        assert undim_value(
            inst, "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations", "OneD"
        ) == Decimal("19.19")
        assert undim_value(
            inst, "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations", "OneD"
        ) == Decimal("19.17")

    def test_infy_q4fy26_quarter_and_annual(self):
        inst = parse(INFY_Q4)
        assert undim_value(inst, "RevenueFromOperations", "OneD") == Decimal(464020000000)
        assert undim_value(inst, "ProfitLossForPeriod", "OneD") == Decimal(85090000000)
        assert undim_value(inst, "RevenueFromOperations", "FourD") == Decimal(1786500000000)
        assert undim_value(inst, "ProfitLossForPeriod", "FourD") == Decimal(294740000000)
        # Annual cash flow exists ONLY in the Q4 instance (IND-1 finding).
        assert undim_value(inst, "CashFlowsFromUsedInOperatingActivities", "FourD") == Decimal(
            339860000000
        )
        assert inst.facts_for_tag("CashFlowsFromUsedInOperatingActivities") != []
        # EPS is annual for the FourD context.
        assert undim_value(
            inst, "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations", "FourD"
        ) == Decimal("71.58")

    def test_hul_q1_fy27(self):
        inst = parse(HUL_Q1)
        assert undim_value(inst, "RevenueFromOperations", "OneD") == Decimal(173410000000)
        assert undim_value(inst, "ProfitBeforeTax", "OneD") == Decimal(36320000000)
        assert undim_value(inst, "ProfitLossForPeriod", "OneD") == Decimal(26800000000)
        assert undim_value(
            inst, "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations", "OneD"
        ) == Decimal("11.38")
        # No cash-flow concepts in the quarterly instance.
        assert inst.facts_for_tag("CashFlowsFromUsedInOperatingActivities") == []

    def test_hul_q4fy26_discontinued_operations_visible(self):
        inst = parse(HUL_Q4)
        # FY2025-26 includes the ice-cream demerger: total PAT differs from
        # continuing-operations PAT (both retained, never merged).
        total = undim_value(inst, "ProfitLossForPeriod", "FourD")
        continuing = undim_value(inst, "ProfitLossForPeriodFromContinuingOperations", "FourD")
        assert total == Decimal(150590000000)
        assert continuing == Decimal(106670000000)
        assert undim_value(inst, "CashFlowsFromUsedInOperatingActivities", "FourD") == Decimal(
            109990000000
        )
        assert undim_value(
            inst,
            "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
            "FourD",
        ) == Decimal(12580000000)

    def test_exceptional_items_captured(self):
        assert undim_value(parse(HUL_Q1), "ExceptionalItemsBeforeTax", "OneD") == Decimal(
            -750000000
        )
        assert undim_value(parse(INFY_Q4), "ExceptionalItemsBeforeTax", "FourD") == Decimal(
            -12890000000
        )


class TestUnitsAndDecimals:
    def test_units(self):
        inst = parse(INFY_Q1)
        revenue = inst.facts_for_tag("RevenueFromOperations")[0]
        assert revenue.unit_ref == "INR"
        assert revenue.decimals == "-7"
        eps = inst.facts_for_tag(
            "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations"
        )[0]
        assert eps.unit_ref == "INR/shares"
        assert eps.decimals == "INF"
        assert eps.is_per_share_unit
        assert not revenue.is_per_share_unit

    def test_dimensioned_facts_are_separable(self):
        inst = parse(INFY_Q1)
        segment = [f for f in inst.facts_for_tag("SegmentRevenue") if f.is_dimensioned]
        assert segment, "segment facts exist"
        assert all(f.context.dimensions for f in segment)

    def test_all_committed_fixtures_parse(self):
        for fixture in ALL_FIXTURES:
            inst = parse(fixture)
            assert len(inst.facts) > 50
            assert inst.qualifiers.get("DescriptionOfPresentationCurrency") == "INR"


class TestFilingMetaCarried:
    def test_revision_status_comes_from_metadata_not_instance(self):
        from datetime import date

        from quarterline.sources.india.revisions import FilingMeta

        meta = FilingMeta(
            issuer_id="IN-INFY",
            scope="consolidated",
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
            published_at=date(2026, 7, 23),
            revision_status="Original",
            audited_status="Audited",
        )
        inst = parse_instance((INDIA_FIXTURES_DIR / INFY_Q1).read_bytes(), filing_meta=meta)
        assert inst.filing_meta is meta
        assert inst.filing_meta.revision_status == "Original"
        # The instance itself declares no revision field:
        assert "revision" not in {k.lower() for k in inst.qualifiers}

    def test_path_and_bytes_parsing_agree(self, tmp_path):
        source = INDIA_FIXTURES_DIR / INFY_Q1
        assert (
            parse_instance(source).rounding_trait
            == parse_instance(source.read_bytes()).rounding_trait
        )
