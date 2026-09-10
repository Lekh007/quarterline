# India reconciliation review packet (IND-3)

Status: ready for human review, 2026-09-11. Scope: **Infosys (`IN-INFY`) and
Hindustan Unilever (`IN-HINDUNILVR`) only** — the two issuers whose identifiers
are verified and whose filings are acquired. This packet accompanies
`data/validation/india_reconciliation.csv` (79 rows, 25-column contract) and the
corrected methodology (`docs/india_financial_methodology.md` §4). Claim-audit
corrections with their rationale are recorded in `docs/india_source_audit.md`
§2, §5, §9.1 and §10 — nothing was silently edited.

Machine-readable artifact: `data/validation/india_reconciliation.csv`
(regenerate with `quarterline.sources.india.reconcile.write_validation_csv()`).
The IND-2 file `data/india_reconciliation.csv` is superseded (it now contains
only a supersession pointer).

## 1. Review-status vocabulary

| Status | Meaning |
|---|---|
| `automated_check_passed` | Structural/precision checks passed; no rendered document compared. |
| `agent_checked_against_document` | The agent extracted the cited page and confirmed the displayed value against the structured fact (page + displayed value recorded per row). |
| `human_review_pending` | No reliable rendered comparison was possible (or none was established); a human must read the cited document. |
| `human_approved` | **Never set by code.** No row in this packet uses it, by design. Nothing below is human-approved until Lekhraj signs off on the checklist in §9. |

Comparison statuses used in the CSV: `matched`,
`matched_within_declared_precision`, `scope_or_basis_difference_recorded`,
`extraction_scrambled`, `no_rendered_comparison_available`,
`mismatch_flagged_do_not_force` (unused so far — no row was forced to match).

## 2. Reconciliation results — Infosys (40 rows: 39 agent-checked, 1 pending)

All values consolidated scope, full rupees; crore displays reconcile exactly at
the declared `decimals="-7"` precision.

| Period (exact dates) | Concept | Value (₹ Cr) | Source document | Rendered cross-check | Status |
|---|---|---|---|---|---|
| Q1 FY2026-27 (2026-04-01..06-30) | revenue_from_operations | 48,211 | NSE XBRL seq 177385 | Reg-33 PDF p.12: "48,211" | matched |
| Q1 FY2026-27 | total_income | 49,195 | NSE XBRL 177385 | Reg-33 p.12: "49,195" | matched |
| Q1 FY2026-27 | profit_before_tax (2 tag variants) | 11,028 | NSE XBRL 177385 | Reg-33 p.12: "11,028" (PBT = PBIT, exceptional nil) | matched |
| Q1 FY2026-27 | profit_after_tax (2 variants) | 7,775 | NSE XBRL 177385 | Reg-33 p.12: "7,775" | matched |
| Q1 FY2026-27 | profit_attributable_to_owners | 7,769 | NSE XBRL 177385 | Reg-33 p.12: "7,769" | matched |
| Q1 FY2026-27 | exceptional_items | 0 | NSE XBRL 177385 | Reg-33 p.12: "−" (nil) | matched |
| Q1 FY2026-27 | eps_basic / eps_diluted (2 variants each) | 19.19 / 19.17 | NSE XBRL 177385 | Reg-33 p.12 EPS table | matched |
| Q4 FY2025-26 (2026-01-01..03-31) | revenue_from_operations | 46,402 | NSE XBRL seq 152465 | Reg-33 p.12, "Quarter ended March 31, 2026" column: "46,402" | matched |
| Q4 FY2025-26 | total_income | 47,561 | NSE XBRL 152465 | Reg-33 p.12: "47,561" | matched |
| Q4 FY2025-26 | profit_before_tax (2 variants) | 10,797 | NSE XBRL 152465 | Reg-33 p.12: "10,797" | matched |
| Q4 FY2025-26 | profit_after_tax (2 variants) | 8,509 | NSE XBRL 152465 | Reg-33 p.12: "8,509" | matched |
| Q4 FY2025-26 | profit_attributable_to_owners | 8,501 | NSE XBRL 152465 | Reg-33 p.12: "8,501" | matched |
| Q4 FY2025-26 | exceptional_items | 0 | NSE XBRL 152465 | Reg-33 p.12: "−" | matched |
| Q4 FY2025-26 | eps_basic / eps_diluted | 21.01 / 20.98 | NSE XBRL 152465 | Reg-33 p.12 | matched |
| FY2025-26 annual (2025-04-01..2026-03-31) | revenue_from_operations | 1,78,650 | NSE XBRL 152465 | Reg-33 p.12, "Year ended March 31, 2026" column | matched |
| FY2025-26 annual | total_income | 1,82,972 | NSE XBRL 152465 | Reg-33 p.12 | matched |
| FY2025-26 annual | profit_before_tax | 39,995 (after exceptional) / PBIT 41,284 | NSE XBRL 152465 | Reg-33 p.12: 41,284 − 1,289 = 39,995 | matched |
| FY2025-26 annual | profit_after_tax (2 variants) | 29,474 | NSE XBRL 152465 | Reg-33 p.12 | matched |
| FY2025-26 annual | profit_attributable_to_owners | 29,440 | NSE XBRL 152465 | Reg-33 p.12 | matched |
| FY2025-26 annual | exceptional_items | **−1,289** | NSE XBRL 152465 | Reg-33 p.12: "1,289" expense (Impact of Labour Codes) | **scope_or_basis_difference_recorded — SIGN CONVENTION** (see §6) |
| FY2025-26 annual | eps_basic / eps_diluted | 71.58 / 71.46 | NSE XBRL 152465 | Reg-33 p.12 | matched |
| FY2025-26 annual | cash_flow_operations | 33,986 | NSE XBRL 152465 | Condensed-FS PDF p.6: "33,986" | matched |
| FY2025-26 annual | capex (PP&E) | 2,727 | NSE XBRL 152465 | Condensed-FS p.6: "(2,727)" parenthesized outflow | matched |
| FY2025-26 annual | capex (intangibles) | 0 | NSE XBRL 152465 | none established | **human_review_pending** |
| **Q1 FY2026-27 quarter** | **cash_flow_operations (REPORTED, PDF-sourced)** | **9,330** | **IR condensed-FS PDF p.6** | same page: "Net cash generated by operating activities 9,330" | matched |

