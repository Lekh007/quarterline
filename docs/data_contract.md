# Quarterline — Data Contract

Reference for the persisted schema (SPEC §9) plus the repository-owned
auxiliary tables. Every financial decimal is stored losslessly as canonical
TEXT; Python converts via `Decimal` at the repository boundary (contract C2).
Proving tests: `tests/unit/test_store_models.py`, `tests/integration/test_migrations.py`,
`tests/integration/test_facts_*`, `tests/unit/test_documents_repo.py`,
`tests/retrieval_test_helpers.py`-driven retrieval suites, and
`tests/integration/test_postgres_profile.py` (skips without PostgreSQL).

## Conventions

- **Decimal-as-TEXT**: values are written with `decimal_to_text` and read with
  `text_to_decimal` (exact `Decimal` roundtrip, never float). JSON APIs
  serialize via pydantic `model_dump(mode="json")` so clients receive canonical
  strings, never floats (`tests/api/test_api_json.py` asserts no-float
  recursively).
- **Missing ≠ zero**: absent data stays `NULL`; display layers render
  "n/a"/"missing" (`tests/integration/test_ui_watchlist.py`).
- **Revisions preserved**: re-ingesting inserts-or-skips on content hashes;
  revised observations coexist with originals rather than overwriting them
  (SPEC §2.1.10).

## §9.1 `companies`

| Field | Notes |
|---|---|
| id | PK |
| ticker | unique, indexed (US v1 identity) |
| cik | unique, indexed — one CIK = one issuer regardless of ticker changes |
| name, sector, industry, country, reporting_currency | descriptive; GICS sector (AMZN = Consumer Discretionary) |
| fiscal_year_end | "MM-DD" string |
| created_at, updated_at | server-default now() |

## §9.2 `source_artifacts`

| Field | Notes |
|---|---|
| id | PK |
| source | e.g. `sec-archives`, `ir-pdf` |
| source_url | exact downloaded URL |
| fetched_at, local_path | cache location under `storage/raw` |
| content_hash | sha256 — provenance + idempotency |
| content_type, http_etag, http_last_modified | revalidation metadata |
| parser_version | e.g. `html_clean-1`, `pymupdf-1` |

## §9.3 `fact_observations` (raw reported values)

| Field | Notes |
|---|---|
| id | PK |
| company_id | FK companies |
| source_artifact_id | FK source_artifacts |
| accession, form, filed_at | filing identity (kept verbatim) |
| taxonomy, original_tag, canonical_concept | us-gaap tag + mapped concept (§10.1 order) |
| value_decimal | **TEXT decimal**, lossless |
| unit, currency | unit filtering precedes tag priority |
| period_start, period_end | `NULL` start = instant fact |
| period_kind | instant \| quarter \| year_to_date \| annual \| other_duration (duration-band inference, no 90-day assumption) |
| source_fy, source_fp | original source fiscal labels kept separately — never trusted as the economic period of a comparative |
| reporting_scope | scope filter for derivation |
| context_metadata_json | raw fy/fp/frame context |
| observation_hash | **UNIQUE** sha256 over company+tag+unit+periods+value+frame → re-ingestion idempotent, revised values preserved |

## §9.4 `normalized_facts`

| Field | Notes |
|---|---|
| id | PK |
| company_id | FK |
| concept | canonical concept name |
| period_start, period_end, fiscal_year, fiscal_quarter, period_kind | period identity (§10.2) |
| reporting_scope | part of the unique key |
| value_decimal | **TEXT decimal** |
| unit | part of the unique key |
| selection_policy, normalization_version | which rule produced this row |
| is_derived, derivation_method | e.g. `ytd_subtraction`, `q4_annual_minus_9m` |
| available_at | filing timestamp used for as-of selection |
| data_quality_status | ok / review flags |

**Unique key**: full period identity + scope + unit (+ concept, company).
SQLite treats a `NULL period_start` as distinct in the unique key, so repos
upsert by the full key with NULL-aware handling (instant facts) — see
`store/repositories/facts.py`. Annual and Q4 facts never share one conflicting
key (SPEC §9.4).

## §9.5 `fact_lineage`

| Field | Notes |
|---|---|
| normalized_fact_id | FK |
| source_observation_id | FK |
| role | `direct_source` \| `annual_total` \| `prior_ytd` (constants exported from `store/repositories/facts.py`) |

Every normalized fact carries lineage rows, so `explain_metric` and the
provenance UI can trace direct vs derived values to accession/form/filed_at
(SPEC §2.1.8; `tests/integration/test_facts_aapl_fixture.py`,
`tests/api/test_api_json.py` provenance contract).

## §9.6 `derived_metrics`

