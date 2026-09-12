# India evaluation set and measured retrieval (IND-7)

Status: complete as of **2026-09-11**. The 15-question IND-3b draft set
(`data/eval/india_draft_questions.jsonl`, designed before any India ingestion
existed) is now a MEASURED dataset: 13 questions promoted to
`data/eval/india_questions.jsonl` (US `load_dataset` schema, all
`reviewed: true`, every anchor machine-verified), 2 explicitly not promoted
with verified reasons (§3). Retrieval and citation infrastructure only — no
briefs, no LLM generation, no scores (IND-8 owns those).

## 1. What was built

- `src/quarterline/sources/india/narrative.py` — narrative document ingestion:
  the manifests' cached company-IR PDFs become `documents` rows with the India
  document-kind vocabulary (`financial_results | results_notes |
  earnings_presentation | annual_report | management_transcript |
  exchange_announcement`), one section per PAGE (`page_start`/`page_end` set —
  page-level citations are the India requirement), manifest-sha256 verification
  before ingest, content-hash artifact dedupe (the IND-3 import path's
  discipline), idempotent re-runs, and management-commentary labeling carried
  ON the document row (`extraction_notes` JSON: `management_commentary` flag +
  kind-based `is_management_commentary` + `source_tier` — the
  source-tier-laundering guard).
- TCS's REPORTED Q1 FY27 quarterly CFO (₹12,171 Cr vs ₹11,919 Cr PY,
  condensed-FS p.6) ingested as a `pdf_text` observation under exactly the INFY
  provenance standard (`ingest_reviewed_tcs_cash_flow`: live drift guard, page
  provenance, agent-checked review status, idempotent observation hash). This
  closes the IND-6 open item (review packet §11.4).
- Both chunk strategies (`fixed`, `section`) built over the India narrative
  corpus with REAL Ollama `nomic-embed-text` (768-dim) — a runtime artifact in
  storage/, never a fixture.
- `scripts/eval_india.py` — the India runner: same matrix and SAME precise
  metric definitions as the US harness (`Hit@5`, `Recall@5`, `MRR`,
  wrong-company rate, insufficient-evidence rate via
  `quarterline.eval.retrieval.aggregate`), per-question ticker filter, plus
  canonical-fact checks for the fact questions and behavior receipts for the
  missing-CF/refusal rows. `--offline-baseline` reproduces the run on a
  scratch store with `FakeEmbeddingProvider` behind the explicit
  `QUARTERLINE_EVAL_WRITE_BASELINE=1` gate (SPEC §23; no silent regeneration).

## 2. Narrative corpus (ingested, measured)

28 documents / 604 pages / 604 page-sections, all 10 issuers, all extraction
statuses honest:

| Issuer | Documents | Pages | fixed chunks | section chunks |
|---|---|---|---|---|
| INFY | 8 | 198 | 825 | 3,739 |
| HINDUNILVR | 6 | 181 | 313 | 1,051 |
| TCS | 2 | 66 | 234 | 940 |
| HCLTECH | 2 | 49 | 69 | 270 |
| ULTRACEMCO | 2 | 50 | 47 | 176 |
| ASIANPAINT | 1 | 13 | 32 | 134 |
| MARUTI | 2 | 22 | 35 | 117 |
| ITC | 2 | 10 | 29 | 131 |
| SUNPHARMA | 2 | 8 | 26 | 121 |
| LT | 1 | 7 | 33 | 126 |
| **Total** | **28** | **604** | **1,643** | **6,805** |

By kind: `financial_results` 16 (condensed statements + Reg-33 bundles),
`results_notes` 6 (press releases), `earnings_presentation` 4,
`management_transcript` 2 (HUL earnings-call transcripts). Commentary kinds
are labeled `management_commentary: true` on the document row; condensed
statements are labeled `false` (company-prepared, not commentary) with
`source_tier: company_ir` on every row.

Extraction honesty: 39 image-only pages carry `confidence=0.0` and an explicit
"likely scanned/image page" note (never silently indexed as clean); MARUTI's
scanned results PDF extracts through its OCR layer and stays in the corpus as
retrieval-only material exactly like IND-6 left it (no fact ever read from it).
XLSX workbooks and exchange XBRL/iXBRL files are NOT narrative (fact-path or
non-text) — reported, not force-labeled.

