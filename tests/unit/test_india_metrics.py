"""India metric tests (IND-4, india-metrics-v1) on REAL fixture data.

QoQ revenue Q4 FY26 -> Q1 FY27 computes the exact Decimal; PAT margins are the
owners'/group's PAT over revenue (never total_income); the exceptional impact
preserves each issuer's source sign (INFY FY26 -1,289 Cr expense; HUL Q4 +247 Cr
gain); YoY metrics report the typed missing status with an explanation while the
prior-year quarters are un-ingested — never fabricated. Denominator rules follow
the US convention. Quarter and annual metric rows coexist at the same
period_end via the store-key suffix.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from india_test_helpers import run_ind4_pipeline
from sqlalchemy import select

from quarterline.sources.india.metrics import (
    FORMULA_VERSION_INDIA_METRICS,
    IndiaMetricResult,
    _FactView,
    build_metric_results,
    compute_india_metrics,
    eps_growth_yoy_metric,
    exceptional_impact_metric,
    pat_margin_owners_metric,
    revenue_qoq_metric,
    revenue_yoy_metric,
    store_metric_id,
)
from quarterline.store.db import session_scope
from quarterline.store.models import DerivedMetric, FactObservation

Q1_IDENTITY = (date(2026, 4, 1), date(2026, 6, 30), "quarter")
Q4_IDENTITY = (date(2026, 1, 1), date(2026, 3, 31), "quarter")
FY_IDENTITY = (date(2025, 4, 1), date(2026, 3, 31), "annual")


def _company_id(value_text: str) -> int:
    with session_scope() as session:
        return session.scalar(
            select(FactObservation.company_id).where(
                FactObservation.taxonomy == "in-capmkt",
                FactObservation.canonical_concept == "revenue_from_operations",
                FactObservation.value_decimal == value_text,
            )
        )


def _infy_id() -> int:
    return _company_id("482110000000")


def _hul_id() -> int:
    return _company_id("173410000000")


def _result_for(
    issuer_id: str,
    metric_id: str,
    period_end: date,
    period_kind: str = "quarter",
) -> IndiaMetricResult:
    with session_scope() as session:
        results = build_metric_results(session, issuer_id, period_end=period_end)
    return next(r for r in results if r.metric_id == metric_id and r.period_kind == period_kind)


class TestGrowthMetrics:
    def test_qoq_q4_to_q1_exact_decimal(self, india_imported):
        """Q4 FY26 -> Q1 FY27: (48,211 - 46,402) / 46,402 as an exact Decimal."""
        _, _ = india_imported
        run_ind4_pipeline()
        result = _result_for("IN-INFY", "india_revenue_qoq", date(2026, 6, 30))
        expected = Decimal(482110000000) / Decimal(464020000000) - Decimal(1)
        assert result.status == "ok"
        assert result.value == expected
        assert result.unit == "ratio"
        assert result.formula_version == FORMULA_VERSION_INDIA_METRICS
        assert result.input_fact_ids, "input fact ids must be recorded"
        assert any("482110000000" in note and "464020000000" in note for note in result.notes)
        # the QoQ metric for the Q4 quarter itself is missing (no Q3 FY26 ingested)
        q4_qoq = _result_for("IN-INFY", "india_revenue_qoq", date(2026, 3, 31))
        assert q4_qoq.status == "missing"
        assert q4_qoq.value is None

    def test_hul_qoq_computes_and_flags_reviewed_inputs(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()
        result = _result_for("IN-HINDUNILVR", "india_revenue_qoq", date(2026, 6, 30))
        expected = Decimal(173410000000) / Decimal(163510000000) - Decimal(1)
        assert result.status == "ok"
        assert result.value == expected
        # HUL P&L facts carry the IND-3 pending status; the metric notes it
        assert any("requires_manual_review" in note for note in result.notes)

    def test_yoy_missing_with_explanation_both_issuers(self, india_imported):
        """Prior-year quarters are NOT ingested: typed missing status with what
        would be needed — for BOTH issuers, never one computed and one faked."""
        _, _ = india_imported
        run_ind4_pipeline()
        for issuer in ("IN-INFY", "IN-HINDUNILVR"):
            result = _result_for(issuer, "india_revenue_yoy", date(2026, 6, 30))
            assert result.status == "missing"
            assert result.value is None
            assert any(
                "not present in ingested sources" in note
                and "pdf_text" in note
                and "IR PDF comparative column" in note
                for note in result.notes
            ), result.notes
            # the current-quarter fact is still referenced as the input
            assert result.input_fact_ids

    def test_eps_growth_yoy_missing_consistently(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()
        statuses = set()
        for issuer in ("IN-INFY", "IN-HINDUNILVR"):
            result = _result_for(issuer, "india_eps_growth_yoy", date(2026, 6, 30))
            assert result.status == "missing"
            assert result.value is None
            statuses.add(result.status)
        assert statuses == {"missing"}

    def test_growth_is_quarter_only(self, india_imported):
        """No YoY/QoQ row is produced for the annual identity (growth metrics
        need matching quarters; annual growth needs prior-year annuals)."""
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session:
            stored = list(
                session.scalars(
                    select(DerivedMetric).where(
                        DerivedMetric.metric.in_(("india_revenue_yoy", "india_revenue_qoq"))
                    )
                )
            )
        assert stored  # quarter rows exist
        assert all("@annual" not in m.metric for m in stored)


class TestMarginMetrics:
    def test_pat_margin_owners_exact_with_label(self, india_imported):
        """7,769 / 48,211 with the exact numerator/denominator in the notes."""
        _, _ = india_imported
        run_ind4_pipeline()
        result = _result_for("IN-INFY", "india_pat_margin_owners", date(2026, 6, 30))
        expected = Decimal(77690000000) / Decimal(482110000000)
        assert result.status == "ok"
        assert result.value == expected
        assert result.unit == "ratio"
        assert "profit_attributable_to_owners / revenue_from_operations" in result.notes
        assert len(result.input_fact_ids) == 2

    def test_group_margin_is_a_separate_metric(self, india_imported):
        """india_pat_margin_group (7,775/48,211) is stored separately and never
        conflated with the owners' margin (7,769/48,211)."""
        _, _ = india_imported
        run_ind4_pipeline()
        owners = _result_for("IN-INFY", "india_pat_margin_owners", date(2026, 6, 30))
        group = _result_for("IN-INFY", "india_pat_margin_group", date(2026, 6, 30))
        assert group.value == Decimal(77750000000) / Decimal(482110000000)
        assert group.value != owners.value
        with session_scope() as session:
            metrics = {
                m.metric
                for m in session.scalars(
                    select(DerivedMetric).where(
                        DerivedMetric.company_id == _infy_id(),
                        DerivedMetric.period_end == date(2026, 6, 30),
                    )
                )
            }
        assert {"india_pat_margin_owners", "india_pat_margin_group"} <= metrics

    def test_annual_margin_row_suffixed_not_colliding(self, india_imported):
        """The FY26 annual identity and the Q4 quarter share 2026-03-31; the
        store keeps both rows (kind suffix), never overwriting one with the other."""
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session:
            rows = list(
                session.scalars(
                    select(DerivedMetric).where(
                        DerivedMetric.company_id == _infy_id(),
                        DerivedMetric.period_end == date(2026, 3, 31),
                        DerivedMetric.metric.like("india_pat_margin_owners%"),
                    )
                )
            )
        ids = {row.metric for row in rows}
        assert ids == {"india_pat_margin_owners", "india_pat_margin_owners@annual"}
        quarter_row = next(r for r in rows if r.metric == "india_pat_margin_owners")
        annual_row = next(r for r in rows if r.metric.endswith("@annual"))
        assert quarter_row.value_decimal == str(Decimal(85010000000) / Decimal(464020000000))
        assert annual_row.value_decimal == str(Decimal(294400000000) / Decimal(1786500000000))
        assert quarter_row.formula_version == annual_row.formula_version


