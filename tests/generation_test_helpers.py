"""Shared support for generation-track tests (F5 wave).

Seeds the temporary SQLite database with BOTH halves the pipeline needs:

- the real trimmed AAPL companyfacts fixture via the facts pipeline
  (fact card for the latest fixture quarter, period end 2026-06-27); and
- a purpose-built document corpus whose 8-K discussed periods and 10-Q
  report dates ALIGN with that quarter (plus one deliberately misaligned
  document used by the wrong-period citation tests), indexed with the
  deterministic FakeEmbeddingProvider.

Everything is offline: no network, no Ollama (PLAN ground rule 8). The
:class:`ScriptedProvider` fake stands in for the generation provider and
records every call so tests can assert call counts (repair, cache hits).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from facts_test_helpers import (
    FakeCompanyFactsClient,
    import_ingest_facts,
    load_aapl_fixture,
)

from quarterline.ingest.documents import FormsConfig, ingest_documents
from quarterline.llm.base import GenerationResult
from quarterline.retrieve.embeddings import FakeEmbeddingProvider
from quarterline.retrieve.search import build_index
from quarterline.sources.sec.client import SecDownloadResult
from quarterline.store.db import get_engine
from quarterline.store.models import Base

TESTS_DIR = Path(__file__).resolve().parent
AAPL_CIK = "0000320193"

#: Latest quarter of the real AAPL facts fixture — the document corpus below
#: is built so its filings discuss exactly this period.
ALIGNED_PERIOD_END = date(2026, 6, 27)
#: Deliberately misaligned document period for wrong-period citation tests.
OLD_PERIOD_END = date(2026, 3, 28)


# ---------------------------------------------------------------------------
# Offline SEC client with period-aligned filings
# ---------------------------------------------------------------------------


def _aligned_ten_q_html() -> bytes:
    return b"""<!DOCTYPE html>
<html><head><title>10-Q</title></head><body>
<h1>Part I - Financial Information</h1>
<table>
<tr><td>Item 1.</td><td>Financial Statements</td><td>3</td></tr>
<tr><td>Item 2.</td><td>Management's Discussion and Analysis of Financial Condition and Results of Operations</td><td>12</td></tr>
</table>
<h2>Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations</h2>
<p>Revenue increased during the quarter driven by services growth across every
segment, while operating expenses grew more slowly than revenue, gross margin
expanded, and operating cash flow remained robust. The company continued
returning capital to shareholders.</p>
<h1>Part II - Other Information</h1>
<h2>Item 1A. Risk Factors</h2>
<p>The risk factors previously disclosed in the annual report have not changed
materially, although supply chain concentration and evolving data regulation
remain areas of continued attention for the business.</p>
<h2>Item 6. Exhibits</h2>
<p>Exhibit 31 certification rules apply.</p>
</body></html>"""


def _exhibit_html(discussed_period_text: str, quarter_wording: str) -> bytes:
    return f"""<!DOCTYPE html>
