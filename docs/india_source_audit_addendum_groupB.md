# India source audit — addendum group B (IND-6b: Maruti Suzuki, UltraTech Cement, Sun Pharmaceutical, Larsen & Toubro)

Status: acquisition milestone addendum, complete as of **2026-09-11**.
Parent document: `docs/india_source_audit.md` (IND-1); sibling: `docs/india_source_audit_addendum_groupA.md`
(IND-6a). Scope is identical: identifier verification, official document acquisition for the two target
periods (Q1 FY2026-27, Q4+FY 2025-26), access-condition recording, and cash-flow availability evidence.
Normalization, parsing, and scoring remain out of scope.

Method note: all documents were fetched **2026-09-11 through a normal interactive browser session**
(same pattern as IND-1/IND-6a). No captcha was solved, no cookie was forged, no header spoofing, no
aggregator (Screener/Trendlyne/Moneycontrol) was used. Every file listed was (re)fetched by this
milestone through the browser and verified byte-exact via sha256 after transfer. **Retry context:**
this milestone is a retry of an earlier same-day attempt that died before documenting anything; that
attempt left unverified files in `storage/raw/india/` for these four issuers. Per the group A policy,
every such file was re-fetched from source and confirmed byte-identical before being accepted — all
eight exchange XBRLs and all four IR PDFs matched today's fetches exactly. Two artifacts left by the
earlier attempt could **not** be byte-verified and were **removed** rather than accepted:
`ultratech_cement/q1_fy2026-27/ultratech-q1fy27-results-press-release.html` (a rendered page dump —
rendered HTML is not byte-stable, so it cannot pass hash verification; the UltraTech Q1 press release
remains available at `https://www.ultratechcement.com/corporate/media/press-releases/financial-results-q1fy27`
and is recorded as URL-only) and `ultratech_cement/q1_fy2026-27/exchange_bse/ultratech-q1fy27-bse-results-summary.json`
(a BSE results-summary API response — a derived summary, not a filing document).

Provenance manifest: `tests/fixtures/india/manifest_groupB.json` (entry shape mirrors IND-6a's
`manifest_groupA.json`: `committed_fixtures` + `storage_cache_only`, plus a `cash_flow_evidence` block).

---

## 1. Identifier verification (official/primary sources only)

| Issuer | ISIN | NSE symbol | BSE scrip code | Where verified |
|---|---|---|---|---|
| Maruti Suzuki India Limited (`IN-MARUTI`) | `INE585B01010` | `MARUTI` | `532500` | 1) NSE quote page heading "Maruti Suzuki India Limited (INE585B01010)" (series EQ); 2) BSE stock page body "Maruti Suzuki India Ltd … (INE585B01010)" at `…/maruti/532500/`; 3) inside the NSE XBRL instance (`in-capmkt:ISIN`, `Symbol`, `ScripCode`) |
| UltraTech Cement Limited (`IN-ULTRACEMCO`) | `INE481G01011` | `ULTRACEMCO` | `532538` | 1) NSE quote page heading "UltraTech Cement Limited (INE481G01011)"; 2) BSE stock page header "(ULTRACEMCO \| 532538 \| INE481G01011)"; 3) inside the NSE XBRL instance |
| Sun Pharmaceutical Industries Limited (`IN-SUNPHARMA`) | `INE044A01036` | `SUNPHARMA` | `524715` | 1) NSE quote page heading "Sun Pharmaceutical Industries Limited (INE044A01036)"; 2) BSE stock page header "(SUNPHARMA \| 524715 \| INE044A01036)"; 3) inside the NSE XBRL instance |
| Larsen & Toubro Limited (`IN-LT`) | `INE018A01030` | `LT` | `500510` | 1) NSE quote page heading "Larsen & Toubro Limited (INE018A01030)"; 2) BSE stock page header "(LT \| 500510 \| INE018A01030)"; 3) inside the NSE XBRL instance |

All identifiers agree across exchange and filing sources for all four issuers. All four NSE symbols
were confirmed as-is from the watchlist (`MARUTI`, `ULTRACEMCO`, `SUNPHARMA`, `LT` — no renames).
This raises verified watchlist coverage to **10 of 10** (the CSV itself is updated by a later merging
agent). BSE body-ISIN discipline was applied throughout (group A's title-echo trap avoided; no title
text was treated as evidence).

---

## 2. Document inventory (acquired, hashed)

