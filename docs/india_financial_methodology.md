# India financial methodology (IND-2, hardened in IND-3, extended in IND-4)

Status: IND-2 parsing-feasibility milestone; IND-3 (2026-09-11) financial-correctness
hardening per the reviewer corrections (claim audit:
`docs/india_source_audit.md` §10; review packet: `docs/india_reconciliation_review.md`);
IND-4 (2026-09-09) canonical facts, lineage, and versioned metrics.
This document encodes the normalization rules that the India pipeline
(`src/quarterline/sources/india/`) implements and that later derivation/scoring
waves MUST keep. Every rule here is traceable to observed evidence in
`docs/india_source_audit.md` (IND-1 §5, §9, §10) or to the committed fixtures
(`tests/fixtures/india/`). Nothing on this page is inferred from column labels,
press commentary, or aggregator sites.

## 1. Units and precision rules (lakh / crore, EPS exemption)

- All SEBI `in-capmkt` fact values are **full rupees**. The instance's
  `LevelOfRounding="Crores"` trait describes the *presentation* scale of the
  human-readable rendering only; `339860000000` IS ₹33,986 Cr. The parser stores
  the value exactly as filed (`units.normalize_amount` is a passthrough) and
  carries the trait in `context_metadata_json.rounding_trait`. The trait is
  NEVER applied as a multiplier — not once, not twice.
- **`decimals="-7"` is PRECISION, not scale**: it declares the source's own
  rounding (values are exact to ₹1,00,00,000). Displayed crore figures must
  reconcile EXACTLY with that precision (`482110000000` → ₹48,211 Cr, integer
  crores). Stored facts are never changed to match a rounded display.
- Standard Indian units: `LAKH = 100,000`, `CRORE = 10,000,000`
  (`units.py`). Display strings are parsed only with an explicit scale
  (`"₹1,234 Cr"`) or an explicitly declared scale (from a detected
  `"Rs in Crores"` header). A display string with neither raises
  `AmbiguousScaleError` — ambiguity surfaces, never guesses.
- **EPS and per-share values NEVER inherit a lakh/crore multiplier.** Concepts
  `eps_basic` / `eps_diluted` carry the `per_share` flag in the concept map;
  facts in per-share units (declared unit `INRPerShare`, logical `INR/shares`,
  `decimals="INF"`) are stored with `unit="INR/share"` and the raw per-share
  value (`19.19` stays `19.19`). A "₹ in Crores" header on the same page as
  EPS does not scale EPS.
- **Unit semantics are verified, not assumed (IND-3).** A mapped fact whose
  declared unit is not `INR` (money concepts) or `INR/shares` (per-share
  concepts) is skipped and counted as `skipped_unknown_unit` — share counts,
  percentages, or any unknown unit semantic go to review, never into INR.

## 2. Scope rules (consolidated vs standalone)

- **Consolidated is the default series.** Metrics, growth, and scores are built
  from `reporting_scope="consolidated"` facts only.
- **Never substitute a standalone value into a consolidated series** (and vice
  versa), even when the other scope is missing for a period. Missing stays
  missing (SPEC 2.1.5/2.1.6).
- Scope is a **per-filing property**: the exchange files two separate XBRL
  instances per period. Inside an instance the scope is declared by
  `NatureOfReportStandaloneConsolidated`; the import path cross-checks the
  declared scope against the import argument and refuses mismatches.
- Observations of different scopes are distinct identities (the reporting scope
  is part of the observation hash). Consolidated and standalone values of the
  same concept+period can coexist without ever being mixed.

## 3. Period rules (Indian fiscal calendar)

- FY runs **1 April → 31 March**. `FY2026-27` ends 2027-03-31 (numeric
  `fiscal_year` = ending year). Q1 ends 30 Jun, Q2 30 Sep, Q3 31 Dec,
  Q4/annual 31 Mar.
- Period kind is derived **only from the context (start, end) dates**:
  `quarter` (~3 months), `year_to_date` (6/9-month cumulative; also 12-month
  periods not ending 31 March), `annual` (12 months ending 31 March),
  `instant`. Instance-declared strings (`ReportingQuarter="First quarter"`,
  `TypeOfReportingPeriod="Quarterly"`) are metadata, never classification.
- **A 9-month cumulative value is not Q3.** Annual, YTD, and quarter values
  sharing an end date remain separate observations with separate period kinds
  (SPEC 2.1.9) and can never be mixed in one series. Deriving a standalone Q3
  from 9M − 6M is a *derivation-layer* operation with explicit lineage — never a
  relabeling of the 9M value.
- Prior-year comparison quarter = same start/end one year earlier
  (`periods.prior_year_quarter`).
