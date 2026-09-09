"""JSON fact APIs (SPEC §19): /api/c/{ticker}/facts, /metrics, /provenance.

Contracts under test: Decimals serialize as strings (never floats), 404 JSON
for unknown tickers/metrics, METRIC_IDS allowlist gate, and no route touches
LLM modules even when they are unimportable.
"""

from __future__ import annotations

import pytest
from facts_test_helpers import facts_cli_guard  # noqa: F401 (pytest fixture)
from fastapi.testclient import TestClient
from ui_test_helpers import (
    block_llm_modules,  # noqa: F401 (pytest fixture)
    configure_app_db,
    make_client,
)

pytestmark = pytest.mark.usefixtures("facts_cli_guard")

LATEST_END = "2026-06-27"  # fixture's latest complete quarter (FY2026 Q3)


@pytest.fixture
def api_client(tmp_path, monkeypatch) -> TestClient:
    configure_app_db(tmp_path, monkeypatch)
    with make_client() as client:
        yield client


def _assert_no_floats(payload: object) -> None:
    if isinstance(payload, dict):
        for value in payload.values():
            _assert_no_floats(value)
    elif isinstance(payload, list):
        for value in payload:
            _assert_no_floats(value)
    else:
        assert not isinstance(payload, float), f"float leaked into JSON: {payload!r}"


def test_metrics_series_returns_eight_quarters_with_string_decimals(api_client) -> None:
    response = api_client.get("/api/c/AAPL/metrics")

    assert response.status_code == 200
    body = response.json()
    assert body["ticker"] == "AAPL"
    assert body["n_quarters"] == 8
    assert len(body["quarters"]) == 8
    ends = [q["period_end"] for q in body["quarters"]]
    assert ends == sorted(ends)
    assert ends[-1] == LATEST_END
    latest = body["quarters"][-1]
    revenue = latest["metrics"]["revenue"]
    assert isinstance(revenue["value"], str)  # Decimal string, never a float
    assert revenue["status"] == "ok"
    assert revenue["formula_version"] is not None
    _assert_no_floats(body)


def test_facts_endpoint_returns_quarter_fact_card(api_client) -> None:
    response = api_client.get("/api/c/AAPL/facts")

    assert response.status_code == 200
    card = response.json()
    assert card["ticker"] == "AAPL"
    assert card["period_end"] == LATEST_END
    assert card["fiscal_quarter"] == "Q3"
    metric_ids = {m["metric_id"] for m in card["metrics"]}
    assert {"revenue", "revenue_yoy", "quarter_label", "fundamental_score"} <= metric_ids
    for metric in card["metrics"]:
        assert metric["provenance"]["formula_version"] is not None
    _assert_no_floats(card)


def test_facts_endpoint_accepts_period_end(api_client) -> None:
    response = api_client.get("/api/c/AAPL/facts?period_end=2025-09-27")

    assert response.status_code == 200
    card = response.json()
    assert card["period_end"] == "2025-09-27"
    by_id = {m["metric_id"]: m for m in card["metrics"]}
    # Q4 diluted EPS is never derived (SPEC 10.3): it must be missing, not guessed.
    assert by_id["diluted_eps"]["status"] == "missing"
    assert by_id["diluted_eps"]["value"] is None


def test_unknown_ticker_returns_404_json(api_client) -> None:
    for path in (
        "/api/c/NOPE/metrics",
        "/api/c/NOPE/facts",
        "/api/c/NOPE/provenance/revenue?period_end=2026-06-27",
    ):
        response = api_client.get(path)
        assert response.status_code == 404, path
        assert "error" in response.json()


def test_bogus_metric_id_is_rejected_by_allowlist(api_client) -> None:
    response = api_client.get(
        f"/api/c/AAPL/provenance/definitely_not_a_metric?period_end={LATEST_END}"
    )

    assert response.status_code == 404
    assert "allowlist" in response.json()["error"]


def test_invalid_period_end_returns_404_json(api_client) -> None:
    response = api_client.get("/api/c/AAPL/facts?period_end=not-a-date")

    assert response.status_code == 404
    assert "period_end" in response.json()["error"]


def test_provenance_json_contract(api_client) -> None:
    response = api_client.get(f"/api/c/AAPL/provenance/revenue_yoy?period_end={LATEST_END}")

    assert response.status_code == 200
    report = response.json()
    assert report["ticker"] == "AAPL"
    assert report["metric_id"] == "revenue_yoy"
    assert report["derivation"] == "computed"  # growth is computed from two facts
    assert report["formula_text"] and "revenue_yoy" in report["formula_text"]
    assert isinstance(report["value"], str)
    assert report["sources"], "computed metric must expose its input observations"
    for source in report["sources"]:
        assert source["accession"] and "-" in source["accession"]
        assert source["form"] == "10-Q"
        assert source["filed_at"] is not None
    _assert_no_floats(report)


@pytest.mark.usefixtures("block_llm_modules")
def test_routes_work_with_llm_modules_blocked(api_client) -> None:
    import importlib

    with pytest.raises(ImportError):
        importlib.import_module("quarterline.llm")

    for path in ("/", "/screener", "/c/AAPL", "/api/c/AAPL/metrics"):
        response = api_client.get(path)
        assert response.status_code == 200, path
