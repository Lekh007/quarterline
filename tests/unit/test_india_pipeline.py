"""India observation-pipeline tests (IND-2): mapping into fact_observations + idempotency."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from india_test_helpers import INDIA_FIXTURES_DIR, INFY_Q1
from sqlalchemy import select

from quarterline.core.normalization import observation_hash
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

    def test_cash_flow_only_from_annual_instance(self, india_imported):
        """CFO/capex exist only at reported (annual) frequency — never fabricated."""
        _, _ = india_imported
        ingest_observations("IN-INFY")
        ingest_observations("IN-HINDUNILVR")
        cfo = _observations("cash_flow_operations")
        assert len(cfo) == 2  # one per issuer; Q1 instances carried none
        for row in cfo:
            assert row.period_kind == "annual"
            assert (row.period_start, row.period_end) == (date(2025, 4, 1), date(2026, 3, 31))
        infy = next(o for o in _by_seq(cfo, INFY_Q4_SEQ))
        hul = next(o for o in _by_seq(cfo, HUL_Q4_SEQ))
        assert Decimal(infy.value_decimal) == Decimal(339860000000)
        assert Decimal(hul.value_decimal) == Decimal(109990000000)
        capex = _observations("capex")
        assert all(o.period_kind == "annual" for o in capex)

    def test_unmapped_tags_reported_not_ingested(self, india_imported):
        _, _ = india_imported
        report = ingest_observations("IN-INFY")
        assert "SegmentRevenue" in report.unmapped_tags  # segment total, not P&L revenue
        assert "OtherIncome" in report.unmapped_tags
        ingested_tags = {o.original_tag for o in _observations()}
        assert "SegmentRevenue" not in ingested_tags

    def test_both_eps_variants_retained(self, india_imported):
        """Headline and continuing-only EPS both stay as observations (never merged)."""
        _, _ = india_imported
        ingest_observations("IN-INFY")
        basic = _observations("eps_basic")
        tags = {o.original_tag for o in basic}
        assert "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations" in tags
        assert "BasicEarningsLossPerShareFromContinuingOperations" in tags

    def test_unverified_issuer_never_ingests(self, india_imported):
        _, _ = india_imported
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
