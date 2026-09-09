# Quarterline

Local-first public-company research desk: official filings in, explainable
research out.

**Research and education only. Not investment advice.**

## Status

**Wave 0 — foundation.** Installable package, configuration, database schema +
migrations, `/health`, SEC client (identity gate, throttling, retries, disk
cache), and the verified US watchlist. Financial normalization, retrieval,
generation, and the agent workflow arrive in later waves; their CLI subcommands
print wave notices.

## Quickstart

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
# Edit .env and set a real EDGAR_IDENTITY (name + contact email).
# Live SEC ingestion refuses to run while the identity is a placeholder.

uv sync --extra dev          # or: make install
uv run quarterline db upgrade  # or: make db-init
uv run quarterline serve       # or: make serve  (http://127.0.0.1:8000)
uv run pytest -q               # or: make test
```

Optional quality gates:

```bash
uv run ruff check .
uv run ruff format --check .
```

## CLI overview

```bash
uv run quarterline db upgrade        # apply schema migrations (works now)
uv run quarterline serve             # local web app (works now)
uv run quarterline ingest facts --tickers AAPL MSFT   # wave 1
uv run quarterline ingest documents --watchlist data/watchlist_us.csv  # wave 1
uv run quarterline index build --strategy section     # wave 2
uv run quarterline search --ticker AAPL --query "..." # wave 2
```

`make` targets mirror these commands (see `Makefile`).
