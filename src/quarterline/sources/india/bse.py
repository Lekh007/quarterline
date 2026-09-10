"""BSE stock/filing endpoints — documented STUBS (IND-2).

No network calls exist in this milestone. BSE serves 403 (Akamai-style) to
non-browser clients; BSE mirrors the NSE Integrated Filing (Finance) filings
("Integrated Filing (Finance) from March 2025 quarter" on the stock page), but
attachment downloads were NOT exercised during IND-1 — download stability is
unassessed (docs/india_source_audit.md §2 and §6.10). NSE sufficed as the
exchange tier; BSE is recorded for identifier cross-verification only.
"""

from __future__ import annotations

from quarterline.sources.india.errors import ManualImportRequired

AUDIT_DOC = "docs/india_source_audit.md"

BSE_BASE_URL = "https://www.bseindia.com"

#: Stock page pattern (identifiers, announcements, Integrated Filing tab).
#: <SCHEME>/<COMPANY-SLUG>/<SYMBOL>/<SCRIP-CODE>/ as observed for INFY/HUL.
BSE_STOCK_PAGE = BSE_BASE_URL + "/stock-share-price"

#: API host observed serving market/listing data (403 to non-browser clients).
BSE_API_BASE_URL = "https://api.bseindia.com"

#: BSE scrip codes double as the XBRL entity identifier scheme value inside the
#: in-capmkt instances (scheme http://www.sebi.gov.in/in-capmkt/ScripCode).
BSE_ENTITY_IDENTIFIER_SCHEME = "http://www.sebi.gov.in/in-capmkt/ScripCode"

#: Download stability unassessed (audit §6.10) — keep the conservative spacing.
MIN_EXCHANGE_REQUEST_INTERVAL_SECONDS = 1.0


def fetch_not_available_in_runtime() -> None:
    """Raise :class:`ManualImportRequired` — BSE rejects non-browser clients.

    BSE attachment downloads are additionally unassessed (audit: AUDIT_DOC §6.10);
    the exchange tier of record is NSE until BSE downloads are proven.
    """
    raise ManualImportRequired(
        "BSE serves 403 to non-browser HTTP clients and attachment download "
        f"stability is unassessed (audit: {AUDIT_DOC} §2, §6.10). Use the NSE "
        "exchange tier or import documents manually via "
        "`quarterline ingest india-document`."
    )
