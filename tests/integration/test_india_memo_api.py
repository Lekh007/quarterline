"""IND-8 India memo workflow + generation route API tests (offline).

The India memo workflow reuses the US graph with the India tool bindings and
the India prompt/gate: identical budgets (4 tool calls incl. export, 12
transitions, 1 repair, deadline), identical approval-gated export, identical
audit trail. Scripted providers are injected at the provider factories; the
store is the seeded INFY+HUL corpus with FakeEmbeddingProvider.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from india_generation_test_helpers import (
    HUL,
    INFY,
    Q1_FY27_END,
    ScriptedProvider,
    brief_context_passages,
    india_generation_db_fixture,  # noqa: F401 (registers the fixture)
    india_memo_payload,
    passage_ids_by_slug,
    scripted_result,
)

from quarterline.store.db import session_scope


@pytest.fixture
def client(india_generation_db, monkeypatch) -> TestClient:
    """Offline app over the seeded India store; fake embeddings, dead Ollama."""
    monkeypatch.setenv("EMBED_PROVIDER", "fake")
    from quarterline.api.main import create_app

    with TestClient(create_app()) as testclient:
        yield testclient


@pytest.fixture
def ids_and_labels(india_generation_db) -> dict:
    with session_scope() as session:
        ids = passage_ids_by_slug(brief_context_passages(session), india_generation_db["documents"])
        from quarterline.sources.india.brief import application_label_of
        from quarterline.sources.india.factcard import build_india_fact_card

        return {
            "infy_statement": ids["infy-statement"],
            "infy_label": application_label_of(
                build_india_fact_card(session, INFY, period_end=Q1_FY27_END)
            ),
        }


def test_india_memo_happy_path_to_approval_and_export(
    client, india_generation_db, monkeypatch, ids_and_labels
) -> None:
    provider = ScriptedProvider(
        [
            scripted_result(
                india_memo_payload(ids_and_labels["infy_label"], ids_and_labels["infy_statement"])
            )
        ]
    )
    import quarterline.llm.generation as generation_module

    monkeypatch.setattr(
        generation_module, "default_generation_provider", lambda settings=None: provider
    )

    response = client.post(
        "/api/india-memos",
        json={"issuer_id": INFY, "memo_type": "quarter_review"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "awaiting_approval"
    assert body["approval_request_id"] is not None
    assert body["ticker"] == INFY
    # identical budgets to the US memo (SPEC §20)
    assert body["budgets"]["tool_calls"] == {"used": 2, "limit": 4}
    assert body["budgets"]["transitions"] == {"used": 8, "limit": 12}
    assert body["budgets"]["memo_repair_attempts"] == {"used": 0, "limit": 1}
    assert body["tool_call_log"][0]["tool_name"] == "get_india_facts"
    assert body["tool_call_log"][1]["tool_name"] == "search_india_filings"
    assert provider.call_count == 1
    # India metric mention rendered by Python from the fact card
    assert body["metric_facts"], body["metric_facts"]

    # approve-export with the exact memo content exports (md)
    approve = client.post(
        f"/api/india-memos/{body['run_id']}/approve-export",
        json={
            "approval_request_id": body["approval_request_id"],
            "export_type": "md",
            "memo_content": body["memo_content"],
        },
    )
    assert approve.status_code == 200
    exported = approve.json()
    assert exported["status"] == "exported"
    assert exported["exported"]["filename"].startswith("memo-")
    assert exported["run"]["status"] == "completed"


def test_india_memo_approval_hash_tie_refuses_tampered_content(
    client, india_generation_db, monkeypatch, ids_and_labels
) -> None:
    provider = ScriptedProvider(
        [
            scripted_result(
                india_memo_payload(ids_and_labels["infy_label"], ids_and_labels["infy_statement"])
            )
        ]
    )
    import quarterline.llm.generation as generation_module

    monkeypatch.setattr(
        generation_module, "default_generation_provider", lambda settings=None: provider
    )
    created = client.post(
        "/api/india-memos", json={"issuer_id": INFY, "memo_type": "quarter_review"}
    ).json()

    tampered = client.post(
        f"/api/india-memos/{created['run_id']}/approve-export",
        json={
            "approval_request_id": created["approval_request_id"],
            "export_type": "md",
            "memo_content": created["memo_content"] + "\ntampered",
        },
    )
    assert tampered.status_code == 409
    assert "hash" in tampered.json()["error"] or "changed" in tampered.json()["error"]

    # the untouched content still exports (the tampered attempt decided nothing)
    good = client.post(
        f"/api/india-memos/{created['run_id']}/approve-export",
        json={
            "approval_request_id": created["approval_request_id"],
            "export_type": "md",
            "memo_content": created["memo_content"],
        },
    )
    assert good.status_code == 200


def test_india_memo_unknown_or_unverified_issuer_404(client) -> None:
    unknown = client.post(
        "/api/india-memos", json={"issuer_id": "IN-NOPE", "memo_type": "quarter_review"}
    )
    assert unknown.status_code == 404


def test_india_memo_requires_issuer_id_and_valid_type(client) -> None:
    missing = client.post("/api/india-memos", json={"memo_type": "quarter_review"})
    assert missing.status_code == 400
    assert "issuer_id" in missing.json()["error"]
    bad_type = client.post("/api/india-memos", json={"issuer_id": INFY, "memo_type": "hype"})
    assert bad_type.status_code == 400


def test_india_memo_advice_question_refused_without_provider_call(
    client, india_generation_db, monkeypatch
) -> None:
    def _boom(settings=None):
        raise AssertionError("provider must not be constructed for advice requests")

    import quarterline.llm.generation as generation_module

    monkeypatch.setattr(generation_module, "default_generation_provider", _boom)
    response = client.post(
        "/api/india-memos",
        json={
            "issuer_id": HUL,
            "memo_type": "quarter_review",
            "question": "Should I buy HUL shares now?",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "refused"
    assert any("advice-policy" in error for error in body["errors"])


def test_india_memo_get_route_returns_the_checkpointed_run(
    client, india_generation_db, monkeypatch, ids_and_labels
) -> None:
    provider = ScriptedProvider(
        [
            scripted_result(
                india_memo_payload(ids_and_labels["infy_label"], ids_and_labels["infy_statement"])
            )
        ]
    )
    import quarterline.llm.generation as generation_module

    monkeypatch.setattr(
        generation_module, "default_generation_provider", lambda settings=None: provider
    )
    created = client.post(
        "/api/india-memos", json={"issuer_id": INFY, "memo_type": "quarter_review"}
    ).json()
    fetched = client.get(f"/api/india-memos/{created['run_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["run_id"] == created["run_id"]
    missing = client.get("/api/india-memos/" + "0" * 32)
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# POST /in/{issuer_id}/brief (HTML + JSON, degraded + scripted + refused)
# ---------------------------------------------------------------------------


def test_brief_route_degrades_honestly_with_provider_down(client) -> None:
    response = client.post("/in/IN-INFY/brief", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "provider_unavailable"
    assert body["brief"] is None  # never unvalidated prose
    assert len(body["facts"]) >= 10  # the fact card is shown
    assert len(body["evidence"]) >= 1  # the retrieved pages are shown
    assert "generation provider unavailable" in body["reasons"][0]


def test_brief_route_renders_the_validated_partial_with_scripted_provider(
    client, india_generation_db, monkeypatch, ids_and_labels
) -> None:
    provider = ScriptedProvider(
        [
            scripted_result(
                india_memo_payload(ids_and_labels["infy_label"], ids_and_labels["infy_statement"])
            )
        ]
    )
    import quarterline.sources.india.brief as brief_module

    monkeypatch.setattr(brief_module, "default_generation_provider", lambda settings=None: provider)
    payload = {
        "status": "ok",
        "label_echo": ids_and_labels["infy_label"],
        "metric_mentions": [{"metric_id": "india_revenue_qoq", "template": "reported_change"}],
        "bullets": [
            {
                "text": (
                    "Management stated demand for digital services remained "
                    f"strong. [{ids_and_labels['infy_statement']}]"
                ),
                "evidence_ids": [ids_and_labels["infy_statement"]],
            }
        ],
        "risks": [],
        "open_questions": [],
    }
    provider.script.append(scripted_result(payload))

    response = client.post(
        "/in/IN-INFY/brief",
        json={},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    html = response.text
    assert ids_and_labels["infy_label"] in html  # the echoed application label
    assert ids_and_labels["infy_statement"] in html  # the page-level citation
    assert "+3.9%" in html  # Python-rendered QoQ (never a model-typed number)
    assert "evidence-link" in html


def test_brief_route_refuses_advice_focus(client, india_generation_db, monkeypatch) -> None:
    def _boom(settings=None):
        raise AssertionError("provider must not be constructed for advice requests")

    import quarterline.sources.india.brief as brief_module

    monkeypatch.setattr(brief_module, "default_generation_provider", _boom)
    response = client.post(
        "/in/IN-HINDUNILVR/brief",
        json={"focus": "What is the price target for HUL?"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "refused"


def test_brief_route_validations(client) -> None:
    unknown = client.post("/in/IN-NOPE/brief", json={})
    assert unknown.status_code == 404
    bad_field = client.post("/in/IN-INFY/brief", json={"metric": "x"})
    assert bad_field.status_code == 400
    assert "unknown field" in bad_field.json()["error"]
    bad_period = client.post("/in/IN-INFY/brief", json={"period_end": "junk"})
    assert bad_period.status_code == 400
    bad_scope = client.post("/in/IN-INFY/brief", json={"scope": "combined"})
    assert bad_scope.status_code == 400


def test_india_company_page_carries_the_brief_panel(client) -> None:
    html = client.get("/in/IN-INFY").text
    assert 'hx-post="/in/IN-INFY/brief"' in html
    assert 'id="india-brief-panel"' in html
    assert "Never a recommendation or forecast" in html