8 consolidated exchange XBRLs are committed as fixtures (byte-identical to their storage copies; all
< 5 MB; none excerpted); PDFs stay storage-only. Full per-file provenance (URL, seq_id, broadcast,
audited status, revision, sha256, size) is in `tests/fixtures/india/manifest_groupB.json`.
Abbreviated inventory:

| Issuer | Period | Scope | Doc | Format | Tier | sha256 (first 12) | Bytes |
|---|---|---|---|---|---|---|---|
| Maruti Suzuki | Q1 FY27 | consolidated | financial_results | XBRL | exchange (NSE) | `7fb5064437ee…` | 16,554 |
| Maruti Suzuki | Q4+FY26 | consolidated | financial_results | XBRL | exchange (NSE) | `e3930de091a3…` | 65,791 |
| Maruti Suzuki | Q1 FY27 | cons+standalone | financial_results (Reg-33, **no CF**) | PDF | company_ir | `3b30f5505021…` | 1,533,467 |
| Maruti Suzuki | Q1 FY27 | consolidated | results_presentation | PDF | company_ir | `bcc231d87d18…` | 445,755 |
| UltraTech | Q1 FY27 | consolidated | financial_results | XBRL | exchange (NSE) | `8b19da5c20e3…` | 16,083 |
| UltraTech | Q4+FY26 | consolidated | financial_results | XBRL | exchange (NSE) | `48970feff6d0…` | 62,418 |
| UltraTech | Q1 FY27 | consolidated | financial_results (limited-review report + statement, **no CF**) | PDF | company_ir | `a402f6b63c6e…` | 5,614,883 |
| UltraTech | Q1 FY27 | consolidated | results_presentation (21.1 MB) | PDF | company_ir | `213291043b3b…` | 21,104,239 |
| Sun Pharma | Q1 FY27 | consolidated | financial_results | XBRL | exchange (NSE) | `9fd3435e3a3c…` | 15,226 |
| Sun Pharma | Q4+FY26 | consolidated | financial_results | XBRL | exchange (NSE) | `6e96cf3f1355…` | 62,447 |
| Sun Pharma | Q1 FY27 | consolidated | financial_results (statement, **no CF**) | PDF | company_ir | `8565330c7082…` | 592,712 |
| Sun Pharma | Q1 FY27 | consolidated | press_release | PDF | company_ir | `2b9b4d98e842…` | 345,137 |
| L&T | Q1 FY27 | consolidated | financial_results | XBRL | exchange (NSE) | `50737f577af7…` | 83,029 |
| L&T | Q4+FY26 | consolidated | financial_results (Original — no consolidated revision exists) | XBRL | exchange (NSE) | `235f96d9a399…` | 149,489 |
| L&T | Q1 FY27 | consolidated | financial_results (statement, **no CF**) | PDF | company_ir | `a71aadd8b847…` | 499,433 |
| L&T | Q4+FY26 | standalone — **Revision** (seq 156063, latest of two; evidence only) | XBRL | exchange (NSE) | `22ee5f334fa3…` | 2,946,994 |

Filing metadata observed in the NSE Integrated Filing- Financials listing (all consolidated unless
stated): Maruti Q1 seq 181271, 31-Jul-2026 18:27:07 IST, Un-Audited, Original; Maruti Q4 seq 153810,
28-Apr-2026 19:47:40 IST, Audited, Original. UltraTech Q1 seq 176208, 20-Jul-2026 19:58:45 IST,
Un-Audited, Original; UltraTech Q4 seq 153313, 27-Apr-2026 20:18:12 IST, Audited, Original.
Sun Pharma Q1 seq 181265, 31-Jul-2026 18:24:07 IST, Un-Audited, Original; Sun Pharma Q4 seq 160266,
22-May-2026 18:39:14 IST, Audited, Original. L&T Q1 seq 179377, 28-Jul-2026 18:17:12 IST,
Un-Audited, Original; L&T Q4 consolidated seq 155661, 05-May-2026 19:30:17 IST, Audited, Original.
No consolidated revision exists for any group B issuer-period.

Not obtained (with reason): standalone XBRLs and iXBRL renderings for the target periods (not
required; consolidated is the ingest priority — except the one standalone revision cached as revision
evidence, above); L&T Q4 **standalone** superseded Original/first-revision instances (out of scope;
identity recorded in the manifest); L&T press release/presentation (optional; not pursued after the
required items were secured); UltraTech press release kept as URL-only (see method note). Nothing
required was unobtainable.

