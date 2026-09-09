"""Shared support for retrieval tests (not a test module).

Provides an offline fixture corpus built from ``tests/fixtures/documents/``:
the REAL Apple EX-99.1 exhibit, the synthetic injection exhibit, the synthetic
10-Q payload, and the synthetic ACME PDF — ingested into the test database,
then indexed with the deterministic (NON-PRODUCTION) ``FakeEmbeddingProvider``.
No network and no Ollama anywhere (SPEC §26; plan ground rule 8).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from quarterline.ingest.documents import FormsConfig, ingest_documents, ingest_pdf_file
from quarterline.retrieve.embeddings import FakeEmbeddingProvider
from quarterline.retrieve.search import build_index
from quarterline.sources.sec.client import SecDownloadResult
from quarterline.store.db import get_engine
from quarterline.store.models import Base

REPO_ROOT = Path(__file__).resolve().parent
DOCUMENTS_FIXTURES = REPO_ROOT / "fixtures" / "documents"
RETRIEVAL_FIXTURES = REPO_ROOT / "fixtures" / "retrieval"

AAPL_CIK = "0000320193"

REAL_EXHIBIT = (DOCUMENTS_FIXTURES / "real_aapl_8k_ex991_q2fy26.htm").read_bytes()
INJECTION_EXHIBIT = (DOCUMENTS_FIXTURES / "injection_filing.html").read_bytes()

#: Document ids in ingestion order (fresh test DB): 1 = synthetic 10-Q,
#: 2 = real Apple exhibit, 3 = injection exhibit, 4 = ACME PDF.
DOC_TEN_Q, DOC_REAL_8K, DOC_INJECTION_8K, DOC_ACME_PDF = 1, 2, 3, 4


class FixtureSecClient:
    """Offline stand-in for SecClient: canned submissions JSON + exhibit bytes."""

    def __init__(self) -> None:
        base = f"https://www.sec.gov/Archives/edgar/data/{AAPL_CIK}"
        self.json_by_url: dict[str, object] = {
            f"https://data.sec.gov/submissions/CIK{AAPL_CIK}.json": {
                "filings": {
                    "recent": {
                        "accessionNumber": [
                            "0000320193-26-000099",
                            "0000320193-26-000011",
                            "0000320193-26-000013",
                        ],
                        "form": ["10-Q", "8-K", "8-K"],
                        "filingDate": ["2026-05-01", "2026-04-30", "2026-01-29"],
                        "reportDate": ["2026-03-28", "2026-04-30", "2026-01-29"],
                        "primaryDocument": ["a10-q.htm", "a8-k.htm", "a8-k2.htm"],
                        "items": ["", "2.02", "2.02"],
                    }
                }
            },
            f"{base}/000032019326000011/index.json": {
                "directory": {"item": [{"name": "a8-kex991q2202603282026.htm"}]}
            },
            f"{base}/000032019326000013/index.json": {
                "directory": {"item": [{"name": "exhibit-99-pressrelease.htm"}]}
            },
        }
        self.bytes_by_url: dict[str, bytes] = {
            f"{base}/000032019326000099/a10-q.htm": _TEN_Q_HTML,
            f"{base}/000032019326000011/a8-kex991q2202603282026.htm": REAL_EXHIBIT,
            f"{base}/000032019326000013/exhibit-99-pressrelease.htm": INJECTION_EXHIBIT,
        }

    def get_json(self, url: str) -> object:
        return self.json_by_url[url]

    def download(self, url: str) -> SecDownloadResult:
        return SecDownloadResult(
            content=self.bytes_by_url[url],
            source_url=url,
            etag='"fixture-etag"',
            last_modified="Wed, 09 Sep 2026 00:00:00 GMT",
            cache_hit=False,
        )

    def close(self) -> None:
        pass


#: Fictional but structurally realistic 10-Q (ToC table + real sections).
_TEN_Q_HTML = b"""<!DOCTYPE html>
<html><head><title>10-Q</title></head><body>
<h1>Part I - Financial Information</h1>
<table>
<tr><td>Item 1.</td><td>Financial Statements</td><td>3</td></tr>
<tr><td>Item 2.</td><td>Management's Discussion and Analysis of Financial Condition and Results of Operations</td><td>12</td></tr>
</table>
<h2>Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations</h2>
<p>Revenue increased 12 percent driven by strong services growth across every segment during the quarter, while operating expenses grew more slowly than revenue and gross margin expanded. The company continued returning capital to shareholders while operating cash flow remained robust.</p>
<h1>Part II - Other Information</h1>
<h2>Item 1A. Risk Factors</h2>
<p>The risk factors previously disclosed in the annual report have not changed materially, although supply chain concentration and evolving data regulation remain areas of continued attention for the business this year.</p>
<h2>Item 6. Exhibits</h2>
<p>Exhibit 31 certification rules apply.</p>
</body></html>"""


def fixture_provider() -> FakeEmbeddingProvider:
    """The deterministic test provider pinned by the fixture manifest."""
    return FakeEmbeddingProvider(dim=64, seed=20260909)


@pytest.fixture(name="retrieval_db")
def retrieval_db_fixture(offline_env):
    """Fresh schema + ingested fixture corpus + both chunk strategies indexed.

    Yields nothing; use ``session_scope()`` against the configured test DB.
    Registered under the name ``retrieval_db`` so test modules can import the
    fixture under an aliased module attribute (avoids F811 param shadowing).
    """
    Base.metadata.create_all(get_engine())
    tmp = offline_env
    tmp.mkdir(parents=True, exist_ok=True)
    watchlist = tmp / "watchlist.csv"
    watchlist.write_text(
        f"ticker,cik,name,sector,country\nAAPL,{AAPL_CIK},Apple Inc.,Information Technology,US\n",
        encoding="utf-8",
    )
    report = ingest_documents(watchlist, FormsConfig(), client=FixtureSecClient())
    assert report.documents_failed == 0, report.errors
    ingest_pdf_file(
        "ACME",
        "https://ir.example.invalid/reports/q2-review.pdf",
        date(2026, 7, 15),
        DOCUMENTS_FIXTURES / "synthetic_mda.pdf",
    )
    with_fixtures = fixture_provider()
    from quarterline.store.db import session_scope

    with session_scope() as session:
        fixed = build_index(session, "fixed", with_fixtures)
        section = build_index(session, "section", with_fixtures)
    return {"fixed": fixed, "section": section}


def load_fixture_embeddings() -> dict:
    """The committed, deterministic (NOT real-model) embedding artifact."""
    return json.loads((RETRIEVAL_FIXTURES / "fixture_embeddings.json").read_text(encoding="utf-8"))
