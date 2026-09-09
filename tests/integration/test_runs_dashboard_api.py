"""Integration: runs API + observability dashboard over seeded events (SPEC §24)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quarterline.observability.events import emit_run_event, new_run_id
from quarterline.store.db import get_engine
from quarterline.store.models import Base


@pytest.fixture
def client(offline_env: Path):
    Base.metadata.create_all(get_engine())
    from quarterline.api.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def test_runs_api_empty_state(client) -> None:
    response = client.get("/api/runs")
    assert response.status_code == 200
    assert response.json() == {"runs": [], "count": 0}


def test_runs_api_lists_and_traces_seeded_runs(client) -> None:
    run_id = new_run_id()
    emit_run_event(
        run_id,
        {
            "endpoint": "ask",
            "company_id": "AAPL",
            "provider": "ollama",
            "model": "qwen3:4b",
            "prompt_version": "brief-v1",
            "embedding_model": "fake-embed",
            "corpus_version": "ix-1",
            "chunking_strategy": "section",
            "retrieval_strategy": "hybrid",
            "reranker_model": None,
            "input_tokens": 100,
            "output_tokens": 40,
            "token_count_source": "provider",
            "retrieval_latency_ms": 12.5,
            "generation_latency_ms": 90.0,
            "total_latency_ms": 103.0,
            "json_valid": True,
            "citation_valid": True,
            "factcheck_passed": True,
            "retained_bullet_count": 4,
            "dropped_bullet_count": 1,
            "answer_status": "ok",
            "cache_hit": False,
            "error_type": None,
            "estimated_api_cost": None,
            "cost_currency": None,
            "cost_basis": None,
        },
    )
    emit_run_event(run_id, {"endpoint": "ask", "answer_status": "ok", "step": "done"})

    listing = client.get("/api/runs").json()
    assert listing["count"] == 1
    assert listing["runs"][0]["run_id"] == run_id
    assert listing["runs"][0]["endpoint"] == "ask"

    trace_response = client.get(f"/api/runs/{run_id}")
    assert trace_response.status_code == 200
    trace = trace_response.json()
    assert trace["run"]["status"] == "ok"
    assert len(trace["events"]) == 2
    payload = json.loads(json.dumps(trace["events"][0]))
    assert payload["provider"] == "ollama"
    assert payload["retrieval_latency_ms"] == 12.5


def test_runs_api_unknown_run_is_404(client) -> None:
    assert client.get("/api/runs/deadbeef").status_code == 404


def test_dashboard_renders_seeded_metrics(client) -> None:
    run_id = new_run_id()
    emit_run_event(
        run_id,
        {
            "endpoint": "search",
            "provider": "fake",
            "model": "fake-embed",
            "retrieval_latency_ms": 5.0,
            "answer_status": "ok",
            "cache_hit": True,
        },
    )
    response = client.get("/dashboard")
    assert response.status_code == 200
    html = response.text
    assert "Observability dashboard" in html
    assert "1 runs" in html  # sample sizes surface, not just rates
    assert "search" in html
    assert run_id[:12] in html  # recent-runs table links the trace
    assert f"/api/runs/{run_id}" in html
    assert "Low-n numbers are noisy" in html or "low-n" in html.lower()


def test_dashboard_graceful_empty_state(client) -> None:
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "No runs recorded yet" in response.text


def test_dashboard_shows_latest_eval_summary(client) -> None:
    from datetime import UTC, datetime

    import sqlalchemy

    from quarterline.store.models import EvalRun

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            sqlalchemy.insert(EvalRun).values(
                dataset_version="abc123",
                chunking_strategy="section",
                retrieval_config="hybrid",
                status="complete",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                notes="fixture-corpus retrieval matrix",
            )
        )
    html = client.get("/dashboard").text
    assert "Last eval run" in html
    assert "hybrid" in html
