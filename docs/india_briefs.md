# India briefs and the memo workflow (IND-8)

Status: complete as of **2026-09-12**. India generation through the SAME
guardrails as the US side: official facts + retrieved evidence in, constrained
validated prose out, page-level citations, honest insufficient-evidence,
advice refusal. No scores, no recommendations, no quarter labels. The India
evaluation/retrieval basis is `docs/india_evaluation.md`; the labeling and
cash-flow rules this module enforces live in `docs/india_financial_methodology.md`
(commentary labeling, reported cash-flow frequencies, sign semantics, scope).

## 1. What was built

- `src/quarterline/sources/india/brief.py` — the India metric-reference
  allowlist, the India constrained-writer schema (C11 mirrored with the India
  allowlist), Python renderers, the India validation gate
  (`india-validation-v1`), and `generate_india_brief(...)` (fact card ->
  India narrative retrieval -> bounded context -> ONE generation + ONE repair
  pass -> gate -> `ok | partial | insufficient_evidence | provider_unavailable
  | refused`), with the §25 generation cache (successes only) and C12 events
  (`endpoint="india_brief"`).
- `prompts/brief_india/v1.txt`, `prompts/memo_india/v1.txt` — versioned,
  hashed prompt artifacts carrying every US §17 rule PLUS the India bindings
  (below).
- `src/quarterline/agent/tools.py` + `graph.py` — India tool bindings
  (`get_india_facts`, `search_india_filings`) and the India memo path
  (`market="india"` on `MemoRequest`); budgets, checkpoints, approval-gated
  export and the audit trail are the UNCHANGED US machinery.
- `POST /in/{issuer_id}/brief` (JSON or HTMX partial
  `partials/india_brief.html`), `POST /api/india-memos`,
  `GET /api/india-memos/{run_id}`,
  `POST /api/india-memos/{run_id}/approve-export` (reuses the US memo_review
  rendering). Degraded mode per SPEC §25: provider down -> facts + evidence +
  honest banner, never unvalidated prose.
- Tests: `tests/integration/test_india_generation.py` (12 gate behaviors),
  `tests/integration/test_india_memo_api.py` (11 workflow/route tests), seeded
  from committed INFY/HUL fixtures + a clearly-synthetic narrative corpus
  (`tests/india_generation_test_helpers.py`), all offline.

## 2. The India metric-reference allowlist

`INDIA_METRIC_REFERENCES` = the ten canonical concepts (`revenue_from_operations`,
`total_income`, `profit_before_tax`, `profit_after_tax`,
`profit_attributable_to_owners`, `exceptional_items`, `eps_basic`,
`eps_diluted`, `cash_flow_operations`, `capex`) + the six `india_*` metrics
(`india_revenue_yoy`, `india_revenue_qoq`, `india_pat_margin_owners`,
`india_pat_margin_group`, `india_exceptional_impact_pbt`,
`india_eps_growth_yoy`). US `METRIC_IDS` are unreachable: an India brief that
mentions `revenue_yoy` cannot even decode (pydantic validator), and after the
single repair pass the pipeline fails in a controlled way — facts + evidence,
never prose.

**llm/ duplication, reported honestly:** `llm/schemas.py` hardcodes the US
`METRIC_IDS` inside the `Brief`/`MetricMention` pydantic validators, and
`llm/**` internals were not modifiable in this wave. `brief.py` therefore
mirrors contract C11 India-side (`IndiaMetricMention`/`IndiaBrief`/
`IndiaMemoOutput` + `india_brief_json_schema`/`india_memo_json_schema`) with
the India allowlist injected exactly the way the US module injects its own.
What is duplicated: the schema shells, the JSON-schema builders and the
gate/report plumbing (the `ValidationReport` DTO itself is REUSED from
`llm.generation`). What is NOT duplicated: the shared gates —
`core.citations.validate_statement_citations`, `core.factcheck` extraction,
`core.advice_policy` — are imported and reused; only the India card semantics
are new. If a later wave generalizes `llm/schemas.py` to parametrize the
allowlist, this module collapses onto it with no behavioral change.

### Renderers (the model never types a number)

Like the US `metric_mention` expansion, every number the user sees is rendered
by Python from the India fact card at the brief's PRIMARY period identity
(the latest quarter identity; else the latest):

