# India source audit — NSE/BSE feasibility for Quarterline (IND-1)

Status: acquisition-half feasibility milestone, complete as of **2026-09-11**.
Scope: identifier verification, official document acquisition (two consecutive periods), access-condition
recording, and the cash-flow reporting-frequency finding for the ingestion design rule.
Normalization, parsing, and scoring are **out of scope** (IND-2 builds `src/quarterline/sources/india/`
against the files and manifest this milestone produced).

Observed on the ground: every India source tried **rejected the non-browser
HTTPS client tested** (plain `httpx` GETs with a research user agent — note this
means an automated non-browser client, NOT an unencrypted `http://` URL; all
tested endpoints were HTTPS throughout).
>
> **CORRECTED (IND-3, 2026-09-11 — reviewer correction B, recorded not silently
> edited):** earlier drafts phrased this as "every Indian source rejects plain
> HTTP", which exceeded the evidence in three ways: (1) it universalized two
> issuers' IR sites and two exchange hosts to "every Indian source"; (2) "plain
> HTTP" was ambiguous — the tested clients used **HTTPS**, what failed was
> *non-browser* client fingerprinting (Akamai 403 / TCP drop against httpx);
> (3) browser-assisted acquisition is a **prototype approach**: it proves only
> that these tested URLs, through these clients (interactive Chromium +
> Playwright page-context fetch), on 2026-09-11, returned these files. It does
> NOT establish that all automated access is impossible (browser-automation
> clients, headless fleets, or other fingerprints were not measured from the
> project runtime), and it does NOT establish that commercial redistribution is
> permitted (SPEC 2.4.5 posture unchanged). The per-URL evidence remains in §2
> and the manifest. All documents below were obtained through a normal
> interactive browser session. No captcha was solved, no cookie was forged, no
> anti-bot control was bypassed, and no commercial aggregator (Screener,
> Trendlyne, Moneycontrol) was used (SPEC 2.4.6).

---

## 1. Identifier verification (official/primary sources only)

| Issuer | ISIN | NSE symbol | BSE scrip code | Where verified |
|---|---|---|---|---|
| Infosys Limited (`IN-INFY`) | `INE009A01021` | `INFY` | `500209` | 1) NSE quote page heading "Infosys Limited (INE009A01021)" (`nseindia.com/get-quote/equity/INFY/Infosys-Limited`, series EQ); 2) BSE stock page header "(INFY \| 500209 \| INE009A01021)" (`bseindia.com/stock-share-price/infosys-ltd/infy/500209/`); 3) Infosys IR "Share Details" table ("NSE Symbol: INFY, BSE Symbol: INFY"); 4) inside the NSE Integrated Filing XBRL instance itself (`in-capmkt:ISIN`, `in-capmkt:Symbol`, `in-capmkt:ScripCode`) |
| Hindustan Unilever Limited (`IN-HINDUNILVR`) | `INE030A01027` | `HINDUNILVR` | `500696` | 1) NSE quote page heading "Hindustan Unilever Limited (INE030A01027)"; 2) BSE stock page header "(HINDUNILVR \| 500696 \| INE030A01027)"; 3) HUL's own results letter (page 1 of `hul-jq26-financial-results.pdf`: "Stock Code: BSE: 500696, NSE: HINDUNILVR, ISIN: INE030A01027"); 4) inside the NSE XBRL instance itself |

All four identifiers for both companies agree across exchange and company sources. The remaining 8
proposed watchlist companies are **unverified** (`proposed` rows in `data/watchlist_india.csv`).

---

## 2. Per-source access findings

| Source (role) | Plain httpx (research UA) | Real browser session | Anti-bot / wall observed | Formats served | Stability assessment |
|---|---|---|---|---|---|
| `www.infosys.com` IR site + `/documents/*.pdf` | **403** Akamai "Access Denied" (ref `18.bf3b4017…`), also on direct PDF GETs | Works fully (pages and PDFs) | Akamai edge bot rule on every path incl. `documents/` PDFs | HTML pages, PDF, XLS | Documented download pages with deterministic per-quarter URL pattern (`/investors/reports-filings/quarterly-results/<FY>/<Q>/documents/…`), history back to FY2001 visible; block means httpx-style ingestion is impossible without a browser-grade client |
| `www.hul.co.in` IR site + `/files/*` | **403** Akamai (ref `18.2cfed417…`), also on direct PDF GET | Works fully | Akamai edge bot rule | HTML, PDF, XLSX | Quarterly landing pages use stable slugs (`…/june-quarter-2026-results/`); file URLs are short stable slugs (`/files/hul-jq-26-results.pdf`) but `/files/<guid>/…` legacy names exist, so URLs are slug-stable, not API-stable. One old page 404'd (`/investors/financial-results/` renamed to `/investors/results-and-presentations/`) — expect page reorganizations |
| `www.bseindia.com` + `api.bseindia.com` | **403** on homepage, stock page, and API | Stock pages work (identifiers, announcements, "Integrated Filing (Finance)" tab visible: "from March 2025 quarter") | Akamai-style 403 | HTML | BSE mirrors the same filings NSE has; **attachment downloads not exercised** (NSE sufficed) — BSE download stability **unassessed** |
| `www.nseindia.com` (web + `/api/*`) | **Read timeout** (silent connection drop), homepage and `/api/quote-equity` | Works fully; first-party JSON APIs callable from the page's own session | TCP-level drop of non-browser clients (no HTTP response at all) | HTML, JSON, XML, iXBRL HTML | Undocumented first-party endpoints; **fragile** — can change without notice. Legacy financial-results API is frozen at the Dec-2024 quarter (see §3) |
| `nsearchives.nseindia.com` (file archive) | **Read timeout** (silent drop) | Direct file GET works (XML/iXBRL serve inline) | Same TCP-level drop; additionally **CORS blocks cross-origin reads** from `www.nseindia.com` | XML (XBRL instances), iXBRL HTML | File URLs embed internal numeric IDs (e.g. `INTEGRATED_FILING_INDAS_1700136_23072026054446_WEB.xml`) — **no stable per-period URL contract**; must always be discovered through the listing API |