class TestExceptionalImpact:
    def test_infy_fy26_impact_negative_expense_direction(self, india_imported):
        """41,284 -> 39,995: impact -1,289 Cr (stored sign), identity checked
        against the PBIT observation, share of PBT in the notes."""
        _, _ = india_imported
        run_ind4_pipeline()
        result = _result_for("IN-INFY", "india_exceptional_impact_pbt", date(2026, 3, 31), "annual")
        assert result.status == "ok"
        assert result.value == Decimal(-12890000000)
        assert result.unit == "INR"
        notes = "\n".join(result.notes)
        assert "identity verified against observation" in notes
        assert "share of PBT:" in notes
        assert "-12890000000 / 399950000000" in notes  # -1,289 Cr / 39,995 Cr in full rupees
        assert "reduces profit before tax" in notes
        assert "INFY prints a negative impact as a positive expense" in notes

    def test_hul_q4_gain_direction_preserved(self, india_imported):
        """HUL Q4 +247 Cr demerger gain INCREASES PBT as stored — the source
        direction is preserved, never normalized to INFY's expense presentation."""
        _, _ = india_imported
        run_ind4_pipeline()
        result = _result_for(
            "IN-HINDUNILVR", "india_exceptional_impact_pbt", date(2026, 3, 31), "quarter"
        )
        assert result.status == "ok"
        assert result.value == Decimal(2470000000)
        notes = "\n".join(result.notes)
        assert "increases profit before tax" in notes
        assert "HUL prints gains/losses with the same sign" in notes
        # and the FY26 loss is negative for the same issuer
        annual = _result_for(
            "IN-HINDUNILVR", "india_exceptional_impact_pbt", date(2026, 3, 31), "annual"
        )
        assert annual.value == Decimal(-2350000000)

    def test_persisted_rows_at_both_identities(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session:
            rows = list(
                session.scalars(
                    select(DerivedMetric).where(
                        DerivedMetric.metric.like("india_exceptional_impact_pbt%")
                    )
                )
            )
        infy = _infy_id()
        hul = _hul_id()
        by_key = {(r.company_id, r.metric, r.period_end): Decimal(r.value_decimal) for r in rows}
        march = date(2026, 3, 31)
        june = date(2026, 6, 30)
        assert by_key[(infy, "india_exceptional_impact_pbt@annual", march)] == Decimal(-12890000000)
        assert by_key[(infy, "india_exceptional_impact_pbt", march)] == Decimal(0)
        assert by_key[(hul, "india_exceptional_impact_pbt", march)] == Decimal(2470000000)
        assert by_key[(hul, "india_exceptional_impact_pbt@annual", march)] == Decimal(-2350000000)
        assert by_key[(hul, "india_exceptional_impact_pbt", june)] == Decimal(-750000000)


class TestDenominatorRules:
    """Zero/negative denominators follow the US convention: null value + note."""

    @staticmethod
    def _view(concept: str, value: str | None) -> _FactView | None:
        if value is None:
            return None
        return _FactView(
            fact_id=1,
            concept=concept,
            value=Decimal(value),
            period_start=Q1_IDENTITY[0],
            period_end=Q1_IDENTITY[1],
            period_kind="quarter",
            data_quality_status=None,
        )

    def _owners_metric(self, owners: str | None, revenue: str | None) -> IndiaMetricResult:
        return pat_margin_owners_metric(
            Q1_IDENTITY,
            self._view("profit_attributable_to_owners", owners),
            self._view("revenue_from_operations", revenue),
        )

    def test_zero_denominator_invalid_with_note(self):
        result = self._owners_metric("7769000000", "0")
        assert result.status == "invalid"
        assert result.value is None
        assert "zero denominator" in result.notes[1]

    def test_negative_denominator_unsuitable_with_note(self):
        result = self._owners_metric("100", "-5000")
        assert result.status == "unsuitable"
        assert result.value is None
        assert "negative denominator" in result.notes[1]

    def test_missing_input_missing_with_note(self):
        result = self._owners_metric("7769000000", None)
        assert result.status == "missing"
        assert result.value is None
        assert "revenue_from_operations" in result.notes[0]

    def test_negative_prior_growth_unsuitable(self):
        def view(value: str) -> _FactView:
            return _FactView(
                fact_id=1,
                concept="revenue_from_operations",
                value=Decimal(value),
                period_start=Q1_IDENTITY[0],
                period_end=Q1_IDENTITY[1],
                period_kind="quarter",
                data_quality_status=None,
            )

        result = revenue_yoy_metric(Q1_IDENTITY, view("1000"), view("-500"))
        assert result.status == "unsuitable"
        assert result.value is None
        assert "absolute change: 1500" in result.notes

        zero_prior = revenue_qoq_metric(Q1_IDENTITY, view("1000"), view("0"))
        assert zero_prior.status == "invalid"

    def test_eps_growth_and_impact_functions_registered(self):
        """Every mission metric exists and eps/impact share the version."""
        assert eps_growth_yoy_metric(Q1_IDENTITY, None, None).formula_version == (
            FORMULA_VERSION_INDIA_METRICS
        )
        assert exceptional_impact_metric(FY_IDENTITY, None, None).formula_version == (
            FORMULA_VERSION_INDIA_METRICS
        )
        assert store_metric_id("india_pat_margin_owners", "annual") == (
            "india_pat_margin_owners@annual"
        )
        assert store_metric_id("india_revenue_qoq", "quarter") == "india_revenue_qoq"


class TestPersistence:
    def test_compute_india_metrics_persists_typed_missing_rows(self, india_imported):
        """Missing metrics are PERSISTED as status='missing' with value NULL —
        a typed status in the store, never a zero."""
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session:
            missing = list(
                session.scalars(
                    select(DerivedMetric).where(
                        DerivedMetric.company_id == _infy_id(),
                        DerivedMetric.status == "missing",
                    )
                )
            )
        assert missing
        assert all(m.value_decimal is None for m in missing)
        # and the report vocabulary carries both statuses
        report = compute_india_metrics("IN-INFY")  # idempotent re-run
        assert report.metric_rows_created == 0
        assert report.by_status.get("missing", 0) > 0
        assert report.by_status.get("ok", 0) > 0

    def test_compute_is_idempotent(self, india_imported):
        _, _ = india_imported
        run_ind4_pipeline()
        with session_scope() as session:
            before = len(list(session.scalars(select(DerivedMetric.id))))
        report = compute_india_metrics("IN-INFY")
        assert report.metric_rows_created == 0
        with session_scope() as session:
            after = len(list(session.scalars(select(DerivedMetric.id))))
        assert before == after


def test_no_us_metric_id_reuse(india_imported):
    """India metric ids are disjoint from the US allowlist in the STORE."""
    _, _ = india_imported
    run_ind4_pipeline()
    with session_scope() as session:
        infy_metrics = set(
            session.scalars(
                select(DerivedMetric.metric).where(DerivedMetric.company_id == _infy_id())
            )
        )
    assert infy_metrics
    assert all(m.startswith("india_") for m in infy_metrics)