---

## 3. Access conditions encountered

All via the browser session; plain non-browser clients were not re-tested (IND-1 §2 stands).

| Source (role) | URLs exercised | Result | Notes |
|---|---|---|---|
| `www.nseindia.com` (quote page + integrated-filing API) | `/get-quote/equity/<SYM>/…`, `GET /api/integrated-filing-results?&symbol=<SYM>&type=Integrated%20Filing-%20Financials&page=1&size=40` | Works fully from the page's own session | Same undocumented API as IND-1/IND-6a; returned 12 filings per symbol, fields identical (`seq_Id`, `broadcast_Date`, `consolidated`, `audited`, `type_Sub`, `revised_Date`, `revision_Remark`, `xbrl`, `ixbrl`, `qe_Date`) |
| `nsearchives.nseindia.com` (file archive) | direct navigations to `/corporate/xbrl/INTEGRATED_FILING_INDAS_<id>_<ts>_WEB.xml` | Direct file GET works; XML served inline | Same-origin `fetch()` inside the served XML page returns byte-exact bodies. CORS discipline re-confirmed: a `fetch()` of `nsearchives…` attempted from `investors.larsentoubro.com` failed with a fetch error; re-navigating to an `nsearchives` URL first resolved it (same operational pattern as group A's mid-fetch incident) |
| `www.bseindia.com` (stock pages) | `/stock-share-price/…` for 532500, 532538, 524715, 500510 | Pages render identifiers/ISIN in body after JS load (5–6 s wait) | One navigation (Sun Pharma) timed out at the tool's 30 s limit while the page continued loading in the background and completed ~6 s later — no retry was needed. Attachment downloads again not exercised (NSE sufficed) |
| `www.marutisuzuki.com` (IR) | `/corporate/investors/company-updates` > Quarterly Reports dropdown, `content/dam/arena-eds/corporate/pdf/company-reports/2026-2027/*.pdf` | Works fully | `/investors` and `/corporate/investors/financials` are 404/redirect traps — the canonical quarterly-results listing is under **company-updates**, a JS dropdown (categories × FY); options only exist in DOM, so the category button was clicked programmatically. DAM folder `company-reports/<FY>/` with descriptive filenames |
| `www.ultratechcement.com` (IR) | `/corporate/investors-/financials-`, `/corporate/media/press-releases/financial-results-q1fy27`, `content/dam/ultratechcementwebsite/pdf/financials/**` | Works fully | Note the trailing-hygiene slugs (`investors-`, `financials-`). The Financials page's quarterly-results links are **server-rendered but hidden** — raw HTML (fetched same-origin) contains the full DAM listing back to FY10 under `pdf/financials/financial-results/fy<NN>/q<N>/`; the visible DOM shows only annual reports until JS interaction. The Q1 press release is an HTML page (no PDF), recorded URL-only |
| `sunpharma.com` (IR) | `/investors/` (WordPress), `wp-content/uploads/2026/07/*.pdf` | Works fully | Most conventional layout of the group: per-quarter upload folder with `Q1FY27-SPIL-Financial-Results.pdf` + press-release PDFs |
| `investors.larsentoubro.com` (IR) | `/Quarterly-Results-Archives.aspx` | Works fully | L&T's IR lives on a **separate subdomain**; `www.larsentoubro.com/corporate/investors*` paths are 404s. Results download link is built in a JS `onclick` handler (`fnDownloadpdf('…/upload/Quarterly/FY2027QuarterlyLTJune2026-website.pdf')`) — invisible to plain anchor enumeration |

Pacing: fetches spaced >= 4 s per host; no rate-limit or Retry-After headers observed anywhere. The
shared browser session (left over from group A / the earlier attempt) was reused via a dedicated tab;
no other agent's tab was touched.

---

## 4. Revision finding — L&T Q4+FY26 standalone (scope-asymmetric revision, metadata-only cause)

Group A documented the corpus's first real revision (Asian Paints Q4 consolidated). Group B adds a
second, structurally different case:

- L&T's Q4 FY26 **consolidated** filing has **no revision** (single Original, seq 155661).
- The Q4 FY26 **standalone** filing was revised **twice**: Original seq 155701 (broadcast
  05-May-2026 21:48:59 IST) → Revision seq 155858 (06-May-2026 19:29:57 IST, remark verbatim
  "Share Capital number updated in the required format_") → Revision seq 156063 (07-May-2026
  18:28:47 IST, remark verbatim: "The amount reported under Paid_up Share Capital in the XBRL
  filing _Financial Results Section_ was inadvertently mentioned as number of shares instead of the
  corresponding amount_ The same has now been corrected_ There is no impact on the financial results_").
- The latest standalone revision (seq 156063, 2.9 MB, IFIndAs V2.0) is cached storage-only as
  revision evidence (`LT-Q4FY26-standalone-nse-integrated-filing-xbrl-REVISION.xml`).

New consequences beyond group A's findings: (1) **revisions can be scope-asymmetric** — standalone
revised while consolidated is not, so revision selection must be per (issuer, period, scope), never
per issuer-period; (2) a revision can chain (**two** sequential revisions of one Original), so
selection must pick the latest revision, not "original + one revision"; (3) a revision may exist for
**XBRL metadata corrections only** (share capital reported as a count instead of an amount) with
"no impact on the financial results" — i.e. a revision does not imply any financial fact changed;
(4) as with Asian Paints, `broadcast_ist` is `null` on revision rows and `revised_Date` carries the
timestamp; the revision chain is identifiable only through the listing metadata.

---

## 5. Taxonomy versions observed (per instance)

| Instance | Header comment |
|---|---|
| All four Q1 FY27 consolidated (filed Jul 2026) | `IFIndAs V2.1 (26-06-2026)` |
| All four Q4+FY26 consolidated (filed Apr/May 2026) | `IFIndAs V2.0 (06-02-2026)` |
| L&T Q4 standalone Revision (re-filed 07-May-2026) | `IFIndAs V2.0 (06-02-2026)` |

Consistent with the IND-1/IND-6a pattern (V2.0 for April/May filings, V2.1 for July filings). Note
the refinement: L&T's standalone revision was re-filed **within days** (not months, as Asian Paints')
and still carries V2.0 — so taxonomy version tracks the filing-generation date, not the revision
event. Namespace and schemaRef behavior unchanged (`in-capmkt` 2026-01-31; relative schemaRef,
schema not bundled).

---

## 6. Cash-flow availability evidence (per issuer)

Per IND-3's rule: report what each document actually shows, at its actual frequency.

| Issuer | Q1 FY27 exchange XBRL (consolidated) | Q4+FY26 exchange XBRL (consolidated) | Q1 FY27 company-IR PDF | Q1 PDF prior-year comparative |
|---|---|---|---|---|
| Maruti Suzuki | 0 cash-flow facts; `WhetherCashFlowStatementIsApplicableOnCompany` absent | Annual CF: `CashFlowsFromUsedInOperatingActivities = 190999000000` (2025-04-01..2026-03-31, ₹19,099.9 Cr; instance declares LevelOfRounding=Millions so the display unit is ₹ million: 190,999) | NOT PRESENT (Reg-33 standalone+consolidated results PDF; zero "cash" text mentions) | YES ("Quarter ended June 30, 2025" Unaudited column; values in ₹ million) |
| UltraTech | 0 cash-flow facts; declaration fact absent | Annual CF: `153158600000` (₹15,315.86 Cr) | NOT PRESENT (limited-review report + Reg-33/52(4) statement; zero cash-flow mentions) | YES (30/06/2025 Unaudited column + Year Ended 31/03/2026; in ₹ Crore) |
| Sun Pharma | 0 cash-flow facts; declaration fact absent | Annual CF: `124191800000` (₹12,419.18 Cr) | NOT PRESENT (consolidated results statement; zero cash-flow mentions) | YES (30.06.2025 Unaudited column; "( in Million)") |
| L&T | 0 cash-flow facts; declaration fact absent | Annual CF: `167409700000` (₹16,740.97 Cr) | NOT PRESENT (consolidated results statement; zero cash-flow mentions) | YES (June 30, 2025 [Reviewed] column + Year ended March 31, 2026; in ₹ Crore) |

Corpus-wide picture after group B: the IND-1/IND-3/IND-6a pattern (quarterly CF absent from the
exchange feed; annual CF presented with the March-quarter filing) now holds for **10 issuers × 2
periods** of consolidated instances. In group B, **none** of the four Q1 IR PDFs carries any
cash-flow statement, so no group B issuer offers quarter-granularity CF in the ingested corpus
(Infosys and TCS remain the only ones). The no-fabrication rule (never treat annual CF as a quarter;
never interpolate) applies unchanged.

---

## 7. Other observations for the ingestion milestone

1. **`LevelOfRounding` varies by issuer, not by filing.** MARUTI and SUNPHARMA instances declare
   `Millions`; ULTRACEMCO and LT declare `Crores` — consistently across both periods. Values remain
   exact full rupees in all eight instances (e.g. Maruti revenue context 2026-04-01..2026-06-30,
   `RevenueFromOperations = 524698000000` = ₹524,698 million = ₹52,469.8 Cr); `decimals` also varies
   (`-6`/`-5` here vs `-7` in IND-1's corpus). Unit/display normalization must read the per-instance
   declaration, never assume Crores.
2. **Single-segment issuer case (Maruti).** Its Q1 instance declares
   `IsCompanyReportingMultisegmentOrSingleSegment = "Single segment"` with a narrative
   `DescriptionOfSingleSegment` fact and carries **no** `Segment*` dimensional facts — parsers must
   tolerate a segment-less instance (the IND-1 corpus contained only multi-segment issuers). Maruti
   also carries an explanatory-text fact stating notes are omitted from XBRL due to the character
   limit and pointing to the PDF.
3. **Instance sizes vary ~10× at Q1 consolidated scope** (Sun Pharma 15 KB vs L&T 83 KB; Q4: Maruti
   66 KB vs L&T 149 KB). Never assume a size or segment-count shape (extends IND-6a finding).
4. **Broadcast-date spread continues:** March-quarter consolidated filings 27-Apr (UltraTech),
   28-Apr (Maruti), 05-May (L&T), 22-May (Sun Pharma); June-quarter filings 20-Jul → 31-Jul.
   Filing-driven discovery, not calendar assumptions, remains mandatory.
5. **IR-site structural variance grows:** group B adds (a) a JS-dropdown filing library whose options
   exist only in the DOM (Maruti), (b) server-rendered-but-hidden DAM link lists in raw HTML
   (UltraTech), (c) a separate IR subdomain with JS-`onclick` download links (L&T), and (d) a plain
   WordPress upload folder (Sun Pharma). Discovery must always go through the IR listing page per
   issuer, never a shared URL pattern.
6. **Q1 audit-status variance continues:** all four group B issuers label their Q1 consolidated
   results Un-Audited/Unaudited (limited review) — consistent with HCLTech/ITC/Asian Paints, unlike
   Infosys/TCS ("Audited"). `audited_status` must always come from the filing/listing, never inferred.
7. **Watchlist status.** All four issuers are now identifier-verified and document-proven; the CSV
   `verified` update is left to the merging agent per the no-shared-files rule. With group A and B,
   all 10 watchlist rows are evidenced.

## 8. Artifacts produced by this milestone

- `storage/raw/india/maruti_suzuki/{q1_fy2026-27,q4_fy2025-26}/` — 4 files (incl. `exchange_nse/`)
- `storage/raw/india/ultratech_cement/{q1_fy2026-27,q4_fy2025-26}/` — 4 files (incl. `exchange_nse/`)
- `storage/raw/india/sun_pharmaceutical/{q1_fy2026-27,q4_fy2025-26}/` — 4 files (incl. `exchange_nse/`)
- `storage/raw/india/larsen_toubro/{q1_fy2026-27,q4_fy2025-26}/` — 4 files (incl. the standalone
  Q4 revision evidence under `exchange_nse/`)
- `tests/fixtures/india/MARUTI-Q1FY27-…xbrl.xml`, `MARUTI-Q4FY26-…xbrl.xml`,
  `ULTRACEMCO-Q1FY27-…xbrl.xml`, `ULTRACEMCO-Q4FY26-…xbrl.xml`,
  `SUNPHARMA-Q1FY27-…xbrl.xml`, `SUNPHARMA-Q4FY26-…xbrl.xml`,
  `LT-Q1FY27-…xbrl.xml`, `LT-Q4FY26-…xbrl.xml`
  (all consolidated instances, byte-identical to storage copies, all < 5 MB, none excerpted)
- `tests/fixtures/india/manifest_groupB.json` — provenance for all 16 files, identifiers,
  cash-flow evidence
- `docs/india_source_audit_addendum_groupB.md` — this document