## 3. Reconciliation results — Hindustan Unilever (39 rows: 6 agent-checked, 33 pending)

| Period (exact dates) | Concept | Value (₹ Cr) | Source document | Rendered cross-check | Status |
|---|---|---|---|---|---|
| Q1 FY2026-27 (2026-04-01..06-30) | revenue_from_operations | 17,341 | NSE XBRL seq 179457 | segment note p.7 shows "17,341"; P&L p.6 linear text yields "17,149" — see §7 | **human_review_pending** (tension flagged) |
| Q1 FY2026-27 | total_income | 17,529 | NSE XBRL 179457 | p.6 extraction interleaves | human_review_pending |
| Q1 FY2026-27 | profit_before_tax | 3,632 (PBIT 3,707, exceptional −75) | NSE XBRL 179457 | p.6 extraction interleaves | human_review_pending |
| Q1 FY2026-27 | profit_after_tax (2 variants) | 2,680 | NSE XBRL 179457 | p.6 extraction interleaves | human_review_pending |
| Q1 FY2026-27 | profit_attributable_to_owners | 2,673 | NSE XBRL 179457 | p.6 extraction interleaves | human_review_pending |
| Q1 FY2026-27 | eps_basic / eps_diluted | 11.38 / 11.38 | NSE XBRL 179457 | "11.38" appears on p.6; column association not machine-verifiable | human_review_pending |
| Q1 FY2026-27 | exceptional_items | −75 | NSE XBRL 179457 | p.6 extraction interleaves | human_review_pending |
| Q4 FY2025-26 (2026-01-01..03-31) | revenue_from_operations | 16,351 | NSE XBRL seq 155083 | p.8 extraction interleaves (component tokens of the Mar-2026 column sum to 16,351) | human_review_pending |
| Q4 FY2025-26 | profit_after_tax (2 variants) | 2,994 total / 3,006 continuing | NSE XBRL 155083 | p.8 interleaves | human_review_pending |
| Q4 FY2025-26 | profit_attributable_to_owners | 2,992 | NSE XBRL 155083 | p.8 interleaves | human_review_pending |
| Q4 FY2025-26 | eps_basic / eps_diluted | 12.73 total / 12.76 continuing / 12.72 diluted | NSE XBRL 155083 | p.8 interleaves | human_review_pending |
| Q4 FY2025-26 | exceptional_items | +247 (gain) | NSE XBRL 155083 | letter covers FY only | human_review_pending |
| FY2025-26 annual | revenue_from_operations | 64,468 | NSE XBRL 155083 | p.8 interleaves | human_review_pending |
| FY2025-26 annual | total_income | 65,219 | NSE XBRL 155083 | p.8 interleaves | human_review_pending |
| FY2025-26 annual | profit_before_tax | 13,827 (PBIT 14,062) | NSE XBRL 155083 | letter p.1: "PBT 13,812 from continuing operations" | **scope_or_basis_difference_recorded** (see §6) |
| FY2025-26 annual | profit_after_tax | 15,059 total / 10,667 continuing | NSE XBRL 155083 | letter p.1: continuing PAT "10,652" | **scope_or_basis_difference_recorded** on the continuing row |
| FY2025-26 annual | profit_attributable_to_owners | 15,040 | NSE XBRL 155083 | p.8 interleaves | human_review_pending |
| FY2025-26 annual | exceptional_items | **−235** (loss) | NSE XBRL 155083 | letter p.1: "loss of Rs. 235 crores" | matched |
| FY2025-26 annual | eps_basic / eps_diluted | 64.01 / 45.25 continuing / 64.00 diluted | NSE XBRL 155083 | p.8 interleaves | human_review_pending |
| FY2025-26 annual | cash_flow_operations | 10,999 | NSE XBRL 155083 | results PDF p.11: "Net cash flows generated from operating activities - [A] 10,999" (Year ended 31st March, 2026, Rs in Crores) | matched |
| FY2025-26 annual | capex (PP&E) | 1,258 | NSE XBRL 155083 | p.11: "(1,258)" — rendered parenthesized, XBRL positive magnitude | matched |
| FY2025-26 annual | capex (intangibles) | 103 | NSE XBRL 155083 | p.11: "(103)" | matched |
| Q1 FY2026-27 | cash_flow_operations | **not present in any ingested source** | — | — | missing-data status `not_present_in_ingested_sources` (never zero; never an error) |

