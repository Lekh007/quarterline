"""NSE Integrated Filing (Finance) endpoints — documented STUBS (IND-2).

No network calls exist in this milestone. Every NSE surface observed in IND-1
rejects non-browser HTTP clients (plain httpx gets a silent TCP-level connection
drop, no HTTP response at all), and the archive file URLs embed internal numeric
IDs so there is no stable per-period URL contract — listings must always be
resolved through the listing API first (docs/india_source_audit.md §2 and §6.1/6.2).

Constants below record the OBSERVED endpoints so the future browser-grade fetch
layer has one documented reference; they are not a stable API contract.
"""

from __future__ import annotations

from quarterline.sources.india.errors import ManualImportRequired

AUDIT_DOC = "docs/india_source_audit.md"

NSE_BASE_URL = "https://www.nseindia.com"

#: Integrated Filing- Financials listing (live module, from the Mar-2025 quarter).
#: Undocumented JSON API, callable only inside a browser session; no contract.
NSE_INTEGRATED_FILING_PAGE = (
    NSE_BASE_URL + "/companies-listing/corporate-integrated-filing"
    "?integratedType=integratedfilingfinancials"
)
NSE_INTEGRATED_FILING_API = (
    NSE_BASE_URL + "/api/integrated-filing-results"
)  # params: &symbol=<SYM>&type=Integrated%20Filing-%20Financials&page=1&size=20

#: File archive. Direct GET works only from a browser-grade client; URLs embed
#: internal IDs (e.g. INTEGRATED_FILING_INDAS_1700136_23072026054446_WEB.xml).
NSE_ARCHIVE_BASE_URL = "https://nsearchives.nseindia.com"

#: Legacy financial-results module — FROZEN at the quarter ended 31-Dec-2024 and
#: its from/to parameters are ignored. Do not build on it (audit §6.3).
NSE_LEGACY_RESULTS_PAGE = NSE_BASE_URL + "/companies-listing/corporate-filings-financial-results"
NSE_LEGACY_RESULTS_API = NSE_BASE_URL + "/api/corporates-financial-results"

#: Observed cross-origin restriction: nsearchives blocks cross-origin reads.
NSE_ARCHIVE_CORS_BLOCKED = True

#: Conservative spacing for future exchange fetches (audit §2 "Rate limits"):
#: no India-specific limits measured yet; keep >= 1 s per exchange host.
MIN_EXCHANGE_REQUEST_INTERVAL_SECONDS = 1.0


def fetch_not_available_in_runtime() -> None:
    """Raise :class:`ManualImportRequired` — NSE rejects non-browser clients.

    Documents must be acquired through the manual-import path
    (``ingest india-document`` / ``ir_documents.import_document``) until a
    browser-grade fetch layer is built and measured.
    """
    raise ManualImportRequired(
        "NSE rejects non-browser HTTP clients (silent TCP drop; audit: "
        f"{AUDIT_DOC} §2). Acquire documents via a browser session and import "
        "them with `quarterline ingest india-document` "
        "(quarterline.sources.india.ir_documents.import_document)."
    )