| template | valid for | rendering |
|---|---|---|
| `reported_value` | all ten concepts + margins + exceptional impact | INR: `₹48,211 Cr (₹ 482,110,000,000 exact filed amount)` (crore presentation + exact rupees); per-share: `₹19.17/share`; margins: percent with both inputs named; impact: crore + the per-issuer sign-semantics sentence |
| `reported_change` | ONLY `india_revenue_yoy`, `india_revenue_qoq`, `india_eps_growth_yoy` | percent (never percentage points — these are relative changes): `Revenue changed +3.9% versus the immediately preceding fiscal quarter (percent, not percentage points).` |
| `reported_level` | ONLY the two PAT margins | the SAME documented scale anchors the US gate uses (30% high / 10% moderate / 0-30% low / negative) |

Missing stays missing: a mention whose value is `missing`/`invalid` at the
identity is dropped with a recorded issue (`missing stays missing`), never
rendered as 0.

## 3. The India validation gate (`india-validation-v1`)

Checks in SPEC §18 order; dropped content is dropped WHOLE with recorded
reasons; dropped-but-not-empty -> `partial`; nothing useful ->
`insufficient_evidence`; advice-driven total loss -> `refused`:

| # | check | India semantics |
|---|---|---|
| 1 | schema_validity | `IndiaBrief` (extra=forbid; off-allowlist metric ids cannot parse) |
| 2 | status_validity | abstention statuses present nothing |
| 3 | period_label_echo_equality | **not a rating**: the model must echo the APPLICATION PERIOD LABEL (`period_label`, dates-only, e.g. `Q1 FY2026-27 (2026-04-01..2026-06-30)`) exactly — India issuers deliberately have no quarter label/score |
| 4-7 | citation checks | `core.citations` reused verbatim; bullets/risks require citations; expected ticker = the issuer's NSE symbol, expected period = the primary identity end (document period travels with the reference, so wrong-period pages drop) |
| 8 | metric_reference_validity | INDIA allowlist (defense in depth) + template structure table above |
| 9 | numeric_consistency | India card at the primary identity: India fiscal labels (`FY2026-27`, `2025-26`) masked first, then the documented core normalizer; percent claims match growth/margin ratios x100; money/plain claims match exact-rupees/crores/millions views of INR facts; per-share and pp claims have NO targets (unsupported by construction). **This is the mechanism that contradicts cash-flow fabrication:** where the fact layer's typed status for quarterly CFO is `not_present_in_ingested_sources`, no money claim can match at that identity and the statement drops |
| 10 | advice_policy_compliance | `core.advice_policy` reused verbatim |
| 11 | scope_attribution_consistency (India) | a statement naming the OPPOSITE scope (`standalone` on a consolidated brief) drops — deliberately strict: prose naming the other scope is treated as value attribution, and values never cross scopes (methodology §2) |
| 12 | commentary_attribution (India, deterministic half) | a bullet/risk whose citations are ALL management-commentary documents (`management_commentary` flag from the document row) must carry an attribution marker ("management stated...", "according to management", ...); otherwise it stated commentary as unattributed fact and drops. Open questions are exempt (questions, not assertions). **What stays prompt-level:** whether an attributed claim is TRUE is not decidable deterministically and is enforced by `prompts/brief_india/v1.txt` / `prompts/memo_india/v1.txt` ("presentations, transcripts and press releases are MANAGEMENT STATEMENTS... attribute them") — documented, not simulated |

Consolidated/standalone confusion (mission item) is covered by BOTH: the
prompt rule makes the scope binding, and check 11 deterministically drops
opposite-scope prose; a scripted brief calling standalone values consolidated
fails the gate (tested).

## 4. Cache (SPEC §25 pattern)

`_cache_key` = SHA-256 over company(issuer), period, scope, fact-card hash,
evidence/context hash, provider, model, prompt version + hash, generation
settings, `INDIA_VALIDATION_VERSION`, `FACTCHECK_VERSION`,
`ADVICE_POLICY_VERSION`, `NORMALIZATION_VERSION` (india-normalization-v1),
`FORMULA_VERSION_INDIA_METRICS` (india-metrics-v1). Successes only
(`ok`/`partial`) are persisted; a cache hit never calls the provider
(tested, idempotent outcome).

## 5. Memo workflow India support

`MemoRequest.market: Literal["us","india"] = "us"` (additive; US behavior and
budgets untouched). With `market="india"`:

- plan = `get_india_facts` (typed issuer_id/period_end/scope ->
  `build_india_fact_card`, read-only) + `search_india_filings` (SearchService
  with the issuer ticker filter; mode `brief` -> the brief page preset,
  otherwise all India narrative kinds) (+ `get_prices` on market-context
  language, as in the US); `export_memo` reused as-is;