<html><head><title>Press release</title></head><body>
<h1>Apple reports {quarter_wording} results</h1>
<p>CUPERTINO, California — Apple today announced financial results for its
{discussed_period_text}. Revenue grew on strong services performance, gross
margin expanded, and operating cash flow remained robust.</p>
<p>Management identified supply chain concentration and evolving data
regulation as continued areas of attention for the business.</p>
</body></html>""".encode()


_ALIGNED_EXHIBIT = _exhibit_html("fiscal third quarter ended June 27, 2026", "third quarter")
_OLD_EXHIBIT = _exhibit_html("fiscal second quarter ended March 28, 2026", "second quarter")


class AlignedSecClient:
    """Offline stand-in: filings whose reporting periods match the facts
    fixture quarter (plus one misaligned 8-K for wrong-period tests)."""

    def __init__(self) -> None:
        base = f"https://www.sec.gov/Archives/edgar/data/{AAPL_CIK}"
        self._base = base
        self.json_by_url: dict[str, object] = {
            f"https://data.sec.gov/submissions/CIK{AAPL_CIK}.json": {
                "filings": {
                    "recent": {
                        "accessionNumber": [
                            "0000320193-26-000201",
                            "0000320193-26-000202",
                            "0000320193-26-000113",
                        ],
                        "form": ["10-Q", "8-K", "8-K"],
                        "filingDate": ["2026-07-24", "2026-07-02", "2026-04-30"],
                        "reportDate": ["2026-06-27", "2026-06-28", "2026-03-29"],
                        "primaryDocument": [
                            "a10-q-fy26q3.htm",
                            "a8-k-fy26q3.htm",
                            "a8-k-fy26q2.htm",
                        ],
                        "items": ["", "2.02", "2.02"],
                    }
                }
            },
            f"{base}/000032019326000202/index.json": {
                "directory": {"item": [{"name": "a8kex991q320260702.htm"}]}
            },
            f"{base}/000032019326000113/index.json": {
                "directory": {"item": [{"name": "a8kex991q220260430.htm"}]}
            },
        }
        self.bytes_by_url: dict[str, bytes] = {
            f"{base}/000032019326000201/a10-q-fy26q3.htm": _aligned_ten_q_html(),
            f"{base}/000032019326000202/a8kex991q320260702.htm": _ALIGNED_EXHIBIT,
            f"{base}/000032019326000113/a8kex991q220260430.htm": _OLD_EXHIBIT,
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


# ---------------------------------------------------------------------------
# Scripted generation provider (fake; records calls)
# ---------------------------------------------------------------------------


class ScriptedProvider:
    """Deterministic GenerationProvider fake: pops a scripted item per call.

    Items may be GenerationResult values (returned) or exceptions (raised).
    Every call is recorded so tests can assert provider call counts (exactly
    one repair pass, zero calls on cache hits, zero calls on abstention).
    """

    provider_name = "scripted"
    model_id = "scripted-test-model"

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def verify_model(self) -> None:
        return None

    def generate(self, messages: list[dict], json_schema: dict | None = None) -> GenerationResult:
        self.calls.append({"messages": messages, "json_schema": json_schema})
        if not self.script:
            raise AssertionError("ScriptedProvider ran out of scripted responses")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def scripted_result(payload: dict) -> GenerationResult:
    """A GenerationResult whose text is the given JSON document."""
    return GenerationResult(
        text=json.dumps(payload),
        provider="scripted",
        model=ScriptedProvider.model_id,
        input_tokens=120,
        output_tokens=40,
        token_count_source="provider",
        latency_ms=1.5,
        finish_reason="stop",
    )


def scripted_text(raw: str) -> GenerationResult:
    """A GenerationResult whose text is raw (invalid-JSON) output."""
    return GenerationResult(
        text=raw,
        provider="scripted",
        model=ScriptedProvider.model_id,
        input_tokens=120,
        output_tokens=40,
        token_count_source="provider",
        latency_ms=1.5,
        finish_reason="stop",
    )


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


@pytest.fixture(name="generation_db")
def generation_db_fixture(tmp_path, monkeypatch):
    """Facts fixture + aligned document corpus + section index, fully offline."""
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("STORAGE_DIR", str(storage))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")  # nothing listens here
    Base.metadata.create_all(get_engine())
    facts = import_ingest_facts().ingest_facts(
        tickers=["AAPL"],
        watchlist="data/watchlist_us.csv",
        client=FakeCompanyFactsClient(load_aapl_fixture()),
    )
    assert facts.errors == {}, facts.errors

    watchlist = storage / "watchlist.csv"
    watchlist.write_text(
        f"ticker,cik,name,sector,country\nAAPL,{AAPL_CIK},Apple Inc.,Information Technology,US\n",
        encoding="utf-8",
    )
    docs = ingest_documents(watchlist, FormsConfig(), client=AlignedSecClient())
    assert docs.documents_failed == 0, docs.errors

    from quarterline.store.db import session_scope

    with session_scope() as session:
        stats = build_index(session, "section", fixture_embedding_provider())
    return {"index": stats}


def fixture_embedding_provider() -> FakeEmbeddingProvider:
    """Same deterministic provider family the retrieval fixtures pin."""
    return FakeEmbeddingProvider(dim=64, seed=20260909)


def document_periods() -> dict[int, date | None]:
    """document_id -> period_end as ingested (aligned docs + the old 8-K)."""
    from sqlalchemy import select

    from quarterline.store.db import session_scope
    from quarterline.store.models import Document

    with session_scope() as session:
        return {doc.id: doc.period_end for doc in session.scalars(select(Document)).all()}


__all__ = [
    "AAPL_CIK",
    "ALIGNED_PERIOD_END",
    "OLD_PERIOD_END",
    "AlignedSecClient",
    "ScriptedProvider",
    "document_periods",
    "fixture_embedding_provider",
    "generation_db_fixture",
    "scripted_result",
    "scripted_text",
]
