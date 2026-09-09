"""Integration tests: the full facts pipeline over the synthetic edge-case
fixture into a temporary SQLite database (SPEC 10, 26).

Covers: persistence of observations/facts/lineage, quarterly-vs-annual
collision prevention in the DB, instant-fact NULL-key upsert idempotency,
re-ingestion idempotency, restatement handling (latest_available vs as_of),
YTD/Q4 derivation lineage, and as-of reconstruction of derived facts.
All asserted financial values are read from the fixture payload.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from facts_test_helpers import (
    FakeCompanyFactsClient,
    create_schema,
    d,
    facts_cli_guard,  # noqa: F401 (pytest fixture imported into this module)
    find_fact_entry,
    import_ingest_facts,
    instant_fact_for,
    load_synthetic_fixture,
)
from sqlalchemy import select

from quarterline.core.models import PeriodKind
from quarterline.store.db import session_scope
from quarterline.store.models import NormalizedFact
from quarterline.store.repositories.companies import CompaniesRepo
from quarterline.store.repositories.facts import (
    ROLE_ANNUAL_TOTAL,
    ROLE_DIRECT_SOURCE,
    ROLE_PRIOR_YTD,
    FactsRepo,
)

pytestmark = pytest.mark.usefixtures("facts_cli_guard")


def _ingest(**kwargs):
    return import_ingest_facts().ingest_facts(**kwargs)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """Fresh DB + the synthetic fixture ingested once; returns (fixture, report)."""
    storage = tmp_path / "storage"
    monkeypatch.setenv("STORAGE_DIR", str(storage))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    create_schema()
    # The synthetic company is NOT on the real watchlist; register it explicitly.
    with session_scope() as sess:
        CompaniesRepo(sess).upsert_company(
            ticker="SYNTH",
            cik="9999999",
            name="SYNTHETIC EDGE CASE CORP (NOT A REAL COMPANY)",
            sector="Test",
            country="XX",
        )
    fixture = load_synthetic_fixture()
    report = _ingest(
        tickers=["SYNTH"],
        watchlist="data/watchlist_us.csv",
        client=FakeCompanyFactsClient(fixture),
    )
    assert report.errors == {}, report.errors
    return fixture, report


def company_id(session) -> int:
    company = CompaniesRepo(session).get_by_ticker("SYNTH")
    assert company is not None
    return company.id


# ---------------------------------------------------------------------------
# Persistence and idempotency
# ---------------------------------------------------------------------------


def test_pipeline_persists_observations_facts_and_lineage(seeded) -> None:
    _fixture, report = seeded
    assert report.observations_inserted > 0
    assert report.facts_created > 0
    assert report.lineage_rows > 0
    assert report.derived_metrics_upserted > 0


def test_reingestion_is_idempotent(tmp_path, monkeypatch, seeded) -> None:
    _fixture, first = seeded
    fixture = load_synthetic_fixture()
    second = _ingest(
        tickers=["SYNTH"],
        watchlist="data/watchlist_us.csv",
        client=FakeCompanyFactsClient(fixture),
    )
    assert second.observations_inserted == 0
    assert second.observations_skipped == first.observations_inserted
    assert second.facts_created == 0
    assert second.lineage_rows == first.lineage_rows  # lineage rows are not duplicated


def test_instant_facts_upsert_without_null_key_duplicates(seeded) -> None:
    """SQLite treats NULL period_start as distinct in unique indexes; the
    repository must upsert instants explicitly (Wave-0 log gotcha)."""
    with session_scope() as sess:
        cid = company_id(sess)
        repo = FactsRepo(sess)
        cash = instant_fact_for(repo, cid, "cash", d("2024-11-02"))
        assert cash is not None
        fixture, _report = seeded
        # Re-ingest: the cash instant at the 53-week year end must not duplicate.
        _ingest(
            tickers=["SYNTH"],
            watchlist="data/watchlist_us.csv",
            client=FakeCompanyFactsClient(fixture),
        )
        rows = list(
            sess.execute(
                select(NormalizedFact).where(
                    NormalizedFact.company_id == cid,
                    NormalizedFact.concept == "cash",
                    NormalizedFact.period_end == d("2024-11-02"),
                )
            ).scalars()
        )
        assert len(rows) == 1


def test_annual_and_quarter_facts_coexist_in_database(seeded) -> None:
    """Annual FY2024 and derived Q4 FY2024 share an end date but must coexist."""
    with session_scope() as sess:
        cid = company_id(sess)
        repo = FactsRepo(sess)
        annual = repo.find_fact(
            cid, "revenue", d("2023-10-29"), d("2024-11-02"), "annual", "consolidated"
        )
        q4 = repo.find_fact(
            cid, "revenue", d("2024-07-28"), d("2024-11-02"), "quarter", "consolidated"
        )
        assert annual is not None and q4 is not None
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
        assert q4.value_decimal is not None and annual.value_decimal is not None
        assert q4.period_kind == "quarter" and annual.period_kind == "annual"
        assert Decimal(q4.value_decimal) == Decimal(str(annual_entry["val"])) - Decimal(
            str(ytd9_entry["val"])
        )


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------


def test_derived_fact_lineage_roles_persisted(seeded) -> None:
    with session_scope() as sess:
        cid = company_id(sess)
        repo = FactsRepo(sess)
        q2 = repo.find_fact(
            cid, "revenue", d("2024-01-28"), d("2024-04-27"), "quarter", "consolidated"
        )
        assert q2 is not None and q2.is_derived
        lineage = repo.lineage_for_fact(q2.id)
        roles = {lin.role for _obs, lin in lineage}
        assert roles == {ROLE_ANNUAL_TOTAL, ROLE_PRIOR_YTD}
        direct_roles = set()
        q1 = repo.find_fact(
            cid, "revenue", d("2023-10-29"), d("2024-01-27"), "quarter", "consolidated"
        )
        assert q1 is not None and not q1.is_derived
        direct_roles = {lin.role for _obs, lin in repo.lineage_for_fact(q1.id)}
        assert direct_roles == {ROLE_DIRECT_SOURCE}


def test_quarters_available_ordered_and_metric_series(seeded) -> None:
    with session_scope() as sess:
        cid = company_id(sess)
        repo = FactsRepo(sess)
        quarters = repo.quarters_available(cid)
        assert [q.fiscal_quarter for q in quarters] == ["Q1", "Q2", "Q3", "Q4"] * 2
        assert [q.fiscal_year for q in quarters] == [2023, 2023, 2023, 2023, 2024, 2024, 2024, 2024]
        # 53-week year's Q4 keeps quarter kind with its 14-week (98 calendar
        # day, 97-day end-start) span
        assert (quarters[-1].period_end - quarters[-1].period_start).days == 97
        series = repo.metric_series(cid, "cfo_to_net_income", n_quarters=4)
        assert len(series) == 4
        assert series == sorted(series, key=lambda m: m.period_end)


# ---------------------------------------------------------------------------
# Restatement + as-of through the DB path
# ---------------------------------------------------------------------------


def test_facts_for_quarter_as_of_restated_value(seeded) -> None:
    with session_scope() as sess:
        cid = company_id(sess)
        repo = FactsRepo(sess)
        # latest_available (default): restated value (second filing) is in force
        latest = repo.facts_for_quarter(cid, d("2024-01-27"))
        assert latest["revenue"].value_decimal == "1080"
        # as_of between original and restatement filings: original value
        between = repo.facts_for_quarter(cid, d("2024-01-27"), as_of=d("2024-03-01"))
        assert between["revenue"].value_decimal == "1050"
        assert between["revenue"].selection_policy == "as_of"
        # as_of before ANY filing of the quarter: fact absent, not zero
        before = repo.facts_for_quarter(cid, d("2024-01-27"), as_of=d("2024-01-15"))
        assert "revenue" not in before


def test_as_of_reconstructs_derived_quarter_from_lineage(seeded) -> None:
    with session_scope() as sess:
        cid = company_id(sess)
        repo = FactsRepo(sess)
        fixture = load_synthetic_fixture()
        ytd6 = find_fact_entry(
            fixture,
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            start="2023-10-29",
            end="2024-04-27",
        )
        # Derived Q2 exists in force once the Q2 10-Q (2024-05-03) has been filed.
        after = repo.facts_for_quarter(cid, d("2024-04-27"), as_of=d("2024-06-01"))
        assert after["revenue"].value_decimal == str(ytd6["val"] - 1080)
        # Before that filing: no Q2 fact at all (not yet derivable point-in-time).
        before = repo.facts_for_quarter(cid, d("2024-04-27"), as_of=d("2024-03-01"))
        assert "revenue" not in before


def test_observations_preserve_revised_and_original(seeded) -> None:
    with session_scope() as sess:
        cid = company_id(sess)
        repo = FactsRepo(sess)
        drafts = repo.observations_for_company(cid, concept="revenue")
        restated = [
            obs
            for obs in drafts
            if obs.period_start == d("2023-10-29") and obs.period_end == d("2024-01-27")
        ]
        values = {obs.value_decimal for obs in restated}
        assert values == {"1050", "1080"}  # both vintages preserved (SPEC 2.1.10)


def test_observations_as_of_excludes_future_filings(seeded) -> None:
    with session_scope() as sess:
        cid = company_id(sess)
        repo = FactsRepo(sess)
        all_obs = repo.observations_for_company(cid, concept="revenue")
        early = repo.observations_for_company(cid, concept="revenue", as_of=d("2023-06-01"))
        assert len(early) < len(all_obs)
        assert all(obs.filed_at is not None and obs.filed_at <= d("2023-06-01") for obs in early)


# ---------------------------------------------------------------------------
# Fiscal calendar through the pipeline
# ---------------------------------------------------------------------------


def test_inferred_fye_and_fiscal_labels(seeded) -> None:
    with session_scope() as sess:
        company = CompaniesRepo(sess).get_by_ticker("SYNTH")
        assert company is not None
        assert company.fiscal_year_end == "10"  # October FYE inferred
        repo = FactsRepo(sess)
        quarters = repo.quarters_available(company.id)
        assert quarters[0].fiscal_quarter == "Q1" and quarters[0].fiscal_year == 2023
        assert quarters[-1].fiscal_quarter == "Q4" and quarters[-1].fiscal_year == 2024
        annual = repo.find_fact(
            company.id, "revenue", d("2023-10-29"), d("2024-11-02"), "annual", "consolidated"
        )
        assert annual is not None and annual.fiscal_year == 2024  # 53-week spill
        assert annual.period_kind == PeriodKind.annual.value
