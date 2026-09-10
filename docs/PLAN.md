# Quarterline — Build Orchestration Plan

Source of truth: **`docs/SPEC.md`** (the verbatim master build specification). This file defines how
the build is parallelized across worker agents, which files each agent owns, the interfaces that pin
modules together, and current status. The orchestrator (ZCode) dispatches waves, verifies each wave
with real test runs, and commits.

## 1. Ground rules (SPEC §0 — binding on every worker agent)

1. Read `docs/SPEC.md` and this file **fully** before writing any code.
2. Implement only your wave's scope, only inside your owned files. Never "fix" files you don't own —
   other agents may be editing them concurrently; report integration gaps in your final report instead.
3. Do NOT run `git commit` (orchestrator commits). Do NOT edit `pyproject.toml` or run
   `uv lock`/`uv sync` — all dependencies are preinstalled; use `uv run ...` for everything.
4. Never claim a feature works unless you exercised it. Include real command output in your report.
5. Never invent company financial data, filings, source URLs, benchmark results, or eval scores.
   Missing data stays missing (never zero). Never silently weaken a validation to make a test pass.
6. Every wave must leave the full test suite green: `uv run pytest -q` and `uv run ruff check .`.
7. Append your row to `docs/implementation_log.md` (work, commands, results, limitations, unverified).
8. Tests must run offline: no live SEC calls, no Ollama dependency in unit/CI tests. Mark any
   network-dependent test with `@pytest.mark.live` (deselected by default via pyproject config).
9. Windows + Git Bash environment. Use `uv run` wrappers and forward-slash paths.

## 2. Waves, agents, file ownership

| Wave | Agent | Scope (SPEC §§) | Owned files | Depends on |
|---|---|---|---|---|
| 0 | F0 foundation | §4–§9, §29 | `pyproject.toml`, `uv.lock`, `Makefile`, `.env.example`, `README.md`, `alembic.ini`, `migrations/`, `.github/workflows/ci.yml`, `src/quarterline/{__init__,__main__,cli,config}.py`, `src/quarterline/core/models.py`, `src/quarterline/store/{db,models}.py`, `src/quarterline/sources/sec/{client,companyfacts,submissions,tagmap}.py`, `src/quarterline/ingest/cache.py`, `src/quarterline/observability/events.py` (stub), `src/quarterline/api/{main,dependencies}.py`, `src/quarterline/api/routers/health.py`, `src/quarterline/api/templates/{base,index}.html`, `src/quarterline/api/static/app.css`, `data/watchlist_us.csv`, `data/tagmap_us.yml`, tests for the above | — |
| 1 | F1 financial | §10–§12, §26 (financial+scores) | `src/quarterline/core/{periods,normalization,ratios,quarter_label,quality,provenance}.py`, `src/quarterline/ingest/facts.py`, `src/quarterline/store/repositories/{base,companies,facts}.py`, `tests/fixtures/sec/`, `tests/unit/` financial+scores tests, `tests/integration/test_facts_*` | W0 |
| 1 | F2 documents | §13 | `src/quarterline/ingest/{html_clean,pdf_extract,sections,documents}.py`, `src/quarterline/store/repositories/documents.py`, `src/quarterline/sources/prices/` (stub only), `tests/fixtures/documents/`, document tests | W0 |
| 2 | F3 retrieval | §14–§16 | `src/quarterline/retrieve/**`, `src/quarterline/store/repositories/search_sqlite.py`, `data/eval/corpus_manifest.json` (schema), `tests/` retrieval tests, fixture embeddings | W1 |
| 2 | F4 UI/API (deterministic) | §19 (non-LLM routes) | `src/quarterline/api/routers/{companies,filings,screener}.py`, `src/quarterline/api/templates/{company,filing,screener,partials/*}`, `src/quarterline/api/static/{app.css,app.js,vendor/}` | W1 |
| 3 | F5 generation | §17–§18, §2.2 | `src/quarterline/llm/**`, `src/quarterline/core/{citations,factcheck,advice_policy}.py`, `prompts/**`, `src/quarterline/api/routers/{brief,ask}.py`, `src/quarterline/api/templates/memo_review.html` + brief/ask partials, generation tests | W2 |
| 3 | F6 eval+observability | §21–§24 | `src/quarterline/eval/**`, `src/quarterline/observability/{metrics,tracing}.py`, `src/quarterline/api/routers/{runs,dashboard}.py`, `src/quarterline/api/templates/dashboard.html`, `data/eval/questions.jsonl`, `data/eval/baselines/`, `.github/workflows/scheduled_eval.yml`, eval tests | W2 |
| 4 | F7 agent | §20 | `src/quarterline/agent/**`, `src/quarterline/sources/prices/yfinance_provider.py`, `src/quarterline/api/routers/memo.py`, `src/quarterline/store/repositories/runs.py`, `docs/failure_cases.md`, agent/security tests | W3 |
| 5 | F8 integration | §23, §27–§28, §31 | `src/quarterline/store/repositories/search_postgres.py`, `docker-compose.yml`, `docs/{architecture,data_contract,financial_methodology,retrieval_experiments,evaluation,threat_model,commercial_readiness}.md`, pg contract tests | W4 |