## 4. Period-label validation (every previously demonstrated value)

Verification date: **2026-09-11**. Application labels are Quarterline's own
presentation layer, derived from exact context dates only (`periods.period_label`);
source labels are the issuers'/instance's own. Calendar dates are the primary
truth.

| Source document (filing id) | Broadcast / filed | Context dates (verified in instance) | Source fiscal label | Application label | Scope | Flags |
|---|---|---|---|---|---|---|
| INFY Q1 FY27 XBRL (NSE seq 177385) | 2026-07-23 17:40:57 IST (board meeting same day 13:30–15:38; prior intimation 2026-06-15) | 2026-04-01..2026-06-30 (`DateOfStart/EndOfReportingPeriod` agree) | "Quarterly" / "First quarter"; company style "June 2026 quarter" | Q1 FY2026-27 | consolidated, Audited, Original | none |
| INFY Q4+FY26 XBRL (NSE seq 152465) | 2026-04-23 20:47:17 IST (board meeting 2026-04-23) | quarter 2026-01-01..2026-03-31; annual 2025-04-01..2026-03-31 (separate contexts `OneD`/`FourD`) | "Quarterly" / "Fourth quarter" (+ year context) | Q4 FY2025-26 / FY2025-26 annual | consolidated, Audited, Original | none |
| HUL Q1 FY27 XBRL (NSE seq 179457) | 2026-07-28 19:41:55 IST (board meeting 08:30–11:00 same day) | 2026-04-01..2026-06-30 | "Quarterly" / "First quarter" | Q1 FY2026-27 | consolidated, Un-Audited (limited review), Original | none |
| HUL Q4+FY26 XBRL (NSE seq 155083) | 2026-04-30 21:06:22 IST (board meeting 2026-04-30) | quarter 2026-01-01..2026-03-31; annual 2025-04-01..2026-03-31 | "Quarterly" / "Fourth quarter" | Q4 FY2025-26 / FY2025-26 annual | consolidated, Audited, Original | none |
| INFY IR condensed-FS PDF (Q1) | document dated 2026-07-23 (accompanies seq 177385) | statement period "for the three months ended June 30, 2026" → 2026-04-01..2026-06-30 | "three months ended June 30, 2026" | Q1 FY2026-27 | consolidated | none |

Explicit flag checks (all four filings + PDF):

