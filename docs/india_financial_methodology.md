# India financial methodology (IND-2)

Status: parsing-feasibility milestone, 2026-09-11. This document encodes the
normalization rules that the India pipeline (`src/quarterline/sources/india/`)
implements and that later derivation/scoring waves MUST keep. Every rule here is
traceable to observed evidence in `docs/india_source_audit.md` (IND-1 §5, §9) or
to the committed fixtures (`tests/fixtures/india/`). Nothing on this page is
inferred from column labels, press commentary, or aggregator sites.

## 1. Units rule (lakh / crore, EPS exemption)

- All SEBI `in-capmkt` fact values are **full rupees**. The instance's
  `LevelOfRounding="Crores"` trait describes the *presentation* scale of the
  human-readable rendering only; `339860000000` IS ₹33,986 Cr. The parser stores
  the value exactly as filed (`units.normalize_amount` is a passthrough) and
  carries the trait in `context_metadata_json.rounding_trait`. The trait is
  NEVER applied as a multiplier.
- Standard Indian units: `LAKH = 100,000`, `CRORE = 10,000,000`
  (`units.py`). Display strings are parsed only with an explicit scale
  (`"₹1,234 Cr"`) or an explicitly declared scale (from a detected
  `"Rs in Crores"` header). A display string with neither raises
  `AmbiguousScaleError` — ambiguity surfaces, never guesses.
- **EPS and per-share values NEVER inherit a lakh/crore multiplier.** Concepts
  `eps_basic` / `eps_diluted` carry the `per_share` flag in the concept map;
  facts in per-share units (`unitRef INRPerShare`, logical `INR/shares`) are
  stored with `unit="INR/share"` and the raw per-share value (`19.19` stays
  `19.19`). A "₹ in Crores" header on the same page as EPS does not scale EPS.

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

## 4. Cash-flow frequency rule (IND-1 finding, now encoded)

For the exchange-filed record, cash-flow statements are **annual-only**, filed
with the March-quarter results. Encoded consequences:

- `cash_flow_operations` (`CashFlowsFromUsedInOperatingActivities`) and `capex`
  (`PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities`) are
  mapped **only for the annual (Q4) instance**. Quarterly exchange instances
  carry zero cash-flow concepts (verified: INFY Q1 FY27 and HUL Q1 FY27 have
  none; HUL's Q1 workbook has no cash-flow sheets).
- **Never fabricate a quarterly cash-flow statement** for an India issuer, and
  **never treat the annual CF as a Q4 quarter CF** (the annual value shares
  31-Mar with Q4 but is period-kind `annual`).
- Infosys is the documented exception: quarter-granularity CF exists only in
  company-IR condensed-statement PDFs (Q1 FY27 net cash from operating
  activities ₹9,330 Cr, p. 6). Ingesting it is a later PDF milestone; until then
  India cash-flow metrics are annual-cadence.
- H2 cash flow, if ever derived, comes only from compatible annual − H1 inputs
  and is labeled **H2** — never "Q3+Q4" as separate quarters.
- HUL's FY2025-26 includes discontinued operations (ice-cream demerger): the
  annual `ProfitLossForPeriod` (₹1,50,590 Cr) and
  `ProfitLossForPeriodFromContinuingOperations` (₹1,06,670 Cr) differ materially
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
every instance; `cash_flow_operations, capex` only in annual instances. US
concept IDs (`data/tagmap_us.yml`) are never used for India facts. Uncertain
fallback mappings are flagged in `data/tagmap_india.yml` (`# uncertain:`
comments) and `docs/india_source_audit.md` §9.1. Unknown tags resolve to an
explicit unmapped result and are counted in the ingest report — never silently
dropped.
