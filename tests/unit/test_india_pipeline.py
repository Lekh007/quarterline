"""India observation-pipeline tests (IND-2/IND-3): mapping into fact_observations + idempotency."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from india_test_helpers import INDIA_FIXTURES_DIR, INFY_Q1
from sqlalchemy import select

from quarterline.core.normalization import observation_hash
from quarterline.sources.india.cash_flow import (
    cash_flow_availability,
    reported_from_observation_rows,
)
from quarterline.sources.india.data_status import MissingDataStatus
from quarterline.sources.india.issuers import require_verified
from quarterline.sources.india.pipeline import ingest_observations
from quarterline.store.db import session_scope
from quarterline.store.models import FactObservation

#: NSE seq ids identifying each consolidated filing (from the manifest).
INFY_Q1_SEQ = "177385"
INFY_Q4_SEQ = "152465"
HUL_Q1_SEQ = "179457"
HUL_Q4_SEQ = "155083"


def _observations(concept: str | None = None) -> list[FactObservation]:
    with session_scope() as session:
        stmt = select(FactObservation).where(FactObservation.taxonomy == "in-capmkt")
        if concept is not None:
            stmt = stmt.where(FactObservation.canonical_concept == concept)
        rows = list(session.scalars(stmt.order_by(FactObservation.id)))
        session.expunge_all()
        return rows


def _by_seq(rows: list[FactObservation], seq: str) -> list[FactObservation]:
    return [o for o in rows if o.accession == seq]


def _synthetic_instance_xml(
    *,
    period_start: str,
    period_end: str,
    cfo_value: str,
    context_id: str = "OneD",
) -> bytes:
    """A CLEARLY SYNTHETIC in-capmkt instance carrying ONE cash-flow fact.

    Used only to prove the parser accepts legitimately reported non-annual
    cash-flow durations (IND-3 correction A) — never presented as company
    results (no real quarterly/half-yearly exchange CF existed to acquire).
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!-- SYNTHETIC TEST FIXTURE: clearly-labeled parser test, not a company filing -->\n"
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"'
        ' xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt"'
        ' xmlns:iso4217="http://www.xbrl.org/2003/iso4217">\n'
        f'<xbrli:context id="{context_id}">\n'
        "  <xbrli:entity>\n"
        '    <xbrli:identifier scheme="http://www.sebi.gov.in/in-capmkt/ScripCode">500209</xbrli:identifier>\n'
        "  </xbrli:entity>\n"
        f"  <xbrli:period><xbrli:startDate>{period_start}</xbrli:startDate>"
        f"<xbrli:endDate>{period_end}</xbrli:endDate></xbrli:period>\n"
        "</xbrli:context>\n"
        '<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>\n'
        f'<in-capmkt:NatureOfReportStandaloneConsolidated contextRef="{context_id}">Consolidated</in-capmkt:NatureOfReportStandaloneConsolidated>\n'
        f'<in-capmkt:LevelOfRounding contextRef="{context_id}">Crores</in-capmkt:LevelOfRounding>\n'
        f'<in-capmkt:CashFlowsFromUsedInOperatingActivities contextRef="{context_id}"'
        f' unitRef="INR" decimals="-7">{cfo_value}</in-capmkt:CashFlowsFromUsedInOperatingActivities>\n'
        "</xbrli:xbrl>\n"
    ).encode()


def _import_synthetic(path, period_start: str, period_end: str, seq: str) -> None:
    from quarterline.sources.india.ir_documents import import_document

    import_document(
        issuer_id="IN-INFY",
        path=path,
        doc_type="financial_results",
        period_start=date.fromisoformat(period_start),
        period_end=date.fromisoformat(period_end),
        scope="consolidated",
        published_at=date(2026, 10, 20),
        seq_id=seq,
        revision_status="Original",
        notes="SYNTHETIC parser-test instance, not a company filing",
    )


