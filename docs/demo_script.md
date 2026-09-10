# Three-minute demo script (Milestone 9)

Everything here is measured behavior on the dev machine; nothing is
staged. Prereqs: `storage/quarterline.db` seeded via
`uv run python scripts/demo_real_models.py --no-brief` (fixture-sourced
AAPL facts + the real 8-K exhibit, nomic-indexed), Ollama running with
`qwen3:4b` + `nomic-embed-text`.

## 0:00–0:20 — The problem

> "Public-company filings are machine-readable but not research-readable.
> Quarterline turns SEC filings into comparable quarterly facts, explainable
> signals, and source-linked research briefs — entirely local, with the LLM
> as the least-trusted component in the system."

## 0:20–0:50 — Deterministic core (works with the LLM off)

1. `uv run quarterline serve`, open http://127.0.0.1:8000.
2. Watchlist: 15 companies, latest quarter, revenue YoY, margins, FCF,
   quarter label — point at the caption: *"Rule-based quarterly performance
   label. Not a recommendation or forecast."*
3. Company page: eight-quarter charts; every metric links to provenance —
   click `revenue_yoy` → full lineage: accession, form, filed date, direct
   vs derived (Q4 = annual − 9-month YTD, shown with its inputs).
4. Screener: filter revenue_yoy > 0, operating-margin change ≥ 0 — note it
   runs with Ollama stopped.

## 0:50–1:30 — Grounded RAG

5. `uv run python -m quarterline search --ticker AAPL --query
   "what did management say about revenue and the March quarter"
   --strategy section --retrieval hybrid --mode brief --top-k 3`
   — real nomic-embed-text vectors, hybrid RRF fusion, evidence IDs shown.
6. Measured matrix (docs/retrieval_experiments.md): 7 of 8 configs at 100%
   Hit@5 with real embeddings, zero wrong-company retrievals, p95 < 60 ms.

## 1:30–2:20 — The validation gate (the interesting part)

7. `POST /api/c/AAPL/brief` with a scripted failing model, or replay the
   measured runs: qwen3:4b emits schema-valid JSON but cites
   `[ev-…]` incorrectly and picks a structurally invalid template — the gate
   rejects each bullet with a recorded reason, status becomes
   `insufficient_evidence`, and the UI shows facts + evidence, **never**
   unvalidated prose.
8. `POST /api/ask` "Should I buy AAPL?" → research-only refusal +
   alternatives (advice-policy-v1).
9. Enum-constrained decoding: metric IDs, label echo, and citation IDs are
   undecodable unless they come from the verified vocabularies.

## 2:20–3:00 — Agent + eval + honesty

10. `POST /api/memos` — LangGraph memo workflow: typed tools
    (get_company_facts, search_filings, get_prices, export_memo), budgets
    (4 tool calls, 12 transitions), checkpoints, then approve-export with a
    tampered memo → 409 (approval is hash-tied to exact content).
11. /dashboard — P50/P95, JSON-repair rate, refusal rate, eval summary.
12. Close on docs/failure_cases.md: seven *actually exercised* failures
    with traces and regression tests — "every capability statement in this
    repo traces to a test, a log row, or a measured report."