- **Publication date vs document metadata:** consistent — every NSE broadcast
  timestamp falls on the declared board-approval date; the Infosys press release
  is dated July 23, 2026; the HUL letter is dated April 30, 2026 context.
- **Reporting period vs source:** consistent — instance qualifiers
  `DateOfStart/EndOfReportingPeriod` equal every mapped fact's context dates.
- **Future-dated filings (vs 2026-09-11):** none — latest broadcast is
  2026-07-28.
- **Annual presented as quarterly:** no — annual values live on separate
  `FourD` contexts and separate observations; the Q4-filing quarter values were
  verified to be 3-month figures (INFY Q4 revenue ₹46,402 Cr is the "Quarter
  ended March 31, 2026" column on Reg-33 p.12 and ~26% of the annual
  ₹1,78,650 Cr; HUL Q4 quarter ₹16,351 Cr equals the sum of its rendered
  quarter-column components 16,172 + 35 + 144).
- **Comparative misidentified as current:** no — the exchange Q1/Q4 instances
  carry NO prior-year duration contexts at all (only prior-year instants for
  balance-sheet comparatives); every ingested duration observation is a
  current-period figure from its own filing. (HUL's rendered PDFs print
  prior-year columns, but nothing from them is ingested.)

**Two consecutive quarterly reporting periods per issuer: YES — the acceptance
condition is MET.** INFY: Jan–Mar 2026 quarter and Apr–Jun 2026 quarter (both
verified as 3-month durations at document level). HUL: same two quarters, same
verification.

## 5. Cash-flow availability matrix (company × source × period)

| Company | Period | Exchange XBRL | Company-IR PDF | Company-IR workbook | Status in corpus |
|---|---|---|---|---|---|
| INFY | Q1 FY2026-27 (Apr–Jun 2026) | no CF facts (0 of 89) | **condensed-FS PDF p.6: quarterly CFO ₹9,330 Cr (REPORTED — ingested)** | n/a | quarter CFO available (IR tier); capex: no quarterly figure reported |
| INFY | FY2025-26 annual | CFO ₹33,986 Cr, capex ₹2,727 Cr (+ intangibles 0) | condensed-FS PDF p.6 corroborates | n/a | annual reported |
| HUL | Q1 FY2026-27 (Apr–Jun 2026) | no CF facts (0 of 82) | results PDF (30 pp.): no CF statement | sheets exactly `SEBI Consolidated, Segment Consolidated, SEBI Standalone, Segment Standalone` — no CF sheets | `not_present_in_ingested_sources` |
| HUL | FY2025-26 annual | CFO ₹10,999 Cr, capex ₹1,258 Cr + ₹103 Cr intangibles | results PDF p.11 (consolidated) + p.19 (standalone): annual CF statements | `Cash Flow Consolidated` / `Cash Flow Standalone` sheets (annual) | annual reported |

Derivation consequences: INFY `annual − quarterly`... cannot produce H2 (a
single quarter does not share the annual start boundary — correctly rejected by
`derive_by_subtraction`). H2 for either issuer requires a half-year CF document,
which is not in the corpus: any H2 is `source_not_ingested`, and a hypothetical
"Q4 = annual − 9M" is `not_applicable` until a 9M CF document is acquired.
Nothing is ever divided.

## 6. Sign semantics — documented per issuer from actual statement presentation

These conventions differ between the rendered statements and the XBRL facts and
must never be assumed constant across issuers or formats:

- **INFY exceptional items:** the rendered Reg-33 P&L prints the FY26
  exceptional item (Impact of Labour Codes) as a **positive 1,289** deducted
  from "Profit before exceptional item and tax" (41,284 − 1,289 = 39,995). The
  XBRL fact signs it **−1,289 Cr** (i.e. exceptional = PBT − PBIT). Same
  magnitude, opposite display sign. Recorded as
  `scope_or_basis_difference_recorded`; never coerced.
- **HUL exceptional items:** signed as the profit impact in BOTH the letter and
  the XBRL: FY26 "a loss of Rs. 235 crores" ⇔ XBRL −235 Cr; Q4 quarter +247 Cr
  (gain, demerger-related) ⇔ XBRL +247 Cr. `matched` with the convention
  documented on the row.
- **Capex (both issuers):** rendered cash-flow statements show investing
  outflows **parenthesized** — INFY "(2,727)", HUL "(1,258)"/"(103)"; the XBRL
  purchase facts are **positive magnitudes**. Display-vs-fact sign difference
  is a presentation convention, recorded on each row.

