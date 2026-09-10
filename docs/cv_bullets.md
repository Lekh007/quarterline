# CV bullets — drafted ONLY from measured results

Every claim below traces to code, a test, or a measured report in this repo
(SPEC §30 Milestone 9 rule: no invented metrics). Numbers in brackets are the
evidence. Trim to the role you're applying for.

## AI Engineer bullets

- Built **Quarterline**, a local-first public-company research desk (Python
  3.12, FastAPI, SQLAlchemy 2/Alembic, SQLite FTS5 + NumPy vector search,
  PostgreSQL/pgvector profile) that turns SEC filings into comparable
  quarterly facts, rule-based signals, and citation-verified research briefs.
- Designed a **guardrails-first LLM architecture**: the model never computes
  numbers — it selects allowlisted metric references rendered by Decimal-exact
  Python, and every bullet passes a 10-check validation gate (schema, citation
  existence/ownership, label echo, numeric consistency, advice policy) before
  display; measured local 4B/7B models failing citation and metric-vocabulary
  constraints and had the gate reject 100% of unsupported content.
- Implemented **schema-constrained generation** (Ollama structured outputs)
  with enum-locked metric IDs, label echo, and citation IDs after measuring
  systematic field-transposition failures across two instruct models; fixed a
  failure-caching bug the live runs exposed.
- Built **hybrid retrieval** (FTS5 lexical + dense vectors + RRF fusion,
  optional cross-encoder rerank with graceful degradation) over two chunking
  strategies; with real nomic-embed-text vectors measured **7/8 configs at
  100% Hit@5, zero wrong-company retrievals, <60 ms p95** on the frozen
  question set (n=11, fixture corpus — labeled as such).
- Shipped **LLMOps**: 11 span-anchored reviewed eval questions with
  Hit@5-vs-Recall@5-precise metrics, a 2×4 (chunking × retrieval) experiment
  matrix, regression CI with a no-silent-baseline-regeneration rule,
  structured run telemetry, and a live metrics dashboard.
- Implemented a **bounded LangGraph research-memo agent**: typed tools over
  read-only repositories, hard budgets (4 tool calls, 12 graph transitions,
  deadline), persistent checkpoints, and export approval cryptographically
  tied to exact memo content; documented 7 actually-exercised failure modes
  with traces and regression tests.
- Engineered **SEC data ingestion** with fair-access throttling (≥200 ms
  spacing, retry/backoff, content-hash caching), XBRL tag fallbacks,
  fiscal-calendar period classification (52/53-week years), YTD→quarterly
  derivation with full provenance lineage, and Decimal-exact metrics;
  verified values reconcile against official filings.

## Interview soundbites (from measured events)

- "A cached validation failure briefly poisoned the brief cache — my
  real-model run caught it in one request; failures are never cached now."
- "The chunker silently dropped the second half of long documents; the eval
  matrix caught it, and fixing it took lexical Hit@5 from 71% to 100% — the
  baseline was regenerated through an explicit, diff-printing gate."
- "Two different instruct models made the identical JSON field-swap mistake,
  which told me it was a prompt problem, not a model problem — prompt v2."

## Honest scope statement (use this)

Research/education tool; no recommendations, no price targets. Personal
project: single-user, local-first; multi-user auth, hosted-inference
economics, and data-licensing review are documented as pre-commercial gaps
(docs/commercial_readiness.md).
