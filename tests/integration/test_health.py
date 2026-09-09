"""Integration tests for GET /health (SPEC §19)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from quarterline.api.main import create_app


def _db_url(tmp_path: Path, name: str) -> str:
    return f"sqlite:///{(tmp_path / name).as_posix()}"


def test_health_ok_db_models_down_returns_200_degraded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", _db_url(tmp_path, "health-ok.db"))
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")  # nothing listens here

    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["database"] == "ok"
    assert body["generation_provider"] == "unavailable"
    assert body["embedding_provider"] == "unavailable"
    assert body["status"] == "degraded"


def test_health_provider_statuses_cached_for_30s(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", _db_url(tmp_path, "health-cache.db"))
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")

    with TestClient(create_app()) as client:
        first = client.get("/health")
        app_state = client.app.state.provider_health_cache
        assert app_state is not None
        second = client.get("/health")

    assert first.json() == second.json()
    assert client.app.state.provider_health_cache is app_state  # same cache entry reused


def test_health_broken_database_returns_503(tmp_path: Path, monkeypatch) -> None:
    # Parent directory does not exist -> SQLite cannot open the database.
    broken_url = f"sqlite:///{(tmp_path / 'missing-dir' / 'app.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", broken_url)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")

    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["database"] == "fail"
    assert body["status"] == "degraded"