- **Source labels vs application labels (IND-3):** the issuer's own labels
  (Infosys' "quarterly results, June 2026 quarter" style; the instance
  qualifiers `ReportingQuarter="First quarter"`,
  `TypeOfReportingPeriod="Quarterly"`) are SOURCE metadata. The application's
  `Q1 FY2026-27` style label is Quarterline's OWN presentation layer, derived
  from the exact context dates only. Calendar dates are the primary truth;
  every displayed label must be reproducible from
  `period_start`/`period_end` (see `periods.period_label`).

## 4. Cash-flow rule (IND-3 corrected policy — supersedes IND-2 §4)

The IND-2 rule "CFO/capex map only from annual instances" was **wrong (too
restrictive)**. The actual requirement is: **do not invent quarterly cash flow
when the source does not report it** — which is not the same as restricting
extraction to annual documents. The corrected policy:

- **Reported observations** are accepted from an identified official document
  whenever the concept is established, the exact reporting duration is known,
  scope/units are known, and extraction provenance is preserved. Eligible
  reported durations (`cash_flow.REPORTED_CF_DURATIONS`): `quarter`,
  `half_year`, `nine_month_ytd`, `annual`, `other_duration`. Exact start/end
  dates are stored regardless of label. A quarterly CFO in an Infosys IR
  condensed-FS PDF is legitimate and IS extractable (text-level extraction with
  page provenance via `pdf_results.extract_cash_flow_statement`; any table
  ambiguity yields `requires_manual_review` — never a guess).
- **Derived observations** come ONLY from checked subtraction
  (`cash_flow.derive_by_subtraction`): the concept must be additive, the
  cumulative periods must share the correct starting boundary, units/scope/
  revision status must match, and BOTH source observations are preserved.
  Examples: `annual − H1 = H2` (labeled **H2**, never Q4);
  `nine_month_ytd − H1 = Q3`. Permitted only when the underlying observations
  actually exist and pass the compatibility checks
  (`IncompatibleDerivation` otherwise).
- **NEVER**: divide annual CFO by four, divide half-year by two, treat H2 as
  Q4, assume a 6-month observation is H1 without checking dates, or
  annualize/trailing-figure from incomplete coverage. No division code path
  exists in `cash_flow.py` by construction (structurally tested).
- **Missing-data statuses (IND-3, distinct, never zero)** —
  `data_status.MissingDataStatus`: `not_present_in_ingested_sources`,
  `source_not_ingested`, `extraction_failed`, `requires_manual_review`,
  `not_applicable`. Where older documents said a company "has no quarterly
  CF", the correct statement is "quarterly CF **not present in the ingested
  sources**" — a statement about our corpus, not about the company. Absence is
  never rendered as 0 and never raised as an error.
- Verified availability in the acquired corpus: INFY — annual CFO/capex
  (Q4 exchange instance) AND quarterly CFO (IR condensed-FS PDF p.6);
  HUL — annual CFO/capex only (Q4 exchange instance); HUL Q1 has no CF in any
  ingested source.
- HUL's FY2025-26 includes discontinued operations (ice-cream demerger): the
  annual `ProfitLossForPeriod` (₹15,059 Cr) and
  `ProfitLossForPeriodFromContinuingOperations` (₹10,667 Cr) differ materially
  and are kept as separate observations; any continuing-vs-total derivation must
  choose explicitly.

## 5. Revision selection policy

- The revision status (`Original` / `Revised` + `revised_Date` +
  `revision_Remark`) lives in the exchange listing metadata, not in the XBRL
  instance. It is carried (`FilingMeta`, `.filing.json` sidecar) — never
  inferred from document content. No revised submission existed in the acquired
  filings; revised handling is designed-for and covered by clearly-synthetic
  tests only.
- **latest_available view:** the latest *publication* per (issuer, scope,
  period-end) wins (`revisions.select_latest`).
- **as_of view:** filings published after the as-of timestamp are excluded; the
  value in force at the timestamp is used (SPEC 10.4).
- **Both originals and revisions are retained** in `fact_observations` — the
  same value re-reported hashes to the same observation (idempotent), a
  genuinely revised value hashes differently and is preserved alongside the
  original (SPEC 2.1.10). Selection happens at the normalized/derived layer,
  never by deleting evidence.

## 6. No-EBITDA-conflation rule