**Access terms note:** nothing observed suggests redistribution rights; files are cached locally for
research exactly like the SEC fixtures (SPEC 2.4.5 posture: public accessibility does not grant
commercial redistribution rights).

**Rate limits:** no `Retry-After`/rate-limit headers seen anywhere; ~30 requests were made over ~1 hour
(single-shot, no retry storms). India-specific limits are untested — IND-2 should keep a conservative
spacing (>= 1 s between requests to exchange hosts) analogous to the SEC >= 200 ms rule until measured.

---

## 3. Exchange filing architecture finding (Integrated Filing, SEBI)

Observationally verified from NSE/BSE themselves:

- NSE's legacy **Financial Results** module (`/companies-listing/corporate-filings-financial-results`,
  API `GET /api/corporates-financial-results?index=equities&symbol=<SYM>&period=Quarterly`) contains
  filings only **up to the quarter ended 31-Dec-2024**. Its `from`/`to` parameters were ignored in
  testing. Each legacy filing carries an Ind-AS XBRL instance URL.
- From the **March-2025 quarter** onward, results live under NSE's **"Integrated Filing- Financials"**
  module (`/companies-listing/corporate-integrated-filing?integratedType=integratedfilingfinancials`,
  API `GET /api/integrated-filing-results?&symbol=<SYM>&type=Integrated%20Filing-%20Financials&page=1&size=20`).
  BSE's stock page states the same ("Integrated Filing (Finance) from March 2025 quarter").
  For INFY and HINDUNILVR this module holds 12 filings each (Mar-2025 → Jun-2026 quarters),
  i.e. **consolidated + standalone as two separate filings per period**, each with fields:
  `seq_Id`, broadcast timestamp, `Consolidated|Standalone`, `Audited|Un-Audited`, `Original` +
  revision fields (`revised_Date`, `revision_Remark`), and URLs to an **XBRL instance** and an
  **iXBRL HTML** rendering.
- XBRL taxonomy of every acquired instance: header comment **`IFIndAs V2.1 (26-06-2026)`**, namespace
  `http://www.sebi.gov.in/xbrl/2026-01-31/in-capmkt` (SEBI "Integrated Finance Ind AS" `in-capmkt`),
  `schemaRef` = relative `in-capmkt-ent-2026-01-31.xsd` (**schema not bundled** with the instance —
  offline validation requires fetching the SEBI taxonomy separately).
  Entity identification inside the instance is by **BSE scrip code** (`xbrli:identifier
  scheme="http://www.sebi.gov.in/in-capmkt/ScripCode"`), plus `in-capmkt:ISIN/Symbol/MSEISymbol` facts.
  Units are raw INR (`LevelOfRounding = "Crores"` means the *presentation* scale, values are full rupees,
  e.g. `339860000000` = ₹33,986 Cr).

Per SPEC 27, the Integrated Filing applicability date should still be confirmed against the official
SEBI circular text before encoding hard rules; the observation above is from exchange UIs/APIs directly.

---

## 4. Document inventory (acquired, hashed)

All files are cached under `storage/raw/india/<issuer>/<period>/` with sha256 recorded in
`tests/fixtures/india/manifest.json` (committed fixture copies: the 4 consolidated exchange XBRLs).
26 files total (16 company-IR + 10 exchange-archive). Abbreviated inventory:

### Infosys (`IN-INFY`)