`src/quarterline/ingest/pipeline.py` and `src/quarterline/api/templates/index.html` are finalized by
the **orchestrator** at wave boundaries (aggregator wiring). `docs/implementation_log.md` is
append-only shared; append only your own row.

## 3. Pinned interfaces (implemented in W0; later waves code *against* them)

- **C1 Config**: `quarterline.config.get_settings()` → cached pydantic-settings `Settings` with every
  field from SPEC §7. `Settings.edgar_identity_is_valid()` refuses empty/placeholder identities.
- **C2 DB**: `quarterline.store.db.get_engine()`, `session_scope()`; foreign_keys=ON; decimal values
  persisted losslessly as canonical TEXT decimal strings (converted via `Decimal` in repos).
  `store/models.py` defines **all** SPEC §9 tables.
- **C3 SEC**: `SecClient(settings)` with `get_json(url)`, `get_text(url)`, `download(url) -> CachedResponse`
  (fields: content bytes, etag, last_modified, cache_hit, source_url); enforces valid identity for live
  methods, ≥200 ms request spacing (thread-safe), 429/503 backoff, max retries, disk cache via
  `ingest.cache`. Helpers: `fetch_companyfacts(client, cik) -> dict`, `fetch_submissions(client, cik) -> dict`,
  `list_recent_filings(client, cik, forms, limit)`, `build_filing_url(accession, primary_document)`.
- **C4 Tag map**: `sources.sec.tagmap.load_tagmap() -> dict[concept, list[tag]]` (ordered fallbacks, SPEC §10.1).
- **C5 Repos**: session-scoped, typed methods: `CompaniesRepo`, `FactsRepo`, `DocumentsRepo`.
  No raw SQL escapes to the LLM/agent layer (SPEC §2.3.5).
- **C6 Fact card**: `core.provenance.build_fact_card(session, ticker, period_end|None) -> FactCard`
  (pydantic, defined in `core/models.py`). `MetricValue`: `{metric_id, value: Decimal|None, unit,
  status: ok|missing|invalid|unsuitable, provenance{observation_ids, derived_from, formula_version}, notes}`.
- **C7 Metric allowlist** (`core.models.METRIC_IDS`, single source of truth): `revenue, revenue_yoy,
  gross_profit, gross_margin, operating_income, operating_margin, operating_margin_change_pp, net_income,
  net_margin, diluted_eps, shares_diluted, shares_yoy, cfo, capex_outflow, fcf, fcf_margin, current_ratio,
  cash, total_assets, total_liabilities, long_term_debt, long_term_net_debt_proxy, cfo_to_net_income,
  quarter_label, fundamental_score, data_coverage`.
- **C8 Evidence ID**: `ev-` + first 12 hex chars of `sha1("{document_id}|{strategy}|{start}|{end}|{text_hash}")`;
  resolvable from the `chunks` table; rendered at `GET /evidence/{evidence_id}`.