## 7. Unresolved mismatches and `human_review_pending` list (34 rows)

1. **HUL Q1 revenue tension (highest priority).** The exchange XBRL and the
   segment note (results PDF p.7) agree at ₹17,341 Cr for the June-2026
   quarter, but the P&L page's linear text extraction yields "17,149" and its
   component rows (16,172 + 35 + 144 = 16,351) belong to the March-2026
   quarter column. Strong evidence of a column-interleaving extraction
   artifact — but the P&L line must be read by a human; not guessed.
2. **HUL Q1: all 12 P&L rows** — same 4-column rendering (Jun-2026 qtr /
   Jun-2025 qtr / Mar-2026 qtr / FY26) interleaves under linear extraction.
   Manual reading of `hul-jq26-financial-results.pdf` pp.6–8 settles all of
   them.
3. **HUL Q4: 21 rows** (quarter and annual P&L, EPS, exceptional Q4) — the
   consolidated P&L page 8 of `hul-mq26-financial-results.pdf` interleaves the
   same way; the letter (p.1) and the cash-flow page (p.11) are already
   agent-checked, so only the P&L table needs human eyes.
4. **INFY FY26 intangible capex = 0** — no rendered reference established
   (row left `no_rendered_comparison_available`, pending).

Everything else (45 rows) is `agent_checked_against_document` with page +
displayed value recorded; 0 rows are marked approved.

## 8. Remaining source-access limitations

- All acquisition to date is **browser-assisted and manual** (prototype): plain
  httpx is rejected by every tested host (403 / TCP drop). Headless-browser
  reliability from the project runtime is unmeasured; nothing here proves all
  automated access impossible, nor is any redistribution right claimed.
- NSE archive URLs embed internal ids — always resolve via the listing API;
  the legacy financial-results API is frozen at Dec-2024; BSE download path
  unassessed; standalone instances and the Q4 IR PDFs are cached but not
  committed fixtures (gitignored `storage/raw/india/`).
- The SEBI `in-capmkt` schema is not bundled with the instances (offline XBRL
  validation needs the taxonomy fetched separately).

## 9. Review checklist (for Lekhraj)

- [ ] Read HUL Q1 `hul-jq26-financial-results.pdf` pp.6–8 and confirm the
      consolidated P&L line values against §3 (esp. revenue 17,341 vs the
      scrambled "17,149"; PAT 2,680; owners 2,673; EPS 11.38).
- [ ] Read HUL Q4 `hul-mq26-financial-results.pdf` p.8 (consolidated P&L) and
      confirm the 21 pending rows (Q4 quarter 16,351 / 16,615 / 3,928 / 2,994 /
      2,992 / 12.73; annual 64,468 / 65,219 / 13,827 / 14,062 / 15,059 / 15,040
      / 64.01).
- [ ] Confirm the HUL Q4 quarter exceptional item is a **+247 Cr gain** on the
      rendered statement (demerger-related) and that the XBRL sign convention
      (§6) is how we want to store it.
- [ ] Confirm the recorded sign-semantics policy (§6) is acceptable as the
      storage contract (XBRL sign preserved; rendered sign documented).
- [ ] Confirm the corrected cash-flow policy (methodology §4): INFY's quarterly
      IR-PDF CFO is admitted as a reported observation; HUL Q1 CF stays
      `not_present_in_ingested_sources`.
- [ ] Spot-check the two INFY PDF anchors: `consol-fy27-q1-finstatement.pdf`
      p.6 → "9,330"; Reg-33 p.12 → "48,211 / 46,402 / 178,650".
- [ ] Confirm acceptance that two consecutive quarterly periods per issuer are
      covered (§4) so the financial-correctness gate for INFY + HUL closes.
- [ ] After review: set `human_approved` explicitly by hand if deserved (no
      code path sets it), and record the reviewer + date in this file.

## 10. Reproducing

```
uv run python -c "from quarterline.sources.india.reconcile import write_validation_csv; print(write_validation_csv())"
uv run pytest tests/unit/test_india_reconcile.py tests/unit/test_india_cash_flow.py -q
```

The live-PDF drift tests re-extract the cached `storage/raw/india/` PDFs and
fail if any recorded page reference or displayed value stops matching the
document.
