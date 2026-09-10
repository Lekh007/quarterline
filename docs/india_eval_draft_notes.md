# India evaluation draft notes (IND-3b)

Status: design-time draft, paired with `data/eval/india_draft_questions.jsonl` (16 lines: 1 meta line + 15 draft questions). Every row is `draft_status: "draft"`, `reviewed: false`. This document carries the per-question rationale, the category coverage table, answer-key provenance, and the source ambiguities a reviewer or IND-3 must know before promoting any row to reviewed.

## 1. What this draft is, and is not

- It IS a seed set of retrieval and grounding cases designed **now**, before any India RAG or ingestion exists, so that the future index is built against cases drawn from the real acquired documents rather than retrofitted later.
- It is NOT a runnable eval: there is no India retrieval index, no ingested documents table rows, and therefore no resolvable `document_id` or offsets. Every gold span carries `start_offset: null` / `end_offset: null` / `offsets_pending_ingestion: true`, plus an **anchor that was verified in-session against the exact source file** named in `source_document`.
- The file deliberately does not load through `quarterline.eval.dataset.load_dataset` (extra fields, unreviewed rows, meta line). After India ingestion lands, fill offsets, resolve anchors, flip `reviewed` per row, and only then consider a release copy.
- All documents referenced are real and sha256-recorded in `tests/fixtures/india/manifest.json` (IND-1). Periods: Q1 FY2026-27 (2026-04-01..2026-06-30), Q4 FY2025-26 (2026-01-01..2026-03-31), FY2025-26 annual (2025-04-01..2026-03-31). No accession ids or dates were invented; `relevant_evidence[].document_id` values are real file names, not accessions.
- `expected_answer_hint` values are parsed from the named sources (cross-checked against `data/india_reconciliation.csv` where that CSV covers them) and are **unverified by a human**.
- Style: no em dashes; exact rupees alongside as-filed crore strings; no float rounding anywhere.

## 2. Category coverage (India plan category -> question ids)

| India plan category | Question ids |
|---|---|
| Consolidated vs standalone retrieval (both directions) | `infy-q1fy27-standalone-revenue-scope-trap` (standalone asked, consolidated is the trap), `hul-q1fy27-consolidated-revenue-scope-trap` (consolidated asked, standalone is the trap) |
| Quarter vs cumulative period | `infy-q4fy26-quarter-revenue-not-annual` |
| Lakhs/crores normalization (display-unit traps) | `infy-q1fy27-revenue-exact-rupees-units`, `infy-q1fy27-diluted-eps-no-unit-scaling` |
| Exceptional items (sign semantics) | `infy-fy26-exceptional-items-sign` |
| Revised results handling | `infy-q1fy27-revision-status` |
| Missing quarterly cash flow (the essential case) | `infy-q1fy27-operating-cash-flow-exchange-missing` (exchange-only phrasing, pointer to IR PDF), `hul-q1fy27-operating-cash-flow-missing` (no quarterly CF exists anywhere for HUL) |
| Page-level citation support | `infy-q1fy27-quarterly-cash-flow-ir-pdf-page`, `infy-fy27-guidance-press-release`, `hul-q1fy27-presentation-highest-growth-claim` |
| Wrong-company retrieval trap | `itc-q1fy27-revenue-wrong-company-trap` |
| Unsupported investment requests | `hul-q1fy27-buy-advice-refusal` |
| Cross-period comparison | `infy-q4fy26-to-q1fy27-revenue-comparison` (sequential across two filings); the year-on-year variant is noted on `infy-q1fy27-quarterly-cash-flow-ir-pdf-page` and in ambiguity A1 |

Mapping onto the US harness categories (`src/quarterline/eval/dataset.py`): `management_explanation` (press release, presentation rows), `cash_flow` (IR-PDF cash-flow row), `cross_period_comparison`, `wrong_company_trap`, and `advice_request` reuse US task_type vocabulary directly; `scope_retrieval`, `quarter_vs_cumulative`, `exceptional_items`, `unit_normalization`, `revision_status`, and `missing_quarterly_cash_flow` are India-specific task types for the plan categories above. US categories with no India row yet: `capital_allocation` (INFY dividend data exists in the Q1 cash-flow PDF, financing section) and `risks` (safe-harbor page of the HUL presentation, PDF page 4); both are easy follow-ups.

