# Quarterline — Architecture

Status: implemented and tested as described. Where something is only partially
verified, this document says so (SPEC §31.20). Source of truth: `docs/SPEC.md`;
build history: `docs/implementation_log.md`.

## Component diagram (SPEC §5, verbatim shape)

```text
                       OFFICIAL SOURCES
                    SEC / company IR / prices
                              |
                  Download, cache, validate
                              |
              +---------------+----------------+
              |                                |
       FINANCIAL DATA                    DOCUMENT DATA
       Reported facts                    HTML / PDF text
              |                                |
       Normalize periods                  Section parsing
              |                                |
       Derived metrics                    Chunk + embed
              |                                |
       Screens / signals                Lexical + dense search
              |                                |
              |                          RRF + reranker
              |                                |
              +---------- FACT CARD -----------+
                              |
                       CONSTRAINED WRITER
                              |
                 Schema + citation validation
                              |
                    Numeric claim validation
                              |
                       API / UI / memo
```

The agent workflow uses these services; it does not reimplement them.

## Module map

| Layer | Module | Responsibility (SPEC §) |
|---|---|---|
| Entry | `quarterline.cli` | Subcommand registry; lazy-imports wave packages; missing optional packages degrade to notices (§29) |
| Config | `core/config.py` (`quarterline.config`) | Cached pydantic-settings `Settings`; placeholder-identity gate; SEC interval clamped ≥200 ms (§7) |
| Sources | `sources.sec.client` | `SecClient`: identity gate, thread-safe ≥200 ms spacing, 429/503 backoff, disk cache with ETag/Last-Modified + dated-cache fallback (§2.4, §25) |
| Sources | `sources.sec.{companyfacts,submissions,tagmap}` | companyfacts/submissions fetchers, ordered tag-fallback map loader (§10.1) |
| Sources | `sources.prices.yfinance_provider` | Lazy `yfinance` import; all errors → `PriceProviderUnavailable`, never blocks a facts-only memo (§20) |
| Ingest | `ingest.facts` | companyfacts → observations → normalized facts → derived metrics; per-company error isolation; idempotent re-run (§10) |
| Ingest | `ingest.{html_clean,sections,pdf_extract,documents}` | EDGAR HTML cleaning with exact offsets, 10-Q/10-K/8-K section detection, PyMuPDF PDF extraction (`ok/needs_ocr/failed`), watchlist document pipeline with source-artifact provenance (§13) |
| Ingest | `ingest.cache` | Disk cache backing `SecClient` (§25 raw cache) |
| Store | `store.{db,models}` | Engine/session, 19 ORM tables, decimals as canonical TEXT (§9) |
| Store | `store.repositories.{companies,facts,documents,runs}` | Typed session-scoped repos; raw SQL never escapes to LLM/agent (§2.3.5) |
| Store | `store.repositories.search_sqlite` | SQLite index: chunks + FTS5 + float32 BLOB vectors + index manifest (§9.7–§9.8, §15) |
| Store | `store.repositories.search_postgres` | PostgreSQL 16 + pgvector mirror of the same contract: tsvector+GIN lexical (`postgres-tsrank`), pgvector `chunk_vectors` cosine, PG manifest; `get_search_repo` factory (§15). **Contract tests exist and skip cleanly; live-Postgres verification pending a Postgres DBAPI in `pyproject.toml`** |
| Financial core | `core.{periods,normalization,ratios,quarter_label,quality,provenance}` | Period classification, tag selection, YTD/Q4 derivation, ratios/growth in `Decimal`, quarter label, experimental score, fact-card lineage (§10–§12) |
| Generation guardrails | `core.{citations,factcheck,advice_policy}` | Citation checks 4–7, metric-mention expansion + free-text numeric checking, research-only advice policy (§18, §2.2) |
| Retrieval | `retrieve.{chunk_fixed,chunk_section}` | `fixed-1` (~400 tok/80 overlap) and `section-1` (~200 tok children, parents, bounded windows) (§14) |
| Retrieval | `retrieve.{embeddings,lexical,vector,fusion,reranker}` | C9 provider protocol (Ollama + deterministic `fake`), FTS5 lexical, NumPy cosine, RRF, import-guarded cross-encoder (§15–§16, §25) |
| Retrieval | `retrieve.{search,context}` | `SearchService` flow + evidence policy; ≤6 passages ≤600 tokens with resolvable C8 ids (§16) |
| LLM | `llm.{base,ollama,openrouter,schemas,prompts,generation,repair}` | C10 provider protocol, Ollama adapter, hard-consent OpenRouter fallback, C11 brief/answer schemas, versioned prompts, one repair pass, validation-gated pipeline, brief cache (§17) |
| Eval | `eval.{dataset,retrieval,generation,judge,regression,report}` | Dataset loader, 2×4 retrieval matrix, generation metrics, optional pinned judge, thresholded regression, markdown/JSON reports (§21–§23) |
| Observability | `observability.{events,metrics,tracing}` | C12 events (hash-only question/query), metrics with sample sizes, run traces (§24) |
| Agent | `agent.{state,schemas,tools,graph,checkpoints,approval}` | LangGraph memo workflow: rule-based planner, typed tools, budgets, full-state checkpoints, approval-gated export (§20) |
| API/UI | `api.main` + `api/routers/*` + templates/static | Deterministic routes over the store; LLM routes lazy-import the stack; safe filing viewer; `/evidence/{id}`; dashboard; memo review + approval (§19) |

