# Quarterline - Retrieval Experiments

All numbers on this page were produced by running the committed harness
against the **fixture corpus**: the frozen real Apple EX-99.1 exhibit
(accession 0000320193-26-000011) plus synthetic 10-Q / injection / PDF
fixtures, indexed with the deterministic **fake embedding provider**
(`FakeEmbeddingProvider`, dim 64, seed 20260909 - hash vectors with **no
semantic signal**, NON-PRODUCTION by design).

> **Label**: every matrix below is a *fixture-corpus, n=11, deterministic
> fake-embedding* measurement. It verifies plumbing, filtering, fusion
> ordering, and regression gates - it is **NOT** a real-model quality
> measurement (SPEC §22/§23).

Reproduce:

```bash
DATABASE_URL=sqlite:///<tmp>/eval.db EMBED_PROVIDER=fake \
  uv run python -m quarterline eval retrieval --dataset data/eval/questions.jsonl
```

## Measured matrix - current baseline (2026-09-10, wave F8 re-run)

2 strategies × 4 retrieval configs (SPEC §16 "Reranker experiment"), n=11
questions (7 with gold spans), 0% wrong-company, 0% period-filter errors in
every config:

| Strategy | Retrieval | Hit@5 | Recall@5 | MRR | Latency p50/p95 (ms) |
|---|---|---:|---:|---:|---:|
| fixed | lexical | 100.0% | 100.0% | 0.929 | 12.7 / 25.7 |
| fixed | dense | 42.9% | 42.9% | 0.226 | 11.2 / 13.4 |
| fixed | hybrid | 85.7% | 85.7% | 0.619 | 14.2 / 15.5 |
| fixed | hybrid-rerank | 85.7% | 85.7% | 0.619 | 14.7 / 24.9 |
| section | lexical | 100.0% | 100.0% | 1.000 | 18.5 / 24.1 |
| section | dense | 85.7% | 85.7% | 0.445 | 11.1 / 13.1 |
| section | hybrid | 85.7% | 85.7% | 0.714 | 12.3 / 12.8 |
| section | hybrid-rerank | 85.7% | 85.7% | 0.714 | 14.7 / 22.4 |

These match the committed regression baseline
(`data/eval/baselines/fixture_baseline.json`); the CLI regression check
reports PASS for all 8 configs.