The `in-capmkt` P&L does not publish an "EBITDA" line. India EBITDA-style
quantities must never be read off a reported line or conflated with
`ProfitBeforeExceptionalItemsAndTax`. Any EBITDA-style derivation must be built
explicitly from mapped components (e.g. `ProfitBeforeTax` +
`FinanceCosts` + `DepreciationDepletionAndAmortisationExpense` +
`ExceptionalItemsBeforeTax`, per the derivation wave's formula version) with
lineage, and must handle the discontinued-operations split (rule 4) explicitly.
Until such a derivation exists, the app displays no India "EBITDA" figure.

## 7. Mapped concept inventory

`revenue_from_operations, total_income, profit_before_tax, profit_after_tax,
profit_attributable_to_owners, exceptional_items, eps_basic, eps_diluted` map in
every instance; `cash_flow_operations, capex` are accepted at their ACTUAL
reported duration (IND-3 corrected policy, §4 — in the acquired corpus: annual
from the Q4 instances for both issuers, plus INFY's reported quarterly CFO from
the IR condensed-FS PDF). `revenue_from_operations` and `total_income` are kept
DISTINCT (every acquired period differs: total income includes other income);
`profit_after_tax` and `profit_attributable_to_owners` are kept DISTINCT
(non-controlling interests). US concept IDs (`data/tagmap_us.yml`) are never
used for India facts. Uncertain fallback mappings are flagged in
`data/tagmap_india.yml` (`# uncertain:` comments) and
`docs/india_source_audit.md` §9.1. Unknown tags resolve to an explicit unmapped
result and are counted in the ingest report — never silently dropped; the
INFY undimensioned `SegmentRevenue` total (a segment-disclosure total that
numerically equals P&L revenue) stays unmapped so a segment fact can never
masquerade as the company total.

## 8. Canonical facts and metrics (india-normalization-v1 / india-metrics-v1, IND-4)

The reconciled observations become **canonical facts** (`normalized_facts`) and
**versioned metrics** (`derived_metrics`) — the data layer IND-5's indicator
panels read. Every canonical fact carries provenance back to observations via
`fact_lineage` (role `direct_source`); every metric carries `formula_version`
and input fact ids; anything not computable from ingested observations carries a
typed missing-data status (§4) — never zero, never guessed. **No aggregate score
exists for India issuers** (no scores, no recommendations): IND-5 renders these
indicators and their missing statuses as-is.

### 8.1 Selection policy (`india-normalization-v1`)

- **Scope**: consolidated is the default research series. Standalone
  observations are normalized into facts under their OWN `reporting_scope` and
  are never substituted into a consolidated series (or vice versa) — the scope
  is part of the fact's unique period identity.
- **Recency**: the latest publication (`filed_at`) wins per (issuer, scope,
  concept, period identity). Older filings' observations are preserved in
  `fact_observations`; lineage and history remain queryable (the superseded
  observation keeps its lineage row). Policy label on every fact:
  `selection_policy="india-latest-publication"`.
- **Period identity**: (period_start, period_end, period_kind, reporting_scope)
  — quarter, annual, and any future half_year/nine_month_ytd facts coexist
  without collision. Annual facts carry `fiscal_quarter = NULL` even when they
  end 31 March (which is also Q4's end date); only quarter-kind facts carry a
  `Q1..Q4` label.
- **Tag fallbacks**: where a concept has ordered fallback tags
  (`data/tagmap_india.yml`), observations from the same publication are selected
  in tagmap-priority order and the lineage row records which tag won (via the
  winning observation's `original_tag`). Examples: `profit_before_tax` selects
  `ProfitBeforeTax` (INFY FY26: 39,995 Cr) over the fallback
  `ProfitBeforeExceptionalItemsAndTax` (41,284 Cr, retained as an observation
  and used as the exceptional-impact cross-check); `capex` selects the PP&E
  purchase over the intangibles fallback.
- **Concept namespace**: India canonical concepts are exactly the
  `data/tagmap_india.yml` allowlist and never collide with US `METRIC_IDS`
  (asserted by tests).
- **Data quality**: every canonical fact carries the IND-3 reconciliation
  review status of its winning observation — `agent_checked_against_document`
  where the packet verified the rendered document, `requires_manual_review`
  where the packet left the row human-review-pending (all HUL P&L rows from the
  interleaved rendered pages; INFY FY26 intangible capex), `None` where the
  packet does not cover the observation. Review status is CARRIED, never
  resolved: e.g. HUL Q1 FY27 revenue stores the XBRL value ₹17,341 Cr with
  `requires_manual_review` while the rendered-P&L tension (17,341 vs the
  scrambled 17,149) awaits Lekhraj's reading (review packet §7.1).

### 8.2 Metric definitions (`india-metrics-v1`)

All arithmetic is `Decimal`. Every metric returns
`{value, unit, formula_version, status, input_fact_ids, notes}` and is
persisted to `derived_metrics` (quarter identities under the plain
`india_*` id; non-quarter identities under a period-kind suffix `@annual` /
`@ytd` / `@other`, because `derived_metrics` keys on `period_end` alone and the
Q4 quarter + annual share 31 March). Missing metrics are PERSISTED as
`status="missing"` with `value = NULL`.

| Metric | Formula (exact numerator / denominator) | Notes |
|---|---|---|
| `india_revenue_yoy` | revenue_from_operations(current quarter) / revenue_from_operations(matching prior-year quarter) − 1 | Ingested facts ONLY. Prior-year quarters are not ingested for either issuer, so the metric returns `status="missing"` with the explanation ("not present in ingested sources; available in the issuer's IR PDF comparative column — ingest it as an observation with pdf_text provenance"). Never a fabricated comparative. |
| `india_revenue_qoq` | revenue_from_operations(current quarter) / revenue_from_operations(immediately preceding fiscal quarter) − 1 | Q4 FY26 → Q1 FY27 computes (INFY: 48,211/46,402 − 1). Missing when the prior quarter is not ingested; never annualized, interpolated, or annualized from incomplete coverage. |
| `india_pat_margin_owners` | profit_attributable_to_owners / revenue_from_operations | Labelled with both inputs. NEVER total_income; the group margin is the SEPARATE metric below. |
| `india_pat_margin_group` | profit_after_tax / revenue_from_operations | Deliberately separate so owners' and group PAT margins are never conflated. |
| `india_exceptional_impact_pbt` | impact = exceptional_items (= profit_before_tax − ProfitBeforeExceptionalItemsAndTax); share of PBT = impact / profit_before_tax | `value` = impact in INR; the share travels in notes. When the instance reports both variants the identity impact == PBT − PBIT is cross-checked (a failed identity is `invalid`, never forced). SIGN SEMANTICS PER ISSUER, preserved as reported and never normalized: INFY FY26 stores **−1,289 Cr** (an expense reducing PBT; the rendered Reg-33 P&L prints it as a positive 1,289 deducted from PBIT: 41,284 − 1,289 = 39,995); HUL Q4 FY26 stores **+247 Cr** (a demerger gain increasing PBT; HUL renders gains/losses with the same sign as stored). |
| `india_eps_growth_yoy` | eps_diluted(current quarter) / eps_diluted(matching prior-year quarter) − 1 | Same ingested-facts rule as revenue YoY, applied consistently to BOTH issuers (no computing one issuer's YoY while silently reporting the other's as equivalent without its missing status). |

**Design decision (comparatives)**: prior-year comparative columns in the
issuers' rendered PDFs are NOT ingested as observations in this milestone
(HUL's printed comparatives were never machine-reliable to extract — the
scrambling finding; nothing under them is ingested). Consequently ALL YoY
metrics return the typed missing status for both issuers, with the note naming
exactly what would enable them (comparative observations with
`extraction_method=pdf_text`, page provenance, and
`agent_checked_against_document` review status). If a later wave ingests them,
it must do so for both issuers under the same provenance standard.

**Denominator rules (US convention)**: zero denominator → `value=NULL`,
`status="invalid"`, note; negative denominator (or negative growth prior) →
`value=NULL`, `status="unsuitable"`, note with the absolute change; missing
input fact → `status="missing"` with the concept named.

**Cash-flow cells** are coverage entries, not derived metrics: canonical CF
facts exist only at REPORTED frequencies — currently annual CFO/capex (FY26)
for both issuers plus INFY's reported Q1 FY27 quarterly CFO (IR condensed-FS
PDF p.6, extraction `pdf_text`, review `agent_checked_against_document`). HUL
quarterly CF reports `not_present_in_ingested_sources`; H2 for either issuer
reports `source_not_ingested` (H2 = annual − H1 requires an H1 CF observation;
nothing is ever divided).

### 8.3 Fact card and coverage (IND-5 contract)

`factcard.build_india_fact_card(session, issuer_id, period_end=None,
scope="consolidated")` returns a pydantic card with: verified issuer identity;
period identities with exact dates, the APPLICATION label (Quarterline's own,
from dates only) AND the SOURCE label (instance qualifiers
`ReportingQuarter`/`TypeOfReportingPeriod`, carried not trusted); canonical
facts with provenance (observation ids, artifact, accession/URL, published
date, audit + revision status, review status, winning tag); every metric with
its formula version, status, input fact ids and notes; a per-concept coverage
block (present / missing + the exact missing status); and the blocked
cash-flow derivations. `factcard.coverage_report(session, issuer_id)` spans all
ingested periods. The card carries NO score — the fixed note states that
downstream panels render indicators only. CLI: `verify india-facts --issuer
IN-INFY [--scope consolidated] [--period-end YYYY-MM-DD]` (registry key
`verify:india-facts`).
