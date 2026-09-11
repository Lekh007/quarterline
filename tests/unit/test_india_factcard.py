"""India fact-card tests (IND-4): build_india_fact_card + coverage_report.

The card is the data layer IND-5 reads: verified issuer identity, exact period
dates with source + application labels, canonical facts with full provenance,
metrics with formula versions, and a per-concept coverage block carrying the
exact missing-data statuses. Consolidated is the default with an explicit scope
override; no aggregate score exists anywhere on the card.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from india_test_helpers import (
    run_ind4_pipeline,
    synthetic_instance_xml,
)

from quarterline.sources.india.factcard import (
    NO_SCORE_NOTE,
    build_india_fact_card,
    coverage_report,
    render_india_fact_card,
)
from quarterline.sources.india.normalization import normalize_canonical_facts
from quarterline.sources.india.pipeline import ingest_observations
from quarterline.store.db import session_scope
from quarterline.store.models import FactObservation


class TestBuildCard:
    def test_consolidated_default_latest_period(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session:
            card = build_india_fact_card(session, "IN-INFY")
        assert card.scope == "consolidated"
        # latest period end in the consolidated scope
        assert card.period_identities[0].period_end == date(2026, 6, 30)
        assert card.period_identities[0].period_kind == "quarter"
        assert card.period_identities[0].application_label == (
            "Q1 FY2026-27 (2026-04-01..2026-06-30)"
        )
        # the source's own labels are carried alongside (never trusted)
        assert 'ReportingQuarter="First quarter"' in (card.period_identities[0].source_label or "")
        issuer = card.issuer
        assert issuer.issuer_id == "IN-INFY"
        assert issuer.isin == "INE009A01021"
        assert issuer.ticker_nse == "INFY"
        assert issuer.bse_code == "500209"
        assert issuer.verification_status == "verified"

    def test_period_end_selects_shared_end_identities(self, india_imported):
        """period_end=2026-03-31 shows BOTH the Q4 quarter and the annual."""
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session:
            card = build_india_fact_card(session, "IN-INFY", period_end="2026-03-31")
        kinds = {identity.period_kind for identity in card.period_identities}
        assert kinds == {"quarter", "annual"}
        facts_by_kind = {(fact.period_kind, fact.concept) for fact in card.canonical_facts}
        assert ("quarter", "revenue_from_operations") in facts_by_kind
        assert ("annual", "revenue_from_operations") in facts_by_kind

    def test_canonical_facts_with_provenance(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session:
            card = build_india_fact_card(session, "IN-INFY", period_end="2026-03-31")
        revenue = next(
            f
            for f in card.canonical_facts
            if f.period_kind == "quarter" and f.concept == "revenue_from_operations"
        )
        assert revenue.value == Decimal(464020000000)
        assert revenue.display == "₹46,402 Cr"
        assert revenue.data_quality_status == "agent_checked_against_document"
        provenance = revenue.provenance
        assert provenance.observation_ids
        assert provenance.accession == "152465"
        assert provenance.published_at == date(2026, 4, 23)
        assert provenance.revision_status == "Original"
        assert provenance.audited_status == "Audited"
        assert provenance.original_tag == "RevenueFromOperations"
        assert provenance.review_status == "agent_checked_against_document"
        assert provenance.source_url and provenance.source_url.startswith("https://")
        assert provenance.artifact_id is not None
        # provenance observation ids resolve to real observations
        with session_scope() as session:
            for obs_id in provenance.observation_ids:
                assert session.get(FactObservation, obs_id) is not None

    def test_pdf_cash_flow_provenance_on_card(self, india_imported):
        """INFY Q1 CFO carries its IR-PDF provenance on the card."""
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session:
            card = build_india_fact_card(session, "IN-INFY")  # latest = Q1 FY27
        cfo = next(f for f in card.canonical_facts if f.concept == "cash_flow_operations")
        assert cfo.value == Decimal(93300000000)
        assert cfo.display == "₹9,330 Cr"
        assert cfo.period_kind == "quarter"
        assert "consol-fy27-q1-finstatement.pdf" in (cfo.provenance.filing_identifier or "")
        assert cfo.provenance.review_status == "agent_checked_against_document"

    def test_metrics_carry_formula_versions_and_statuses(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session:
            card = build_india_fact_card(session, "IN-INFY", period_end="2026-03-31")
        assert card.metrics
        for metric in card.metrics:
            assert metric.formula_version == "india-metrics-v1"
            assert metric.status in ("ok", "missing", "invalid", "unsuitable")
        qoq = next(m for m in card.metrics if m.metric_id == "india_revenue_qoq")
        assert qoq.status == "missing"  # Q3 FY26 not ingested
        yoy = next(m for m in card.metrics if m.metric_id == "india_revenue_yoy")
        assert yoy.status == "missing"
        # the annual identity's metrics carry the @annual persisted id
        annual_margins = [m for m in card.metrics if m.metric_id == "india_pat_margin_owners"]
        assert {m.persisted_metric_id for m in annual_margins} == {
            "india_pat_margin_owners",
            "india_pat_margin_owners@annual",
        }

    def test_coverage_block_correctness(self, india_imported):
        """Per-concept cells: present / missing + the exact missing status."""
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session:
            infy_card = build_india_fact_card(session, "IN-INFY")  # Q1 FY27
            hul_card = build_india_fact_card(session, "IN-HINDUNILVR")  # Q1 FY27
        infy_cells = {c.concept: c for c in infy_card.coverage}
        assert infy_cells["cash_flow_operations"].status == "present"
        assert infy_cells["capex"].status == "missing"
        assert infy_cells["capex"].missing_status == "not_present_in_ingested_sources"
        hul_cells = {c.concept: c for c in hul_card.coverage}
        # HUL has NO quarterly cash flow in any ingested source — status, not 0
        assert hul_cells["cash_flow_operations"].status == "missing"
        assert hul_cells["cash_flow_operations"].missing_status == "not_present_in_ingested_sources"
        assert hul_cells["cash_flow_operations"].value is None
        assert hul_cells["capex"].status == "missing"

    def test_derivations_report_blocked_status(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session:
            card = build_india_fact_card(session, "IN-HINDUNILVR")
        assert card.derivations
        for derivation in card.derivations:
            assert derivation.status == "source_not_ingested"
            assert derivation.target_label.startswith("H2")
            assert derivation.target_label != "Q4"

    def test_no_score_note_and_hul_review_gate(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session:
            card = build_india_fact_card(session, "IN-HINDUNILVR")
        assert card.no_score_note == NO_SCORE_NOTE
        revenue = next(f for f in card.canonical_facts if f.concept == "revenue_from_operations")
        # the scrambling review-pending item is carried, not resolved
        assert revenue.data_quality_status == "requires_manual_review"
        assert revenue.provenance.review_status == "human_review_pending"
        assert revenue.value == Decimal(173410000000)

    def test_explicit_scope_override(self, india_imported, tmp_path):
        """scope='standalone' returns the standalone series (never consolidated)."""
        from datetime import date as _date

        from quarterline.sources.india.ir_documents import import_document

        _, _ = india_imported
        run_ind4_pipeline()
        path = tmp_path / "SYNTHETIC-INFY-standalone-card.xml"
        path.write_bytes(
            synthetic_instance_xml(
                period_start="2026-04-01",
                period_end="2026-06-30",
                revenue_value="41000000000",
                scope="standalone",
            )
        )
        import_document(
            issuer_id="IN-INFY",
            path=path,
            doc_type="financial_results",
            period_start=_date(2026, 4, 1),
            period_end=_date(2026, 6, 30),
            scope="standalone",
            published_at=_date(2026, 8, 15),
            seq_id="SYNTHSA2",
            notes="SYNTHETIC card-test instance, not a company filing",
        )
        ingest_observations("IN-INFY")  # pick up the synthetic import
        normalize_canonical_facts("IN-INFY")
        with session_scope() as session:
            card = build_india_fact_card(session, "IN-INFY", scope="standalone")
        assert card.scope == "standalone"
        revenue = next(f for f in card.canonical_facts if f.concept == "revenue_from_operations")
        assert revenue.value == Decimal(41000000000)
        assert revenue.reporting_scope == "standalone"

    def test_unknown_period_end_raises(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session, pytest.raises(LookupError):
            build_india_fact_card(session, "IN-INFY", period_end="2019-03-31")

    def test_unnormalized_store_raises_with_instruction(self, india_store):
        with (
            session_scope() as session,
            pytest.raises(LookupError, match="import a document first"),
        ):
            build_india_fact_card(session, "IN-INFY")


class TestCoverageReport:
    def test_across_all_ingested_periods(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session:
            report = coverage_report(session, "IN-INFY")
        labels = [identity.application_label for identity in report.identities]
        assert len(report.identities) == 3  # FY26 annual, Q4 FY26, Q1 FY27
        assert any("FY2025-26 annual" in label for label in labels)
        assert any(label.startswith("Q4 FY2025-26") for label in labels)
        assert any(label.startswith("Q1 FY2026-27") for label in labels)
        # each identity covers every canonical concept
        assert all(len(identity.cells) == 10 for identity in report.identities)
        annual = next(i for i in report.identities if "annual" in i.application_label)
        assert all(cell.status == "present" for cell in annual.cells)
        q4 = next(i for i in report.identities if i.application_label.startswith("Q4"))
        by_concept = {cell.concept: cell for cell in q4.cells}
        # the Q4 QUARTER identity has no cash flow (the annual is a separate one)
        assert by_concept["cash_flow_operations"].status == "missing"
        assert by_concept["cash_flow_operations"].value is None

    def test_render_includes_provenance_and_no_score(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session:
            card = build_india_fact_card(session, "IN-INFY")
        text = render_india_fact_card(card)
        assert "India fact card — IN-INFY" in text
        assert "agent_checked_against_document" in text
        assert "india-metrics-v1" in text
        assert "not_present_in_ingested_sources" in text
        assert "No aggregate score exists" in text
