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

**PENDING.** The orchestrator's wave-5 verification runs the same 2×4 matrix
with `nomic-embed-text` embeddings (Ollama) and `qwen3:4b` generation against
the frozen corpus, per SPEC §23 scheduled/manual evaluation. Until those
numbers exist, no real-model retrieval or generation quality is claimed
anywhere in this documentation set.
