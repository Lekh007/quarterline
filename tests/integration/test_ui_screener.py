"""Screener (GET/POST /screener) — deterministic filters over fact cards.

Expected subsets are derived by hand from the same fixture data the UI is
seeded with (only AAPL carries data; the other 14 watchlist companies are
empty and must never appear in results). The screener must keep working with
every LLM module unimportable (SPEC §19, §25).
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from facts_test_helpers import (
    facts_cli_guard,  # noqa: F401 (pytest fixture)
    find_fact_entry,
    load_aapl_fixture,
)
from fastapi.testclient import TestClient
from ui_test_helpers import (
    block_llm_modules,  # noqa: F401 (pytest fixture)
    configure_app_db,
    make_client,
)

pytestmark = pytest.mark.usefixtures("facts_cli_guard")

LATEST_END = "2026-06-27"


def _aapl_latest_revenue_yoy() -> Decimal:
    """Hand-derived expected YoY: fixture Q3 revenues, current vs prior year."""
    fixture = load_aapl_fixture()
    current = Decimal(
        str(
            find_fact_entry(
                fixture,
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                start="2026-03-29",
                end="2026-06-27",
            )["val"]
        )
    )
    prior = Decimal(
        str(
            find_fact_entry(
                fixture,
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                start="2025-03-30",
                end="2025-06-28",
            )["val"]
        )
    )
    return current / prior - 1


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    configure_app_db(tmp_path, monkeypatch)
    with make_client() as testclient:
        yield testclient


def _matched_tickers(html: str) -> list[str]:
    return re.findall(r'data-ticker="([A-Z]+)"', html.split('id="screener-rows"')[-1])


def test_screener_revenue_yoy_filter_matches_fixture_subset(client) -> None:
    expected_yoy = _aapl_latest_revenue_yoy()

    below = client.get("/screener", params={"revenue_yoy_gt": str(expected_yoy - Decimal("0.01"))})
    above = client.get("/screener", params={"revenue_yoy_gt": str(expected_yoy + Decimal("0.01"))})

    assert below.status_code == above.status_code == 200
    assert _matched_tickers(below.text) == ["AAPL"]
    assert _matched_tickers(above.text) == []  # strict > comparison
    assert "No companies match the active filters" in above.text


def test_screener_quarter_label_filter(client) -> None:
    # The fixture's latest AAPL quarter is labeled Strong (3+ true signals).
    strong = client.get("/screener", params={"quarter_label": "strong"})
    weak = client.get("/screener", params={"quarter_label": "weak"})

    assert strong.status_code == weak.status_code == 200
    assert _matched_tickers(strong.text) == ["AAPL"]
    assert _matched_tickers(weak.text) == []
    # the chosen label is echoed back in the form
    assert '<option value="strong" selected>' in strong.text


def test_screener_minimum_data_coverage_filter(client) -> None:
    passing = client.get("/screener", params={"minimum_data_coverage": "0.8"})

    assert passing.status_code == 200
    # AAPL's fixture quarter covers all 14 core concepts (coverage 1.0).
    assert _matched_tickers(passing.text) == ["AAPL"]


def test_screener_out_of_range_coverage_is_a_visible_error(client) -> None:
    response = client.get("/screener", params={"minimum_data_coverage": "1.01"})

    assert response.status_code == 200
    # Out-of-range input is an explicit validation error, never silently
    # coerced; the invalid filter is dropped and the page says so.
    assert "must be between 0 and 1" in response.text
    assert _matched_tickers(response.text) == ["AAPL"]  # filter ignored, not guessed


def test_screener_sector_filter(client) -> None:
    it = client.get("/screener", params={"sector": "Information Technology"})
    energy = client.get("/screener", params={"sector": "Energy"})

    assert it.status_code == energy.status_code == 200
    assert _matched_tickers(it.text) == ["AAPL"]
    assert _matched_tickers(energy.text) == []


def test_screener_combined_filters(client) -> None:
    response = client.get(
        "/screener",
        params={"revenue_yoy_gt": "0", "fcf_positive": "on", "sector": "Information Technology"},
    )

    assert response.status_code == 200
    assert _matched_tickers(response.text) == ["AAPL"]
    # both filters echoed
    assert 'name="revenue_yoy_gt" value="0"' in response.text
    assert 'name="fcf_positive" checked' in response.text


def test_screener_post_echoes_state_and_filters(client) -> None:
    response = client.post(
        "/screener",
        data={"revenue_yoy_gt": "0.1", "operating_margin_gt": "", "quarter_label": ""},
    )

    assert response.status_code == 200
    assert 'name="revenue_yoy_gt" value="0.1"' in response.text
    assert "1 of 1" in response.text or "match the active filters" in response.text


def test_screener_invalid_decimal_shows_error(client) -> None:
    response = client.get("/screener", params={"revenue_yoy_gt": "abc"})

    assert response.status_code == 200
    assert "not a valid decimal number" in response.text


def test_screener_no_filters_lists_every_company_with_data(client) -> None:
    response = client.get("/screener")

    assert response.status_code == 200
    assert _matched_tickers(response.text) == ["AAPL"]  # empty companies never match
    assert "no filters active" in response.text


@pytest.mark.usefixtures("block_llm_modules")
def test_screener_works_with_llm_modules_blocked(client) -> None:
    response = client.get("/screener", params={"revenue_yoy_gt": "0.1"})

    assert response.status_code == 200
    assert _matched_tickers(response.text) == ["AAPL"]


def test_screener_rows_render_exact_decimal_values(client) -> None:
    response = client.get("/screener")

    assert response.status_code == 200
    # AAPL's fixture FCF, rendered from the exact Decimal in USD millions --
    # values flow through string-exact, never via float round-trips.
    assert "31,914.0" in response.text