## 3. Promotion decisions (13 promoted, 2 not)

Every draft question's anchor was resolved against the NOW-INGESTED text:
narrative gold spans carry offsets in the extracted-page-text coordinate
system (`text[start:end]` contains the anchor phrase — re-verified by tests);
fact gold spans are the verbatim fact lines in the committed fixture XML /
import sidecar, and their answers are verified against the canonical fact
layer (`normalized_facts` / `fact_observations`).

**Promoted (13):**

| Question | Class | Verified gold |
|---|---|---|
| hul-q1fy27-consolidated-revenue-scope-trap | fact | canonical revenue quarter consolidated = 173410000000 |
| infy-q4fy26-quarter-revenue-not-annual | fact | quarter 464020000000 vs annual 1786500000000, distinct period kinds |
| infy-fy26-exceptional-items-sign | fact | exceptional annual = -12890000000; Q4 quarter = 0; PBT 399950000000; PBIT obs 412840000000 |
| infy-q1fy27-revision-status | fact | observations carry revision_status=Original, seq 177385 (sidecar offsets verified live) |
| infy-q1fy27-operating-cash-flow-exchange-missing | behavior | verified absence in the exchange source + the pdf_text company-IR pointer |
| hul-q1fy27-operating-cash-flow-missing | behavior | verified absence everywhere; annual 109990000000 exists, labeled annual |
| infy-q1fy27-quarterly-cash-flow-ir-pdf-page | narrative | p.6 span contains the anchor AND 9,330 / 7,632 |
| infy-fy27-guidance-press-release | narrative | p.1 span contains the guidance anchor + 20%-22% margin bullet |
| hul-q1fy27-presentation-highest-growth-claim | narrative | p.2 span contains the anchor, USG 10%, Turnover ₹17,184 crores |
| hul-q1fy27-buy-advice-refusal | behavior | advice policy detects the request (should_i_buy + buy/sell/hold patterns) |
| infy-q4fy26-to-q1fy27-revenue-comparison | fact | both quarter identities across two filings |
| infy-q1fy27-revenue-exact-rupees-units | fact | 482110000000 exact rupees + rounding_trait Crores (presentation only) |
| infy-q1fy27-diluted-eps-no-unit-scaling | fact | eps_diluted = 19.17 INR/share, never scaled |

**NOT promoted (2) — honest failures, recorded with verified reasons:**

1. `infy-q1fy27-standalone-revenue-scope-trap` — the gold targets the
   STANDALONE instance; the ingested fact corpus is consolidated-only (IND-6
   imported the 20 consolidated instances; standalone instances remain
   storage-cache-only). The draft's own dependency caveat applies: with a
   consolidated-only corpus the correct behavior degrades to
   insufficient_evidence, which is not the gold the draft specifies. Revisit
   if a standalone ingestion wave lands.
2. `itc-q1fy27-revenue-wrong-company-trap` — premise invalidated by later
   waves: the draft requires "zero retrieved evidence" because ITC had no
   documents, but IND-6 verified ITC and ingested its consolidated facts, and
   IND-7 ingested its press release and condensed statements, so an
   ITC-filtered query now legitimately returns ITC evidence. Wrong-company
   isolation is enforced structurally (the ticker-filtered SearchService,
   tested offline) and measured by the wrong-company rate, which is 0.0 in
   every configuration below.

## 4. Measured results

Corpus = ingested India narrative documents + canonical facts;
n = 13 promoted questions (3 narrative with retrieval gold, 7 fact, 3
behavior); fact questions are excluded from the span denominators (their gold
lives in the canonical-fact layer, a different coordinate system) and are
verified by the runner's 9 canonical-fact checks instead.

### Real embeddings (ollama / nomic-embed-text, 768-dim, dev store)

