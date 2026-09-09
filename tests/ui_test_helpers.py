"""Shared helpers for the wave-2 UI/API deterministic-track tests (F4).

Seeds a temporary SQLite database with the real trimmed AAPL companyfacts
fixture (via ``facts_test_helpers``) so TestClient requests never touch the
network or a model. The whole watchlist (15 companies) is registered by the
facts pipeline, so the watchlist page shows AAPL plus 14 gracefully empty
companies — exactly the production shape.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from facts_test_helpers import (
    FakeCompanyFactsClient,
    create_schema,
    import_ingest_facts,
    load_aapl_fixture,
)
from fastapi.testclient import TestClient

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
INJECTION_FIXTURE_TEXT = (FIXTURES_DIR / "documents" / "injection_filing.html").read_text(
    encoding="utf-8"
)

#: All LLM modules must stay unimportable for the deterministic routes (SPEC §25).
LLM_MODULE_NAMES = (
    "quarterline.llm",
    "quarterline.llm.brief",
    "quarterline.llm.ask",
    "quarterline.llm.schemas",
    "quarterline.llm.providers",
)


def configure_app_db(tmp_path, monkeypatch) -> None:
    """Point the app at a fresh tmp SQLite DB and ingest the AAPL fixture."""
    storage = tmp_path / "storage"
    monkeypatch.setenv("STORAGE_DIR", str(storage))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")  # nothing listens here
    create_schema()
    report = import_ingest_facts().ingest_facts(
        tickers=["AAPL"],
        watchlist="data/watchlist_us.csv",
        client=FakeCompanyFactsClient(load_aapl_fixture()),
    )
    assert report.errors == {}, report.errors


def make_client() -> TestClient:
    """TestClient for the configured app (Ollama dead: deterministic paths only)."""
    from quarterline.api.main import create_app

    return TestClient(create_app())


@pytest.fixture
def block_llm_modules(monkeypatch):
    """Make every LLM module unimportable for the duration of a test.

    The deterministic routes must never import generation code (SPEC §25:
    with Ollama stopped, tables, charts and the screener still work). With
    these names set to None, any accidental ``quarterline.llm`` import raises
    ImportError and fails the test.
    """
    for name in LLM_MODULE_NAMES:
        monkeypatch.setitem(sys.modules, name, None)
