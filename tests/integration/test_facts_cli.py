"""Integration tests for the facts CLI handlers registered into
``quarterline.cli.SUBCOMMAND_REGISTRY`` (``ingest facts``, ``verify facts``)."""

from __future__ import annotations

import argparse

import pytest
from facts_test_helpers import (
    FACTS_CLI_KEYS,
    FakeCompanyFactsClient,
    create_schema,
    ensure_facts_cli_registered,
    facts_cli_guard,  # noqa: F401 (pytest fixture imported into this module)
    import_ingest_facts,
    load_aapl_fixture,
)

from quarterline.cli import SUBCOMMAND_REGISTRY

pytestmark = pytest.mark.usefixtures("facts_cli_guard")


def _args(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def test_handlers_are_registered_when_module_imported() -> None:
    # Importing the module registers the handlers (idempotent re-registration
    # guards against a previous test's guard having cleaned the registry).
    ensure_facts_cli_registered()
    module = import_ingest_facts()
    assert "ingest:facts" in SUBCOMMAND_REGISTRY
    assert "verify:facts" in SUBCOMMAND_REGISTRY
    assert SUBCOMMAND_REGISTRY["ingest:facts"] is module._handle_ingest_facts
    assert SUBCOMMAND_REGISTRY["verify:facts"] is module._handle_verify_facts


def test_registration_is_import_side_effect(tmp_path, monkeypatch) -> None:
    # Without importing the facts module the CLI shows the wave-1 notice.
    monkeypatch.chdir(tmp_path)
    from quarterline.cli import main as cli_main

    for key in FACTS_CLI_KEYS:
        SUBCOMMAND_REGISTRY.pop(key, None)
    try:
        assert cli_main(["verify", "facts", "--ticker", "AAPL"]) == 2
    finally:
        pass


def test_cli_verify_end_to_end(tmp_path, monkeypatch, capsys) -> None:
    """Full CLI path against a seeded tmp DB using the real AAPL fixture.

    NOTE: `python -m quarterline verify facts` only reaches these handlers once
    the registering module is imported by the entry point (wiring gap reported
    to the orchestrator); here the registration import is explicit.
    """
    from quarterline.cli import main as cli_main

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    monkeypatch.chdir(tmp_path)
    create_schema()
    ensure_facts_cli_registered()

    report = import_ingest_facts().ingest_facts(
        tickers=["AAPL"],
        watchlist="data/watchlist_us.csv",
        client=FakeCompanyFactsClient(load_aapl_fixture()),
    )
    assert report.errors == {}

    code = cli_main(["verify", "facts", "--ticker", "AAPL"])
    assert code == 0
    out = capsys.readouterr().out
    assert "verify facts: AAPL (CIK 0000320193" in out
    assert "revenue" in out and "net_income" in out
    # provenance columns: accession + form + filed date + direct/derived flags
    assert "0000320193-26-000020" in out  # real Q3 FY2026 10-Q accession
    assert "10-Q" in out and "10-K" in out
    assert "[d:" in out  # direct fact marker
    assert "D:" in out or "pD:" in out  # derived fact markers

    # ingest handler on a re-run: without a valid EDGAR identity the live path
    # refuses (SPEC 7 identity gate surfaces through the CLI as exit 1)
    facts_module = import_ingest_facts()
    code = facts_module._handle_ingest_facts(
        _args(tickers=["AAPL"], watchlist="data/watchlist_us.csv")
    )
    captured = capsys.readouterr().out
    assert code == 1
    assert "ERROR" in captured
    assert "refusing live SEC" in captured

    # verify handler as a function: unknown ticker exits 1 with message
    code = facts_module._handle_verify_facts(_args(ticker="NOPE"))
    assert code == 1
    assert "unknown ticker" in capsys.readouterr().out


def test_verify_facts_requires_ingested_company(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    monkeypatch.chdir(tmp_path)
    create_schema()
    facts_module = import_ingest_facts()
    code = facts_module._handle_verify_facts(_args(ticker="MSFT"))
    assert code == 1
    assert "unknown ticker" in capsys.readouterr().out
