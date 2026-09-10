# Quarterline — Financial Methodology

What the numbers mean, how they are derived, and where each rule is enforced.
Every rule cites the SPEC section it implements and the test file proving it.
All arithmetic uses Python `Decimal` (SPEC §11); units travel with every value
(`MetricValue.unit`); missing inputs produce `status=missing`, never zero
(SPEC §2.1.5–2.1.6).

## 1. Concept mapping — ordered tag fallback (SPEC §10.1)

`data/tagmap_us.yml` (loaded by `sources.sec.tagmap.load_tagmap`, contract C4)
defines ordered fallbacks, e.g.:

- `revenue`: RevenueFromContractWithCustomerExcludingAssessedTax →
  SalesRevenueNet → Revenues → RevenueFromContractWithCustomerIncludingAssessedTax
- `capex_outflow`: PaymentsToAcquirePropertyPlantAndEquipment
- `cfo`: NetCashProvidedByUsedInOperatingActivities
- plus gross_profit, operating_income, net_income, diluted_eps, shares_diluted,
  cash, total_assets, total_liabilities, long_term_debt, current_assets,
  current_liabilities.

Selection applies tag priority **only after** filtering for compatible units,
scope, and economic period — a higher-priority annual value can never displace
a lower-priority valid quarterly value. Missing concepts are logged, never
filled by an LLM.
Proven by: `tests/unit/test_tagmap.py`, `tests/unit/test_normalization.py`
("ordered tag fallback", "quarterly versus annual collision prevention",
"unit-incompatible tag" fixture).

## 2. Period classification (SPEC §10.2)

`core/periods.py`:

- Duration-band inference: ~13/14-week spans → `quarter`, ~52/53-week →
  `annual`, intermediate cumulative spans → `year_to_date`, instants →
  `instant`. **No 90-day-quarter assumption.**
- FYE-month fiscal math with 53-week spill handling; fiscal calendars that
  differ from calendar quarters are supported.
- Source `fy`/`fp` labels are stored (`context_metadata_json`,
  `source_fy`/`source_fp`) but never trusted as the economic period of a
  comparative observation.
Proven by: `tests/unit/test_periods.py` (duration bands, 53-week year, Oct FYE
synthetic fixture), `tests/unit/test_normalization.py` (instant vs duration).

## 3. Quarterly derivation (SPEC §10.3)

For compatible additive flow concepts only:

```text
Q2 = six-month YTD − Q1
Q3 = nine-month YTD  − six-month YTD
Q4 = annual          − nine-month YTD
```