- **C9 Embeddings**: `EmbeddingProvider` protocol: `model_id: str`, `dim: int`, `embed(texts: list[str]) -> list[list[float]]`.
- **C10 Generation**: `GenerationProvider.generate(messages: list[dict], json_schema: dict|None) -> GenerationResult`
  `{text, provider, model, input_tokens, output_tokens, token_count_source, latency_ms, finish_reason}` (SPEC §17).
- **C11 Brief schema**: pydantic models in `llm/schemas.py` per SPEC §17; statuses `ok|partial|
  insufficient_evidence|provider_unavailable|refused`; `label_echo` validated against the code label.
- **C12 Telemetry**: `observability.events.new_run_id() -> str`, `emit_run_event(run_id, event: dict)`
  (W0 provides JSON-lines stub; F6 persists to `runs`/`run_events` keeping the same signatures).

**Gap rule**: if a contract you depend on is incomplete (parallel wave), implement against the contract
with fixture-backed tests of your own and report the gap. Never edit another wave's file.

## 4. Known blockers / environment notes

- **EDGAR_IDENTITY**: user contact identity not yet provided. The app refuses live SEC ingestion with a
  placeholder identity (SPEC §7). Development-time one-shot fetches of official public metadata
  (CIK verification, fixture documents) use the declared UA `Quarterline build (fixture verification)`.
  Live watchlist ingestion remains **unverified** until the user supplies a real identity.
- **GitHub remote**: not yet created; `ci.yml` ships but CI runs only after the user pushes.
- **Ollama models**: `qwen3:4b` / `nomic-embed-text` not yet pulled; generation evals (W5) depend on them.
- **Reranker**: `sentence-transformers`/`torch` live behind a `[rerank]` extra, not installed by default;
  reranker code must import-guard and degrade to hybrid-with-metadata per SPEC §25.

## 5. Status (updated by orchestrator after each wave)

| Wave | Status | Gate evidence |
|---|---|---|
| 0 foundation | done | 45 tests, ruff clean, `/health` exercised, 15 CIKs verified vs SEC ticker file |
| 1 financial / documents | done (orchestrator wiring applied) | 192 tests, ruff clean, migration `52e66ea29e8e` (document metadata cols), `verify facts` CLI works on fixture DB |
| 2 retrieval / UI | done | 309 tests, ruff clean, both chunk strategies + hybrid search demoed on real 8-K fixture, 21 routes smoke-tested with Ollama unreachable |
| 3 generation / eval+obs | done | 496 tests, ruff clean, fixture eval baseline committed, 2×4 matrix measured on fixture corpus |
| 4 agent workflow | done | 561 tests, ruff clean, budgets+approval exercised, 7 real failure cases documented |
| 5 integration + acceptance | done | PG profile wired via factory + psycopg extra; docs set complete; live-model verification recorded in the implementation log |

Orchestrator wiring at wave-1 boundary: `ingest/__init__.py` registers handler modules; `cli.main()`
lazy-imports wave packages (missing future packages tolerated, nested ImportErrors surface);
`documents.event_date`/`extraction_notes` + `sections.confidence`/`notes` columns added and now
persisted by the documents pipeline; W0 CLI-notice test updated (`ingest facts` is implemented).

Wave-5 integration results: (1) authoritative fix applied in `core/ratios.py` + `core/provenance.py` —
growth/margin-change metrics now carry real input concepts in `derived_from` (router enrichment is a
no-op); (2) `/evidence/{id}` left as-is — the indexed lookup is itself a documented content-derived
scan; the real improvement is a persisted `evidence_id` column (future schema migration); (3) `/dashboard`
live since F6. Orchestrator also applied: brief-prompt v2 (measured metric_id/template swaps on
qwen3:4b + qwen2.5:7b), Ollama schema-constrained decoding, enum-constrained metric_id/label_echo/
evidence_ids, failures-excluded-from-cache fix, generation timeout setting.
