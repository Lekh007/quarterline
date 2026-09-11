# India source audit — addendum group A (IND-6a: TCS, HCLTech, ITC, Asian Paints)

Status: acquisition milestone addendum, complete as of **2026-09-11**.
Parent document: `docs/india_source_audit.md` (IND-1). This addendum extends IND-1's findings to four
more issuers (group A of the remaining eight watchlist rows). Scope is identical: identifier
verification, official document acquisition for the two target periods (Q1 FY2026-27, Q4+FY
2025-26), access-condition recording, and cash-flow availability evidence. Normalization, parsing,
and scoring remain out of scope.

Method note: all documents were fetched **2026-09-11 through a normal interactive browser session**
(same pattern as IND-1). No captcha was solved, no cookie was forged, no header spoofing, no
aggregator (Screener/Trendlyne/Moneycontrol) was used. Every file listed was (re)fetched by this
milestone through the browser and verified byte-exact via sha256 after transfer; files already
present in `storage/raw/india/` from an earlier same-day session were re-fetched and confirmed
byte-identical before being accepted — nothing is claimed on the earlier session's word.

Provenance manifest: `tests/fixtures/india/manifest_groupA.json` (entry shape mirrors IND-1's
`manifest.json`: `committed_fixtures` + `storage_cache_only`, plus a `cash_flow_evidence` block).

---

## 1. Identifier verification (official/primary sources only)

| Issuer | ISIN | NSE symbol | BSE scrip code | Where verified |
|---|---|---|---|---|
| Tata Consultancy Services Limited (`IN-TCS`) | `INE467B01029` | `TCS` | `532540` | 1) NSE quote page heading "Tata Consultancy Services Limited (INE467B01029)"; 2) BSE stock page "Tata Consultancy Services Ltd", code 532540 in URL/title, ISIN in page; 3) inside the NSE XBRL instance (`in-capmkt:ISIN`, `Symbol`, `ScripCode`) |
| HCL Technologies Limited (`IN-HCLTECH`) | `INE860A01027` | `HCLTECH` | `532281` | 1) NSE quote page heading "HCL Technologies Limited (INE860A01027)"; 2) BSE stock page header "(HCLTECH \| 532281 \| INE860A01027)"; 3) inside the NSE XBRL instance |
| ITC Limited (`IN-ITC`) | `INE154A01025` | `ITC` | `500875` | 1) NSE quote page heading "ITC Limited (INE154A01025)"; 2) BSE stock page header "(ITC \| 500875 \| INE154A01025)"; 3) inside the NSE XBRL instance |
| Asian Paints Limited (`IN-ASIANPAINT`) | `INE021A01026` | `ASIANPAINT` | `500820` | 1) NSE quote page heading "Asian Paints Limited (INE021A01026)"; 2) BSE stock page "Asian Paints Ltd", code 500820 in URL/title, ISIN in page; 3) inside the NSE XBRL instance |

All identifiers agree across exchange and filing sources for all four issuers. This raises verified
watchlist coverage to 6 of 10 (the CSV itself is updated by a later merging agent).

**Verification trap found (HCLTech/BSE):** a BSE stock-share-price URL with a *wrong* scrip code
(e.g. `…/hcl-technologies/hcltech/7229/`) still renders a page whose `<title>` echoes the requested
code ("… Hcltech Live Share Price, 7229 | BSE"). The title alone is NOT evidence of the scrip code.
Verification must use the page body ISIN/header or the filing's `in-capmkt:ScripCode`. Correct BSE
codes came from the filings themselves and were then confirmed on the BSE pages.

---

## 2. Document inventory (acquired, hashed)

8 consolidated exchange XBRLs are committed as fixtures (byte-identical to their storage copies;
all < 5 MB; none excerpted); PDFs stay storage-only. Full per-file provenance (URL, seq_id,
broadcast, audited status, revision, sha256, size) is in `tests/fixtures/india/manifest_groupA.json`.
Abbreviated inventory:

| Issuer | Period | Scope | Doc | Format | Tier | sha256 (first 12) | Bytes |
|---|---|---|---|---|---|---|---|
| TCS | Q1 FY27 | consolidated | financial_results | XBRL | exchange (NSE) | `ef4034549b7f…` | 45,281 |
| TCS | Q4+FY26 | consolidated | financial_results | XBRL | exchange (NSE) | `17d9c6bb6ac7…` | 109,844 |
| TCS | Q1 FY27 | consolidated | financial_results (condensed Ind AS FS, 55 pp., **quarterly CF on p.6**) | PDF | company_ir | `87b3c84815de…` | 761,871 |
| TCS | Q1 FY27 | consolidated | press_release (INR commentary) | PDF | company_ir | `80ce98d2c228…` | 510,766 |
| HCLTech | Q1 FY27 | consolidated | financial_results | XBRL | exchange (NSE) | `1c80d808b822…` | 34,782 |
| HCLTech | Q4+FY26 | consolidated | financial_results | XBRL | exchange (NSE) | `f2fa3b023c43…` | 88,846 |
| HCLTech | Q1 FY27 | cons+standalone | financial_results (Reg-33, 20 pp., no CF) | PDF | company_ir | `ebd29de8acc5…` | 5,325,815 |
| HCLTech | Q1 FY27 | consolidated | press_release (investor release) | PDF | company_ir | `0cfa589ce8a1…` | 4,258,196 |
| ITC | Q1 FY27 | consolidated | financial_results | XBRL | exchange (NSE) | `251a20e96060…` | 40,157 |
| ITC | Q4+FY26 | consolidated | financial_results | XBRL | exchange (NSE) | `366b95ff7cc7…` | 101,253 |
| ITC | Q1 FY27 | consolidated | financial_results (cfs, 4 pp., no CF) | PDF | company_ir | `012dd4036b08…` | 253,464 |
| ITC | Q1 FY27 | consolidated | press_release | PDF | company_ir | `4b501d6aaf49…` | 464,394 |
| Asian Paints | Q1 FY27 | consolidated | financial_results | XBRL | exchange (NSE) | `7855266114cb…` | 15,408 |
| Asian Paints | Q4+FY26 | consolidated | financial_results — **REVISED** (seq 174871, latest) | XBRL | exchange (NSE) | `ba770bf8ee20…` | 59,433 |
| Asian Paints | Q4+FY26 | consolidated | financial_results — superseded **Original** (seq 163991) | XBRL | exchange (NSE) | `d63ea07699b8…` | 58,120 |
| Asian Paints | Q1 FY27 | cons+standalone | financial_results (results PDF, 13 pp., no CF) | PDF | company_ir | `3b11c119e70e…` | 5,817,492 |

Filing metadata observed in the NSE Integrated Filing- Financials listing (all consolidated):
TCS Q1 seq 173420, bcast 09-Jul-2026 18:36:12 IST, Audited, Original. TCS Q4 seq 150312,
09-Apr-2026 23:42:16 IST, Audited, Original. HCLTech Q1 seq 174185, 13-Jul-2026 19:17:07 IST,
Un-Audited, Original. HCLTech Q4 seq 151938, 21-Apr-2026 19:09:44 IST, Audited, Original.
ITC Q1 seq 181205, 31-Jul-2026 17:23:53 IST, Un-Audited, Original. ITC Q4 seq 159756,
21-May-2026 18:32:29 IST, Audited, Original. Asian Paints Q1 seq 180064, 29-Jul-2026 18:54:04 IST,
Un-Audited, Original. Asian Paints Q4 original seq 163991, 29-May-2026 19:51:48 IST, Audited,
Original; revision seq 174871, 15-Jul-2026 20:33:14 IST (see §4).

Not obtained (with reason): standalone XBRLs and iXBRL renderings for these issuers (not required
by this milestone; the consolidated path is the ingest priority), ITC's results XLSX workbook and
presentations/transcripts for all four (optional items; TCS/ITC/HCLTech press releases were taken
instead), and HCLTech's 7.53 MB revised STANDALONE Q4 XBRL (out of required scope; existence
documented in §4). Nothing required was unobtainable.