class TestIngestObservations:
    def test_ingest_populates_observations(self, india_imported):
        _, _ = india_imported
        infy_report = ingest_observations("IN-INFY")
        hul_report = ingest_observations("IN-HINDUNILVR")
        assert infy_report.artifacts_parsed == 2
        assert hul_report.artifacts_parsed == 2
        assert infy_report.observations_inserted > 0
        assert infy_report.observations_skipped == 0

    def test_idempotent_rerun_adds_zero_rows(self, india_imported):
        _, _ = india_imported
        first_infy = ingest_observations("IN-INFY")
        first_hul = ingest_observations("IN-HINDUNILVR")
        total_after_first = len(_observations())
        assert (
            total_after_first == first_infy.observations_inserted + first_hul.observations_inserted
        )

        second_infy = ingest_observations("IN-INFY")
        second_hul = ingest_observations("IN-HINDUNILVR")
        assert second_infy.observations_inserted == 0
        assert second_hul.observations_inserted == 0
        assert second_infy.observations_skipped == first_infy.observations_inserted
        assert len(_observations()) == total_after_first

    def test_duplicate_import_is_idempotent_at_artifact_level(self, india_imported):
        """Re-importing the same official file reuses the artifact row (content-addressed)."""
        from pathlib import Path

        from quarterline.sources.india.ir_documents import import_document
        from quarterline.store.models import SourceArtifact

        _, _ = india_imported
        before = ingest_observations("IN-INFY")
        import_document(
            issuer_id="IN-INFY",
            path=INDIA_FIXTURES_DIR / INFY_Q1,
            doc_type="financial_results",
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
            scope="consolidated",
            published_at=date(2026, 7, 23),
            seq_id=INFY_Q1_SEQ,
            revision_status="Original",
        )
        with session_scope() as session:
            artifacts = list(
                session.scalars(select(SourceArtifact).where(SourceArtifact.source == "india"))
            )
        infy_names = [Path(a.local_path).name for a in artifacts]
        # the duplicate import did not create a second copy of the same document
        assert infy_names.count(INFY_Q1) == 1
        after = ingest_observations("IN-INFY")
        assert after.observations_inserted == 0
        assert before.observations_inserted > 0

    def test_quarter_and_annual_contexts_are_distinct_observations(self, india_imported):
        _, _ = india_imported
        ingest_observations("IN-INFY")
        revenue = _observations("revenue_from_operations")
        infy_q4_revenue = _by_seq(revenue, INFY_Q4_SEQ)
        kinds = {o.period_kind for o in infy_q4_revenue}
        assert kinds == {"quarter", "annual"}
        q4 = next(o for o in infy_q4_revenue if o.period_kind == "quarter")
        fy = next(o for o in infy_q4_revenue if o.period_kind == "annual")
        assert (q4.period_start, q4.period_end) == (date(2026, 1, 1), date(2026, 3, 31))
        assert (fy.period_start, fy.period_end) == (date(2025, 4, 1), date(2026, 3, 31))
        assert q4.source_fy == 2026 and q4.source_fp == "Q4"
        assert fy.source_fy == 2026 and fy.source_fp == "FY"
        # Q1 FY27 lands in the next fiscal year.
        infy_q1_revenue = _by_seq(revenue, INFY_Q1_SEQ)
        assert all(o.source_fy == 2027 and o.source_fp == "Q1" for o in infy_q1_revenue)

    def test_fiscal_labels_match_period_dates_both_issuers(self, india_imported):
        """Every observation's fiscal label is consistent with its context dates."""
        _, _ = india_imported
        ingest_observations("IN-INFY")
        ingest_observations("IN-HINDUNILVR")
        expectations = {
            (date(2026, 4, 1), date(2026, 6, 30), "quarter"): (2027, "Q1"),
            (date(2026, 1, 1), date(2026, 3, 31), "quarter"): (2026, "Q4"),
            (date(2025, 4, 1), date(2026, 3, 31), "annual"): (2026, "FY"),
        }
        checked = 0
        for row in _observations():
            pair = (row.period_start, row.period_end, row.period_kind)
            if pair in expectations:
                fy, fp = expectations[pair]
                assert (row.source_fy, row.source_fp) == (fy, fp), row.original_tag
                checked += 1
        assert checked > 20  # the four fixtures' duration observations all conform

    def test_values_are_exact_full_rupees(self, india_imported):
        _, _ = india_imported
        ingest_observations("IN-INFY")
        revenue = _observations("revenue_from_operations")
        q1 = next(o for o in _by_seq(revenue, INFY_Q1_SEQ) if o.period_kind == "quarter")
        assert Decimal(q1.value_decimal) == Decimal(482110000000)
        assert q1.unit == "INR"
        assert q1.currency == "INR"
        assert q1.taxonomy == "in-capmkt"
        assert q1.original_tag == "RevenueFromOperations"
        assert q1.form == "IFIndAs"
        assert q1.filed_at == date(2026, 7, 23)

    def test_eps_never_scaled(self, india_imported):
        _, _ = india_imported
        ingest_observations("IN-INFY")
        eps = _observations("eps_basic")
        q1 = next(o for o in _by_seq(eps, INFY_Q1_SEQ))
        assert q1.unit == "INR/share"
        assert Decimal(q1.value_decimal) == Decimal("19.19")
        metadata = json.loads(q1.context_metadata_json)
        assert metadata["rounding_trait"] == "Crores"  # carried as metadata, never applied

    def test_scope_and_revision_in_context_metadata(self, india_imported):
        _, _ = india_imported
        ingest_observations("IN-INFY")
        revenue = _observations("revenue_from_operations")
        q1 = next(o for o in _by_seq(revenue, INFY_Q1_SEQ))
        metadata = json.loads(q1.context_metadata_json)
        assert q1.reporting_scope == "consolidated"
        assert metadata["reporting_scope"] == "consolidated"
        assert metadata["revision_status"] == "Original"
        assert metadata["audited_status"] == "Audited"
        assert metadata["namespace"] == "in-capmkt"

    def test_hul_q1_unaudited_status_carried(self, india_imported):
        """HUL labels Q1 'Unaudited' (limited review) vs Infosys 'Audited'."""
        _, _ = india_imported
        ingest_observations("IN-HINDUNILVR")
        revenue = _observations("revenue_from_operations")
        hul_q1 = next(o for o in _by_seq(revenue, HUL_Q1_SEQ))
        assert json.loads(hul_q1.context_metadata_json)["audited_status"] == "Unaudited"

    def test_cash_flow_annual_accepted_and_ingested(self, india_imported):
        """Annual CFO/capex from the Q4 filings ingest as period_kind 'annual'."""
        _, _ = india_imported
        ingest_observations("IN-INFY")
        ingest_observations("IN-HINDUNILVR")
        cfo = _observations("cash_flow_operations")
        assert len(cfo) == 2  # one per ingested issuer; Q1 exchange instances carry none
        for row in cfo:
            assert row.period_kind == "annual"
            assert (row.period_start, row.period_end) == (date(2025, 4, 1), date(2026, 3, 31))
        infy = next(o for o in _by_seq(cfo, INFY_Q4_SEQ))
        hul = next(o for o in _by_seq(cfo, HUL_Q4_SEQ))
        assert Decimal(infy.value_decimal) == Decimal(339860000000)
        assert Decimal(hul.value_decimal) == Decimal(109990000000)
        capex = _observations("capex")
        assert all(o.period_kind == "annual" for o in capex)

    def test_reported_quarterly_cash_flow_accepted(self, india_store, tmp_path):
        """CORRECTION A: a legitimately REPORTED quarterly CFO must be extractable.

        The IND-2 'annual instances only' restriction is gone. The instance is a
        CLEARLY SYNTHETIC parser test (labeled in the XML itself) because no real
        quarterly exchange CF exists in the fixtures.
        """
        xml = _synthetic_instance_xml(
            period_start="2026-04-01",
            period_end="2026-06-30",
            cfo_value="93300000000",
        )
        path = tmp_path / "SYNTHETIC-INFY-quarterly-cf.xml"
        path.write_bytes(xml)
        _import_synthetic(path, "2026-04-01", "2026-06-30", "SYNTHQ1")
        report = ingest_observations("IN-INFY")
        cfo = _observations("cash_flow_operations")
        quarterly = [o for o in cfo if o.accession == "SYNTHQ1"]
        assert len(quarterly) == 1
        row = quarterly[0]
        assert row.period_kind == "quarter"
        assert (row.period_start, row.period_end) == (date(2026, 4, 1), date(2026, 6, 30))
        assert Decimal(row.value_decimal) == Decimal(93300000000)
        assert row.unit == "INR"
        assert report.observations_inserted >= 1

    def test_reported_half_year_cash_flow_accepted(self, india_store, tmp_path):
        """A half-year CFO (6-month duration) is equally legitimate when reported."""
        xml = _synthetic_instance_xml(
            period_start="2026-04-01",
            period_end="2026-09-30",
            cfo_value="1230000000",
        )
        path = tmp_path / "SYNTHETIC-INFY-halfyear-cf.xml"
        path.write_bytes(xml)
        _import_synthetic(path, "2026-04-01", "2026-09-30", "SYNTHH1")
        ingest_observations("IN-INFY")
        row = next(o for o in _observations("cash_flow_operations") if o.accession == "SYNTHH1")
        assert row.period_kind == "year_to_date"  # 6-month cumulative, from dates only
        assert (row.period_start, row.period_end) == (date(2026, 4, 1), date(2026, 9, 30))
        assert Decimal(row.value_decimal) == Decimal(1230000000)

    def test_missing_quarterly_cash_flow_is_not_present_not_zero(self, india_imported):
        """HUL Q1 FY27: quarterly CF is NOT in the ingested sources — a status, never 0."""

        _, _ = india_imported
        ingest_observations("IN-HINDUNILVR")
        hul_q1_rows = _by_seq(_observations(), HUL_Q1_SEQ)
        assert hul_q1_rows  # the Q1 filing produced P&L observations...
        assert not [o for o in hul_q1_rows if o.canonical_concept == "cash_flow_operations"]
        reported = reported_from_observation_rows(_observations("cash_flow_operations"))
        status, observation = cash_flow_availability(
            "cash_flow_operations",
            reported,
            scope="consolidated",
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
        )
        assert status == MissingDataStatus.NOT_PRESENT_IN_INGESTED_SOURCES.value
        assert observation is None
        # and no zero was invented for the period either
        zero_cfs = [
            o
            for o in _observations()
            if o.canonical_concept == "cash_flow_operations"
            and o.period_end == date(2026, 6, 30)
            and Decimal(o.value_decimal) == 0
        ]
        assert not zero_cfs

    def test_unknown_unit_semantics_skipped_not_guessed(self, india_store, tmp_path):
        """A mapped tag in an unknown unit is counted for review, never ingested as INR."""
        xml = _synthetic_instance_xml(
            period_start="2026-04-01",
            period_end="2026-06-30",
            cfo_value="100",
        ).decode("utf-8")
        # Swap the unit declaration to pure shares — an unknown semantic for a
        # money concept. The pipeline must skip it (review), not store INR 100.
        xml = xml.replace(
            '<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>',
            '<xbrli:unit id="INR"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>',
        )
        path = tmp_path / "SYNTHETIC-INFY-unknown-unit.xml"
        path.write_text(xml, encoding="utf-8")
        _import_synthetic(path, "2026-04-01", "2026-06-30", "SYNTHUNIT")
        report = ingest_observations("IN-INFY")
        assert report.skipped_unknown_unit >= 1
        assert not [o for o in _observations() if o.accession == "SYNTHUNIT"]

    def test_unmapped_tags_reported_not_ingested(self, india_imported):
        _, _ = india_imported
        report = ingest_observations("IN-INFY")
        assert "SegmentRevenue" in report.unmapped_tags  # segment total, not P&L revenue
        assert "OtherIncome" in report.unmapped_tags
        ingested_tags = {o.original_tag for o in _observations()}
        assert "SegmentRevenue" not in ingested_tags

    def test_dimensioned_and_undimensioned_segment_totals_never_ingested(self, india_imported):
        """The INFY Q1 SegmentRevenue total (48,211 Cr == P&L revenue) stays unmapped."""
        _, _ = india_imported
        ingest_observations("IN-INFY")
        revenue = [
            o for o in _observations("revenue_from_operations") if o.period_end == date(2026, 6, 30)
        ]
        # exactly ONE revenue observation for the quarter (the P&L line), even
        # though SegmentRevenue carries the same value on the same context
        assert len(revenue) == 1
        assert revenue[0].original_tag == "RevenueFromOperations"
        from quarterline.sources.india.xbrl_parse import parse_instance

        instance = parse_instance((INDIA_FIXTURES_DIR / INFY_Q1).read_bytes())
        trap = [
            f
            for f in instance.undimensioned_numeric_facts()
            if f.tag == "SegmentRevenue" and f.value_decimal == Decimal(482110000000)
        ]
        assert trap  # the trap exists in the real fixture...
        assert all(f.context_ref == "OneD" for f in trap)

    def test_revenue_and_total_income_are_distinct_concepts(self, india_imported):
        """revenue_from_operations ≠ total_income: distinct concepts, distinct values."""
        _, _ = india_imported
        ingest_observations("IN-INFY")
        ingest_observations("IN-HINDUNILVR")

        def keyed(concept: str) -> dict:
            return {
                (o.accession, o.period_kind, o.period_end): Decimal(o.value_decimal)
                for o in _observations(concept)
                if o.original_tag in ("RevenueFromOperations", "Income")
            }

        revenue = keyed("revenue_from_operations")
        income = keyed("total_income")
        assert set(revenue) == set(income) and len(revenue) == 6
        for key, value in revenue.items():
            assert income[key] != value, key  # every reported period differs
            assert income[key] > value  # total income includes other income

    def test_pat_and_attributable_to_owners_are_distinct(self, india_imported):
        """profit_after_tax ≠ profit_attributable_to_owners (NCI difference preserved)."""
        _, _ = india_imported
        ingest_observations("IN-INFY")
        ingest_observations("IN-HINDUNILVR")

        def keyed(concept: str, tag: str) -> dict:
            return {
                (o.accession, o.period_kind, o.period_end): Decimal(o.value_decimal)
                for o in _observations(concept)
                if o.original_tag == tag
            }

        pat = keyed("profit_after_tax", "ProfitLossForPeriod")
        owners = keyed("profit_attributable_to_owners", "ProfitOrLossAttributableToOwnersOfParent")
        assert set(pat) == set(owners)
        differing = [k for k in pat if pat[k] != owners[k]]
        assert differing  # NCI is non-zero somewhere in every filing set
        # INFY Q1: 7,775 group vs 7,769 owners
        assert pat[(INFY_Q1_SEQ, "quarter", date(2026, 6, 30))] == Decimal(77750000000)
        assert owners[(INFY_Q1_SEQ, "quarter", date(2026, 6, 30))] == Decimal(77690000000)

    def test_unverified_issuer_never_ingests(self, india_imported, tmp_path, monkeypatch):
        """Guard exercised against a synthetic all-proposed registry (the live
        registry verified all 10 rows in IND-6)."""
        from india_test_helpers import write_all_proposed_watchlist

        from quarterline.sources.india import issuers as issuers_module

        monkeypatch.setattr(
            issuers_module, "DEFAULT_WATCHLIST_PATH", write_all_proposed_watchlist(tmp_path)
        )
        with pytest.raises(ValueError, match="not verified"):
            require_verified("IN-TCS")

    def test_pipeline_without_import_fails_cleanly(self, india_store):
        with pytest.raises(LookupError, match="import a document first"):
            ingest_observations("IN-INFY")

    def test_observation_hash_identity(self, india_store):
        """Same value re-reported = one observation; a revised value hashes differently."""
        from quarterline.sources.india.ir_documents import import_document

        import_document(
            issuer_id="IN-INFY",
            path=INDIA_FIXTURES_DIR / INFY_Q1,
            doc_type="financial_results",
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
            scope="consolidated",
            published_at=date(2026, 7, 23),
            seq_id=INFY_Q1_SEQ,
            revision_status="Original",
        )
        ingest_observations("IN-INFY")
        original = _observations("revenue_from_operations")[0]
        common = (
            "INE009A01021",
            "in-capmkt",
            "RevenueFromOperations",
            "INR",
            date(2026, 4, 1),
            date(2026, 6, 30),
        )
        assert original.observation_hash == observation_hash(
            *common, Decimal(482110000000), "consolidated"
        )
        # SYNTHETIC revised value (no real revision was ever published): a
        # genuinely different number hashes differently so both would be retained.
        revised = observation_hash(*common, Decimal(499990000000), "consolidated")
        assert revised != original.observation_hash
        # Standalone scope is a different identity even for the same value.
        standalone = observation_hash(*common, Decimal(482110000000), "standalone")
        assert standalone != original.observation_hash
