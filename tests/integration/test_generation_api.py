"""API tests for POST /api/c/{ticker}/brief and POST /api/ask (SPEC §19).

Runs the real app via TestClient against the offline generation fixture store.
The default generation provider points at a dead Ollama port, so the brief
route exercises its explicit degraded mode (SPEC §25); scripted providers are
injected at the service layer for the ok/refused paths (no live model).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from generation_test_helpers import (  # noqa: F401 (registers generation_db)
    generation_db_fixture,
)


@pytest.fixture
def client(generation_db) -> TestClient:
    from quarterline.api.main import create_app

    with TestClient(create_app()) as testclient:
        yield testclient


def test_brief_route_degraded_provider_returns_facts_and_evidence_json(client) -> None:
    response = client.post("/api/c/AAPL/brief", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "provider_unavailable"
    assert body["ticker"] == "AAPL"
    assert body["brief"] is None
    assert body["facts"], "degraded response still carries the fact card"
    assert body["evidence"], "degraded response still carries retrieved evidence"
    assert any("provider unavailable" in reason.lower() for reason in body["reasons"])


def test_brief_route_degraded_html_shows_banner(client) -> None:
    response = client.post(
        "/api/c/AAPL/brief",
        json={},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    html = response.text
    assert "generation unavailable" in html
    assert "not generated prose" in html
    assert "Fact card" in html
    assert "Retrieved evidence" in html


def test_brief_route_unknown_ticker_404(client) -> None:
    response = client.post("/api/c/NOPE/brief", json={})
    assert response.status_code == 404
    assert "error" in response.json()


def test_brief_route_rejects_unknown_fields(client) -> None:
    response = client.post("/api/c/AAPL/brief", json={"metric": "revenue"})
    assert response.status_code == 400
    assert "unknown field" in response.json()["error"]


def test_brief_route_invalid_period_end_400(client) -> None:
    response = client.post("/api/c/AAPL/brief", json={"period_end": "not-a-date"})
    assert response.status_code == 400


def test_ask_route_refuses_advice_with_research_only_alternative(client) -> None:
    response = client.post(
        "/api/ask",
        json={"question": "should I buy AAPL?", "ticker": "AAPL"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "refused"
    assert "does not provide investment advice" in body["answer"]["text"]
    assert body["advice_policy_version"] == "advice-policy-v1"


def test_ask_route_requires_question_field(client) -> None:
    response = client.post("/api/ask", json={})
    assert response.status_code == 400


def test_ask_route_get_method_not_allowed(client) -> None:
    # questions travel in POST bodies, never URL query strings (SPEC §19)
    assert client.get("/api/ask", params={"question": "x"}).status_code == 405


def test_ask_route_degraded_provider_reports_unavailable(client) -> None:
    response = client.post(
        "/api/ask",
        json={"question": "What did management say about revenue growth?", "ticker": "AAPL"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "provider_unavailable"
    assert body["evidence"], "evidence still shown while generation is down"


def test_brief_route_success_path_with_injected_provider(client, monkeypatch) -> None:
    """The route's lazy service call is exercised with a scripted outcome by
    monkeypatching the generation entry point (the route resolves the name
    from the generation module at call time)."""
    from quarterline.llm.generation import GenerationOutcome
    from quarterline.llm.schemas import Brief

    outcome = GenerationOutcome(
        kind="brief",
        status="ok",
        ticker="AAPL",
        label_display="Strong",
        label_value="strong",
        brief=Brief(
            status="ok",
            label_echo="Strong",
            bullets=[
                {
                    "text": "Management attributed services growth to the results. "
                    "[ev-0123456789ab]",
                    "evidence_ids": ["ev-0123456789ab"],
                }
            ],
        ),
        metric_facts=["Revenue changed +9.5% year over year."],
    )

    def fake_generate_brief(ticker, period_end=None, **_kwargs):
        assert ticker == "AAPL"
        return outcome

    monkeypatch.setattr("quarterline.llm.generation.generate_brief", fake_generate_brief)
    response = client.post("/api/c/AAPL/brief", json={}, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "Management attributed services growth" in response.text
    assert "/evidence/ev-0123456789ab" in response.text
    assert "Revenue changed +9.5% year over year." in response.text