Conditions: same company, same concept and unit, same reporting scope,
compatible accounting basis, compatible source revisions (revision vintage
attached to the value's FIRST filing with a ≤100-day window, so unchanged
comparatives don't manufacture mismatches), correct consecutive fiscal periods.
Both inputs + derivation version are preserved (`fact_lineage` roles
`annual_total` / `prior_ytd`; `derivation_method` on the normalized fact).

**Never derived** (SPEC §10.3 "Do not derive this way"): EPS, weighted-average
diluted shares, margins, ratios, instant balance-sheet values. If direct
quarterly EPS/shares cannot be established they stay missing.
Proven by: `tests/unit/test_normalization.py` ("YTD-to-quarter derivation",
"Q4 derivation", "No subtraction-based derivation of EPS or weighted shares",
restatement/revision-vintage cases), `tests/integration/test_facts_aapl_fixture.py`
(real AAPL fixture: Q4s derived from annual−9m with lineage).

## 4. Historical selection (SPEC §10.4)

`latest_available` (UI default) and `as_of(timestamp)`; as-of never uses
information filed after the timestamp (reconstructs direct + derived facts
from observations/lineage at the timestamp).
Proven by: `tests/unit/test_normalization.py` + `tests/integration/test_facts_pipeline.py`
("As-of selection excludes future filings", "Restatement selection").

## 5. Ratios (SPEC §11.1)

Formulas, units, and sign semantics (`core/ratios.py`, `formula_version` =
`ratios-v1`; capex path `capex-semantics-v1`):

| Metric | Formula | Unit | Notes |
|---|---|---|---|
| gross_margin | gross_profit / revenue | pct | denominator ≤ 0 → null + status |
| operating_margin | operating_income / revenue | pct | |
| net_margin | net_income / revenue | pct | |
| capex_outflow | −reported_capex (reported negative outflow → positive magnitude) | usd | Reported sign preserved in the observation; positive reported value (inflow semantics) → flagged for review, never hidden by `abs()` |
| fcf | cfo − capex_outflow | usd | capex as positive outflow magnitude |
| fcf_margin | fcf / revenue | pct | |
| current_ratio | current_assets / current_liabilities | ratio | |
| long_term_net_debt_proxy | long_term_debt − cash | usd | Explicitly a **proxy**, labeled as such; excludes short-term debt, leases, investments |

Proven by: `tests/unit/test_ratios.py` ("Capex/FCF sign", "Zero and negative
denominator handling", proxy labeling).

## 6. Growth (SPEC §11.2)

- `growth = current / prior − 1` for **positive prior values only**; prior ≤ 0
  → percentage growth is **null** with notes (`turned_profitable` /
  `turned_loss_making` per deterministic rules), absolute change shown.
- Margin changes are **percentage points** (`operating_margin_change_pp`).
- YoY compares matching fiscal quarters (prior-year same quarter), never
  arbitrary four-row offsets when periods are missing.
Proven by: `tests/unit/test_ratios.py` ("YoY matching with missing quarters",
growth-null cases), `tests/unit/test_quarter_label.py`.

## 7. Cash conversion (SPEC §11.3)

`cfo_to_net_income = cfo / net_income`, unit `ratio`; **unsuitable** when net
income ≤ 0 — CFO and net income are shown separately instead.
Proven by: `tests/unit/test_ratios.py` (unsuitable-NI cases).

## 8. Quarter label (SPEC §12.1)

Five signals, each `true`/`false`/`unknown` (`core/quarter_label.py`,
`formula_version` = `quarter-label-v1`):

1. revenue_yoy > 0
2. operating-margin YoY change ≥ 0 pp
3. cfo/net_income ≥ 0.8 (only valid when net income > 0)
4. fcf > 0
5. shares_yoy ≤ 2%

**Precedence (exact order)**:
1. Valid CFO/net income < 0.4 for two consecutive quarters → `Weak` (veto first).
2. Fewer than three available signals → `Insufficient data`.
3. ≥ 3 true → `Strong`.
4. ≥ 3 false → `Weak`.
5. Otherwise → `Mixed`.

Every input, rule, and the applied rule's caption are exposed in the UI with
the fixed caption "Rule-based quarterly performance label. Not a
recommendation or forecast." Unknown signals are never treated as false
(SPEC §2.1.7).
Proven by: `tests/unit/test_quarter_label.py` ("Veto precedence", "Missing
signals remain unknown", "Insufficient-data state"),
`tests/integration/test_ui_company.py` (label panel renders all five signals).

## 9. Experimental fundamental score (SPEC §12.2)

Named **experimental fundamental score**, never a validated investment score
(`compute_fundamental_score`):

| Component | Weight | Inputs (scale bounds) |
|---|---:|---|
| profitability | 25 | `scale(operating_margin, 0, 0.30)` |
| cash_conversion | 20 | `scale(cfo_to_net_income, 0.4, 1.2)` when valid (net income > 0) |
| growth | 20 | `scale(revenue_yoy, −0.10, 0.20)` |
| liquidity | 15 | `scale(current_ratio, 0.5, 2.0)` |
| capital_discipline | 10 | mean of `scale(fcf_margin, −0.05, 0.15)` and `100 − scale(shares_yoy, 0, 0.05)` |

- `scale(x, low, high) = 100 × clamp((x − low) / (high − low), 0, 1)`.
- Total defined weight = 90; component scores are **rescaled over available
  weights** when inputs are missing.
- **60% floor**: if available weight < 54 (60% of 90), no aggregate score is
  returned — status `insufficient_weight` with an explanatory note.
- Score clamped to 0–100; valuation-vs-self deferred until aligned historical
  market data exists.
Proven by: `tests/unit/test_quarter_label.py` ("Weight rescaling", "Score
clamping", "Formula-version persistence"), `tests/unit/test_quality.py`
(per-quarter coverage feeds `data_coverage`).

## 10. Provenance and coverage

- Every normalized/derived value carries lineage (`fact_lineage`) or
  `input_fact_ids_json` + `formula_version` (SPEC §2.1.8, §10.3, §11);
  `core/provenance.build_fact_card` / `explain_metric` expose it.
- `core/quality.py` computes per-quarter coverage
  (direct / derived / missing fraction) → `data_coverage` metric used by the
  screener's `minimum_data_coverage` filter.
Proven by: `tests/integration/test_facts_aapl_fixture.py` ("Provenance
completeness"), `tests/api/test_api_json.py` (provenance JSON contract).

## Known limitation (reported, not hidden)

`core/provenance.explain_metric` cannot resolve input lineage for growth-style
metrics (`revenue_yoy`, `shares_yoy`, `operating_margin_change_pp`) whose
`derived_from` refs are `<metric_id>:current`-style; the company page applies
a display-level enrichment until the authoritative core fix lands (see
`docs/implementation_log.md` waves F4/F1 and `docs/PLAN.md` §5 deferred list).