---

## 3. Access conditions encountered

All via the browser session; plain non-browser clients were not re-tested (IND-1 §2 stands).

| Source (role) | URLs exercised | Result | Notes |
|---|---|---|---|
| `www.nseindia.com` (quote page + integrated-filing API) | `/get-quote/equity/<SYM>/…`, `GET /api/integrated-filing-results?&symbol=<SYM>&type=Integrated%20Filing-%20Financials&page=1&size=20` | Works fully from the page's own session | Same undocumented API as IND-1; returned 12–20 filings per symbol with `seq_Id`, `broadcast_Date`, `consolidated` (scope), `audited`, `type_Sub` (Original/Revision), `revised_Date`, `revision_Remark`, `xbrl`, `ixbrl`, `qe_Date` |
| `nsearchives.nseindia.com` (file archive) | direct navigations to `/corporate/xbrl/INTEGRATED_FILING_INDAS_<id>_<ts>_WEB.xml` | Direct file GET works; XML served inline | Same-origin `fetch()` inside the served XML page returns byte-exact bodies (CORS still blocks cross-origin reads from other hosts, per IND-1) |
| `www.bseindia.com` (stock pages) | `/stock-share-price/…` for 532540, 532281, 500875, 500820 | Pages render identifiers/ISIN in body after JS load (4–5 s wait); wrong codes fall back silently (see §1 trap) | Attachment downloads again not exercised (NSE sufficed); BSE download stability remains unassessed |
| `www.tcs.com` (IR) | `/investor-relations/financial-statements#year=2026-27&quarter=quarter1`, `content/dam/tcs/investor-relations/financial-statements/2026-27/q1/IND AS/*.pdf` | Works fully | Document grid is JS-rendered (links appear after ~3 s); deterministic per-quarter URL pattern observed |
| `www.hcltech.com` (IR) | `/investor-relations/financial-results`, `sites/default/files/documents/investor-reports/*.pdf` | Works fully | Long-lived stable filenames, per-quarter pages listing results/release/IFRS/transcript |
| `itcportal.com` (IR) | `/investors.html`, `/investors/quarterly-results.html`, `content/dam/itc-corporate/pdfs/financial-result/quarterly-results-<FY>/<month>-<Y>/*.pdf` | Works fully | Most deterministic layout of the four: per-quarter folder with `…-cfs.pdf/.xlsx`, `…-sfs.pdf/.xlsx`, `Press-Release`, `Presentation`, `faq` |
| `www.asianpaints.com` (IR) | `/more/investors.html`, `/more/investors/investors-landing-page.html?q=financial-results`, `content/dam/asianpaints/website/secondary-navigation/investors/financial-results-2/2026-2027/Q1/*.pdf` | Works fully once the right landing page is found | `/investors.html` and `/investor-relations` are 404s — the canonical path is `/more/investors.html`. Results accordion is JS-rendered; per-quarter DAM folder `financial-results-2/<FY>/<Q>/` |

Operational incident (recorded, not hidden): a parallel acquisition agent shared this browser
intermittently and navigated the active tab mid-fetch once; the in-flight cross-origin fetch failed
with a fetch error (no partial data), the tab was re-navigated and the fetch retried cleanly.
Pacing: fetches spaced >= 4–5 s per host; no rate-limit or Retry-After headers observed anywhere.

---

## 4. Revision finding — Asian Paints Q4+FY26 consolidated (first live revision in the corpus)

IND-1 flagged revisions as a designed-for unknown; group A surfaced the first real one:

- **Original** consolidated Q4 FY26: seq 163991, broadcast 29-May-2026 19:51:48 IST, taxonomy
  IFIndAs **V2.0**, `DeclarationOfUnmodifiedOpinionOrStatementOnImpactOfAuditQualification =
  "Not applicable"`.
- **Revision** consolidated Q4 FY26: seq 174871, revised 15-Jul-2026 20:33:14 IST, taxonomy
  IFIndAs **V2.1**, declaration = `"Declaration of unmodified opinion"`.
