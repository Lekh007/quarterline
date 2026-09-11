"""India canonical-fact selection tests (IND-4, policy india-normalization-v1).

Selection: consolidated default with standalone retained (never substituted),
latest-publication wins while history is retained, quarter/annual coexist,
India concepts never collide with US METRIC_IDS. Lineage: every canonical fact
links to its winning observation; orphans are impossible. Data quality: the
IND-3 review status rides on the fact (HUL P&L scrambling -> requires_manual_review,
never resolved). Idempotency: a second run adds zero rows at every layer.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from india_test_helpers import (
    INDIA_FIXTURES_DIR,
    run_ind4_pipeline,
    synthetic_instance_xml,
)
from sqlalchemy import select

from quarterline.core.models import METRIC_IDS
from quarterline.sources.india.concept_map import INDIA_CONCEPTS
from quarterline.sources.india.data_status import MissingDataStatus
from quarterline.sources.india.metrics import INDIA_METRIC_IDS
from quarterline.sources.india.normalization import (
    DQ_AGENT_CHECKED,
    DQ_REQUIRES_MANUAL_REVIEW,
    NORMALIZATION_VERSION,
    SELECTION_POLICY,
    cash_flow_derivation_statuses,
    normalize_canonical_facts,
)
from quarterline.sources.india.pipeline import ingest_reviewed_pdf_cash_flow
from quarterline.store.db import session_scope
from quarterline.store.models import (
    DerivedMetric,
    FactLineage,
    FactObservation,
    NormalizedFact,
    SourceArtifact,
)


def _india_company_ids() -> list[int]:
    with session_scope() as session:
        ids = list(
            session.scalars(
                select(FactObservation.company_id)
                .where(FactObservation.taxonomy == "in-capmkt")
                .distinct()
            )
        )
    return ids


def _india_facts(scope: str | None = None) -> list[NormalizedFact]:
    company_ids = _india_company_ids()
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(NormalizedFact)
                .where(NormalizedFact.company_id.in_(company_ids))
                .order_by(NormalizedFact.id)
            )
        )
        session.expunge_all()
    if scope is None:
        return rows
    return [row for row in rows if row.reporting_scope == scope]


def _import_synthetic(
    path, period_start: str, period_end: str, seq: str, scope="consolidated"
) -> None:
    """Import a clearly-synthetic instance and ingest its observations."""
    from quarterline.sources.india.ir_documents import import_document
    from quarterline.sources.india.pipeline import ingest_observations

    import_document(
        issuer_id="IN-INFY",
        path=path,
        doc_type="financial_results",
        period_start=date.fromisoformat(period_start),
        period_end=date.fromisoformat(period_end),
        scope=scope,
        published_at=date.fromisoformat("2026-08-15"),  # later than any real filing
        seq_id=seq,
        revision_status="Original",
        notes="SYNTHETIC selection-test instance, not a company filing",
    )
    ingest_observations("IN-INFY")


def _infy_company_id() -> int:
    with session_scope() as session:
        return session.scalar(
            select(FactObservation.company_id).where(
                FactObservation.taxonomy == "in-capmkt",
                FactObservation.canonical_concept == "revenue_from_operations",
                FactObservation.value_decimal == "482110000000",
            )
        )


class TestSelection:
    def test_policy_and_version_stamped_on_every_fact(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()
        facts = _india_facts()
        assert facts
        for fact in facts:
            assert fact.selection_policy == SELECTION_POLICY
            assert fact.normalization_version == NORMALIZATION_VERSION
            assert fact.is_derived is False
            assert fact.concept in INDIA_CONCEPTS

    def test_consolidated_is_default_series(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()
        consolidated = _india_facts("consolidated")
        assert consolidated
        revenue_q1 = next(
            f
            for f in consolidated
            if f.concept == "revenue_from_operations"
            and f.period_end == date(2026, 6, 30)
            and f.period_kind == "quarter"
        )
        assert Decimal(revenue_q1.value_decimal) == Decimal(482110000000)

    def test_quarter_and_annual_coexist_at_march_end(self, india_imported):
        """Q4 quarter and FY annual share 2026-03-31 but are distinct facts."""
        _, _ = india_imported
        run_ind4_pipeline()
        infy = _infy_company_id()
        facts = [
            f
            for f in _india_facts("consolidated")
            if f.company_id == infy
            and f.concept == "revenue_from_operations"
            and f.period_end == date(2026, 3, 31)
        ]
        kinds = {f.period_kind: f for f in facts}
        assert set(kinds) == {"quarter", "annual"}
        assert (kinds["quarter"].period_start, kinds["quarter"].fiscal_quarter) == (
            date(2026, 1, 1),
            "Q4",
        )
        assert (kinds["annual"].period_start, kinds["annual"].fiscal_quarter) == (
            date(2025, 4, 1),
            None,  # the annual is NOT a quarter even though it ends 31 March
        )
        assert Decimal(kinds["quarter"].value_decimal) == Decimal(464020000000)
        assert Decimal(kinds["annual"].value_decimal) == Decimal(1786500000000)

    def test_latest_publication_wins_history_retained(self, india_imported, tmp_path):
        """A later filing's observation wins the canonical fact; the earlier
        observation is preserved and queryable."""
        _, _ = india_imported
        run_ind4_pipeline()
        path = tmp_path / "SYNTHETIC-INFY-later-revenue.xml"
        path.write_bytes(
            synthetic_instance_xml(
                period_start="2026-04-01",
                period_end="2026-06-30",
                revenue_value="49000000000",
            )
        )
        _import_synthetic(path, "2026-04-01", "2026-06-30", "SYNTHLATER")
        normalize_canonical_facts("IN-INFY")
        fact = next(
            f
            for f in _india_facts("consolidated")
            if f.company_id == _infy_company_id()
            and f.concept == "revenue_from_operations"
            and f.period_end == date(2026, 6, 30)
            and f.period_kind == "quarter"
        )
        assert Decimal(fact.value_decimal) == Decimal(49000000000)
        with session_scope() as session:
            lineage = list(
                session.execute(
                    select(FactObservation, FactLineage)
                    .join(FactLineage, FactLineage.source_observation_id == FactObservation.id)
                    .where(FactLineage.normalized_fact_id == fact.id)
                ).all()
            )
            # lineage records BOTH the superseded and the winning observation
            assert {obs.accession for obs, _lin in lineage} == {"177385", "SYNTHLATER"}
            winner = max(lineage, key=lambda pair: pair[0].filed_at)[0]
            assert winner.accession == "SYNTHLATER"
            # the superseded original is still in fact_observations
            originals = list(
                session.scalars(
                    select(FactObservation).where(
                        FactObservation.original_tag == "RevenueFromOperations",
                        FactObservation.period_end == date(2026, 6, 30),
                        FactObservation.value_decimal == "482110000000",
                    )
                )
            )
            assert len(originals) == 1

    def test_standalone_retained_never_substituted(self, india_imported, tmp_path):
        """A standalone observation becomes a standalone-scope fact; the
        consolidated series is untouched."""
        _, _ = india_imported
        run_ind4_pipeline()
        before = {
            Decimal(f.value_decimal)
            for f in _india_facts("consolidated")
            if f.concept == "revenue_from_operations"
        }
        path = tmp_path / "SYNTHETIC-INFY-standalone-revenue.xml"
        path.write_bytes(
            synthetic_instance_xml(
                period_start="2026-04-01",
                period_end="2026-06-30",
                revenue_value="41000000000",
                scope="standalone",
            )
        )
        _import_synthetic(path, "2026-04-01", "2026-06-30", "SYNTHSA", scope="standalone")
        normalize_canonical_facts("IN-INFY")
        standalone = [
            f
            for f in _india_facts("standalone")
            if f.company_id == _infy_company_id()
            and f.concept == "revenue_from_operations"
            and f.period_end == date(2026, 6, 30)
        ]
        assert len(standalone) == 1
        assert standalone[0].reporting_scope == "standalone"
        assert Decimal(standalone[0].value_decimal) == Decimal(41000000000)
        # consolidated series unchanged — no substitution in either direction
        after = {
            Decimal(f.value_decimal)
            for f in _india_facts("consolidated")
            if f.concept == "revenue_from_operations"
        }
        assert after == before
        assert Decimal(41000000000) not in after

    def test_india_concepts_and_metrics_never_collide_with_us(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()
        assert set(INDIA_CONCEPTS) & METRIC_IDS == set()
        assert set(INDIA_METRIC_IDS) & METRIC_IDS == set()
        assert all(metric_id.startswith("india_") for metric_id in INDIA_METRIC_IDS)
        # and no stored India fact was normalized under a US concept name
        company_ids = _india_company_ids()
        with session_scope() as session:
            concepts = set(
                session.scalars(
                    select(NormalizedFact.concept).where(NormalizedFact.company_id.in_(company_ids))
                )
            )
        assert concepts  # India facts exist
        assert concepts <= set(INDIA_CONCEPTS)


class TestDataQuality:
    def test_hul_pnl_scrambling_carries_requires_manual_review(self, india_imported):
        """The IND-3 human-review-pending HUL P&L rows stay review-gated — the
        canonical fact carries the status, never a silent resolution."""
        _, _ = india_imported
        run_ind4_pipeline()
        hul_q1_revenue = next(
            f
            for f in _india_facts("consolidated")
            if f.concept == "revenue_from_operations"
            and f.period_end == date(2026, 6, 30)
            and f.period_kind == "quarter"
            and Decimal(f.value_decimal) == Decimal(173410000000)
        )
        assert hul_q1_revenue.data_quality_status == DQ_REQUIRES_MANUAL_REVIEW
        # HUL Q4 quarter and annual P&L rows are equally pending
        pending = {
            f.data_quality_status
            for f in _india_facts("consolidated")
            if f.concept in ("revenue_from_operations", "profit_after_tax", "eps_basic")
            and Decimal(f.value_decimal)
            in (Decimal(163510000000), Decimal(644680000000), Decimal(2992000000))
        }
        assert pending == {DQ_REQUIRES_MANUAL_REVIEW}

    def test_agent_checked_facts_carry_checked_status(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()
        infy_q1_revenue = next(
            f
            for f in _india_facts("consolidated")
            if f.concept == "revenue_from_operations"
            and Decimal(f.value_decimal) == Decimal(482110000000)
        )
        assert infy_q1_revenue.data_quality_status == DQ_AGENT_CHECKED
        # HUL cash flow / exceptional were agent-checked against the PDFs
        hul_annual_cfo = next(
            f
            for f in _india_facts("consolidated")
            if f.concept == "cash_flow_operations"
            and Decimal(f.value_decimal) == Decimal(109990000000)
        )
        assert hul_annual_cfo.data_quality_status == DQ_AGENT_CHECKED


class TestLineage:
    def test_every_canonical_fact_has_direct_source_lineage(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()
        facts = _india_facts()
        assert len(facts) >= 40  # 2 issuers x 3 identities x ~10 concepts
        with session_scope() as session:
            for fact in facts:
                pairs = list(
                    session.execute(
                        select(FactObservation, FactLineage)
                        .join(
                            FactLineage,
                            FactLineage.source_observation_id == FactObservation.id,
                        )
                        .where(FactLineage.normalized_fact_id == fact.id)
                    ).all()
                )
                assert pairs, f"fact {fact.id} ({fact.concept}) has no lineage"
                assert all(lin.role == "direct_source" for _obs, lin in pairs)

    def test_no_orphan_lineage_rows(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session:
            orphan_facts = list(
                session.scalars(
                    select(FactLineage.id)
                    .outerjoin(NormalizedFact, NormalizedFact.id == FactLineage.normalized_fact_id)
                    .where(NormalizedFact.id.is_(None))
                )
            )
            orphan_observations = list(
                session.scalars(
                    select(FactLineage.id)
                    .outerjoin(
                        FactObservation,
                        FactObservation.id == FactLineage.source_observation_id,
                    )
                    .where(FactObservation.id.is_(None))
                )
            )
        assert not orphan_facts
        assert not orphan_observations

    def test_lineage_records_winning_tag(self, india_imported):
        """Tagmap priority decides which tag wins and lineage shows it."""
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session:
            pbt_fact = session.scalar(
                select(NormalizedFact).where(
                    NormalizedFact.concept == "profit_before_tax",
                    NormalizedFact.period_end == date(2026, 3, 31),
                    NormalizedFact.period_kind == "annual",
                    NormalizedFact.reporting_scope == "consolidated",
                    NormalizedFact.value_decimal == "399950000000",
                )
            )
            assert pbt_fact is not None  # ProfitBeforeTax (39,995) won...
            obs, lineage = session.execute(
                select(FactObservation, FactLineage)
                .join(FactLineage, FactLineage.source_observation_id == FactObservation.id)
                .where(FactLineage.normalized_fact_id == pbt_fact.id)
            ).first()
            assert lineage.role == "direct_source"
            assert obs.original_tag == "ProfitBeforeTax"
            assert obs.value_decimal == "399950000000"
            # ...while the before-exceptional variant (41,284) stays an
            # unselected observation, queryable for the exceptional-impact metric
            pbit = session.scalar(
                select(FactObservation).where(
                    FactObservation.original_tag == "ProfitBeforeExceptionalItemsAndTax",
                    FactObservation.period_kind == "annual",
                    FactObservation.value_decimal == "412840000000",
                )
            )
            assert pbit is not None
            capex_fact = session.scalar(
                select(NormalizedFact).where(
                    NormalizedFact.concept == "capex",
                    NormalizedFact.period_kind == "annual",
                    NormalizedFact.company_id == pbt_fact.company_id,
                )
            )
            capex_obs, _lin = session.execute(
                select(FactObservation, FactLineage)
                .join(FactLineage, FactLineage.source_observation_id == FactObservation.id)
                .where(FactLineage.normalized_fact_id == capex_fact.id)
            ).first()
            assert capex_obs.original_tag == (
                "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"
            )


class TestCashFlowCells:
    def test_infy_quarterly_cfo_is_a_reported_observation_with_provenance(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session:
            obs = session.scalar(
                select(FactObservation).where(
                    FactObservation.canonical_concept == "cash_flow_operations",
                    FactObservation.period_kind == "quarter",
                    FactObservation.period_end == date(2026, 6, 30),
                )
            )
            assert obs is not None
            assert Decimal(obs.value_decimal) == Decimal(93300000000)
            assert obs.form == "PDF"
            assert obs.filed_at == date(2026, 7, 23)
            import json

            metadata = json.loads(obs.context_metadata_json)
            assert metadata["extraction_method"] == "pdf_text"
            assert metadata["page"] == "PDF p.6 (Condensed Consolidated Statement of Cash Flows)"
            assert metadata["review_status"] == "agent_checked_against_document"
            assert metadata["display_value"] == "9,330"
            # the canonical fact exists at the reported quarterly frequency
            fact = session.scalar(
                select(NormalizedFact).where(
                    NormalizedFact.concept == "cash_flow_operations",
                    NormalizedFact.period_kind == "quarter",
                    NormalizedFact.period_end == date(2026, 6, 30),
                )
            )
            assert fact is not None
            assert Decimal(fact.value_decimal) == Decimal(93300000000)

    def test_hul_quarterly_cash_flow_not_present_never_zero(self, india_imported):
        """HUL Q1 FY27 CF is absent from every ingested source: no fact, no
        observation, and no zero invented for it."""
        _, _ = india_imported
        run_ind4_pipeline()
        # HUL = the India company whose Q1 revenue observation is 17,341 Cr.
        with session_scope() as session:
            hul_company_id = session.scalar(
                select(FactObservation.company_id).where(
                    FactObservation.taxonomy == "in-capmkt",
                    FactObservation.canonical_concept == "revenue_from_operations",
                    FactObservation.value_decimal == "173410000000",
                )
            )
            hul_q1_cf_facts = list(
                session.scalars(
                    select(NormalizedFact).where(
                        NormalizedFact.company_id == hul_company_id,
                        NormalizedFact.concept.in_(("cash_flow_operations", "capex")),
                        NormalizedFact.period_end == date(2026, 6, 30),
                    )
                )
            )
            hul_q1_cf_observations = list(
                session.scalars(
                    select(FactObservation).where(
                        FactObservation.company_id == hul_company_id,
                        FactObservation.canonical_concept.in_(("cash_flow_operations", "capex")),
                        FactObservation.period_end == date(2026, 6, 30),
                    )
                )
            )
        assert not hul_q1_cf_facts  # no fact invented for the quarter
        assert not hul_q1_cf_observations  # and no (zero) observation either
        # The ONLY consolidated Q1 FY27 cash-flow observation in the store is
        # INFY's reported PDF CFO.
        with session_scope() as session:
            q1_cf = list(
                session.scalars(
                    select(FactObservation).where(
                        FactObservation.company_id.in_(_india_company_ids()),
                        FactObservation.canonical_concept.in_(("cash_flow_operations", "capex")),
                        FactObservation.period_end == date(2026, 6, 30),
                        FactObservation.reporting_scope == "consolidated",
                    )
                )
            )
        assert len(q1_cf) == 1
        assert Decimal(q1_cf[0].value_decimal) == Decimal(93300000000)

    def test_h2_derivation_reports_blocked_status(self, india_imported):
        """No H1 CF observation exists -> H2 = annual − H1 is blocked, never a number."""
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session:
            infy_company_id = session.scalar(
                select(FactObservation.company_id).where(
                    FactObservation.taxonomy == "in-capmkt",
                    FactObservation.original_tag == "RevenueFromOperations",
                    FactObservation.value_decimal == "482110000000",
                )
            )
            statuses = cash_flow_derivation_statuses(session, infy_company_id)
        assert statuses
        for status in statuses:
            assert status.method == "annual - H1"
            assert status.status == MissingDataStatus.SOURCE_NOT_INGESTED.value
            assert status.target_label.startswith("H2")
            assert status.target_label != "Q4"
        assert {s.concept for s in statuses} == {"cash_flow_operations", "capex"}


class TestIdempotency:
    def test_double_run_adds_zero_rows_at_every_layer(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()

        def counts() -> tuple[int, int, int, int, int]:
            with session_scope() as session:
                return (
                    len(list(session.scalars(select(SourceArtifact.id)))),
                    len(list(session.scalars(select(FactObservation.id)))),
                    len(list(session.scalars(select(NormalizedFact.id)))),
                    len(list(session.scalars(select(FactLineage.id)))),
                    len(list(session.scalars(select(DerivedMetric.id)))),
                )

        first = counts()
        # second full run: observations skip, PDF CFO skips, facts upsert to the
        # same rows, metrics upsert to the same rows
        from quarterline.sources.india.metrics import compute_india_metrics
        from quarterline.sources.india.pipeline import ingest_observations

        for issuer in ("IN-INFY", "IN-HINDUNILVR"):
            obs_report = ingest_observations(issuer)
            assert obs_report.observations_inserted == 0
            assert obs_report.observations_skipped > 0
            norm_report = normalize_canonical_facts(issuer)
            assert norm_report.facts_created == 0
            assert norm_report.lineage_rows_added == 0
            metrics_report = compute_india_metrics(issuer)
            assert metrics_report.metric_rows_created == 0
            assert metrics_report.metric_rows_written > 0
        pdf_report = ingest_reviewed_pdf_cash_flow("IN-INFY")
        assert pdf_report.observations_inserted == 0
        assert pdf_report.observations_skipped == 1
        assert counts() == first

    def test_reimport_same_document_is_idempotent(self, india_imported):
        """Artifact layer: re-importing the same official file adds zero rows."""
        from india_test_helpers import load_india_manifest, period_bounds

        from quarterline.sources.india.ir_documents import import_document

        _, _ = india_imported
        run_ind4_pipeline()

        def artifact_count() -> int:
            with session_scope() as session:
                return len(
                    list(
                        session.scalars(
                            select(SourceArtifact).where(SourceArtifact.source == "india")
                        )
                    )
                )

        before = artifact_count()
        manifest = load_india_manifest()
        for entry in manifest["committed_fixtures"]:
            start, end = period_bounds(manifest, entry["period"])
            import_document(
                issuer_id=entry["issuer_id"],
                path=INDIA_FIXTURES_DIR / entry["file"],
                doc_type=entry["doc_type"],
                period_start=start,
                period_end=end,
                scope=entry["scope"],
                published_at=date.fromisoformat(
                    entry["exchange_filing"]["broadcast_ist"].split(" ")[0]
                ),
                seq_id=entry["exchange_filing"].get("seq_id"),
            )
        assert artifact_count() == before


def test_unverified_issuer_refused():
    from quarterline.sources.india.normalization import normalize_canonical_facts

    with pytest.raises(ValueError, match="not verified"):
        normalize_canonical_facts("IN-TCS")  # proposed, never verified
