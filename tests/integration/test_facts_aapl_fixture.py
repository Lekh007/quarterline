"""Integration tests over the trimmed-but-real AAPL companyfacts fixture
(SPEC 26: real-source financial fixtures; values asserted from the fixture)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from facts_test_helpers import (
    FakeCompanyFactsClient,
    create_schema,
    facts_cli_guard,  # noqa: F401 (pytest fixture imported into this module)
    find_fact_entry,
    import_ingest_facts,
    load_aapl_fixture,
)
from sqlalchemy import select

from quarterline.core.models import METRIC_IDS, MetricStatus
from quarterline.core.provenance import build_fact_card, build_fact_cards, explain_metric
from quarterline.store.db import session_scope
from quarterline.store.models import NormalizedFact
from quarterline.store.repositories.companies import CompaniesRepo
from quarterline.store.repositories.facts import FactsRepo

pytestmark = pytest.mark.usefixtures("facts_cli_guard")


@pytest.fixture(scope="module")
def aapl_payload() -> dict:
    return load_aapl_fixture()


@pytest.fixture
def seeded_aapl(tmp_path, monkeypatch, aapl_payload):
    """A fresh DB with the real AAPL fixture ingested once."""
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    create_schema()
    report = import_ingest_facts().ingest_facts(
        tickers=["AAPL"],
        watchlist="data/watchlist_us.csv",
        client=FakeCompanyFactsClient(aapl_payload),
    )
    assert report.errors == {}, report.errors
    return report


def test_aapl_fact_cards_return_latest_eight_quarters(seeded_aapl) -> None:
    with session_scope() as sess:
        cards = build_fact_cards(sess, "AAPL")
        assert len(cards) == 8  # SPEC 10.5 retention: last eight complete quarters
        ends = [card.period_end for card in cards]
        assert ends == sorted(ends)
        # Latest complete quarter as of the fixture: Q3 FY2026 (ended 2026-06-27).
        latest = cards[-1]
        assert (latest.fiscal_year, latest.fiscal_quarter) == (2026, "Q3")
        assert latest.period_start == date(2026, 3, 29)
        assert latest.period_end == date(2026, 6, 27)


def test_aapl_card_values_match_real_fixture(aapl_payload, seeded_aapl) -> None:
    with session_scope() as sess:
        cards = build_fact_cards(sess, "AAPL")
        latest = cards[-1]
        revenue_metric = next(m for m in latest.metrics if m.metric_id == "revenue")
        # The real reported value for the quarter from the fixture payload.
        entry = find_fact_entry(
            aapl_payload,
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            start="2026-03-29",
            end="2026-06-27",
        )
        assert revenue_metric.value == Decimal(str(entry["val"]))
        assert revenue_metric.status is MetricStatus.ok


def test_aapl_q4_derived_from_real_annual_minus_nine_month(aapl_payload, seeded_aapl) -> None:
    with session_scope() as sess:
        cid = CompaniesRepo(sess).get_by_ticker("AAPL").id
        repo = FactsRepo(sess)
        # FY2025 annual and its nine-month YTD, straight from the fixture payload.
        annual_entry = find_fact_entry(
            aapl_payload,
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            start="2024-09-29",
            end="2025-09-27",
        )
        annual = sess.scalar(
            select(NormalizedFact).where(
                NormalizedFact.company_id == cid,
                NormalizedFact.concept == "revenue",
                NormalizedFact.period_kind == "annual",
                NormalizedFact.period_end == date(2025, 9, 27),
            )
        )
        assert annual is not None
        assert Decimal(annual.value_decimal) == Decimal(str(annual_entry["val"]))
        q4 = repo.find_fact(
            cid, "revenue", date(2025, 6, 29), date(2025, 9, 27), "quarter", "consolidated"
        )
        assert q4 is not None and q4.is_derived
        ytd9 = repo.find_fact(
            cid,
            "revenue",
            date(2024, 9, 29),
            date(2025, 6, 28),
            "year_to_date",
            "consolidated",
        )
        assert ytd9 is not None
        assert Decimal(q4.value_decimal) == Decimal(annual.value_decimal) - Decimal(
            ytd9.value_decimal
        )


def test_aapl_every_card_metric_carries_provenance(seeded_aapl) -> None:
    with session_scope() as sess:
        for card in build_fact_cards(sess, "AAPL"):
            assert {m.metric_id for m in card.metrics} <= METRIC_IDS
            for metric in card.metrics:
                assert metric.provenance.formula_version is not None
                if metric.status is MetricStatus.ok:
                    has_provenance = (
                        metric.provenance.observation_ids
                        or metric.provenance.derived_from
                        # the label's provenance is the rule applied
                        or metric.metric_id == "quarter_label"
                    )
                    assert has_provenance, f"{metric.metric_id} lacks provenance"


def test_aapl_explain_metric_reports_accession_form_filed_at(aapl_payload, seeded_aapl) -> None:
    with session_scope() as sess:
        cards = build_fact_cards(sess, "AAPL")
        latest = cards[-1]
        report = explain_metric(sess, "AAPL", "revenue", latest.period_end)
        assert report.derivation == "direct"
        assert report.formula_version == "norm-v1"
        assert report.sources, "provenance viewer requires at least one source"
        for source in report.sources:
            assert source.accession and "-" in source.accession
            assert source.form == "10-Q"
            assert source.filed_at is not None
        # The winning observation is the latest real filing for that period.
        entry = find_fact_entry(
            aapl_payload,
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            start="2026-03-29",
            end="2026-06-27",
        )
        assert any(
            source.accession == entry["accn"] and str(source.filed_at) == entry["filed"]
            for source in report.sources
        )


def test_aapl_yoy_matches_prior_year_fiscal_quarter(seeded_aapl) -> None:
    with session_scope() as sess:
        cards = build_fact_cards(sess, "AAPL")
        latest = cards[-1]  # FY2026 Q3
        revenue = next(m for m in latest.metrics if m.metric_id == "revenue")
        yoy = next(m for m in latest.metrics if m.metric_id == "revenue_yoy")
        assert yoy.status is MetricStatus.ok
        # YoY computed against FY2025 Q3 (the matching fiscal quarter), read here
        # from the same card series rather than a four-row offset.
        prior = next(c for c in cards if c.fiscal_year == 2025 and c.fiscal_quarter == "Q3")
        prior_revenue = next(m for m in prior.metrics if m.metric_id == "revenue")
        assert yoy.value == revenue.value / prior_revenue.value - 1


def test_aapl_derived_quarter_cfo_and_fcf_sign(seeded_aapl) -> None:
    with session_scope() as sess:
        cards = build_fact_cards(sess, "AAPL")
        latest = cards[-1]
        cfo = next(m for m in latest.metrics if m.metric_id == "cfo")
        capex = next(m for m in latest.metrics if m.metric_id == "capex_outflow")
        fcf = next(m for m in latest.metrics if m.metric_id == "fcf")
        assert cfo.status is MetricStatus.ok and capex.status is MetricStatus.ok
        # capex presented as a positive outflow magnitude; fcf = cfo - capex
        assert capex.value > 0
        assert fcf.value == cfo.value - capex.value


def test_aapl_build_fact_card_single_and_unknown_ticker(seeded_aapl) -> None:
    with session_scope() as sess:
        card = build_fact_card(sess, "AAPL")
        assert card.fiscal_quarter is not None
        assert card.metrics
        with pytest.raises(LookupError):
            build_fact_card(sess, "NOPE")