- Listing `revision_Remark` (verbatim): "The revised consolidated Integrated Financial Statements
  are being re-filed in XBRL by selecting 'Declaration of Unmodified Opinion' in the General
  Information section, pursuant to the BSE communication dated 10th July 2026. Additionally, during
  the review, it was noted that the figure for reserves excluding revaluation reserves was
  inadvertently missed; the XBRL filing has been updated accordingly."
- Both instances are cached; the **revision is the committed fixture** (latest available). The
  revision is identifiable only through the listing metadata (`type_Sub = Revision`,
  `revised_Date`, `revision_Remark`) — the instance content itself does not carry revision status,
  confirming IND-2's design.
- Also observed: HCLTech's **standalone** Q4 FY26 was revised (seq 152277, revised 23-Apr-2026
  12:57:13 IST, 7.53 MB XBRL; Original seq 151943 broadcast 21-Apr-2026 19:22:15 IST). The
  consolidated instance this milestone required has no revision. ITC and TCS show no revisions in
  the target periods.

Consequences: (1) ingestion must implement the listing-driven `Original/Revision` selection before
the first Asian Paints/HCLTech ingest; (2) `broadcast_ist` is `null` for revision rows — the
`revised_Date` field carries the timestamp instead; (3) an original/revision pair can differ in
**taxonomy version** (V2.0 → V2.1 here), reinforcing that per-instance taxonomy version must be
carried and never assumed.

---

## 5. Taxonomy versions observed (per instance)

| Instance | Header comment |
|---|---|
| All four Q1 FY27 consolidated (filed Jul 2026) | `IFIndAs V2.1 (26-06-2026)` |
| TCS / HCLTech / ITC Q4+FY26 consolidated (filed Apr/May 2026) | `IFIndAs V2.0 (06-02-2026)` |
| Asian Paints Q4+FY26 consolidated Original (filed 29-May-2026) | `IFIndAs V2.0 (06-02-2026)` |
| Asian Paints Q4+FY26 consolidated Revision (re-filed 15-Jul-2026) | `IFIndAs V2.1 (26-06-2026)` |

This widens IND-2's finding: the version varies not only across filings but **within one
issuer-period** across an original/revision pair. Namespace and schemaRef behavior are unchanged
from IND-1 (`in-capmkt` 2026-01-31; relative schemaRef, schema not bundled).

---

## 6. Cash-flow availability evidence (per issuer)

Per IND-3's rule: report what each document actually shows, at its actual frequency.

| Issuer | Q1 FY27 exchange XBRL (consolidated) | Q4+FY26 exchange XBRL (consolidated) | Q1 FY27 company-IR PDF | Q1 PDF prior-year comparative |
|---|---|---|---|---|
| TCS | 0 cash-flow facts; `WhetherCashFlowStatementIsApplicableOnCompany` absent | Annual CF: `CashFlowsFromUsedInOperatingActivities = 520940000000` (2025-04-01..2026-03-31 = ₹52,094 Cr) | **QUARTERLY CF present**: "Consolidated Interim Statement of Cash Flows", p.6 of `tcs-q1fy27-consolidated-indas-finstatement.pdf`, headed "Three months ended June 30, 2026 / June 30, 2025"; Net cash flows generated from operating activities ₹12,171 Cr vs ₹11,919 Cr (prior-year quarter) | YES (all statements) |
| HCLTech | 0 cash-flow facts | Annual CF: `199750000000` (₹19,975 Cr) | NOT PRESENT (20-page results PDF has zero cash-flow mentions) | YES ("Three months ended" 2026/2025 columns) |
| ITC | 0 cash-flow facts | Annual CF: `184643100000` (₹1,84,643.1 Cr) | NOT PRESENT (4-page cfs PDF; ITC also offers a `…-cfs.xlsx` workbook, not acquired, which may carry CF) | YES ("quarter ended 30th June, 2026" vs 2025) |
| Asian Paints | 0 cash-flow facts (instance = 15 KB, smallest of the group) | Annual CF: `70881800000` (₹7,088.18 Cr) — identical availability in original and revision | NOT PRESENT (13-page results PDF has zero cash-flow mentions) | YES ("Quarter Ended 30th June, 2026" vs 2025) |