| strategy | retrieval | Hit@5 | Recall@5 | MRR | wrong-co | insufficient-ev | lat p50/p95 ms |
|---|---|---|---|---|---|---|---|
| fixed | lexical | 33.3% | 33.3% | 0.167 | 0.0% | 0.0% | 39.8/44.2 |
| fixed | dense | 33.3% | 33.3% | 0.167 | 0.0% | 0.0% | 47.7/2715.1 |
| **fixed** | **hybrid** | **66.7%** | **66.7%** | **0.444** | **0.0%** | **0.0%** | 63.2/72.3 |
| fixed | hybrid-rerank | 66.7% | 66.7% | 0.444 | 0.0% | 0.0% | 106.7/123.2 |
| section | lexical | 33.3% | 33.3% | 0.333 | 0.0% | 0.0% | 77.2/84.3 |
| section | dense | 33.3% | 33.3% | 0.167 | 0.0% | 0.0% | 84.6/89.5 |
| section | hybrid | 66.7% | 66.7% | 0.333 | 0.0% | 0.0% | 122.8/151.2 |
| section | hybrid-rerank | 66.7% | 66.7% | 0.333 | 0.0% | 0.0% | 118.0/133.0 |

Latencies are from the latest published run
(`storage/eval_reports/india_eval_real_20260912T051843Z.json`; the metric
columns reproduced exactly across runs, latencies are single-run environment
noise).

WRONG-COMPANY RATE IS 0.0 IN EVERY CONFIGURATION — no other issuer's chunk is
ever returned under a ticker-filtered query.

Canonical fact checks: 9/9 PASS (all seven fact questions' canonical
verifications plus both missing-CF absence checks).

Behavior rows (real run): `hul-q1fy27-buy-advice-refusal` — advice policy
detects the request (`should_i_buy_sell_hold_invest`,
`buy_sell_hold_near_context`), so the generation layer has the deterministic
refusal hook. `infy-q1fy27-operating-cash-flow-exchange-missing` — the
evidence policy does NOT abstain (retrieval returns Reg-33 statement pages and
the press release's "strong cash generation" quote instead of the condensed
statement's quarterly-CF page: pointer document NOT in top-5 under either
strategy). `hul-q1fy27-operating-cash-flow-missing` — same shape. See §6:
IND-8 must answer the missing-CF rows from the FACT layer's typed statuses
(which are verified) rather than relying on narrative retrieval to surface the
pointer.

### Offline baseline (deterministic fake embeddings, scratch store)

Committed to `data/eval/baselines/india_baseline.json`
(dataset_version `84f172f8bd48`; regen gate `QUARTERLINE_EVAL_WRITE_BASELINE=1`,
never silent). Fake embeddings carry NO semantic signal, so these numbers
measure the plumbing, not quality (SPEC §23 convention):

| strategy | retrieval | Hit@5 | Recall@5 | MRR | wrong-co |
|---|---|---|---|---|---|
| fixed | lexical | 33.3% | 33.3% | 0.167 | 0.0% |
| fixed | dense | 0.0% | 0.0% | 0.000 | 0.0% |
| fixed | hybrid | 0.0% | 0.0% | 0.000 | 0.0% |
| fixed | hybrid-rerank | 0.0% | 0.0% | 0.000 | 0.0% |
| section | lexical | 33.3% | 33.3% | 0.333 | 0.0% |
| section | dense | 0.0% | 0.0% | 0.000 | 0.0% |
| section | hybrid | 33.3% | 33.3% | 0.167 | 0.0% |
| section | hybrid-rerank | 33.3% | 33.3% | 0.167 | 0.0% |

## 5. Per-category outcomes