| Period | Scope | Doc type | Format | Tier | File | sha256 (first 12) | Bytes |
|---|---|---|---|---|---|---|---|
| Q1 FY2026-27 (2026-04-01..06-30) | cons+standalone | financial_results (Reg-33 + auditors' reports) | PDF | company_ir | `q1_fy2026-27/q1-fy27-financial-results-auditorsreports.pdf` | `0eb82364f852…` | 5,939,298 |
| Q1 FY2026-27 | consolidated | financial_results (condensed Ind AS FS; **quarterly CF on p.6**) | PDF | company_ir | `q1_fy2026-27/consol-fy27-q1-finstatement.pdf` | `798b110997d7…` | 1,005,112 |
| Q1 FY2026-27 | standalone | financial_results | PDF | company_ir | `q1_fy2026-27/sa-fy27-q1-finstatement.pdf` | `28ac9f84603d…` | 906,280 |
| Q1 FY2026-27 | consolidated | press_release (IFRS-INR commentary) | PDF | company_ir | `q1_fy2026-27/infosys-q1fy27-ifrs-inr-press-release.pdf` | `417a69774f09…` | 474,552 |
| Q1 FY2026-27 | consolidated | financial_results | XBRL | **exchange (NSE)** | `q1_fy2026-27/exchange_nse/INFY-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml` (+ committed fixture copy) | `5928e8ca820f…` | 53,642 |
| Q1 FY2026-27 | standalone | financial_results | XBRL | exchange (NSE) | `q1_fy2026-27/exchange_nse/INFY-Q1FY27-standalone-nse-integrated-filing-xbrl.xml` | `6116b0db3b0f…` | 22,223 |
| Q1 FY2026-27 | consolidated | financial_results (iXBRL viewer) | iXBRL HTML | exchange (NSE) | `q1_fy2026-27/exchange_nse/INFY-Q1FY27-consolidated-nse-integrated-filing-ixbrl.html` | `36ab0f47fe43…` | 46,771 |
| Q4+FY (2026-01-01..03-31 / 2025-04-01..2026-03-31) | cons+standalone | financial_results (Reg-33; CF on pp.17, 23) | PDF | company_ir | `q4_fy2025-26/q4-and-12m-fy26-financial-results-auditorsreports.pdf` | `335dbea73b04…` | 5,746,960 |
| Q4+FY | consolidated | financial_results (condensed Ind AS FS) | PDF | company_ir | `q4_fy2025-26/consol-fy26-q4-and-12m-finstatement.pdf` | `8082dacf94b9…` | 1,132,316 |
| Q4+FY | standalone | financial_results | PDF | company_ir | `q4_fy2025-26/sa-fy26-q4-and-12m-finstatement.pdf` | `c8980a8d40d2…` | 1,000,172 |
| Q4+FY | consolidated | press_release (IFRS-INR commentary) | PDF | company_ir | `q4_fy2025-26/infosys-q4fy26-ifrs-inr-press-release.pdf` | `690841a4dc69…` | 555,748 |
| Q4+FY | consolidated | financial_results | XBRL | **exchange (NSE)** | `q4_fy2025-26/exchange_nse/INFY-Q4FY26-consolidated-nse-integrated-filing-xbrl.xml` (+ committed fixture copy) | `6f1e964f2ffa…` | 151,244 |
| Q4+FY | standalone | financial_results | XBRL | exchange (NSE) | `q4_fy2025-26/exchange_nse/INFY-Q4FY26-standalone-nse-integrated-filing-xbrl.xml` | `969f1c76789d…` | 1,826,430 |

Infosys filing metadata (from NSE listing): consolidated Q1 broadcast 2026-07-23 17:40:57 IST (seq 177385);
standalone Q1 17:41:05 (seq 177355); consolidated Q4 2026-04-23 20:47:17 IST (seq 152465); standalone Q4
20:47:44 (seq 152466). All `Audited`, `Original`.

### Hindustan Unilever (`IN-HINDUNILVR`)

| Period | Scope | Doc type | Format | Tier | File | sha256 (first 12) | Bytes |
|---|---|---|---|---|---|---|---|
| Q1 FY2026-27 | cons+standalone | financial_results (limited-review results letter + statements) | PDF | company_ir | `q1_fy2026-27/hul-jq26-financial-results.pdf` | `707e215f1d49…` | 6,154,352 |
| Q1 FY2026-27 | cons+standalone | financial_results (**SEBI-format workbook, 4 sheets, no CF**) | XLSX | company_ir | `q1_fy2026-27/hul-jq26-results-in-excel.xlsx` | `9fe5ceb8d513…` | 884,489 |
| Q1 FY2026-27 | consolidated | results_presentation (management commentary) | PDF | company_ir | `q1_fy2026-27/hul-jq26-results-presentation.pdf` | `e5d2de83d362…` | 4,013,521 |
| Q1 FY2026-27 | consolidated | press_release (earnings call transcript) | PDF | company_ir | `q1_fy2026-27/hul-jq26-earnings-call-transcript.pdf` | `a4bf4c1a0259…` | 197,488 |
| Q1 FY2026-27 | consolidated | financial_results | XBRL | **exchange (NSE)** | `q1_fy2026-27/exchange_nse/HUL-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml` (+ committed fixture copy) | `ee85ebdc4ddf…` | 34,926 |
| Q1 FY2026-27 | standalone | financial_results | XBRL | exchange (NSE) | `q1_fy2026-27/exchange_nse/HUL-Q1FY27-standalone-nse-integrated-filing-xbrl.xml` | `ba2cd9baade9…` | 33,907 |
| Q1 FY2026-27 | consolidated | financial_results (iXBRL viewer) | iXBRL HTML | exchange (NSE) | `q1_fy2026-27/exchange_nse/HUL-Q1FY27-consolidated-nse-integrated-filing-ixbrl.html` | `8bf49603f640…` | 40,857 |
| Q4+FY | cons+standalone | financial_results (audited; **annual CF on pp.11, 19**) | PDF | company_ir | `q4_fy2025-26/hul-mq26-financial-results.pdf` | `2ba594fddeca…` | 21,593,599 |
| Q4+FY | cons+standalone | financial_results (workbook incl. `Cash Flow Consolidated`/`Cash Flow Standalone` sheets) | XLSX | company_ir | `q4_fy2025-26/hul-mq26-results-in-excel.xlsx` | `2f9ad92d67ec…` | 1,008,196 |
| Q4+FY | consolidated | results_presentation | PDF | company_ir | `q4_fy2025-26/hul-mq26-fy26-results-presentation.pdf` | `1aaa879c7c41…` | 5,165,722 |
| Q4+FY | consolidated | press_release (earnings call transcript) | PDF | company_ir | `q4_fy2025-26/hul-mq26-earnings-call-transcript.pdf` | `7ecb4a897285…` | 260,754 |
| Q4+FY | consolidated | financial_results | XBRL | **exchange (NSE)** | `q4_fy2025-26/exchange_nse/HUL-Q4FY26-consolidated-nse-integrated-filing-xbrl.xml` (+ committed fixture copy) | `5f302861c829…` | 100,551 |
| Q4+FY | standalone | financial_results | XBRL | exchange (NSE) | `q4_fy2025-26/exchange_nse/HUL-Q4FY26-standalone-nse-integrated-filing-xbrl.xml` | `1d1b9dc96b3f…` | 808,515 |

HUL filing metadata (from NSE listing): consolidated Q1 broadcast 2026-07-28 19:41:55 IST (seq 179457),
standalone Q1 19:40:21 (seq 179453) — **Un-Audited** (limited review); consolidated Q4 2026-04-30
21:06:22 IST (seq 155083), standalone Q4 21:04:44 (seq 155079) — Audited. All `Original`.

Target periods were found exactly as specified (June 2026 quarter and March 2026 quarter/year); no
fallback to older periods was needed.

---

## 5. Cash-flow reporting frequency finding (design rule input)

**Rule the evidence supports: for the four ingested exchange instances, cash-flow
statements were ANNUAL-only (full financial year, presented with the
March-quarter results). Quarterly cash flow was not present in the ingested
exchange feed for either issuer in the acquired periods; for Infosys it exists
only in company-IR condensed statements. The app must never fabricate a
quarterly cash-flow statement for an India issuer, and must never treat the
annual CF as a Q4 quarter CF.**

> **CORRECTED (IND-3, 2026-09-11 — reviewer corrections A and B, recorded not
> silently edited):** (1) The phrase "quarterly cash flow does not exist in the
> exchange feed" overgeneralized: the evidence covers TWO issuers × TWO periods
> (4 consolidated instances). The correct statement is "quarterly CF **not
> present in the ingested sources**" — a statement about our corpus, never about
> the companies ("HUL has no quarterly cash flow anywhere" was unsupported;
> HUL's Q1 statements simply do not report CF in anything we ingested). (2) The
> IND-2 encoding took this further and restricted CFO/capex extraction to
> ANNUAL instances — that was WRONG (too restrictive): a cash-flow observation
> is acceptable from any identified official document at its actual reported
> duration (quarter/half-year/9M-YTD/annual/other) with known dates, scope,
> units and provenance; Infosys' Q1 condensed-FS PDF quarterly CFO (₹9,330 Cr,
> p.6) is legitimate and must be extractable. See
> `docs/india_financial_methodology.md` §4 (rewritten) and
> `docs/india_reconciliation_review.md` §5. Missing cash flow is now recorded
> as one of five distinct missing-data statuses, never zero.

Evidence, per company:

- **Infosys**
  - Q1 FY27 **exchange XBRL (consolidated)**: zero cash-flow concepts among its 89 facts; the declaration
    fact `WhetherCashFlowStatementIsApplicableOnCompany` is **absent** (quarterly instances carry only
    P&L, EPS, OCI, segment, and identification facts).
  - Q4+FY26 **exchange XBRL (consolidated)**: full indirect cash-flow statement present;
    `WhetherCashFlowStatementIsApplicableOnCompany = true`, `TypeOfCashFlowStatement = "Cash Flow Indirect"`;
    `CashFlowsFromUsedInOperatingActivities = 339860000000` with context period **2025-04-01 → 2026-03-31**
    (i.e., the full year; ₹33,986 Cr at `LevelOfRounding=Crores`).
  - Company IR: Reg-33 results PDF includes CF statements at pp. 17 and 23 (annual). The condensed
    consolidated Ind AS FS PDF (`consol-fy27-q1-finstatement.pdf`, p.6) additionally carries a **quarterly**
    CF statement headed "Three months ended June 30," — net cash generated by operating activities
    ₹9,330 Cr (Q1 FY27) vs ₹7,632 Cr (Q1 FY26). Infosys is therefore the exception: quarter-granularity CF
    is obtainable, but only from company-IR PDFs, not the exchange feed.
- **Hindustan Unilever**
  - Q1 FY27 **exchange XBRL (consolidated)**: zero cash-flow concepts (82 facts).
  - Q1 FY27 results PDF (30 pages): no cash-flow statement (only "cash flow hedge" OCI mentions).
  - Q1 FY27 results workbook: sheets are exactly `SEBI Consolidated, Segment Consolidated, SEBI Standalone,
    Segment Standalone` — **no cash-flow sheets**.
  - March-quarter FY26 results PDF p.11 (consolidated) and p.19 (standalone): "A CASH FLOWS FROM OPERATING
    ACTIVITIES:" under header **"Year ended 31st March, 2026"** (Rs in Crores) — annual only.
  - March-quarter workbook: adds `Cash Flow Consolidated` / `Cash Flow Standalone` sheets (annual).
  - Q4+FY26 exchange XBRL: `CashFlowsFromUsedInOperatingActivities = 109990000000`, context
    2025-04-01 → 2026-03-31 (₹10,999 Cr), `TypeOfCashFlowStatement = "Cash Flow Indirect"`.

Consequence for metric derivation: India "cash conversion"-style metrics can only be computed on an
annual cadence from exchange data (or quarterly for Infosys if IND-2 ingests the IR condensed-statement
PDFs). Interpolating or dividing annual CF into quarters is forbidden.

---

## 6. Gaps and risks for IND-2

1. **Browser requirement.** Every source blocks plain httpx (Akamai 403 on Infosys/HUL/BSE; silent TCP
   drop on NSE/nsearchives). Ingestion needs either a real-browser fetch layer or manually vendored
   files. Headless-browser reliability against these edges was not measured from the project runtime
   (this audit used an interactive session) — treat as unverified.
2. **Undocumented NSE endpoints.** `/api/integrated-filing-results` works today inside a browser session
   but has no contract; parameters may change silently. Archive file URLs embed internal IDs — never
   hardcode; always resolve via the listing API.
3. **Frozen legacy module.** NSE's legacy financial-results API stops at the Dec-2024 quarter and ignores
   date parameters. Do not build on it.
4. **Scope is per-filing, not per-file dimension.** Exchange filings come as two separate XBRL instances
   (Consolidated, Standalone). Infosys' IR Reg-33 PDF bundles both scopes in one document — the parser
   must split by section.
5. **Taxonomy is not US-GAAP.** `in-capmkt` (SEBI Integrated Finance Ind AS, IFIndAs V2.1, dated
   2026-06-26) concepts differ from `us-gaap`; `schemaRef` is relative (schema not bundled), so offline
   XBRL validation needs the SEBI taxonomy fetched separately. Concept mapping table is still to be
   written (SPEC 27 "create explicit concept mappings").
6. **Scale semantics.** `LevelOfRounding = "Crores"` but values are full rupees; HUL PDFs state
   "Rs in Crores"; Infosys condensed statements "In ₹ crore". Unit normalization (rupees vs lakh/crore)
   is mandatory before any comparison (SPEC 27).
7. **Audited-status variance.** Infosys labels Q1 results "Audited"; HUL labels them "Un-Audited"
   (limited review). `audited_status` must come from the filing, never inferred.
8. **Revisions untested.** The listing carries `type_Sub = Original` plus revision fields; no revised
   submission was present to test against. Revision handling remains a designed-for unknown.
9. **HUL discontinued operations.** FY2025-26 results include discontinued operations (ice-cream
   demerger; e.g. PBT from discontinued ops ₹4,336 Cr in the annual CF statement). Growth/derivation code
   must handle continuing-vs-total explicitly.
10. **BSE path unassessed.** BSE hosts equivalent filings (Integrated Filing tab, "from March 2025
    quarter"), but attachment download was not exercised (403 to non-browser clients; NSE sufficed).
11. **Infosys dual reporting standards.** Infosys publishes IFRS (USD and INR) alongside Ind AS; IFRS
    press releases were collected as commentary only. `reporting_standard` for stored facts must default
    to Ind AS for the exchange record; mixing IFRS and Ind AS facts in one series is a contamination risk.
12. **Coverage.** Only 2 of 10 watchlist companies verified; the other 8 rows are `proposed` with empty
    verified fields by design. Exchanges' own company directories were not yet used to verify them.

---

## 7. Recommended ingestion tier order (for the next milestone)

1. **Tier 1 — deterministic:** NSE Integrated Filing (Finance) **XBRL instances** (consolidated first,
   standalone second), discovered via the integrated-filing listing, fetched through a browser-grade
   client. Cash-flow facts only from the annual (Q4) filing. Stable identity = (ISIN, scope, period,
   seq_Id, content hash) — never the URL.
2. **Tier 2 — structured:** company-IR **Excel workbooks** (HUL pattern: SEBI/Segment/Balance Sheet/Cash
   Flow sheets) where offered.
3. **Tier 3 — PDF:** company-IR results PDFs (Infosys Reg-33 bundle; HUL results PDF). Needed for
   quarterly cash flow in Infosys' case (condensed FS PDF) and for auditors' report context.
4. **Tier 4 — RAG only (no facts):** press releases, results presentations, earnings-call transcripts,
   with page-level citations (SPEC 27).

Fetch posture: reuse the exchange tier before IR when scope and period match; keep >= 1 s spacing per
exchange host; never retry-loop against the bot edges; on block, record and fall back per the audit.

---

## 8. Artifacts produced by this milestone

- `storage/raw/india/infosys/{q1_fy2026-27,q4_fy2025-26}/` — 13 files (incl. `exchange_nse/` subdirs)
- `storage/raw/india/hindustan_unilever/{q1_fy2026-27,q4_fy2025-26}/` — 13 files (incl. `exchange_nse/` subdirs)
- `tests/fixtures/india/manifest.json` — provenance manifest (policy, taxonomy note, issuer/identifier
  verification, all 26 files with URL, tier, method, date, sha256, size)
- `tests/fixtures/india/INFY-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml`,
  `INFY-Q4FY26-consolidated-nse-integrated-filing-xbrl.xml`,
  `HUL-Q1FY27-consolidated-nse-integrated-filing-xbrl.xml`,
  `HUL-Q4FY26-consolidated-nse-integrated-filing-xbrl.xml` — committed fixtures (byte-identical to
  storage copies; each < 5 MB, none excerpted)
- `data/watchlist_india.csv` — 10 rows; INFY/HINDUNILVR verified, 8 proposed

---

# 9. IND-2 parsing findings (append; 2026-09-11)

IND-2 built `src/quarterline/sources/india/` against the IND-1 fixtures and proved the parser on the
four committed consolidated instances. Everything below was **observed in the instances**, not assumed.

## 9.1 Actual tags observed (SEBI `in-capmkt`, IFIndAs) and canonical mapping

Mapping lives in `data/tagmap_india.yml` (loaded by `concept_map.py`). US concept IDs are NOT reused;
an India fact can never normalize under a us-gaap-derived concept name.

| Canonical concept | Primary tag (priority 0) | Fallback tag(s) | Certainty |
|---|---|---|---|
| `revenue_from_operations` | `RevenueFromOperations` | — | certain |
| `total_income` | `Income` | — | certain (Ind AS "Total income") |
| `profit_before_tax` | `ProfitBeforeTax` | `ProfitBeforeExceptionalItemsAndTax` | fallback **uncertain**: before-exceptional variant (differs for HUL Q4: 39,280 vs 36,810 Cr) |
| `profit_after_tax` | `ProfitLossForPeriod` | `ProfitLossForPeriodFromContinuingOperations` | fallback **uncertain**: continuing-only; differs materially where discontinued ops exist (HUL FY26: 1,50,590 vs 1,06,670 Cr) |
| `profit_attributable_to_owners` | `ProfitOrLossAttributableToOwnersOfParent` | — | certain |
| `exceptional_items` | `ExceptionalItemsBeforeTax` | — | certain |
| `eps_basic` | `BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations` | `BasicEarningsLossPerShareFromContinuingOperations` | fallback **uncertain** (continuing-only; HUL Q4 12.73 vs 12.76) |
| `eps_diluted` | `DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations` | `DilutedEarningsLossPerShareFromContinuingOperations` | fallback **uncertain** (same caveat) |
| `cash_flow_operations` | `CashFlowsFromUsedInOperatingActivities` | — | certain; **annual instances only** |
| `capex` | `PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities` | `PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities` | fallback **uncertain**: whether Ind AS capex includes intangibles is a derivation-wave decision, not assumed |

> **CORRECTED (IND-3, 2026-09-11, recorded not silently edited):** (1) the crore
> displays in the `profit_before_tax` and `profit_after_tax` rows above carry a
> ×10 slip: the underlying rupee values are exact, and the correct crore
> renderings are ₹3,928 vs ₹3,681 Cr (HUL Q4 quarter PBT vs before-exceptional)
> and ₹15,059 vs ₹10,667 Cr (HUL FY26 annual total vs continuing PAT). (2) The
> `cash_flow_operations` row's "annual instances only" restriction is superseded
> by the IND-3 corrected cash-flow policy
> (`docs/india_financial_methodology.md` §4): cash-flow concepts are accepted at
> their ACTUAL reported duration from any identified official document.

Notable DELIBERATELY-UNMAPPED tags (surfaced in `IndiaInstance.unmapped_tags`, never silently dropped):
`OtherIncome` (already inside `Income`), `Expenses`, `TaxExpense`, `CurrentTax`, `DeferredTax`,
discontinued-ops tags (`ProfitLossFromDiscontinuedOperationsAfterTax`), comprehensive-income tags,
`PaidUpValueOfEquityShareCapital` / `FaceValueOfEquityShareCapital`, and all `Segment*` tags. One trap
found in the real data: INFY Q1 carries a `SegmentRevenue` total on the **undimensioned** quarter
context — it is a segment-disclosure total, not the P&L revenue line, and stays unmapped.

## 9.2 Scope, revision and audit expression in the instances

- **Scope is NOT an XBRL dimension.** Consolidated vs standalone are two separate filings/instances.
  Inside an instance the scope is declared by the `NatureOfReportStandaloneConsolidated` qualifier
  fact (`"Consolidated"` / `"Standalone"`); `ir_documents.import_document` cross-checks the caller's
  scope against this declaration and refuses mismatches.
- **Revision status is NOT in the instance.** It exists only in the NSE listing metadata
  (`revision: Original`, plus `revised_Date`/`revision_Remark` fields); IND-2 carries it via
  `FilingMeta` / the `.filing.json` sidecar written at import time. Unknown stays unknown (never
  inferred from content).
- **Audit fields the instance DOES carry** (all surfaced verbatim): `WhetherResultsAreAuditedOrUnaudited`
  (Infosys Q1 `"Audited"` vs HUL Q1 `"Unaudited"` — variance confirmed), `WhetherResultsAreAuditedOrUnauditedForImpactOfAuditQualification`,
  `DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification` ("Declaration of unmodified
  opinion" / "Not applicable"), `DeclarationPursuantToClauseDOfSubRegulation3OfRegulation33OfSEBILODRRegulation2015`,
  auditor facts (`AuditorsFirmName` etc. under `D_Auditor1` with auditor dimension).
- **FINDING — taxonomy version varies per filing.** The Q1 instances (broadcast July 2026) carry the
  header comment `IFIndAs V2.1 (26-06-2026)`, but BOTH Q4+FY26 instances (filed April 2026) carry
  `IFIndAs V2.0 (06-02-2026)`, which predates the V2.1 release. This refines the IND-1 manifest note
  ("every instance carries V2.1"): the parser carries the per-instance version
  (`IndiaInstance.taxonomy_version`) and nothing may assume V2.1.
- Other qualifier facts confirmed: `LevelOfRounding="Crores"` (presentation only — values are full
  rupees), `TypeOfReportingPeriod="Quarterly"`, `ReportingQuarter="First/Fourth quarter"`,
  `DateOfStart/EndOfFinancialYear`, `DateOfStart/EndOfReportingPeriod`, `DescriptionOfPresentationCurrency="INR"`,
  entity identifier by BSE scrip code, units `INR` and `INRPerShare` (divide), `decimals="-7"` for
  money and `decimals="INF"` for per-share.
- **Contexts observed:** undimensioned `OneD` (quarter), `FourD` (full year, Q4 filings only), `OneI`
  (instant), `PY_I` (prior-year instant, balance-sheet comparatives only — NO prior-year duration
  contexts are present in these Q4 instances), plus dimensioned segment/expense-breakdown/auditor
  contexts. No 6M/9M contexts occur in these fixtures (classification still supports them).
- **INFY Q4 fact count is much larger than HUL's** (526 vs ~300 facts) and INFY's standalone Q4
  instance is 1.8 MB vs 34 KB for HUL Q1 — instance size varies wildly; never assume a size shape.

## 9.3 Parsing milestone artifacts

- `src/quarterline/sources/india/`: `xbrl_parse.py` (stdlib ElementTree; contexts, units, decimals,
  qualifiers, per-instance taxonomy version), `units.py` (LAKH/CRORE, full-rupee passthrough, EPS
  never scaled, display-string parser with explicit declared scale), `periods.py` (FY = 1 Apr → 31 Mar;
  quarter/YTD/annual/instant from dates only), `concept_map.py` + `data/tagmap_india.yml`,
  `revisions.py` (`select_latest`, as-of aware), `issuers.py` (registry, verified-only guard),
  `pdf_results.py` (thin scale/scope header detection; ambiguous → `review_required`), `nse.py` /
  `bse.py` (documented endpoint stubs raising `ManualImportRequired`), `ir_documents.py` (manual
  import → sha256 → content-addressed cache → `source_artifacts` → company upsert country `IN`
  currency `INR`), `pipeline.py` (idempotent `fact_observations` ingestion; re-run adds 0 rows),
  `reconcile.py` (source-linked table + `data/india_reconciliation.csv`).
- Granted single edit to `src/quarterline/ingest/cache.py`: added `cache_file()` (content-addressed
  manual-import helper mirroring `cache_put` style). Existing functions untouched.
- `data/india_reconciliation.csv` — 78 rows generated from the four fixture parses
  (columns: issuer, document, page_or_tag, concept, period, scope, reported_value, normalized_value,
  review_status; every row `reconciled_to_source` because every value comes from a parsed instance).
- `scripts/india_ind2_verification.py` — reproduces the end-to-end INFY verification on a scratch store.
- Verification (2026-09-11): `uv run pytest -q` → 693 passed, 6 skipped; `uv run ruff check .` clean.
  INFY end-to-end: 2 artifacts parsed, 39 observations inserted, second run 0 inserted.
- **Known wiring gap (orchestrator):** `quarterline.cli._WAVE_PACKAGES` does not include
  `quarterline.sources.india`, and `cli.build_parser()` declares no `ingest .../india-document` or
  `verify .../india` subparsers, so `python -m quarterline verify india` fails at argparse level
  (`invalid choice: 'india'`). The handlers themselves are registered at package import
  (`ingest:india-document`, `verify:india`) and were exercised directly; see the package docstring
  for the exact one-line fixes.

---

# 10. IND-3 claim audit (2026-09-11, reviewer correction B)

Every previously published India claim that carries financial or access consequences is
re-audited below. Column semantics: **scope of evidence** states exactly what was tested;
**verdict** is `verified` only when the evidence fully supports the claim as now worded.
Claims were rescoped in place (with `CORRECTED` notes at §2 and §5) rather than silently
rewritten, per reviewer instruction.

| # | Claim (as now worded) | Supporting evidence | Scope of evidence | Verdict | Required correction (from the original claim) |
|---|---|---|---|---|---|
| 1 | **Quarterly cash-flow availability:** "Quarterly CF was not present in the ingested exchange sources for INFY/HUL in the acquired periods; INFY publishes a legitimate quarterly CFO in its company-IR condensed-FS PDF; HUL's Q1 ingested documents report no CF at all." | INFY Q1 FY27 consolidated instance: 0 CF facts among 89, `WhetherCashFlowStatementIsApplicableOnCompany` absent; HUL Q1 instance: 0 CF facts among 82; HUL Q1 results PDF (30 pp.): no CF statement; HUL Q1 workbook: sheets exactly `SEBI Consolidated, Segment Consolidated, SEBI Standalone, Segment Standalone`; INFY Q1 condensed-FS PDF p.6: "Net cash generated by operating activities 9,330" under "for the three months ended June 30, 2026" (extracted 93,300,000,000 with page provenance). | 2 issuers × 2 periods of exchange instances; 4 company-IR PDFs + 2 workbooks, all cached 2026-09-11. Does NOT cover: other issuers, other periods, BSE copies, or future filings. | **Verified** (as rescoped). | Original "quarterly cash flow does not exist in the exchange feed" universalized 4 instances into the whole feed; "HUL has no quarterly cash flow anywhere" was unsupported. Reworded as corpus-scoped absence with a distinct missing-data status (`not_present_in_ingested_sources`), never zero. |
| 2 | **Half-year cash-flow support:** "No half-year CF was present in the ingested sources, and none is derivable yet; derivation annual − H1 = H2 is implemented only as a checked operation that requires both cumulative observations to exist." | No 6-month context occurs in any of the 4 committed instances (verified: only 3-month and 12-month duration contexts); no half-year CF document was acquired. `cash_flow.derive_by_subtraction` implements and tests the compatibility-checked subtraction; nothing divides. | Same 4 instances; derivation logic is synthetic-tested only (no real H1 document acquired). | **Verified** (design-level; untested against real half-year data). | IND-2 had no explicit half-year statement; the corrected contract now defines the eligible duration vocabulary (quarter/half_year/nine_month_ytd/annual/other_duration) and forbids fabrication. |
| 3 | **Browser vs programmatic access:** "These tested URLs, accessed with plain httpx on 2026-09-11, returned 403 (Akamai) or silently dropped the connection; the same files fetched through an interactive Chromium session succeeded. Browser-assisted acquisition is a prototype; no conclusion about all automated access or redistribution rights follows." | §2 per-source table (403 refs `18.bf3b4017…`, `18.2cfed417…`; NSE silent TCP drop); manifest `fetch_methods_used` (httpx rejected; browser byte-exact, sha256-verified). | ~30 requests over ~1 hour on 2026-09-11 from one network fingerprint; headless/automation clients untested from the project runtime. | **Partially verified** — accurate for what was tested; any wider claim is unsupported. | Original "every Indian source rejects plain HTTP" conflated non-browser HTTPS clients with unencrypted HTTP, and overgeneralized. Rescoped at §2 with the correction note. |
| 4 | **Exchange filing scope declarations:** "Consolidated vs standalone is a per-filing property (two separate instances per period), declared inside each instance by `NatureOfReportStandaloneConsolidated` and cross-checked at import." | All 4 consolidated fixtures declare `NatureOfReportStandaloneConsolidated = "Consolidated"`; NSE listing carries separate Consolidated/Standalone filings per period (12 each for both issuers, Mar-2025 → Jun-2026); `ir_documents.import_document` refuses scope mismatches. | The 4 committed consolidated instances + listing metadata; standalone instances cached but not committed. | **Verified**. | None — claim stood as scoped; now provenance-backed in the review packet. |
| 5 | **Fiscal-year labels:** "The source's own labels (Infosys' 'IFRS/Ind AS quarterly results, June 2026 quarter' style; instance qualifiers `ReportingQuarter='First quarter'`, `TypeOfReportingPeriod='Quarterly'`) are metadata. The application's `Q1 FY2026-27` is Quarterline's own presentation layer. Exact context start/end dates are the primary truth, and the application label is derived from those dates only." | Instance qualifier facts in all 4 fixtures; Reg-33 PDF column headers ("Quarter ended June 30, 2026"); context dates verified against every mapped fact (e.g. INFY Q1 revenue context 2026-04-01..2026-06-30). | All 4 fixtures + 2 Reg-33 PDFs. | **Verified**. | IND-2 docs carried no explicit application-vs-source label separation; now stated in methodology §3 and the period-label validation table (review packet §4). |
| 6 | **Currency/scale interpretation:** "Fact values are exact full rupees; `decimals='-7'` is the source's rounding PRECISION; `LevelOfRounding='Crores'` is presentation metadata — never a multiplier. Displayed crore values reconcile exactly at the declared precision (482110000000 → ₹48,211 Cr)." | `decimals="-7"` on every money fact and `decimals="INF"` on per-share facts in all 4 fixtures; `units.normalize_amount` passthrough; display reconciliation asserted against real fixtures; rendered statements ("In ₹ crore" / "(Rs in Crores)") match value/10^7 exactly for every agent-checked reference. | All 4 fixtures; cross-checked displays on 5 rendered PDF pages. | **Verified**. | IND-2 stated the rule but had no test proving raw values are not multiplied twice, nor exact display reconciliation; added (tests `TestFixturePrecisionReconciliation`). |
| 7 | **Revision-selection behavior:** "Revision status exists only in the exchange listing (carried, never inferred); latest-available selection keeps history; a revised filing must not replace facts of a different period/scope/unit/concept; a later filing's comparative value is NOT a revision; duplicate copies of a document are idempotent." | Listing fields `type_Sub`/`revised_Date`/`revision_Remark` (all acquired filings `Original`); observation hash includes scope+value so a revision is a new identity while re-reports are idempotent; `revisions.classify_filing_pair` (IND-3) distinguishes duplicate_copy / original / revised / later_comparative; `select_latest` rejects mixed periods. | Selection logic synthetic-tested (no real revised submission was ever present to acquire — designed-for unknown). | **Partially verified** — logic verified; real-world revised filings untested. | IND-2 conflated "duplicate filing versions" with "revisions"; now four distinct categories with tests. |
