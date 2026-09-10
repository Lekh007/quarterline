# Quarterline — Evaluation

How Quarterline is evaluated, what the committed dataset contains, which
metrics are computed, and what CI does and does not prove (SPEC §21–§23).

## Dataset (`data/eval/questions.jsonl`)

Schema (pydantic-validated by `eval/dataset.py`, SPEC §21):

| Field | Notes |
|---|---|
| id | unique question id |
| question | user question text |
| ticker | company scope |
| period_end | requested reporting context (drives the period filter) |
| expected_behavior | `answer` \| `insufficient_evidence` \| `refusal` |
| category | §21 category tag |
| reviewed | must be true (reviewed mandatory) |
| gold_spans | required for `answer`: list of {document_id (stable EDGAR accession string, dash-stripped resolution via `documents.accession`), start, end, optional anchor} — chunking-independent |

Validation rules: unique ids; `reviewed` mandatory; `answer` ⇔ ≥1 gold span;
provider failure is an **outcome**, never a dataset expectation; the three
abstention kinds are modeled distinctly (insufficient-evidence / policy
refusal / provider failure). Optional per-span `anchor` text makes `reviewed`
machine-checkable: `verify_spans` asserts the anchor sits inside
`text[start:end]` under the pipeline's offset roundtrip invariant.

### The 11 committed reviewed questions

All anchored on the frozen REAL Apple EX-99.1 Q2 FY2026 exhibit (accession
0000320193-26-000011); categories cover the §21 list:

| id | Kind | Notes |
|---|---|---|
| aapl-q2fy26-revenue-driver | management-explanation | CEO revenue-driver quote, span [447,1055) |
| aapl-q2fy26-cash-flow-driver | management-explanation | CFO cash-flow quote, span [1057,1487) |
| aapl-q2fy26-diluted-eps-yoy | period-specific figure | diluted EPS, span [292,445) |
| aapl-q2fy26-capital-return | capital allocation | dividend + $100B buyback, span [1489,1861) |
| aapl-q2fy26-risk-factors | risks | safe-harbor span [2792,3800) |
| aapl-q2fy26-net-sales-yoy | cross-period figure | net sales 111,184 vs 95,359, span [5864,5922) |
| aapl-fy26-h1-operating-cash-flow | cross-period figure | H1 OCF 82,627/53,887, span [10195,10251) |
| aapl-q1fy26-net-sales-not-in-corpus | abstention | Q1-FY26 period trap: filter yields empty corpus → insufficient evidence |
| aapl-retail-store-count-unanswerable | abstention | not stated anywhere in corpus |
| msft-q2fy26-revenue-driver-trap | abstention | wrong-company trap (no MSFT documents) |
| aapl-buy-advice-refusal | refusal | advice request → research-only refusal + alternative |

SPEC §30 Milestone 7 calls for "at least 30 reviewed gold questions"; **11
are committed** — this criterion is intentionally reported as partially met
(see `README.md` status table). Every committed anchor is verified in-session
by tests (`tests/integration/test_eval_retrieval.py`: every gold anchor
resolves in ingested text).

## Retrieval metrics (SPEC §22, `eval/retrieval.py`)

- **Hit@5**: fraction of answerable questions with ≥1 gold-overlapping unit
  in the top 5 (binary per question).
- **Recall@5**: mean **overlap-count coverage** — |gold spans covered| /
  |gold spans| per question. Distinct from Hit@5: a case with Hit=1.0 while
  Recall=0.5 exists in unit tests (`tests/unit/test_eval_metrics.py`) — the
  distinction is deliberate, not a naming accident.
- **MRR**: first relevant reciprocal rank.
- **wrong_company_rate**: any retrieved unit from another company (regression
  threshold: ANY increase fails).
- **period_filter_error_rate** (+ `unknown` bucket): retrieved units outside
  the requested reporting context.
- **Latency P50/P95**: nearest-rank percentiles of `SearchService` wall time;
  rerank latency null when the arm degraded.

## Generation metrics (SPEC §22, `eval/generation.py`)

Measured against a `GenerationRunner` **protocol** (never F5 internals):
JSON validity before/after repair, citation validity, numeric-error rate
pre-filter, unsupported-content remaining (deterministic unbacked-number
proxy), bullet retention, empty answers, answer relevancy (**documented
PROXY**: question-term overlap + concept coverage — not a human judgment),
correct abstention, refusal accuracy (with research-only alternative),
provider-failure rate — distinct status buckets
(`tests/integration/test_eval_generation.py`).

## Optional LLM judge (SPEC §22)

`eval/judge.py` + `prompts/judge/v1.txt`: pinned rubric (faithfulness /
relevance 1–5 + rationale JSON). Disabled unless `ENABLE_LLM_JUDGE=true` AND
`JUDGE_PROVIDER` AND `JUDGE_MODEL` are set; injectable transport; full
provenance (provider/model/prompt version+hash/raw/parsed,
`human_reviewed=False`). Labeled **"never ground truth"** — judge output is
advisory signal only. Out-of-scale responses are rejected
(`tests/unit/test_eval_dataset.py` / judge tests). No live judge run has been
performed (offline rule); the path is MockTransport-tested.

## Regression thresholds (SPEC §23, `eval/regression.py`)

Against the committed baseline `data/eval/baselines/fixture_baseline.json`:

- **Recall@5 drop > 5pp → FAIL** (exactly 5pp passes, 6pp fails — unit-tested
  boundary).
- **Any increase in wrong-company retrieval → FAIL.**
- **New config with no baseline row → FAIL** (when a baseline exists).
- `no_baseline` status is informational only.
- Deterministic citation-validation and financial-normalization invariants
  are enforced by the unit/integration suite, not by the eval CLI.

**Baseline regeneration is explicit, never silent**: writes require
`confirm=True` in code or `QUARTERLINE_EVAL_WRITE_BASELINE=1` in the CLI, and
always print a diff. The wave-3 baseline regeneration used exactly this gate
(see `docs/retrieval_experiments.md`). Note: the write gate is an env var, not
a `--write-baseline` CLI flag, because `cli.py` is wave-0-owned (documented
deviation).

## What CI proves — and what it does not (SPEC §23)

`.github/workflows/ci.yml` runs lint, format check, and the offline test
suite (`-m 'not live'`): unit tests, API tests, migration tests, real-fixture
normalization tests, citation/numeric validation tests, frozen retrieval
regression tests, prompt contract tests — using committed fixture embeddings
with documented provenance. Query-embedding generation is tested separately
from document embeddings (fixture-vs-provider vector regression test).

`.github/workflows/scheduled_eval.yml` runs the fixture eval + regression
check on a schedule/workflow dispatch and uploads reports.

**CI never pretends a fake provider measures generation quality**: the
scripted-fake generation harness is labeled a NON-MEASUREMENT (plumbing
check). Real-model quality (retrieval matrix + generation eval with
`nomic-embed-text`/`qwen3:4b`) runs on an opt-in self-hosted runner or as an
attached local evaluation report — it is **PENDING** at this release (see
`docs/retrieval_experiments.md`).

CI also does not exercise live SEC ingestion (EDGAR_IDENTITY gate), Ollama,
or PostgreSQL (the pgvector contract tests skip without a server + DBAPI).
