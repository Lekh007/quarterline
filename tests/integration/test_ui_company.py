"""Company page (GET /c/{ticker}) and the provenance drill-down page.

Covers: chart data endpoint payload, fact table with direct/derived/missing
badges ("missing", never zero), formula explanations, the SPEC §12.1 label
panel (every input + rule), the brief placeholder, and the provenance page
showing accession/form/filed_at lineage (contract C6).
"""

from __future__ import annotations

import re

import pytest
from facts_test_helpers import facts_cli_guard  # noqa: F401 (pytest fixture)
from fastapi.testclient import TestClient
from ui_test_helpers import configure_app_db, make_client

pytestmark = pytest.mark.usefixtures("facts_cli_guard")

LATEST_END = "2026-06-27"
Q4_END = "2025-09-27"  # fixture quarter with legitimately missing concepts


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    configure_app_db(tmp_path, monkeypatch)
    with make_client() as testclient:
        yield testclient


def test_company_page_renders_charts_label_and_fact_table(client) -> None:
    response = client.get("/c/AAPL")

    assert response.status_code == 200
    html = response.text
    # chart JSON payload + four chart canvases + no-JS summary table
    assert 'id="chart-data"' in html
    assert html.count("<canvas") == 4
    assert "summary-table" in html
    assert "FY2026 Q3" in html
    # fact table badges: reported (direct), derived, formula text + versions
    assert "type-direct" in html and "type-derived" in html
    assert "ratios-v1" in html and "norm-v1" in html
    # formula explanation for a displayed derived metric
    assert "revenue_yoy = current_quarter_revenue" in html
    # provenance drill-down link for revenue_yoy on the latest quarter
    assert f"/c/AAPL/provenance/revenue_yoy?period_end={LATEST_END}" in html
    # SPEC §12.1 label panel: every signal input and rule in the UI
    assert "Quarter label" in html
    for signal_id in (
        "revenue_yoy_positive",
        "operating_margin_change_non_negative",
        "cash_conversion_at_least_0_8",
        "fcf_positive",
        "share_dilution_at_most_2pct",
    ):
        assert signal_id in html
    assert "at_least_three_signals_true" in html  # the applied rule is shown
    assert "Rule-based quarterly performance label." in html
    # brief panel placeholder keeps the final layout (wave F5 fills it)
    assert 'id="brief-panel"' in html  # wave-3: real brief panel replaced the placeholder


def test_missing_data_renders_missing_never_zero(client) -> None:
    response = client.get(f"/c/AAPL?period_end={Q4_END}")

    assert response.status_code == 200
    html = response.text
    assert ">missing<" in html  # explicit missing state in the fact table
    assert "value-missing" in html
    # diluted_eps is never derived for Q4 (SPEC 10.3): shown missing, with a
    # warning note explaining that missing stays missing (never zero-filled).
    assert "missing stays missing" in html or "concept 'diluted_eps' missing" in html
    eps_row = re.search(r'<tr data-metric="diluted_eps">.*?</tr>', html, re.DOTALL).group(0)
    assert ">missing<" in eps_row  # the cell says "missing"...
    assert "value-missing" in eps_row  # ...styled as missing...
    assert not re.search(r"value-cell[^>]*>\s*-?\d", eps_row)  # ...never a number


def test_quarter_selector_switches_period(client) -> None:
    latest = client.get("/c/AAPL")
    earlier = client.get(f"/c/AAPL?period_end={Q4_END}")

    assert latest.status_code == earlier.status_code == 200
    assert f'href="/c/AAPL?period_end={Q4_END}"' in latest.text
    assert f'href="/c/AAPL?period_end={LATEST_END}"' in earlier.text


def test_unknown_ticker_404(client) -> None:
    response = client.get("/c/NOPE")

    assert response.status_code == 404
    assert "NOPE" in response.text
    assert "Research and education only." in response.text  # error page keeps footer


def test_invalid_period_end_404(client) -> None:
    response = client.get("/c/AAPL?period_end=junk")

    assert response.status_code == 404


def test_provenance_page_shows_full_lineage(client) -> None:
    response = client.get(f"/c/AAPL/provenance/revenue_yoy?period_end={LATEST_END}")

    assert response.status_code == 200
    html = response.text
    assert "computed" in html  # derivation class
    assert "revenue_yoy = current_quarter_revenue" in html  # formula text
    assert "0000320193-26-000" in html  # dashed accession of a source observation
    assert "10-Q" in html  # form
    assert "Original tag" in html or "RevenueFromContractWithCustomerExcludingAssessedTax" in html
    assert "Direct?" in html
    assert 'href="/c/AAPL"' in html  # back link


def test_provenance_page_unknown_metric_404(client) -> None:
    response = client.get("/c/AAPL/provenance/not_a_real_metric")

    assert response.status_code == 404
    assert "not_a_real_metric" in response.text