Honest reading of the reranker arms (SPEC §16: "Do not assume reranking
improves results. Publish the measured outcome"): the rerank arms are
byte-identical to plain hybrid **because no cross-encoder is installed**
(`sentence-transformers` lives behind the `[rerank]` extra and is not
installed); the arm degrades to hybrid-with-metadata and rerank latency is
null. With fake embeddings, dense scores carry no semantic signal - the
dense/hybrid levels are artifacts of hash-vector geometry, not retrieval
quality.

## Chunker-fix history (from `docs/implementation_log.md`)

- **Wave 3 (F6)** measured, pre-fix: fixed/lexical Hit@5 71.4%,
  section/hybrid 57.1% (4/7 gold spans); the financial-statement tables
  (gold spans at offsets 5864 and 10195 of the 11,342-char exhibit) were
  unreachable by BOTH strategies.
- **Diagnosed gap**: `_pack_chunks` in `retrieve/chunk_fixed.py` stopped
  early - the document tail (chars 5639–11342) was abandoned when a chunk was
  trimmed inside its predecessor; the section strategy emitted only child 0
  plus its window.
- **Fix (wave-3 orchestrator)**: early-stop bug fixed → lexical Hit@5
  71.4% → **100.0%**, section/hybrid 4/7 → **6/7 (85.7%)**. Frozen eval test
  levels were updated to the measured values so a future fix can only raise
  them.
- **Explicit baseline regeneration** (never silent, SPEC §23): the new
  baseline was written through the gated path
  (`QUARTERLINE_EVAL_WRITE_BASELINE=1` + printed diff). 7 of 8 configs
  improved; **fixed/dense moved 57.1% → 42.9%** - recorded in the diff as a
  fake-embedding geometry artifact, not semantic signal (dense arms are
  meaningless without a real embedding model).

## Reranker

Code path exists and is exercised in its degraded mode
(`tests/unit/test_reranker.py`: import-guard failure fallback, no fake
scores). The real comparison was measured on 2026-09-17 (see the second
real-model matrix below): `cross-encoder/ms-marco-MiniLM-L-6-v2` over the
top-15 candidates, n=30 questions.

Measured verdict (small-n, one-document corpus): the cross-encoder lifts the
fixed strategy from 95.0% to **100.0% Hit@5** (MRR 0.608 → 0.762) but HURTS
the section strategy (90.0% → **70.0% Hit@5**). Plausible cause: section
windows are long, and a MiniLM cross-encoder scores (query, window) pairs
after 512-token truncation, so relevance signal degrades on long inputs;
fixed chunks are short and pair-friendly. Cost: ~330-500 ms per rerank call
(p50 338.5 ms, p95 479.6 ms over 52 calls, CPU) versus ~60 ms p50 for
un-reranked hybrid. Conclusion recorded honestly: **reranking is not a
blanket win; it is chunking-strategy dependent** on this corpus, and no
production-quality claim is made.

## Real-model measurements

Measured 2026-09-10 on the development machine (Ryzen AI 9 HX 370, 31 GB RAM,
RTX 4060 Laptop GPU; Ollama 0.33.3), live `nomic-embed-text` (768-dim, Nomic
query/document prefixes applied) for both index and queries, frozen fixture
corpus, n=11 reviewed questions, 7 gold-bearing. Reproduce with
`uv run python scripts/eval_real_models.py`; raw report in
`storage/eval_reports/real_model_matrix_*.md`.

| strategy | retrieval | Hit@5 | Recall@5 | MRR | wrong-co | period-err | lat p50/p95 ms |
|---|---|---|---|---|---|---|---|
| fixed | lexical | 100.0% | 100.0% | 0.929 | 0.0% | 0.0% | 10.7/18.0 |
| fixed | dense | 100.0% | 100.0% | 0.738 | 0.0% | 0.0% | 35.9/39.7 |
| fixed | hybrid | 100.0% | 100.0% | 0.786 | 0.0% | 0.0% | 35.9/44.4 |
| fixed | hybrid-rerank (degraded) | 100.0% | 100.0% | 0.786 | 0.0% | 0.0% | 38.2/48.6 |
| section | lexical | 100.0% | 100.0% | 1.000 | 0.0% | 0.0% | 10.2/11.2 |
| section | dense | 85.7% | 85.7% | 0.743 | 0.0% | 0.0% | 35.3/37.6 |
| section | hybrid | 100.0% | 100.0% | 0.833 | 0.0% | 0.0% | 47.1/59.5 |
| section | hybrid-rerank (degraded) | 100.0% | 100.0% | 0.833 | 0.0% | 0.0% | 43.6/50.6 |

Findings (measured, small-n):

- Real semantic embeddings removed the fake-embedding artifact entirely:
  fixed/dense rose 42.9% → 100% Hit@5 versus the deterministic-fake matrix
  above. 7 of 8 configs reach 100% Hit@5; section/lexical is perfect (MRR 1.0).
- Zero wrong-company retrievals and zero period-filter errors with live
  queries - the metadata filter path holds on real vectors.
- Lexical (FTS5) remains the strongest single signal and the fastest
  (p50 ≈ 10 ms); hybrid buys robustness at ~3-4× lexical latency, still
  <60 ms p95 locally.
- Reranker arm still degraded in this session (extra not installed);
  superseded by the 2026-09-17 matrix below.

Generation side (same session, `scripts/demo_real_models.py`): live
`qwen3:4b` briefs through the full pipeline produced schema-valid JSON with
correct label echo and real citation IDs once schema-constrained decoding was
enabled, but residual 4B-model compliance failures (inline `[ev-…]` sentence
format, template/metric pair validity) were caught by the §18 gate and
honestly reported as `insufficient_evidence`. An earlier cached-failure bug
(failures written to `brief_cache`) was found by these runs and fixed.
**No clean end-to-end `ok` brief from a local ≤7B model has been measured
yet** - the demo currently proves the guardrail path, not generation quality.

### 2026-09-17 matrix: real embeddings + REAL cross-encoder (n=30)

Same machine (RTX 4060 Laptop GPU, Ollama live `nomic-embed-text`, 768-dim
indexes already in the dev store), dataset expanded to 30 reviewed questions
(20 gold-bearing). The `[rerank]` extra installed and
`cross-encoder/ms-marco-MiniLM-L-6-v2` loaded for real this time (52 rerank
calls, p50 338.5 ms, p95 479.6 ms on CPU); the rerank latency shows up inside
the end-to-end latency column. Reproduce with
`uv run python scripts/eval_real_models.py` (the script labels the reranker
arm in its output and report; raw report in
`storage/eval_reports/real_model_matrix_*.md`).

| strategy | retrieval | Hit@5 | Recall@5 | MRR | wrong-co | period-err | lat p50/p95 ms |
|---|---|---|---|---|---|---|---|
| fixed | lexical | 95.0% | 95.0% | 0.725 | 0.0% | 0.0% | 36.6/39.2 |
| fixed | dense | 95.0% | 95.0% | 0.556 | 0.0% | 0.0% | 42.6/47.1 |
| fixed | hybrid | 95.0% | 95.0% | 0.608 | 0.0% | 0.0% | 59.3/62.5 |
| fixed | hybrid-rerank (REAL cross-encoder) | 100.0% | 100.0% | 0.762 | 0.0% | 0.0% | 399.4/415.0 |
| section | lexical | 95.0% | 95.0% | 0.752 | 0.0% | 0.0% | 38.3/56.1 |
| section | dense | 70.0% | 70.0% | 0.509 | 0.0% | 0.0% | 43.2/46.8 |
| section | hybrid | 90.0% | 90.0% | 0.618 | 0.0% | 0.0% | 61.3/68.4 |
| section | hybrid-rerank (REAL cross-encoder) | 70.0% | 70.0% | 0.585 | 0.0% | 0.0% | 532.3/573.4 |

Findings (measured, small-n):

- The real cross-encoder LIFTS the fixed strategy (95.0% → 100.0% Hit@5,
  MRR 0.608 → 0.762) and HURTS the section strategy (90.0% → 70.0% Hit@5).
  Plausible cause: section windows are long and a MiniLM cross-encoder scores
  them after 512-token truncation, while fixed chunks are short and
  pair-friendly. **Reranking is not a blanket win; it is
  chunking-strategy dependent on this corpus.**
- Rerank cost is real: ~330-500 ms per call (CPU) versus ~60 ms p50 for
  un-reranked hybrid end-to-end.
- Zero wrong-company retrievals and zero period-filter errors again, now at
  n=30 with 4 advice/trap questions retrieving nothing (ticker filter holds).
- fixed/lexical misses one gold span (19/20): the cash-and-equivalents
  table-line span, whose wording does not overlap the question text.
- Versus 2026-09-10 (n=11): per-config numbers are not directly comparable
  (the dataset changed); the structural findings (lexical strongest single
  signal, dense weakest on section, metadata filters holding) replicate.

### 2026-09-17 live-model US brief runs (qwen3:4b, end to end)

Three live `generate_brief('AAPL', 2026-03-28)` runs through the full
pipeline (real nomic index → hybrid retrieval → 4 evidence windows →
schema-constrained qwen3:4b → the §18 10-check gate), recorded verbatim:

1. First attempt (default 600 s generation timeout, model cold after the
   matrix run): `provider_unavailable` - Ollama generation timed out at the
   default 600 s. Recorded as a failure; nothing fabricated.
2. Warm retry (timeout raised to 1200 s): completed in 36.8 s.
   Outcome `partial` - schema-valid JSON, 8 of 10 gate checks PASS, but
   check[4] citation_existence dropped 2 pieces (a bullet and a risk
   statement asserting filing-derived claims with no supplied citation) and
   check[8] metric_reference_validity dropped 1: template `reported_level`
   is not structurally valid for metric `shares_yoy`.
   0 bullets survived the gate; the brief is NOT presented as validated.
3. Cache cleared, default settings: completed in 5.2 s. Outcome
   `insufficient_evidence` - the model returned a schema-valid abstention;
   all 10 gate checks PASS; nothing fabricated. (A repeat call at this point
   served the cached `partial` outcome from run 2 until the cache was
   cleared - BRIEF_CACHE_HOURS=24 works as designed.)

Verdict, honestly stated: the live-model path now runs end to end and the
validation gate is proven live in both directions - it passes clean
abstentions untouched and strips non-compliant prose before anything reaches
the user. **A clean `ok` brief from a local ≤4B model is still unmeasured**;
residual 4B compliance failures (uncited filing-derived claims, invalid
template/metric pairs) remain the limiter, matching the 2026-09-10 demo and
the IND-8 India record. Criterion #11 stays partial for that reason, with
this run as its live evidence.

These are fixture-corpus numbers (one real filing exhibit); they are not
production-scale retrieval quality claims.
