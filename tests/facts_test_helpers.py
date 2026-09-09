"""Shared helpers for Quarterline financial-track tests (F1 wave).

Loads the committed fixtures (real trimmed AAPL data + the clearly-marked
synthetic edge-case fixture) and provides a fixture-backed fake SEC client so
pipeline tests never touch the network (SPEC 26: real-source financial
fixtures; live calls are prohibited in CI).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from quarterline.store.db import get_engine
from quarterline.store.models import Base

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = TESTS_DIR / "fixtures"
AAPL_FIXTURE_PATH = FIXTURES_DIR / "sec" / "aapl_companyfacts_subset.json"
SYNTHETIC_FIXTURE_PATH = FIXTURES_DIR / "sec" / "synthetic_edge_cases.json"
MANIFEST_PATH = FIXTURES_DIR / "provenance_manifest.json"


def load_aapl_fixture() -> dict:
    return json.loads(AAPL_FIXTURE_PATH.read_text(encoding="utf-8"))


def load_synthetic_fixture() -> dict:
    return json.loads(SYNTHETIC_FIXTURE_PATH.read_text(encoding="utf-8"))


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


class FakeCompanyFactsClient:
    """Duck-typed SecClient stand-in returning one canned companyfacts payload."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def get_json(self, url: str) -> dict:
        self.urls.append(url)
        return self.payload


def create_schema(database_url: str | None = None) -> None:
    """Create all tables on the configured (or given) database."""
    from quarterline.store.db import get_engine as _ge

    engine = _ge(database_url) if database_url else get_engine()
    Base.metadata.create_all(engine)


def find_fact_entry(fixture: dict, tag: str, *, start: str, end: str, unit: str = "USD") -> dict:
    """Locate one raw observation in a fixture payload (values stay fixture-read)."""
    entries = fixture["facts"]["us-gaap"][tag]["units"][unit]
    for entry in entries:
        if entry.get("start") == start and entry.get("end") == end:
            return entry
    raise AssertionError(f"fixture entry not found: {tag} {start}..{end} ({unit})")


def fact_for(
    session_facts_repo, company_id: int, concept: str, period_start, period_end, kind="quarter"
):
    return session_facts_repo.find_fact(
        company_id, concept, period_start, period_end, kind, "consolidated"
    )


def instant_fact_for(session_facts_repo, company_id: int, concept: str, period_end):
    return session_facts_repo.find_fact(
        company_id, concept, None, period_end, "instant", "consolidated"
    )


def d(text: str) -> date:
    return date.fromisoformat(text)


#: CLI registry keys registered by quarterline.ingest.facts at import time.
FACTS_CLI_KEYS = ("ingest:facts", "verify:facts")


@pytest.fixture
def facts_cli_guard():
    """Pop the facts CLI registrations after a test that imported the module.

    ``quarterline.ingest.facts`` registers into ``SUBCOMMAND_REGISTRY`` at
    import time (the documented Wave-0 mechanism). Tests import it lazily and
    use this guard so the global registry state seen by other tests (e.g. the
    Wave-0 wave-notice assertions) stays unchanged.
    """
    yield
    from quarterline.cli import SUBCOMMAND_REGISTRY

    for key in FACTS_CLI_KEYS:
        SUBCOMMAND_REGISTRY.pop(key, None)


def import_ingest_facts():
    """Lazily import the facts pipeline module (performs CLI registration)."""
    import quarterline.ingest.facts

    return quarterline.ingest.facts


def ensure_facts_cli_registered() -> None:
    """(Re-)register the facts CLI handlers.

    Import-side effects run only once per process; after the guard fixture has
    popped the registry, tests that need the handlers call this idempotently.
    """
    from quarterline.cli import register_subcommand

    module = import_ingest_facts()
    register_subcommand("ingest:facts", module._handle_ingest_facts)
    register_subcommand("verify:facts", module._handle_verify_facts)
