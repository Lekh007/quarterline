"""One-time build-time verification of the US watchlist CIKs (SPEC §8).

Primary source: the official SEC ticker file ``company_tickers.json``.
Fallback (used when that file is unreachable/blocked, e.g. CDN 403): each
candidate CIK is *verified* against the official ``data.sec.gov/submissions``
metadata for that CIK — the payload must list the expected ticker (and its
company name is recorded verbatim). No identifier ships unverified.

This is a build/maintenance script — never executed by tests or the app.

Usage:
    uv run python scripts/verify_watchlist_ciks.py
"""

from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
WATCHLIST_CSV = REPO_ROOT / "data" / "watchlist_us.csv"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# Build-time identity, distinct from the runtime EDGAR_IDENTITY (PLAN §4).
# SEC fair access requires a UA identifying the requester.
BUILD_USER_AGENT = "Quarterline build (fixture verification) build-contact@quarterline.local"
REQUEST_SPACING_SECONDS = 0.25

WATCHLIST_TICKERS = [
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "META",
    "NVDA",
    "AVGO",
    "ORCL",
    "CRM",
    "ADBE",
    "COST",
    "WMT",
    "CAT",
    "UNP",
    "HON",
]

# Candidate CIKs from general knowledge; every one is verified against official
# SEC metadata before it is written to the CSV. A mismatch aborts the build.
CANDIDATE_CIKS = {
    "AAPL": 320193,
    "MSFT": 789019,
    "GOOGL": 1652044,
    "AMZN": 1018724,
    "META": 1326801,
    "NVDA": 1045810,
    "AVGO": 1730168,
    "ORCL": 1341439,
    "CRM": 1108524,
    "ADBE": 796343,
    "COST": 909832,
    "WMT": 104169,
    "CAT": 18230,
    "UNP": 100885,
    "HON": 773840,
}

# GICS sectors from general knowledge.
SECTORS = {
    "AAPL": "Information Technology",
    "MSFT": "Information Technology",
    "GOOGL": "Communication Services",
    "AMZN": "Consumer Discretionary",
    "META": "Communication Services",
    "NVDA": "Information Technology",
    "AVGO": "Information Technology",
    "ORCL": "Information Technology",
    "CRM": "Information Technology",
    "ADBE": "Information Technology",
    "COST": "Consumer Staples",
    "WMT": "Consumer Staples",
    "CAT": "Industrials",
    "UNP": "Industrials",
    "HON": "Industrials",
}


def fetch_ticker_file(client: httpx.Client) -> dict[str, dict] | None:
    """Primary source: official company_tickers.json keyed by ticker."""
    try:
        response = client.get(TICKERS_URL)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"primary source unavailable ({exc}); falling back to submissions metadata")
        return None
    payload = json.loads(response.content)
    return {str(record["ticker"]).upper(): record for record in payload.values()}


def verify_via_submissions(client: httpx.Client, ticker: str) -> tuple[str, str] | None:
    """Fallback: confirm the candidate CIK's official metadata lists this ticker."""
    candidate = CANDIDATE_CIKS.get(ticker)
    if candidate is None:
        return None
    response = client.get(SUBMISSIONS_URL.format(cik=candidate))
    response.raise_for_status()
    payload = json.loads(response.content)
    tickers = {str(t).upper() for t in payload.get("tickers") or []}
    exch_tickers = {
        str(e.get("ticker", "")).upper()
        for e in payload.get("exchanges") or []
        if isinstance(e, dict)
    }
    if ticker not in tickers and ticker not in exch_tickers:
        print(
            f"ERROR: CIK {candidate:010d} does not report ticker {ticker} "
            f"(found {sorted(tickers)})",
            file=sys.stderr,
        )
        return None
    return f"{candidate:010d}", str(payload.get("name") or "").strip()


def main() -> int:
    headers = {"User-Agent": BUILD_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    rows: list[dict[str, str]] = []
    method: str

    with httpx.Client(headers=headers, timeout=60.0, follow_redirects=True) as client:
        by_ticker = fetch_ticker_file(client)
        if by_ticker is not None:
            method = "company_tickers.json (primary source)"
            for ticker in WATCHLIST_TICKERS:
                record = by_ticker.get(ticker)
                if record is None:
                    print(f"ERROR: {ticker} missing from official ticker file", file=sys.stderr)
                    return 1
                rows.append(
                    {
                        "ticker": ticker,
                        "cik": f"{int(record['cik_str']):010d}",
                        "name": str(record["title"]).strip(),
                        "sector": SECTORS[ticker],
                        "country": "US",
                    }
                )
        else:
            method = (
                "data.sec.gov/submissions metadata (fallback; company_tickers.json was blocked)"
            )
            for ticker in WATCHLIST_TICKERS:
                verified = verify_via_submissions(client, ticker)
                if verified is None:
                    return 1
                cik, name = verified
                # Keep CSV simple: strip commas from names containing them.
                rows.append(
                    {
                        "ticker": ticker,
                        "cik": cik,
                        "name": name.replace(",", ""),
                        "sector": SECTORS[ticker],
                        "country": "US",
                    }
                )
                time.sleep(REQUEST_SPACING_SECONDS)

    with WATCHLIST_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "cik", "name", "sector", "country"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"verification method: {method}")
    print(f"verified {len(rows)} CIKs:")
    for row in rows:
        print(f"  {row['ticker']:>6}  {row['cik']}  {row['name']}  [{row['sector']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