## Data flow (happy path)

1. `ingest facts` → SEC companyfacts (cache-first) → `fact_observations`
   (sha256 `observation_hash`, idempotent) → `normalized_facts` (tag priority
   after unit/scope/period filtering; YTD→quarter and Q4=annual−9m derivation
   with lineage) → `derived_metrics` (Decimal, `formula_version`).
2. `ingest documents` → submissions discovery → cached download →
   `source_artifacts` provenance → HTML clean/PDF extract → `documents` +
   `sections` with exact offsets.
3. `index build --strategy fixed|section` → chunk drafts → `chunks`
   (idempotent unique key) → embeddings (cache by text hash + model identity)
   → FTS/vector rows → `index_manifest` (deterministic 16-hex index version).
4. Query → metadata filter → lexical 20 + dense 20 → RRF 15 → optional
   rerank → top_k → bounded context (≤6 windows, ≤600 tokens) → prompt with
   C8 evidence ids.
5. Brief/answer → insufficient-evidence policy may abstain before any model
   call → one provider call → one repair pass → validation gate (checks 1–10)
   → only validated JSON with resolvable citations reaches the user.
6. Memo (agent) → LangGraph plan → typed read tools → memo gate → approval
   (run-id + content-hash bound) → export to `STORAGE_DIR/exports`.

## Degradation matrix (SPEC §25, implemented and tested)

| Failure | Expected behavior | Where enforced |
|---|---|---|
| Ollama stopped | Tables, charts, screener still work | Deterministic routes never import the LLM stack; verified by blocked-module tests (`tests/api/test_api_json.py`) |
| Generation unavailable | Show facts and retrieved evidence, never fake prose; explicit degraded banner | `llm/generation.py`, `api/routers/{brief,ask}.py` |
| Embedding provider unavailable | Lexical search remains available | `retrieve/search.py` hybrid→lexical degradation |
| Embedding model mismatch | Dense/hybrid refuse the index (`EmbeddingModelMismatchError`); lexical survives; pipeline falls back to lexical | `retrieve/search.py`, both index backends |
| Reranker unavailable | Hybrid results with `degraded[reranker]` metadata; never fake scores | `retrieve/reranker.py` (import-guarded `[rerank]` extra) |
| Price provider unavailable | Omit price context; memo continues facts-only | `sources/prices/yfinance_provider.py`, `agent/tools.py` |
| SEC temporarily unavailable | Clearly dated disk cache | `sources/sec/client.py` + `ingest/cache.py` |
| JSON invalid after repair | Controlled failure response (no raw output) | `llm/repair.py`, `llm/generation.py` |
| No supporting evidence | Insufficient-evidence response, zero provider calls | `retrieve/search.py` evidence policy, `llm/generation.py` |
| PostgreSQL unreachable | SQLite profile fully functional; pgvector contract tests skip (never fail) | `tests/integration/test_postgres_profile.py`, `make test-postgres` |