Corpus-wide picture after group A: the IND-1/IND-3 pattern (quarterly CF absent from the exchange
feed; annual CF only, presented with the March-quarter filing) now holds for **6 issuers × 2
periods** of consolidated instances. Company-IR quarterly CF is obtainable for TCS and Infosys
via their condensed-statement PDFs; not observed for HCLTech, ITC, HUL, or Asian Paints in anything
ingested so far. The no-fabrication rule (never treat annual CF as a quarter; never interpolate)
applies unchanged.

Secondary audit-status observations worth carrying into the parser tests: TCS labels its Q1
consolidated results "Audited" while HCLTech/ITC/Asian Paints label theirs "Un-Audited"; HCLTech
carries a "Declaration of unmodified opinion" string even in its Q1 instance where ITC/Asian Paints
carry "Not applicable"; Asian Paints' Q1 listing shows consolidated "Un-Audited" but standalone
"Audited". `audited_status` must always come from the filing/listing, never inferred (IND-1 gap #7,
still valid).

---

## 7. Other observations for the ingestion milestone

1. **Broadcast-date spread.** March-quarter consolidated filings appeared 09-Apr (TCS), 21-Apr
   (HCLTech), 21-May (ITC), 29-May (Asian Paints); June-quarter filings 09-Jul → 31-Jul. A
   pipeline that polls for a quarter "a few days after quarter end" will not see ITC/Asian Paints
   for ~7–8 weeks; expected-publication windows must be per-issuer, or driven by filing discovery
   rather than the calendar.
2. **Instance sizes vary 10×** across the group even at consolidated scope (Asian Paints Q1 15 KB
   vs TCS Q4 110 KB). Never assume a size or segment-count shape.
3. **IR URL stability differs by issuer**: ITC has the most deterministic per-quarter folder
   pattern; TCS/HCLTech/Asian Paints use per-quarter DAM folders with descriptive filenames.
   Discovery must always go through the IR listing page (as with NSE archive IDs).
4. **BSE title echo trap** (§1) — any future BSE-based identifier verification must parse the page
   body (ISIN/name), not the title.
5. **Watchlist status.** All four issuers are now identifier-verified and document-proven; the CSV
   `verified` update is left to the merging agent per the no-shared-files rule.

## 8. Artifacts produced by this milestone

- `storage/raw/india/tcs/{q1_fy2026-27,q4_fy2025-26}/` — 4 files (incl. `exchange_nse/`)
- `storage/raw/india/hcltech/{q1_fy2026-27,q4_fy2025-26}/` — 4 files (incl. `exchange_nse/`)
- `storage/raw/india/itc/{q1_fy2026-27,q4_fy2025-26}/` — 4 files (incl. `exchange_nse/`)
- `storage/raw/india/asianpaints/{q1_fy2026-27,q4_fy2025-26}/` — 4 files (incl. the retained
  Q4 original/revision pair under `exchange_nse/`)
- `tests/fixtures/india/TCS-Q1FY27-…xbrl.xml`, `TCS-Q4FY26-…xbrl.xml`,
  `HCLTECH-Q1FY27-…xbrl.xml`, `HCLTECH-Q4FY26-…xbrl.xml`, `ITC-Q1FY27-…xbrl.xml`,
  `ITC-Q4FY26-…xbrl.xml`, `ASIANPAINT-Q1FY27-…xbrl.xml`, `ASIANPAINT-Q4FY26-…xbrl.xml`
  (the Q4 fixture is the revised instance; byte-identical to storage copies)
- `tests/fixtures/india/manifest_groupA.json` — provenance for all 16 files, identifiers,
  cash-flow evidence
- `docs/india_source_audit_addendum_groupA.md` — this document
