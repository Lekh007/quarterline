"""Quarterline command-line interface.

Structure (stable for later waves):

- ``SUBCOMMAND_REGISTRY`` maps a flattened subcommand key (e.g.
  ``"ingest:facts"``, ``"db:upgrade"``) to a handler ``Callable[[Namespace], int]``.
- The argparse tree below is already declared for every documented subcommand,
  so later waves never edit this file. They register handlers from their own
  modules at import time::

        from quarterline.cli import register_subcommand

        def handler(args: argparse.Namespace) -> int:
            ...

        register_subcommand("ingest:facts", handler)

  To make registration happen automatically, import the registering module
  from the wave's own package ``__init__`` or from an ``ingest`` entry module;
  ``cli.main`` intentionally imports nothing beyond api/store.

- A registered handler receives the parsed ``Namespace`` and returns the process
  exit code. Unimplemented subcommands print a wave notice on stderr and exit 2.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

Handler = Callable[[argparse.Namespace], int]

#: Flattened subcommand key -> handler. Later waves register into this dict.
SUBCOMMAND_REGISTRY: dict[str, Handler] = {}

#: Wave ownership of still-stubbed subcommands (SPEC §30 milestones / PLAN §2).
WAVE_NOTICES: dict[str, str] = {
    "ingest:facts": "not implemented until wave 1 (F1 financial normalization)",
    "verify:facts": "not implemented until wave 1 (F1 financial normalization)",
    "ingest:documents": "not implemented until wave 1 (F2 document pipeline)",
    "index:build": "not implemented until wave 2 (F3 retrieval)",
    "search": "not implemented until wave 2 (F3 retrieval)",
    "eval:retrieval": "not implemented until wave 3 (F6 evaluation)",
    "eval:generation": "not implemented until wave 3 (F6 evaluation)",
}


def register_subcommand(name: str, handler: Handler) -> None:
    """Register a handler for a flattened subcommand key."""
    SUBCOMMAND_REGISTRY[name] = handler


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def run_db_upgrade(database_url: str | None = None) -> None:
    """Run ``alembic upgrade head`` programmatically against the configured DB."""
    from alembic import command
    from alembic.config import Config

    root = _repo_root()
    alembic_cfg = Config(str(root / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(root / "migrations"))
    if database_url:
        alembic_cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_cfg, "head")


# ---------------------------------------------------------------------------
# Built-in handlers
# ---------------------------------------------------------------------------


def _handle_db_upgrade(args: argparse.Namespace) -> int:
    run_db_upgrade(getattr(args, "database_url", None))
    print("database upgraded to head")
    return 0


def _handle_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from quarterline.api.main import create_app
    from quarterline.config import get_settings

    settings = get_settings()
    host = args.host or settings.host
    port = args.port or settings.port
    uvicorn.run(
        create_app(),
        host=host,
        port=port,
        log_level=settings.log_level.lower(),
    )
    return 0


register_subcommand("db:upgrade", _handle_db_upgrade)
register_subcommand("serve", _handle_serve)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quarterline",
        description="Local-first public-company research desk (research only, not investment advice).",
    )
    parser.add_argument("--version", action="version", version="quarterline 0.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # db upgrade
    db_parser = subparsers.add_parser("db", help="database maintenance")
    db_sub = db_parser.add_subparsers(dest="db_command", required=True)
    db_upgrade = db_sub.add_parser("upgrade", help="apply migrations (alembic upgrade head)")
    db_upgrade.add_argument(
        "--database-url", default=None, help="override DATABASE_URL for this run"
    )
    db_upgrade.set_defaults(registry_key="db:upgrade")

    # serve
    serve = subparsers.add_parser("serve", help="run the local web application")
    serve.add_argument("--host", default=None, help="bind host (default HOST, 127.0.0.1)")
    serve.add_argument("--port", type=int, default=None, help="bind port (default PORT, 8000)")
    serve.set_defaults(registry_key="serve")

    # ingest facts | documents
    ingest = subparsers.add_parser("ingest", help="ingest data into the local store")
    ingest_sub = ingest.add_subparsers(dest="ingest_command", required=True)
    ingest_facts = ingest_sub.add_parser("facts", help="ingest SEC company facts")
    ingest_facts.add_argument(
        "--tickers", nargs="*", default=None, help="tickers to ingest (default: watchlist)"
    )
    ingest_facts.add_argument(
        "--watchlist", default="data/watchlist_us.csv", help="watchlist CSV path"
    )
    ingest_facts.set_defaults(registry_key="ingest:facts")
    ingest_docs = ingest_sub.add_parser("documents", help="ingest filing documents")
    ingest_docs.add_argument(
        "--watchlist", default="data/watchlist_us.csv", help="watchlist CSV path"
    )
    ingest_docs.add_argument(
        "--forms", nargs="*", default=["10-Q", "10-K"], help="form types to download"
    )
    ingest_docs.set_defaults(registry_key="ingest:documents")
    india_doc = ingest_sub.add_parser(
        "india-document", help="manually import an official Indian filing (Phase D)"
    )
    india_doc.add_argument(
        "--issuer", required=True, help="issuer_id from data/watchlist_india.csv"
    )
    india_doc.add_argument("--file", required=True, help="path to the officially downloaded file")
    india_doc.add_argument(
        "--type",
        default="financial_results",
        choices=[
            "financial_results",
            "results_notes",
            "earnings_presentation",
            "annual_report",
            "management_transcript",
            "exchange_announcement",
        ],
        help="India document type",
    )
    india_doc.add_argument("--period-start", required=True, help="period start (YYYY-MM-DD)")
    india_doc.add_argument("--period-end", required=True, help="period end (YYYY-MM-DD)")
    india_doc.add_argument(
        "--scope",
        default="consolidated",
        choices=["consolidated", "standalone"],
        help="reporting scope declared by the filing",
    )
    india_doc.add_argument("--published", required=True, help="publication date (YYYY-MM-DD)")
    india_doc.add_argument("--source-url", default=None, help="official source URL, if known")
    india_doc.add_argument("--notes", default=None, help="free-text provenance notes")
    india_doc.add_argument(
        "--exchange", default=None, help="declaring exchange (NSE/BSE), if known"
    )
    india_doc.add_argument("--seq-id", default=None, help="exchange listing sequence id, if known")
    india_doc.add_argument(
        "--audited",
        default=None,
        choices=["Audited", "Unaudited"],
        help="audit status declared by the filing",
    )
    india_doc.add_argument(
        "--revision",
        default=None,
        choices=["Original", "Revised"],
        help="revision status declared by the listing",
    )
    india_doc.set_defaults(registry_key="ingest:india-document")

    # verify facts
    verify = subparsers.add_parser("verify", help="verification helpers")
    verify_sub = verify.add_subparsers(dest="verify_command", required=True)
    verify_facts = verify_sub.add_parser("facts", help="reconcile stored facts vs SEC source")
    verify_facts.add_argument("--ticker", required=True, help="ticker to verify")
    verify_facts.set_defaults(registry_key="verify:facts")
    verify_india = verify_sub.add_parser(
        "india", help="source-linked reconciliation table for an Indian issuer"
    )
    verify_india.add_argument(
        "--issuer", required=True, help="issuer_id from data/watchlist_india.csv"
    )
    verify_india.set_defaults(registry_key="verify:india")
    verify_india_facts = verify_sub.add_parser(
        "india-facts", help="canonical fact card for an Indian issuer"
    )
    verify_india_facts.add_argument(
        "--issuer", required=True, help="issuer_id from data/watchlist_india.csv"
    )
    verify_india_facts.add_argument(
        "--scope",
        default="consolidated",
        choices=["consolidated", "standalone"],
        help="reporting scope (default: consolidated)",
    )
    verify_india_facts.add_argument(
        "--period-end", dest="period_end", default=None, help="period end (YYYY-MM-DD)"
    )
    verify_india_facts.set_defaults(registry_key="verify:india-facts")

    # index build
    index = subparsers.add_parser("index", help="retrieval index maintenance")
    index_sub = index.add_subparsers(dest="index_command", required=True)
    index_build = index_sub.add_parser("build", help="chunk and embed the document corpus")
    index_build.add_argument(
        "--strategy", choices=["fixed", "section"], default="fixed", help="chunking strategy"
    )
    index_build.add_argument(
        "--tickers", nargs="*", default=None, help="optional ticker subset (default: whole corpus)"
    )
    index_build.set_defaults(registry_key="index:build")

    # search
    search = subparsers.add_parser("search", help="search the ingested document corpus")
    search.add_argument("--ticker", required=True, help="restrict to one company")
    search.add_argument("--query", required=True, help="natural-language query")
    search.add_argument(
        "--strategy", choices=["fixed", "section"], default="section", help="chunking strategy"
    )
    search.add_argument(
        "--retrieval",
        choices=["lexical", "dense", "hybrid", "hybrid-rerank"],
        default="hybrid",
        help="retrieval configuration",
    )
    search.add_argument(
        "--mode",
        choices=["general", "brief", "risk"],
        default="general",
        help="section-filter preset (brief: mda+earnings, risk: risk factors+mda)",
    )
    search.add_argument("--top-k", type=int, default=None, help="override result count")
    search.set_defaults(registry_key="search")

    # eval retrieval | generation
    eval_parser = subparsers.add_parser("eval", help="evaluation runs")
    eval_sub = eval_parser.add_subparsers(dest="eval_command", required=True)
    eval_retrieval = eval_sub.add_parser("retrieval", help="run retrieval evaluation")
    eval_retrieval.add_argument(
        "--dataset", default="data/eval/questions.jsonl", help="evaluation questions file"
    )
    eval_retrieval.set_defaults(registry_key="eval:retrieval")
    eval_generation = eval_sub.add_parser("generation", help="run generation evaluation")
    eval_generation.add_argument(
        "--dataset", default="data/eval/questions.jsonl", help="evaluation questions file"
    )
    eval_generation.set_defaults(registry_key="eval:generation")

    return parser


_WAVE_PACKAGES = (
    "quarterline.ingest",
    "quarterline.sources.india",
    "quarterline.retrieve",
    "quarterline.llm",
    "quarterline.eval",
    "quarterline.agent",
)


def _register_wave_handlers() -> None:
    """Import wave packages so their modules register subcommand handlers.

    A not-yet-implemented package is fine (swallowed); an ImportError raised
    from *inside* an implemented one is a real bug and must surface.
    """
    import importlib

    for name in _WAVE_PACKAGES:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name is None or not exc.name.startswith(name):
                raise


def main(argv: list[str] | None = None) -> int:
    _register_wave_handlers()
    parser = build_parser()
    args = parser.parse_args(argv)
    key: str = getattr(args, "registry_key", "")
    handler = SUBCOMMAND_REGISTRY.get(key)
    if handler is None:
        notice = WAVE_NOTICES.get(key, "not implemented")
        label = key.replace(":", " ")
        print(f"'quarterline {label}' is {notice}.", file=sys.stderr)
        return 2
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
