"""Retrieval CLI integration tests: ``index build`` and ``search`` through the
W0 SUBCOMMAND_REGISTRY (cli.py lazy-imports ``quarterline.retrieve`` in
``main()``), plus the documented arg surface. Fully offline via
``EMBED_PROVIDER=fake``."""

from __future__ import annotations

import pytest
from retrieval_test_helpers import (
    fixture_provider,
    retrieval_db_fixture,  # noqa: F401 (registers the `retrieval_db` fixture)
)

from quarterline.cli import SUBCOMMAND_REGISTRY
from quarterline.cli import main as cli_main
from quarterline.store.db import session_scope
from quarterline.store.repositories.search_sqlite import SearchIndexRepo


@pytest.fixture(autouse=True)
def _fake_embed_provider(monkeypatch):
    """Route the CLI at the deterministic offline provider."""
    monkeypatch.setenv("EMBED_PROVIDER", "fake")


def test_handlers_registered_at_package_import() -> None:
    import quarterline.retrieve  # noqa: F401 - performs registration

    assert "index:build" in SUBCOMMAND_REGISTRY
    assert "search" in SUBCOMMAND_REGISTRY


def test_index_build_arg_surface(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli_main(["index", "build", "--strategy", "fixed", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--strategy" in out
    assert "chunking strategy" in out

    with pytest.raises(SystemExit) as bad:
        cli_main(["index", "build", "--strategy", "nonsense"])
    assert bad.value.code == 2


def test_search_arg_surface(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli_main(
            [
                "search",
                "--ticker",
                "AAPL",
                "--query",
                "q",
                "--strategy",
                "section",
                "--retrieval",
                "hybrid-rerank",
                "--help",
            ]
        )
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--ticker", "--query", "--strategy", "--retrieval"):
        assert flag in out
    assert "hybrid-rerank" in out


def test_index_build_then_search_end_to_end(retrieval_db, capsys) -> None:
    # Rebuild through the CLI (idempotent: everything already indexed).
    assert cli_main(["index", "build", "--strategy", "section"]) == 0
    out = capsys.readouterr().out
    assert "Index build report" in out
    assert "index version:" in out

    assert (
        cli_main(
            [
                "search",
                "--ticker",
                "AAPL",
                "--query",
                "Apple revenue March quarter",
                "--strategy",
                "section",
                "--retrieval",
                "hybrid",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Search results" in out
    assert "ev-" in out  # evidence ids printed
    assert "evidence policy: evidence-policy-v1" in out
    assert "revenue" in out.lower()


def test_search_reports_reranker_degradation(retrieval_db, capsys) -> None:
    exit_code = cli_main(
        [
            "search",
            "--ticker",
            "AAPL",
            "--query",
            "Apple revenue March quarter",
            "--strategy",
            "section",
            "--retrieval",
            "hybrid-rerank",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "degraded[reranker]" in out


def test_search_reports_insufficient_evidence(retrieval_db, capsys) -> None:
    assert (
        cli_main(["search", "--ticker", "MSFT", "--query", "anything", "--retrieval", "hybrid"])
        == 0
    )
    out = capsys.readouterr().out
    assert "INSUFFICIENT EVIDENCE" in out
    assert "(no evidence)" in out


def test_index_build_reports_corpus_stats(retrieval_db, capsys) -> None:
    with session_scope() as session:
        stats = SearchIndexRepo(session).corpus_stats()
    assert stats["documents"] == 4
    assert fixture_provider().model_id in str(stats["embeddings"])