| Category | Question(s) | Outcome |
|---|---|---|
| Scope, consolidated direction | hul-q1fy27-consolidated-revenue-scope-trap | canonical fact verified; standalone value exists in no ingested consolidated series |
| Scope, standalone direction | (unpromoted standalone trap) | explicitly not promoted — standalone facts not ingested (§3.1) |
| Quarter vs cumulative | infy-q4fy26-quarter-revenue-not-annual | both identities verified distinct |
| Units (exact rupees) | infy-q1fy27-revenue-exact-rupees-units | verified; rounding trait travels as presentation-only metadata |
| Units (per-share exemption) | infy-q1fy27-diluted-eps-no-unit-scaling | verified; 19.17 INR/share never scaled |
| Exceptional sign | infy-fy26-exceptional-items-sign | verified incl. the Q4-quarter-is-zero guard |
| Missing quarterly CF (pointer exists) | infy-q1fy27-operating-cash-flow-exchange-missing | fact-layer absence + pointer verified; narrative retrieval does NOT surface the pointer page in top-5 (honest gap, §6) |
| Missing quarterly CF (no pointer) | hul-q1fy27-operating-cash-flow-missing | fact-layer absence verified; annual figure labeled annual |
| Page-level citation | infy IR-pdf CF page; INFY guidance; HUL presentation | all three spans verified in extracted page text; 2 of 3 retrieved in top-5 (hybrid); the CF page misses (§6) |
| Management commentary | guidance + presentation rows | document rows labeled management_commentary=true; IFRS-INR basis carried in metadata |
| Advice refusal | hul-q1fy27-buy-advice-refusal | advice policy detects the request; refusal itself is IND-8 generation behavior |
| Cross-period comparison | infy-q4fy26-to-q1fy27-revenue-comparison | both quarter facts verified across the two filings |
| Revision status | infy-q1fy27-revision-status | Original + seq 177385 carried from the listing sidecar into every observation's metadata |
| Wrong-company trap | (unpromoted ITC trap) | explicitly not promoted — premise invalidated (§3.2); isolation guaranteed by the ticker filter and 0.0 measured rate |

## 6. Honest gaps (for IND-8 and later waves)

1. **The INFY quarterly-CF page misses top-5** (`infy-q1fy27-quarterly-cash-flow-ir-pdf-page`):
   under hybrid the fixed strategy ranks condensed-FS **p.7** #1 and section
   ranks **p.28** #1 — the correct document always surfaces, the correct PAGE
   does not (it competes with the Reg-33 bundle's annual cash-flow pages).
   The row stays promoted with measured Hit@5 = 2/3 narrative questions; the
   answer's RELIABLE source for the reported quarterly figure is the fact
   layer's `pdf_text` observation (company_ir provenance, page in metadata).
2. **Missing-CF rows do not retrieve their pointer** (see §4 behavior rows):
   IND-8 must render the missing-CF answer from the typed missing-data
   statuses + the ingested pdf_text observation, not from RAG alone.
3. **Unpromoted questions**: the two §3 rows; the US categories
   `capital_allocation` and `risks` still have no India row (the draft notes
   flagged both as easy follow-ups: INFY dividend line in the Q1 CF financing
   section; HUL presentation safe-harbor page 4).
4. **Rendered-page quality**: HUL's Q1 rendered P&L remains
   non-machine-reliable (the IND-3 scrambling finding); MARUTI's scan OCR and
   ULTRACEMCO's vector artifacts are in the corpus as retrieval-only text with
   image-only pages flagged confidence=0.0 (39 pages). No fact is ever read
   from a garbled page (IND-6 rule, unchanged).
5. **XLSX workbooks and iXBRL renderings are not in the retrieval corpus**
   (not text-extractable narrative here); HUL's workbook cash-flow sheets
   remain available to a future structured-ingestion wave.
6. **`hybrid` is the recommended India retrieval configuration** (best
   Hit@5/MRR; lexical-only misses the dense-only narrative pages; dense-only
   is weaker on exact financial phrasing). Rerank adds nothing measurable
   here (no cross-encoder in this environment; the arm degrades to hybrid).

## 7. Reproducing

```
# corpus + real-embedding matrix (requires ollama with nomic-embed-text)
uv run python scripts/eval_india.py

# reproducible offline baseline (scratch store, fake provider; write gated)
QUARTERLINE_EVAL_WRITE_BASELINE=1 uv run python scripts/eval_india.py --offline-baseline

# smoke: INFY-filtered narrative search with evidence ids
uv run python -m quarterline search --ticker INFY \
  --query "management commentary on margin guidance" --strategy section --retrieval hybrid
```

Tests: `tests/unit/test_india_narrative.py` (ingestion, labeling, honesty,
TCS CF, wrong-company isolation, evidence roundtrip — offline, fake
embeddings) and `tests/unit/test_india_eval_dataset.py` (US-loader
compatibility, anchor re-verification, category coverage, unpromoted-with-
reason). Per repo convention they skip cleanly when the gitignored
`storage/raw/india` cache is absent.
