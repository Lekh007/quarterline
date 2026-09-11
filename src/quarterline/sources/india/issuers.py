"""India issuer registry from ``data/watchlist_india.csv`` (IND-2).

Only rows with ``verification_status=verified`` may be ingested: identifiers for
verified issuers were confirmed against NSE, BSE and the companies' own IR
material in IND-1 (docs/india_source_audit.md §1); the remaining rows are
``proposed`` placeholders with empty verified fields and must never produce
facts or artifacts.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

#: Repository root when the package is used from its source checkout.
_REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_WATCHLIST_PATH = _REPO_ROOT / "data" / "watchlist_india.csv"

STATUS_VERIFIED = "verified"
STATUS_PROPOSED = "proposed"

#: Storage directory slugs matching the IND-1 + IND-6a/6b cache layout
#: (``storage/raw/india/infosys/...``, ``storage/raw/india/maruti_suzuki/...``).
_STORAGE_SLUGS: dict[str, str] = {
    "IN-INFY": "infosys",
    "IN-HINDUNILVR": "hindustan_unilever",
    "IN-TCS": "tcs",
    "IN-HCLTECH": "hcltech",
    "IN-ITC": "itc",
    "IN-ASIANPAINT": "asianpaints",
    "IN-MARUTI": "maruti_suzuki",
    "IN-ULTRACEMCO": "ultratech_cement",
    "IN-SUNPHARMA": "sun_pharmaceutical",
    "IN-LT": "larsen_toubro",
}


@dataclass(frozen=True)
class Issuer:
    """One verified watchlist issuer (typed record; SPEC 27 required fields)."""

    issuer_id: str
    isin: str
    ticker_nse: str | None
    bse_code: str | None
    name: str | None
    sector: str | None
    verification_status: str
    verified_source: str | None

    @property
    def slug(self) -> str:
        """Directory slug under ``storage/raw/india/`` (IND-1 layout)."""
        return _STORAGE_SLUGS.get(self.issuer_id, self.issuer_id.removeprefix("IN-").lower())


def _issuer_from_row(row: dict[str, str]) -> Issuer | None:
    issuer_id = (row.get("issuer_id") or "").strip()
    if not issuer_id:
        return None
    isin = (row.get("isin") or "").strip()
    status = (row.get("verification_status") or "").strip().lower()
    return Issuer(
        issuer_id=issuer_id,
        isin=isin,
        ticker_nse=(row.get("ticker_nse") or "").strip() or None,
        bse_code=(row.get("bse_code") or "").strip() or None,
        name=(row.get("name") or "").strip() or None,
        sector=(row.get("sector") or "").strip() or None,
        verification_status=status,
        verified_source=(row.get("verified_source") or "").strip() or None,
    )


@lru_cache(maxsize=1)
def _load_cached(path: str) -> tuple[Issuer, ...]:
    issuers: list[Issuer] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            issuer = _issuer_from_row(row)
            if issuer is not None:
                issuers.append(issuer)
    return tuple(issuers)


def load_issuers(path: str | Path | None = None) -> tuple[Issuer, ...]:
    """Load every registry row (verified and proposed) as typed :class:`Issuer` records."""
    return _load_cached(str(Path(path)) if path else str(DEFAULT_WATCHLIST_PATH))


def get_verified_issuers(path: str | Path | None = None) -> tuple[Issuer, ...]:
    """Only issuers whose identifiers are verified against official sources."""
    return tuple(i for i in load_issuers(path) if i.verification_status == STATUS_VERIFIED)


def get_issuer(issuer_id: str, path: str | Path | None = None) -> Issuer:
    """Resolve one issuer by id; raises ``LookupError`` for unknown ids."""
    for issuer in load_issuers(path):
        if issuer.issuer_id == issuer_id.strip().upper():
            return issuer
    known = ", ".join(i.issuer_id for i in load_issuers(path))
    raise LookupError(f"unknown India issuer {issuer_id!r} (registry: {known})")


def require_verified(issuer_id: str, path: str | Path | None = None) -> Issuer:
    """Resolve one issuer and refuse unverified rows (never ingest a proposal)."""
    issuer = get_issuer(issuer_id, path)
    if issuer.verification_status != STATUS_VERIFIED:
        raise ValueError(
            f"issuer {issuer.issuer_id} is {issuer.verification_status!r}, not verified; "
            "verify identifiers against NSE/BSE/company sources before import "
            "(docs/india_source_audit.md §1)"
        )
    return issuer
