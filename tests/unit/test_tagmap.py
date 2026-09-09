"""Unit tests for the tag map and SEC payload helpers (contracts C3/C4)."""

from __future__ import annotations

import httpx
import pytest

from quarterline.config import Settings
from quarterline.sources.sec.client import SecClient
from quarterline.sources.sec.companyfacts import (
    cik10,
    companyfacts_url,
    iter_unit_facts,
    normalize_cik,
)
from quarterline.sources.sec.submissions import (
    FilingRef,
    build_filing_url,
    list_recent_filings,
)
from quarterline.sources.sec.tagmap import load_tagmap

VALID_IDENTITY = "Quarterline research test-contact@quarterline.local"


# -- tag map -----------------------------------------------------------------


def test_tagmap_loads_all_concepts_in_priority_order() -> None:
    tagmap = load_tagmap()
    assert set(tagmap) == {
        "revenue",
        "gross_profit",
        "operating_income",
        "net_income",
        "diluted_eps",
        "shares_diluted",
        "cfo",
        "capex_outflow",
        "cash",
        "total_assets",
        "total_liabilities",
        "long_term_debt",
        "current_assets",
        "current_liabilities",
    }
    assert tagmap["revenue"][0] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert tagmap["revenue"] == [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ]
    assert tagmap["gross_profit"] == ["GrossProfit"]


def test_tagmap_returns_independent_copies() -> None:
    first = load_tagmap()
    first["revenue"].append("MalloryTag")
    assert load_tagmap()["revenue"][-1] != "MalloryTag"


# -- companyfacts helpers ------------------------------------------------------


def test_cik_formatting() -> None:
    assert cik10(320193) == "0000320193"
    assert cik10("0000320193") == "0000320193"
    assert (
        companyfacts_url(789019) == "https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json"
    )
    with pytest.raises(ValueError):
        normalize_cik("nope")


def test_iter_unit_facts_enriches_entries() -> None:
    companyfacts = {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "start": "2026-01-01",
                                "end": "2026-03-31",
                                "val": 1000,
                                "form": "10-Q",
                            },
                            {
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "val": 4000,
                                "form": "10-K",
                            },
                        ],
                        "shares": [{"end": "2026-01-01", "val": 5}],
                    }
                }
            }
        }
    }
    entries = list(iter_unit_facts(companyfacts, "us-gaap", "Revenues"))
    assert [entry["unit"] for entry in entries] == ["USD", "USD", "shares"]
    assert all(entry["taxonomy"] == "us-gaap" and entry["tag"] == "Revenues" for entry in entries)
    assert entries[0]["val"] == 1000

    assert list(iter_unit_facts({"facts": {}}, "us-gaap", "Missing")) == []


# -- submissions helpers -------------------------------------------------------


def _settings(monkeypatch, tmp_path) -> Settings:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    return Settings(_env_file=None, edgar_identity=VALID_IDENTITY)


def test_list_recent_filings_filters_and_limits(monkeypatch, tmp_path) -> None:
    payload = {
        "cik": "320193",
        "filings": {
            "recent": {
                "accessionNumber": [
                    "0000320193-26-000003",
                    "0000320193-25-000099",
                    "0000320193-25-000050",
                ],
                "form": ["10-Q", "8-K", "10-K"],
                "filingDate": ["2026-08-01", "2026-05-01", "2025-11-01"],
                "reportDate": ["2026-06-30", "2026-03-31", "2025-09-30"],
                "primaryDocument": ["aapl-20260630.htm", "ex99.htm", "aapl-20250930.htm"],
            }
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "data.sec.gov"
        return httpx.Response(200, json=payload)

    client = SecClient(_settings(monkeypatch, tmp_path), transport=httpx.MockTransport(handler))
    try:
        refs = list_recent_filings(client, "0000320193", forms=["10-Q", "10-K"], limit=10)
    finally:
        client.close()

    assert refs == [
        FilingRef(
            accession="000032019326000003",
            accession_raw="0000320193-26-000003",
            form="10-Q",
            filed_at="2026-08-01",
            report_period_end="2026-06-30",
            primary_document="aapl-20260630.htm",
        ),
        FilingRef(
            accession="000032019325000050",
            accession_raw="0000320193-25-000050",
            form="10-K",
            filed_at="2025-11-01",
            report_period_end="2025-09-30",
            primary_document="aapl-20250930.htm",
        ),
    ]

    limited = None
    client = SecClient(_settings(monkeypatch, tmp_path), transport=httpx.MockTransport(handler))
    try:
        limited = list_recent_filings(client, 320193, forms=["10-Q", "10-K"], limit=1)
    finally:
        client.close()
    assert len(limited) == 1


def test_build_filing_url() -> None:
    assert (
        build_filing_url("0000320193-26-000003", "aapl-20260630.htm", cik=320193)
        == "https://www.sec.gov/Archives/edgar/data/0000320193/000032019326000003/aapl-20260630.htm"
    )
    assert (
        build_filing_url("0000320193-26-000003", "/nested/doc.htm")
        == "https://www.sec.gov/Archives/edgar/data/000032019326000003/nested/doc.htm"
    )