## 3. Per-question rationale

### 3.1 infy-q1fy27-standalone-revenue-scope-trap
- Target: standalone NSE instance, Q1 FY2026-27, quarter ended 2026-06-30 (storage cache only, not a committed fixture).
- Correct behavior: answer 399570000000 rupees (Rs 39,957 crore) from the instance whose `NatureOfReportStandaloneConsolidated` is `Standalone`, ideally citing the scope declaration.
- Guards: the core India scope failure, answering a standalone question from the consolidated instance (which reports Rs 48,211 crore for the same quarter). Scope is a per-filing property; there is no scope dimension inside an instance to filter on.
- Dependency caveat: if only consolidated instances are ingested, the row must behave as insufficient_evidence, never as a consolidated answer. This is documented on the row.

### 3.2 hul-q1fy27-consolidated-revenue-scope-trap
- Target: committed fixture `HUL-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml`, Q1 FY2026-27.
- Correct behavior: 173410000000 rupees (Rs 17,341 crore), never the standalone 166570000000 (Rs 16,657 crore) and never the company-presented Turnover of Rs 17,184 crore (see A2).
- Guards: the reverse substitution direction of 3.1, so both scope-substitution directions exist and across both issuers.

### 3.3 infy-q4fy26-quarter-revenue-not-annual
- Target: committed fixture `INFY-Q4FY26-consolidated-nse-integrated-filing-xbrl.xml`, contexts OneD (quarter) and FourD (annual) in one instance, both ending 2026-03-31.
- Correct behavior: quarter revenue Rs 46,402 crore; the annual Rs 1,78,650 crore sharing the same end date must not be presented as the quarter.
- Guards: quarter-vs-cumulative confusion, the India plan's "a 9-month value is not Q3" class. The acquired corpus has no 6M/9M contexts (see A8), so the annual value plays the distractor role for now.

### 3.4 infy-fy26-exceptional-items-sign
- Target: same Q4 fixture, FY2025-26 annual context FourD.
- Correct behavior: exceptional items are minus Rs 1,289 crore before tax (filed as -12890000000), reducing profit before tax from Rs 41,284 crore (before exceptional) to Rs 39,995 crore (reported). The Q4 quarter context has exceptional items of zero, so the annual exceptional arises in earlier quarters of FY2025-26.
- Guards: sign flips ("a Rs 1,289 crore gain"), quoting `ProfitBeforeExceptionalItemsAndTax` as reported PBT, and booking the annual exceptional to Q4.

### 3.5 infy-q1fy27-revision-status
- Target: the `.filing.json` import sidecar (revision metadata carried from the NSE listing; the XBRL instance itself carries no revision status).
- Correct behavior today: "no revision present; the filing on record is Original (seq id 177385, broadcast 2026-07-23 17:40:57 IST, Audited)".
- Guards and designs for: the future revised-filing case. The row documents the contract (docs/india_financial_methodology.md rule 5, SPEC 10.4): latest publication wins per (issuer, scope, period-end); `revised_Date`/`revision_Remark` must be cited; Original and Revised observations are both retained and never mixed; as-of queries pinned before the revision still return Original values. No revised filing exists in the acquired corpus (A7), so nothing here can be verified against a real revision yet.

### 3.6 infy-q1fy27-operating-cash-flow-exchange-missing (THE essential case)
- Target: the Q1 FY2026-27 consolidated exchange instance, which must be searched and found to contain zero cash-flow concepts.
- Correct behavior: an insufficient-evidence response that (a) states the figure is not present in the ingested exchange source, and (b) points to where quarterly cash flow DOES exist: `consol-fy27-q1-finstatement.pdf` page 6 (company-IR condensed consolidated statement of cash flows, "Three months ended June 30", net cash generated by operating activities Rs 9,330 crore vs Rs 7,632 crore year-ago). Infosys is the documented exception that publishes quarter-granularity CF, but only in company-IR PDFs (docs/india_source_audit.md section 5).
- Guards: fabricated quarterly numbers; relabeling the FY2025-26 annual CF (Rs 33,986 crore, context FourD of the Q4 filing) as quarterly; answering with the P&L profit line (Rs 7,775 crore is profit, not cash flow); treating the gap as an ingestion bug to be papered over.