| Field | Notes |
|---|---|
| id | PK |
| company_id | FK |
| period_end | quarter the metric belongs to |
| metric | must be in `core.models.METRIC_IDS` allowlist (C7) |
| value_decimal | **TEXT decimal**; NULL value + status records why |
| unit | usd \| usd/share \| ratio \| pct \| pp \| shares \| score |
| formula_version | e.g. `ratios-v1`, `capex-semantics-v1`, `quarter-label-v1` |
| available_at | as-of timestamp |
| input_fact_ids_json | inspectable inputs |
| status | ok \| missing \| unsuitable (e.g. cfo_to_net_income with net income ≤ 0) |

## §9.7 `documents` / `sections` / `chunks`

`documents`: company_id, accession, form, document_kind (10-Q | 10-K |
8-K-exhibit | pdf), period_end (the *discussed* period — never inferred from
the filing date for 8-Ks), filed_at, **event_date + extraction_notes**
(implementation addition: 8-K three-date rule, SPEC §13.1), source_url,
source_artifact_id, extracted_path, extraction_version,
extraction_status (ok | needs_ocr | failed | pending).

`sections`: document_id, section_type (mda | risk_factors | earnings_release |
other …), heading, text, start_offset/end_offset (exact spans into cleaned
text, roundtrip-invariant), page_start/page_end, **confidence + notes**
(implementation addition: uncertain sections stay `other` with recorded
confidence — never a whole-filing mislabel, SPEC §13.2).

`chunks`: document_id, section_id, parent_id, strategy (fixed | section),
strategy_version (`fixed-1`, `section-1`, suffixed `-parent`/`-window` for
row families), text, text_hash (sha256), token_count (chars/4 estimator,
labeled approximate), start_offset/end_offset, page_start/page_end.

**Unique key** `uq_chunks_identity`: (document_id, strategy, strategy_version,
text_hash) → rebuilding the same index yields identical rows and ids
(`tests/integration/test_retrieval.py`).

## §9.8 `embeddings`

chunk_id, text_hash, provider, model, model_revision, dimension, normalized,
vector (**float32 little-endian BLOB** on SQLite), created_at.

**Unique key** `uq_embeddings_identity`: (chunk_id, provider, model,
model_revision). Cross-model comparison is impossible: vectors are selected
strictly by identity, the manifest gate raises `EmbeddingModelMismatchError`
(SPEC §15.7; `tests/unit/test_retrieval_*`, and the pgvector contract test).

## §9.9 Operational records

`prices`, `runs`, `run_events`, `brief_cache` (content-versioned key: company,
period, fact-card hash, evidence/context hash, provider, model, prompt
version+hash, generation settings, validation/factcheck/advice versions),
`agent_runs` (full state checkpoint per transition), `agent_tool_calls`
(audit incl. rejections), `approval_requests` (run-id + content-sha256 +
expiry bound), `eval_runs`, `eval_results`.

## Repository-owned auxiliary tables (outside the wave-0 ORM)

Created lazily by `ensure_schema()`; formalizing via migration is a
recommended follow-up.

| Table | Backend | Purpose |
|---|---|---|
| `chunks_fts` | SQLite | FTS5 contentful virtual table `text, chunk_id UNINDEXED` |
| `index_manifest` | both | One row per (strategy, strategy_version, provider, model, revision): index_version, dim, corpus_hash, counts. **UNIQUE** on that 5-tuple |
| `chunk_fts` | PostgreSQL | `chunk_id` PK/FK + `tsvector` + GIN index (lexical analog of `chunks_fts`) |
| `chunk_vectors` | PostgreSQL | pgvector `vec vector` + provider/model/revision/text_hash/dim/normalized; PK (chunk_id, provider, model, model_revision) |

## Evidence ID scheme (contract C8)

```text
evidence_id = "ev-" + sha1("{document_id}|{strategy}|{start}|{end}|{text_hash}")[:12]
```

- Components are all persisted on the `chunks` row, so the id is resolvable
  from the table (`SearchIndexRepo.get_chunk_by_evidence_id`,
  `GET /evidence/{evidence_id}`).
- Chunk text is the exact slice `document_text[start:end]` of the cleaned
  document (roundtrip invariant), so a supplied window's exact text is always
  recoverable.
- Proven: `tests/integration/test_retrieval.py` (C8 roundtrip + offset-slice
  invariant), `tests/integration/test_ui_filings.py`, and
  `tests/integration/test_postgres_profile.py` for the PG backend.

## Index-version formula (SPEC §15.6)

```text
index_version = sha256(
  "{embedding_provider}|{embedding_model}|{embedding_model_revision}"
  "|{strategy}|{strategy_version}|{corpus_hash}"
)[:16]
corpus_hash   = sha256("\n".join(sorted(chunk text hashes of the build)))
```

Identical on both backends (`compute_index_version` in
`store/repositories/search_sqlite.py`, reused by the PostgreSQL repo).
Changing the embedding model, the chunking strategy/version, or the corpus
content changes the version (`tests/unit/test_retrieval_*`:
"index_version changes with model").
