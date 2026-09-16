# Quarterline

[![CI](https://github.com/Lekh007/quarterline/actions/workflows/ci.yml/badge.svg)](https://github.com/Lekh007/quarterline/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Local-first public-company research desk: official filings in, explainable
research out. Built as an **AI-engineering portfolio project** - the point is
the engineering discipline: provenance for every number, a validation gate in
front of the LLM, measured retrieval experiments, and honest documentation of
what is implemented, measured, optional, and unverified.

**Research and education only. Not investment advice.** Every page carries the
disclaimer; advice-shaped questions get a research-only refusal; the quarter
label is a fixed rule with its caption shown, not a forecast.

## What it is

- **Facts pipeline**: SEC companyfacts → raw observations (sha256-deduped,
  revisions preserved) → normalized quarterly facts (ordered tag fallback,
  YTD→quarter and Q4 derivation with lineage) → derived metrics in `Decimal`
  (margins, growth in pp, capex sign semantics, quarter label, experimental
  fundamental score). Every value is inspectable back to accession/form/file.
- **Document pipeline**: EDGAR HTML + IR PDFs → cleaned text with exact
  offsets → sections (MD&A / risk factors / earnings release, honest
  low-confidence handling) → provenance artifacts.
- **Retrieval**: fixed-window and section parent–child chunking, lexical
  (SQLite FTS5; PostgreSQL tsvector in the optional profile), dense, RRF
  hybrid, optional cross-encoder (degrades gracefully), C8 evidence ids
  resolvable via `GET /evidence/{id}`.
- **Grounded generation**: one provider call → one repair pass → a 10-check
  validation gate (schema, citations 4–7, metric-allowlist, numeric
  consistency vs the fact card, research-only advice compliance). Numbers are
  Python-rendered from validated metric mentions - the model never types
  figures. Insufficient evidence abstains without a model call.
- **Eval/LLMOps**: 30 reviewed anchored questions, 2×4 retrieval matrix with
  regression thresholds and explicit gated baseline regeneration, generation
  harness, optional pinned LLM judge, metrics dashboard with sample sizes.
- **Agent workflow**: LangGraph memo writer - typed read-only tools, tool and
  transition budgets, full-state checkpoints, approval-gated export bound to
  the exact memo content hash.

## Honest status (what works where)

| Capability | Works offline (no models) | Needs Ollama (local models) | Unverified / pending |
|---|---|---|---|
| Install, migrations, `/health`, deterministic UI (watchlist, company charts, screener, filing/evidence viewer, provenance) | yes | - | - |
| Facts ingestion + normalization + ratios + labels | fixture-backed (verified) | - | - (live SEC ingestion of the 15-company watchlist verified 2026-09-14; still refuses a placeholder `EDGAR_IDENTITY` by design) |
| Document ingestion (HTML + PDF), chunking, index build, lexical search | fixture-backed (verified) | - | live SEC download path (same identity gate) |
| Dense / hybrid retrieval, brief + ask generation, memo writing | degraded paths verified (facts + evidence shown, no fake prose) | yes - this is the real path; **live-model verification is in progress at release time** | real-model retrieval/generation quality numbers (see `docs/retrieval_experiments.md` → PENDING) |
| Eval + regression + dashboard | yes (fixture corpus, fake embeddings - labeled as such) | scheduled real-model eval is opt-in | real-model retrieval quality numbers (rerun at n=30 in progress) |
| Agent workflow (budgets, checkpoints, approval-gated export) | yes, offline-verified with fakes | live model memo quality untested | live yfinance/Ollama paths |
| PostgreSQL 16 + pgvector profile | contract tests pass against a live server (`make test-postgres`; `psycopg` ships in the `postgres` extra) | - | - |

Measured retrieval numbers are **fixture-corpus, fake-embedding** measurements
(deterministic hash vectors - plumbing/regression signal, not quality):
see `docs/retrieval_experiments.md` for the matrix and its history.

## Quickstart

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), and (for generation)
[Ollama](https://ollama.com) with `qwen3:4b` + `nomic-embed-text` pulled.

```bash
cp .env.example .env
# Edit .env - LIVE ingestion requires a REAL EDGAR_IDENTITY (name + contact
# email). The app refuses live SEC calls while it is a placeholder. Offline
# fixtures and tests work without it.

uv sync --extra dev                    # or: make install
uv run quarterline db upgrade          # or: make db-init

# Ingest (requires the real EDGAR_IDENTITY above):
uv run quarterline ingest facts --tickers AAPL MSFT
uv run quarterline verify facts --ticker AAPL
uv run quarterline ingest documents --watchlist data/watchlist_us.csv

# Build the search index (offline-capable with EMBED_PROVIDER=fake, but real
# retrieval quality needs: ollama pull nomic-embed-text):
uv run quarterline index build --strategy fixed
uv run quarterline index build --strategy section

uv run quarterline serve               # or: make serve  (http://127.0.0.1:8000)
uv run pytest -q                       # or: make test
```

If you only want to see the app without ingesting anything, `db upgrade` +
`serve` already give you the watchlist shell with explicit "no data ingested"
states (missing stays missing - never zero).

Search CLI (SPEC §29):

```bash
uv run quarterline search \
  --ticker AAPL \
  --query "management discussion of operating expenses" \
  --strategy section \
  --retrieval hybrid-rerank
```

Evaluation:

```bash
make eval            # retrieval matrix + regression, generation harness
make eval-retrieval  # fixture corpus, deterministic embeddings, PASS/FAIL vs baseline
```

Optional PostgreSQL 16 + pgvector profile:

```bash
make test-postgres   # boots docker compose db, runs pgvector contract tests,
                     # tears down. Skips (never fails) without Docker/driver.
```

The driver ships in the `postgres` extra, so these tests execute for real
wherever Docker is available and skip (never fail) where it is not. They run
against their own `quarterline_test` database: the contract tests build schema
outside Alembic, so sharing a database with the app would break the app's
migrations.

Quality gates:

```bash
uv run ruff check .
uv run ruff format --check .
docker compose config   # validates the optional db profile
```

## Run it in containers (app + PostgreSQL + Prometheus + Grafana)

The repo ships a two-stage `Dockerfile` (no build toolchain and no `uv` in the
runtime layer, non-root user, `/health` healthcheck) and a compose stack that
brings up the whole desk on the pgvector profile:

```bash
docker compose up -d          # db + app + prometheus + grafana
docker compose ps             # all four healthy
```

| Service | URL | What it is |
|---|---|---|
| `quarterline` | http://127.0.0.1:8000 | the app (migrations run on every start) |
| `quarterline-db` | 127.0.0.1:5432 | PostgreSQL 16 + pgvector |
| `prometheus` | http://127.0.0.1:9090 | scrapes `/metrics` every 15 s, 5 alert rules |
| `grafana` | http://127.0.0.1:3000 | provisioned "Quarterline - LLMOps" dashboard |

Everything binds to loopback. SPEC §28 still holds: there is no authn/authz
here, so this stack is a local development deployment, not a hosted one.

### `/metrics`

`GET /metrics` renders the same SPEC §24 aggregates the `/dashboard` page shows
- in Prometheus exposition format, computed from the persisted `run_events`
rather than from process counters, so the numbers survive restarts. The
honesty rule carries over: **every rate is exported next to its sample size,
and a rate with no samples is absent rather than zero.** The alert rules in
`deploy/prometheus/rules.yml` all gate on an `n` as well as a rate, so one
unlucky run cannot page anyone.

Ollama stays on the host; the app reaches it via `host.docker.internal`.

## Make targets / CLI (no `make`? run the `uv run` commands above)

`make install` · `make db-init` · `make ingest-facts` · `make ingest-docs` ·
`make embed` · `make serve` · `make test` · `make test-postgres` ·
`make eval` / `eval-retrieval` / `eval-generation` · `make fmt` · `make lint`.

Nothing here promises ingestion time or token throughput - measure your own
hardware (SPEC §29).

## Acceptance criteria (SPEC §31), honestly assessed

| # | Criterion | Status |
|---|---|---|
| 1 | Fresh env can install + initialize DB | met |
| 2 | SEC ingestion retrieves real observations for the 15-company watchlist | met - live run 2026-09-14; the local store holds 25,972 fact observations across 26 issuers (15-company US watchlist + the 10-issuer India corpus) |
| 3 | AAPL/MSFT manually reconciled against official filings | **partial** - AAPL fixture (trimmed real companyfacts) reconciled in tests; MSFT + human sign-off not done |
| 4 | Annual/YTD/quarterly facts do not collide | met (tested) |
| 5 | Every normalized/derived value has inspectable lineage | met (display-level caveat for growth metrics documented in `docs/financial_methodology.md`) |
| 6 | Watchlist/charts/screener work without an LLM | met (smoke-tested with Ollama down) |
| 7 | HTML and text-based PDF ingestion with source metadata | met (fixtures) |
| 8 | Both chunking strategies built and evaluated | met (fixture corpus) |
| 9 | Hybrid retrieval and reranking work with measured comparisons | **partial** - hybrid measured (fake embeddings); reranker verified in degraded mode only, no real cross-encoder comparison |
| 10 | SQLite and optional PostgreSQL profile pass contract tests | met - SQLite passes; the 6 pgvector contract tests executed green against live PostgreSQL 16 + pgvector on 2026-09-15 |
| 11 | Briefs return validated JSON and resolvable citations | **partial** - verified offline (scripted provider + degraded paths); live-model run pending |
| 12 | Numbers rendered from validated references or rejected | met (tested) |
| 13 | Unanswerable → explicit insufficient evidence | met (tested) |
| 14 | Advice → research-only refusal + alternative | met (tested) |
| 15 | Agent tool budgets and approval controls enforced in code | met (tested) |
| 16 | ≥30 reviewed evaluation questions committed | met - 30 committed (20 answerable with machine-verified gold spans, 6 insufficient-evidence incl. 2 wrong-company traps, 4 advice refusals); every anchor verified by `quarterline.eval.dataset.verify_spans` against the ingested corpus; baseline regenerated through the `QUARTERLINE_EVAL_WRITE_BASELINE` gate 2026-09-17 |
| 17 | CI executes reproducible regression checks, no live SEC/paid provider | met - GitHub Actions runs on every push to `main`; latest run green |
| 18 | Dashboard reports real run metrics + sample sizes | met (tested) |
| 19 | Tests pass | met - 988 passed, 0 skipped (2026-09-15; the pgvector contract tests execute for real once the compose database is up) |
| 20 | Docs separate implemented / measured / optional / unverified | met (this docs set) |

Passing these criteria does not justify claiming "hallucination-free" or
"investment-grade" (SPEC §31).

## Documentation index

- `docs/SPEC.md` - master build specification (source of truth)
- `docs/PLAN.md` - build orchestration, wave status, pinned contracts
- `docs/architecture.md` - components, module map, degradation matrix, contracts C1–C12
- `docs/data_contract.md` - table-by-table schema, decimal-as-TEXT, evidence-id + index-version formulas
- `docs/financial_methodology.md` - every financial rule with its SPEC section and proving tests
- `docs/retrieval_experiments.md` - measured matrix (labeled), chunker-fix history, real-model status
- `docs/evaluation.md` - dataset, metrics (Hit@5 vs Recall@5), judge policy, regression thresholds, CI honesty
- `docs/threat_model.md` - STRIDE-lite over the local surface
- `docs/failure_cases.md` - exercised agent failures with real traces
- `docs/commercial_readiness.md` - SPEC §28 boundary: what a launch would need, what this release is NOT
- `docs/implementation_log.md` - append-only per-wave build log (work, commands, results, limitations)