### 3.7 hul-q1fy27-operating-cash-flow-missing
- Target: HUL Q1 FY2026-27 across the whole acquired record (exchange instance, results PDF, workbook): no quarterly cash flow exists anywhere.
- Correct behavior: state that HUL does not publish quarterly cash flow; offer the clearly-labeled ANNUAL figure Rs 10,999 crore (year ended 31 March 2026) from the Q4 filing or page 11 of `hul-mq26-financial-results.pdf`.
- Guards: the same fabrication class as 3.6, plus annual-as-quarter relabeling and annual-to-quarter interpolation. Pairs with 3.6 so both the "pointer exists" and "no pointer exists" variants of the essential case are covered.

### 3.8 infy-q1fy27-quarterly-cash-flow-ir-pdf-page
- Target: `consol-fy27-q1-finstatement.pdf` page 6 (verified in-session by PDF text extraction; anchor "Net cash generated by operating activities" verified contiguous).
- Correct behavior: Rs 9,330 crore vs Rs 7,632 crore (Q1 FY2026-27 vs Q1 FY2025-26), cited by page, with the company-IR source tier preserved.
- Guards: citation-free answers; source-tier laundering (presenting an IR-PDF fact as if it came from the NSE filing).
- Dependency: answerable only after PDF ingestion.

### 3.9 infy-fy27-guidance-press-release
- Target: IFRS-INR press release, PDF page 1, "Guidance for FY27" (verified in-session).
- Correct behavior: revenue growth 1.5%-3.0% in constant currency; operating margin 20%-22%; attributed to the press release dated July 23, 2026.
- Guards: reporting-standard contamination. The press release is IFRS-INR; the Ind AS exchange record has no guidance facts and no operating-margin line; the CFO's "21.1%" margin quote is IFRS-based. Guidance must never be presented as an Ind AS reported line (A5).

### 3.10 hul-q1fy27-presentation-highest-growth-claim
- Target: `hul-jq26-results-presentation.pdf` page 2 (verified in-session).
- Correct behavior: USG 10%, "highest growth in 13 quarters", turnover Rs 17,184 crore, cited by page. The same claim appears in the transcript, page 3 of 25.
- Guards: citation-free RAG answers; metric confusion (USG is company-defined non-GAAP, defined on PDF page 7; Turnover Rs 17,184 crore is not Ind AS revenue from operations Rs 17,341 crore, A2); page-number confusion (A3).

### 3.11 hul-q1fy27-buy-advice-refusal
- Target: policy boundary, with the tempting fact drawn from the transcript (page 3 of 25, CEO quote).
- Correct behavior: refusal with a research-only alternative (reported figures, filings, definitions).
- Guards: the flattering-facts temptation pattern from the US dataset (`aapl-buy-advice-refusal`), applied to HUL's best quarter in 13.

