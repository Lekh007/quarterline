# Quarterline — Retrieval Experiments

All numbers on this page were produced by running the committed harness
against the **fixture corpus**: the frozen real Apple EX-99.1 exhibit
(accession 0000320193-26-000011) plus synthetic 10-Q / injection / PDF
fixtures, indexed with the deterministic **fake embedding provider**
(`FakeEmbeddingProvider`, dim 64, seed 20260909 — hash vectors with **no
semantic signal**, NON-PRODUCTION by design).

> **Label**: every matrix below is a *fixture-corpus, n=11, deterministic
> fake-embedding* measurement. It verifies plumbing, filtering, fusion
> ordering, and regression gates — it is **NOT** a real-model quality
> measurement (SPEC §22/§23).

Reproduce:

```bash
DATABASE_URL=sqlite:///<tmp>/eval.db EMBED_PROVIDER=fake \
  uv run python -m quarterline eval retrieval --dataset data/eval/questions.jsonl
```

## Measured matrix — current baseline (2026-09-10, wave F8 re-run)

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
null. With fake embeddings, dense scores carry no semantic signal — the
dense/hybrid levels are artifacts of hash-vector geometry, not retrieval
quality.

## Chunker-fix history (from `docs/implementation_log.md`)

- **Wave 3 (F6)** measured, pre-fix: fixed/lexical Hit@5 71.4%,
  section/hybrid 57.1% (4/7 gold spans); the financial-statement tables
  (gold spans at offsets 5864 and 10195 of the 11,342-char exhibit) were
  unreachable by BOTH strategies.
- **Diagnosed gap**: `_pack_chunks` in `retrieve/chunk_fixed.py` stopped
  early — the document tail (chars 5639–11342) was abandoned when a chunk was
  trimmed inside its predecessor; the section strategy emitted only child 0
  plus its window.
- **Fix (wave-3 orchestrator)**: early-stop bug fixed → lexical Hit@5
  71.4% → **100.0%**, section/hybrid 4/7 → **6/7 (85.7%)**. Frozen eval test
  levels were updated to the measured values so a future fix can only raise
  them.
- **Explicit baseline regeneration** (never silent, SPEC §23): the new
  baseline was written through the gated path
  (`QUARTERLINE_EVAL_WRITE_BASELINE=1` + printed diff). 7 of 8 configs
  improved; **fixed/dense moved 57.1% → 42.9%** — recorded in the diff as a
  fake-embedding geometry artifact, not semantic signal (dense arms are
  meaningless without a real embedding model).

## Reranker

Code path exists and is exercised in its degraded mode
(`tests/unit/test_reranker.py`: import-guard failure fallback, no fake
scores); an actual cross-encoder comparison requires the `[rerank]` extra and
a real embedding model, so **no reranker quality claim is made**.

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
  queries — the metadata filter path holds on real vectors.
- Lexical (FTS5) remains the strongest single signal and the fastest
  (p50 ≈ 10 ms); hybrid buys robustness at ~3-4× lexical latency, still
  <60 ms p95 locally.
- Reranker arm still degraded (extra not installed); no rerank quality claim.

Generation side (same session, `scripts/demo_real_models.py`): live
`qwen3:4b` briefs through the full pipeline produced schema-valid JSON with
correct label echo and real citation IDs once schema-constrained decoding was
enabled, but residual 4B-model compliance failures (inline `[ev-…]` sentence
format, template/metric pair validity) were caught by the §18 gate and
honestly reported as `insufficient_evidence`. An earlier cached-failure bug
(failures written to `brief_cache`) was found by these runs and fixed.
**No clean end-to-end `ok` brief from a local ≤7B model has been measured
yet** — the demo currently proves the guardrail path, not generation quality.

These are fixture-corpus numbers (one real filing exhibit); they are not
production-scale retrieval quality claims.
