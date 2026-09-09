"""Watchlist page (GET /) — 15 rows, AAPL metrics, graceful empty states.

Every company without ingested data must render an explicit "no data
ingested" row: the UI never assumes facts exist and never substitutes zeros
for missing data (SPEC §2.1.5/2.1.6, §19 Watchlist).
"""

from __future__ import annotations

import re

import pytest
from facts_test_helpers import facts_cli_guard  # noqa: F401 (pytest fixture)
from fastapi.testclient import TestClient
from ui_test_helpers import configure_app_db, make_client

pytestmark = pytest.mark.usefixtures("facts_cli_guard")

LABEL_CAPTION = "Rule-based quarterly performance label. Not a recommendation or forecast."
DISCLAIMER = "Research and education only. Not investment advice."


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    configure_app_db(tmp_path, monkeypatch)
    with make_client() as testclient:
        yield testclient


def test_index_renders_15_rows_including_aapl_with_metrics(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    tickers = re.findall(r'data-ticker="([A-Z]+)"', html)
    assert len(tickers) == 15, "the whole watchlist renders"
    assert tickers[0] == "AAPL"
    assert "no data ingested" in html
    assert html.count("no data ingested") == 14  # only AAPL has fixture data
    assert LABEL_CAPTION in html  # exact SPEC §12.1 caption

    aapl_row = re.search(r'<tr[^>]*data-ticker="AAPL">.*?</tr>', html, re.DOTALL).group(0)
    text = re.sub(r"<[^>]+>", " ", aapl_row)
    assert "FY2026 Q3" in text  # latest complete quarter from the fixture
    assert "%" in text  # revenue YoY and operating margin rendered
    assert "Strong" in text  # quarter label from the fixture values


def test_index_empty_rows_never_substitute_zeros(client) -> None:
    html = client.get("/").text

    for match in re.finditer(r'<tr class="row-empty"[^>]*>.*?</tr>', html, re.DOTALL):
        row = re.sub(r"<[^>]+>", " ", match.group(0))
        assert "no data ingested" in row
        assert "%" not in row, "an empty company must not show any metric value"
        assert "USD" not in row


def test_htmx_request_receives_rows_partial_only(client) -> None:
    response = client.get("/", headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert "<html" not in response.text.lower()
    assert 'data-ticker="AAPL"' in response.text


def test_company_without_data_renders_graceful_page(client) -> None:
    response = client.get("/c/MSFT")  # registered on the watchlist, never ingested

    assert response.status_code == 200
    assert "No data ingested" in response.text
    assert "never substituted" in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/screener",
        "/c/AAPL",
        "/c/MSFT",
        "/c/AAPL/provenance/revenue_yoy",
        "/filings/999999",  # unknown document -> error page still carries it
        "/screener?revenue_yoy_gt=0.1",
    ],
)
def test_every_page_carries_the_research_disclaimer(client, path) -> None:
    response = client.get(path)

    assert response.status_code in {200, 404}
    assert DISCLAIMER in response.text


def test_security_headers_on_every_response(client) -> None:
    response = client.get("/")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