- writer = `prompts/memo_india/v1.txt` + `IndiaMemoOutput` (india_* mentions);
- gate = the India checks above per memo section (evidence_gaps stays
  citation-exempt);
- approval + export + checkpoints + audit trail = the UNCHANGED US machinery
  (hash-tie approval enforced; tested against tampered content).

India drafts carry `metric_mentions=[]` in the stored `MemoDraft` (that
field's item type is US-allowlist-typed); the rendered sentences travel in
`metric_facts` and the mention decisions in `validation_results` — recorded
here rather than hidden.

## 6. Real-model run (measured, honest; qwen3:4b via Ollama)

Reproduced by `scripts/india_brief_real_run.py [timeout_s]` against the dev
store (`storage/quarterline.db`, the IND-7 real-embedding narrative index) for
INFY Q1 FY27 consolidated. Two measured runs on 2026-09-12:

**Run 1 (default 600 s provider timeout):** `provider_unavailable` —
`generation provider unavailable: ollama generation failed: timed out`;
total latency 605,320.9 ms. Honest degraded mode: facts (16 rows) + 6 evidence
windows, `brief is None` — no prose.

**Run 2 (1500 s timeout) — the model answered, and the GATE decided.**
Verbatim outcome (also in `storage/logs/india_brief_real_run2.log`):

```
issuer: IN-INFY | scope: consolidated | period: Q1 FY2026-27 (2026-04-01..2026-06-30)
provider: ollama | model: qwen3:4b
STATUS: insufficient_evidence        cache_hit: False        run_id: 5017d6b595234b01a6039b8dbd239424
reasons (verbatim):
  - check[5]: bullet[0] declared evidence 'ev-2b22c820ac26' never appears in the statement text as [evidence_id]
  - check[5]: risk[0] declared evidence 'ev-2b22c820ac26' never appears in the statement text as [evidence_id]
  - check[5]: open_question[0] declared evidence 'ev-2b22c820ac26' never appears in the statement text as [evidence_id]
  - check[8]: template 'reported_level' is not structurally valid for metric 'profit_before_tax'
  - check[8]: template 'reported_level' is not structurally valid for metric 'profit_after_tax'
gate: india-validation-v1
  check[1] schema_validity: PASS            check[7] citation_sentence_format: PASS
  check[2] status_validity: PASS            check[8] metric_reference_validity: FAIL (dropped 2)
  check[3] period_label_echo_equality: PASS check[9] numeric_consistency: PASS
  check[4] citation_existence: PASS         check[10] advice_policy_compliance: PASS
  check[5] citation_supplied_context: FAIL (dropped 3)
  check[6] citation_company_period: PASS    check[11] scope_attribution: PASS
                                            check[12] commentary_attribution: PASS
brief bullets retained: 0    metric_facts: []    evidence windows supplied: 6
total latency ms: 144107.6   (run event: input_tokens est. 4431, output_tokens 1941
                              provider-reported, json_valid_first_pass TRUE,
                              repair_used FALSE, generation 141.6 s, retrieval 2.4 s)
```

Reading (consistent with the US-side measured finding for a 4B model):
qwen3:4b returned schema-valid JSON on the FIRST pass (no repair needed), echoed
the application period label correctly, cited only REAL supplied evidence ids
(checks 4/6/7 pass — no invented or wrong-period citations), and issued no
advice — but declared citations without attaching them as `[evidence_id]` at
sentence ends (check 5) and used `reported_level` on level-value metrics where
only `reported_value` is structural (check 8). Every generated statement was
dropped as a whole; nothing usable survived, so the outcome is the honest
`insufficient_evidence` — the user sees the fact card and the 6 evidence
pages, never the unvalidated prose. This is an acceptable, recorded result;
no brief was silently rewritten or loosened to make the model pass.

## 7. Honest limitations

- The rendered numbers are exactly as verified as the fact layer beneath
  them: HUL P&L rows still carry `requires_manual_review` (review packet),
  and no brief re-adjudicates that — review statuses travel in fact
  provenance, not in prose.
- The INFY quarterly-CF page misses top-5 retrieval (IND-7 §6): the brief's
  cash-flow honesty comes from the FACT layer (check 9 + coverage statuses),
  which is the documented design, not from RAG recall.
- Commentary truthfulness (check 12's non-deterministic half) and
  citation-sentence relevance remain prompt-level; the gate is necessary, not
  sufficient (SPEC §2.2.6).
- The real-model result below is a single local run on a 4B model — a
  measurement of this environment, not a benchmark.