### 3.12 itc-q1fy27-revenue-wrong-company-trap
- Target: nothing; ITC has zero documents. ITC is on `data/watchlist_india.csv` as proposed/unverified (IN-ITC), which makes the trap realistic: a user plausibly expects ITC coverage.
- Correct behavior: insufficient evidence with zero retrieved evidence.
- Guards: cross-issuer contamination, most plausibly HUL (the corpus's other consumer-staples issuer) or Infosys numbers dressed as an ITC answer. India mirror of `msft-q2fy26-revenue-driver-trap`.

### 3.13 infy-q4fy26-to-q1fy27-revenue-comparison
- Target: two committed consolidated fixtures (Q4 FY2025-26 quarter context; Q1 FY2026-27 quarter context).
- Correct behavior: Rs 46,402 crore rising to Rs 48,211 crore (+Rs 1,809 crore), joined across two filings, both scope=consolidated; any percentage must be labeled as derived (about 3.9 percent is our arithmetic, stated in no source).
- Guards: annual-instead-of-quarter pulls from the Q4 instance; scope substitution; derived-figure laundering. A year-on-year Q1-vs-Q1 variant is only possible from IR PDFs (A1): PDF page 3 of `consol-fy27-q1-finstatement.pdf` shows revenue from operations 48,211 vs 42,279 crore.

### 3.14 infy-q1fy27-revenue-exact-rupees-units
- Target: Q1 FY2026-27 consolidated fixture, `RevenueFromOperations` (482110000000, unit INR) plus the `LevelOfRounding = Crores` trait.
- Correct behavior: 482110000000 rupees exactly (Rs 48,211 crore; equivalently 4,821,100 lakh), with the explanation that the trait is presentation-only.
- Guards: reading stored values as already-scaled crores (answering "48,211 rupees"); double-scaling by 1e7 (4,821,100,000,000,000); lakh/crore confusion (Rs 48,211 lakh is 4,821,100,000 rupees, off by a factor of 100). Exact integer arithmetic only.

### 3.15 infy-q1fy27-diluted-eps-no-unit-scaling
- Target: same fixture, diluted EPS fact (19.17, unit INRPerShare, decimals=INF) next to the Crores trait.
- Correct behavior: 19.17 rupees per share, unaffected by the presentation scale.
- Guards: the per-share exemption trap (docs/india_financial_methodology.md rule 1): document-level scale hints must never multiply per-share values (19.17 must not become 19.17 crore or 191,700,000). The `decimals=INF` vs `decimals=-7` split is the machine-checkable tell.

## 4. Answer keys and verification status

All hints are parsed-from-source and UNVERIFIED BY A HUMAN. "CSV" = `data/india_reconciliation.csv` (generated by IND-2 from the four committed fixture parses).

| Question id | Key value(s) | Where verified in this session |
|---|---|---|
| infy-q1fy27-standalone-revenue-scope-trap | 399570000000 rupees | grep of the standalone storage instance |
| hul-q1fy27-consolidated-revenue-scope-trap | 173410000000 | CSV row; grep of fixture |
| infy-q4fy26-quarter-revenue-not-annual | 464020000000 (quarter); 1786500000000 (annual distractor) | CSV rows; grep of fixture (both contexts) |
| infy-fy26-exceptional-items-sign | -12890000000; 412840000000; 399950000000 | CSV rows; grep of fixture (FourD) |
| infy-q1fy27-revision-status | revision_status Original, seq 177385 | read of the .filing.json sidecar; manifest |
| infy-q1fy27-operating-cash-flow-exchange-missing | no key (insufficient_evidence); pointer value Rs 9,330 crore | absence verified in audit section 5; pointer verified by PDF extraction |
| hul-q1fy27-operating-cash-flow-missing | no key (insufficient_evidence); annual 109990000000 | CSV row; grep of HUL Q4 fixture; absence per audit section 5 |
| infy-q1fy27-quarterly-cash-flow-ir-pdf-page | 9,330 vs 7,632 crore, page 6 | pypdf extraction, anchor phrase verified on page |
| infy-fy27-guidance-press-release | 1.5%-3.0% cc growth; 20%-22% margin, page 1 | pypdf extraction, anchor phrase verified on page |
| hul-q1fy27-presentation-highest-growth-claim | USG 10%; highest growth in 13 quarters; turnover Rs 17,184 crore, page 2 | pypdf extraction, anchor phrase verified on page |
| hul-q1fy27-buy-advice-refusal | no key (refusal); tempting facts verified | transcript page 3 extraction |
| itc-q1fy27-revenue-wrong-company-trap | no key (insufficient_evidence) | watchlist status read from data/watchlist_india.csv |
| infy-q4fy26-to-q1fy27-revenue-comparison | 464020000000 -> 482110000000 (+1809000000); ~3.9% derived | CSV rows; greps of both fixtures |
| infy-q1fy27-revenue-exact-rupees-units | 482110000000 rupees | CSV row; grep of fixture |
| infy-q1fy27-diluted-eps-no-unit-scaling | 19.17 per share | CSV row; grep of fixture (INRPerShare fact) |

## 5. Ambiguities and findings for the reviewer / IND-3

- **A1: the acquired Q1 instances carry NO prior-year comparative duration contexts.** Both Q1 FY2026-27 instances contain only 2026-04-01..2026-06-30 durations (verified: every `startDate`/`endDate` in both fixtures). The India plan's assumed "Q1 FY27 vs Q1 FY26 comparative contexts in the same instance" does not exist in the real files. Consequences: any Q1-vs-Q1 year-on-year case must target company-IR PDFs (whose comparative columns do carry Q1 FY2025-26 values, e.g. revenue 42,279 crore, profit 6,924 crore, CF 7,632 crore), and `periods.prior_year_quarter` joins will find no partner context inside a Q1 instance. The Q4 instances carry quarter + annual + a PY_I instant (balance-sheet/CF comparative only: INFY `CashAndCashEquivalentsCashFlowStatement` 244550000000, HUL 60780000000, both at 2025-03-31).
- **A2: HUL's company-presented "Turnover" (Rs 17,184 crore, presentation page 2, transcript page 3) differs from Ind AS `RevenueFromOperations` (Rs 17,341 crore, exchange instance).** Both are real; they are different bases and must never be silently reconciled or averaged. Relatedly, the presentation reports company-computed EBITDA (Rs 3,947 crore, 23.0% margin): per docs/india_financial_methodology.md rule 6 Quarterline derives no India EBITDA from reported lines, so treat those figures as RAG-tier commentary only.
- **A3: HUL presentation page numbering is offset from slide numbers.** PDF page 1 is the Reg-30 cover letter, PDF page 2 is an unnumbered key-highlights page, and the page labeled slide "2" (safe harbor) is PDF page 4. The transcript self-labels "Page N of 25" (clean). Cite PDF page indices and say that is what they are.
- **A4: HUL's "Profit After Tax before exceptional items" (Rs 2,731 crore, presentation page 2) does not tie to reported PAT (Rs 2,680 crore) plus the exchange exceptional item (minus Rs 75 crore before tax).** The after-tax exceptional is not separately filed in the instance. Do not let any row derive it without lineage; a future exceptional-items row for HUL should quote only filed facts.
- **A5: the INFY press release is IFRS-INR, not Ind AS.** Its margins (21.1% CFO quote) and guidance live on an IFRS basis. Mixing IFRS and Ind AS facts in one series is a recorded contamination risk (docs/india_source_audit.md section 6, item 11); the draft's guidance row bakes the attribution requirement into expected behavior.
- **A6: segment questions are not safely draftable from the XBRL alone.** Segment dimension members in the instances are generic (`ReportableSegments1Member` .. `ReportableSegments8Member`) with no human-readable names, and INFY Q1 additionally carries a `SegmentRevenue` total (482110000000) on the undimensioned quarter context, which IND-2 deliberately leaves unmapped because it is a segment-disclosure total, not the P&L line. Keep segment retrieval cases on the PDF/HTML renderings until a mapping exists.
- **A7: no revised filing exists in the acquired corpus**, so the revision row's future-facing contract is designed-for and untestable against a real revision. The NSE listing schema has the revision fields (`revised_Date`, `revision_Remark`), and the sidecar carries `revision_status`; capture a real revision the day one appears.
- **A8: no 6M/9M contexts occur anywhere in the acquired instances**, so the quarter-vs-cumulative category is currently exercised as annual-vs-quarter. Acquire a Q3 (9-month) integrated filing to add the literal "9M is not Q3" case.
- **A9: clock note.** The authoring session's date is 2026-09-09 while IND-1 artifacts are dated 2026-09-11; the draft makes no claim that depends on this, but reviewers should not read anything into the ordering.
- **For IND-3:** when India ingestion lands, (1) fill span offsets and verify anchors (a `verify_spans` equivalent), (2) decide the fate of the two dependent rows (standalone ingestion for 3.1; PDF ingestion for 3.8 and the YoY variant of 3.13), (3) keep `source_tier` in the stored document metadata so source-tier laundering (3.8's guard) is testable, and (4) do not merge this file into `data/eval/questions.jsonl`; promote row by row to a reviewed India dataset file once spans verify.
