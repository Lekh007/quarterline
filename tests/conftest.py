"""Shared test fixtures: cache isolation between tests."""

from __future__ import annotations

import pytest

from quarterline.store.db import reset_db_caches


@pytest.fixture(autouse=True)
def _reset_settings_and_engine_caches():
    """Every test starts with fresh Settings/engine caches so monkeypatched env applies."""
    reset_db_caches()
    yield
    reset_db_caches()


@pytest.fixture
def offline_env(tmp_path, monkeypatch):
    """Point storage + database at a tmp directory and Ollama at a dead port."""
    storage = tmp_path / "storage"
    monkeypatch.setenv("STORAGE_DIR", str(storage))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")
    return storage