## Pinned contracts (from `docs/PLAN.md` §3)

- **C1 Config**: `quarterline.config.get_settings()` → cached pydantic-settings
  `Settings` with every SPEC §7 field; `Settings.edgar_identity_is_valid()`
  refuses empty/placeholder identities.
- **C2 DB**: `quarterline.store.db.get_engine()`, `session_scope()`;
  foreign_keys=ON; decimals persisted losslessly as canonical TEXT decimal
  strings converted via `Decimal` in repos; `store/models.py` defines all
  SPEC §9 tables.
- **C3 SEC**: `SecClient(settings)` with `get_json`, `get_text`,
  `download(url) -> CachedResponse` (content bytes, etag, last_modified,
  cache_hit, source_url); valid identity enforced for live methods; ≥200 ms
  spacing; 429/503 backoff; disk cache. Helpers: `fetch_companyfacts`,
  `fetch_submissions`, `list_recent_filings`, `build_filing_url`.
- **C4 Tag map**: `sources.sec.tagmap.load_tagmap() -> dict[concept, list[tag]]`
  (ordered fallbacks, SPEC §10.1).
- **C5 Repos**: session-scoped, typed methods (`CompaniesRepo`, `FactsRepo`,
  `DocumentsRepo`, `RunsRepo`, `SearchIndexRepo`/`PostgresSearchIndexRepo`
  via `get_search_repo`). No raw SQL escapes to the LLM/agent layer.
- **C6 Fact card**: `core.provenance.build_fact_card(session, ticker,
  period_end|None) -> FactCard`; `MetricValue = {metric_id, value: Decimal|None,
  unit, status: ok|missing|invalid|unsuitable, provenance{observation_ids,
  derived_from, formula_version}, notes}`.
- **C7 Metric allowlist**: `core.models.METRIC_IDS` (single source of truth):
  revenue, revenue_yoy, gross_profit, gross_margin, operating_income,
  operating_margin, operating_margin_change_pp, net_income, net_margin,
  diluted_eps, shares_diluted, shares_yoy, cfo, capex_outflow, fcf, fcf_margin,
  current_ratio, cash, total_assets, total_liabilities, long_term_debt,
  long_term_net_debt_proxy, cfo_to_net_income, quarter_label,
  fundamental_score, data_coverage.
- **C8 Evidence ID**: `ev-` + first 12 hex chars of
  `sha1("{document_id}|{strategy}|{start}|{end}|{text_hash}")`; resolvable
  from the `chunks` table; rendered at `GET /evidence/{evidence_id}`.
- **C9 Embeddings**: `EmbeddingProvider` protocol: `model_id: str`, `dim: int`,
  `embed(texts: list[str]) -> list[list[float]]`.
- **C10 Generation**: `GenerationProvider.generate(messages, json_schema) ->
  GenerationResult{text, provider, model, input_tokens, output_tokens,
  token_count_source, latency_ms, finish_reason}`.
- **C11 Brief schema**: pydantic models in `llm/schemas.py`; statuses
  `ok|partial|insufficient_evidence|provider_unavailable|refused`;
  `label_echo` validated for equality against the code label.
- **C12 Telemetry**: `observability.events.new_run_id()`, `emit_run_event(run_id,
  event)`; JSONL mirror + `runs`/`run_events` persistence.

## Storage backends

- **Default**: SQLite (`sqlite:///storage/quarterline.db`) — FTS5 lexical,
  float32 BLOB vectors, NumPy cosine. Fully tested offline.
- **Optional profile**: PostgreSQL 16 + pgvector (`docker-compose.yml`,
  `make test-postgres`) — tsvector+GIN lexical (labeled `postgres-tsrank`, NOT
  BM25), native pgvector cosine. Code + contract tests are implemented;
  executing the contract tests additionally requires a Postgres DBAPI
  (`psycopg`/`psycopg2`/`pg8000`) which is **not yet in `pyproject.toml`** —
  until then the tests skip cleanly on every machine, including CI.
