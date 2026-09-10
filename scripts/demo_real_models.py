"""Real-model verification (wave 5, orchestrator).

Seeds the real storage DB from the committed real-source fixtures (offline),
then exercises the LIVE Ollama paths: nomic-embed-text index build + search,
and one qwen3:4b grounded brief through the full validation gate.

Seeded data is fixture-sourced (real SEC values), clearly not live ingestion;
live SEC ingestion remains gated on a real EDGAR_IDENTITY (SPEC §7).

Usage:
    uv run python scripts/demo_real_models.py           # seed + embed + search + brief
    uv run python scripts/demo_real_models.py --no-brief  # seed + embed + search only
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "src"))

AAPL_CIK = "0000320193"


def _fake_companyfacts_client():
    from facts_test_helpers import FakeCompanyFactsClient, load_aapl_fixture

    return FakeCompanyFactsClient(load_aapl_fixture())


def _fake_documents_client():
    raise NotImplementedError


def seed() -> None:
    """Seed the real storage DB from committed fixtures (idempotent)."""
    from quarterline.config import get_settings
    from quarterline.observability import events  # noqa: F401  (side-effect free check)
    from quarterline.store.db import session_scope

    get_settings.cache_clear()

    from facts_test_helpers import FakeCompanyFactsClient, create_schema, load_aapl_fixture

    from quarterline.core.models import METRIC_IDS  # noqa: F401
    from quarterline.ingest.documents import FormsConfig, ingest_documents
    from quarterline.ingest.facts import ingest_facts
    from quarterline.store.repositories.companies import CompaniesRepo

    create_schema()

    with session_scope() as sess:
        CompaniesRepo(sess).upsert_company(
            ticker="AAPL",
            cik=AAPL_CIK,
            name="Apple Inc.",
            sector="Information Technology",
            country="US",
        )

    fixture = load_aapl_fixture()
    t0 = time.perf_counter()
    report = ingest_facts(tickers=["AAPL"], client=FakeCompanyFactsClient(fixture))
    print(f"[seed] facts ingest: {report.summary() if hasattr(report, 'summary') else report}")
    print(f"[seed] facts ingest took {time.perf_counter() - t0:.1f}s")

    # Documents: serve the committed real 8-K exhibit through the offline client seam.
    from quarterline.sources.sec.client import SecDownloadResult

    exhibit_path = REPO / "tests" / "fixtures" / "documents" / "real_aapl_8k_ex991q2fy26.htm"
    if not exhibit_path.exists():
        exhibit_path = next((REPO / "tests" / "fixtures" / "documents").glob("real_aapl_8k*.htm"))
    exhibit = exhibit_path.read_bytes()
    exhibit_name = exhibit_path.name

    class _FakeSecClient:
        def __init__(self, json_by_url, bytes_by_url):
            self.json_by_url = json_by_url
            self.bytes_by_url = bytes_by_url

        def get_json(self, url):
            return self.json_by_url[url]

        def download(self, url):
            return SecDownloadResult(
                content=self.bytes_by_url[url],
                source_url=url,
                etag='"demo-etag"',
                last_modified="Wed, 09 Sep 2026 00:00:00 GMT",
                cache_hit=False,
            )

        def close(self):
            pass

    base = f"https://www.sec.gov/Archives/edgar/data/{AAPL_CIK}"
    accession = "0000320193-26-000011"
    client = _FakeSecClient(
        json_by_url={
            f"https://data.sec.gov/submissions/CIK{AAPL_CIK}.json": {
                "filings": {
                    "recent": {
                        "accessionNumber": [accession],
                        "form": ["8-K"],
                        "filingDate": ["2026-04-30"],
                        "reportDate": ["2026-04-30"],
                        "primaryDocument": ["a8-k.htm"],
                        "items": ["2.02"],
                    }
                },
            },
            f"{base}/{accession.replace('-', '')}/index.json": {
                "directory": {"item": [{"name": "a8-k.htm"}, {"name": exhibit_name}]}
            },
        },
        bytes_by_url={
            f"{base}/{accession.replace('-', '')}/{exhibit_name}": exhibit,
        },
    )
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as fh:
        fh.write("ticker,cik,name,sector,country\n")
        fh.write(f"AAPL,{AAPL_CIK},Apple Inc.,Information Technology,US\n")
        watchlist = fh.name
    try:
        report = ingest_documents(watchlist, FormsConfig(), client=client)
        print(f"[seed] documents: {report.summary()}")
    finally:
        os.unlink(watchlist)


def index_and_search() -> None:
    env = dict(os.environ, EMBED_PROVIDER="ollama", PYTHONPATH=str(REPO / "src"))
    t0 = time.perf_counter()
    for strategy in ("fixed", "section"):
        out = subprocess.run(
            [
                sys.executable,
                "-m",
                "quarterline",
                "index",
                "build",
                "--strategy",
                strategy,
                "--tickers",
                "AAPL",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO),
            timeout=600,
            check=False,
        )
        print(f"[index:{strategy}] rc={out.returncode} {out.stdout.strip()[-400:]}")
        if out.returncode != 0:
            print(out.stderr[-800:])
            raise SystemExit(f"index build failed for {strategy}")
    print(f"[index] real nomic-embed-text indexing took {time.perf_counter() - t0:.1f}s")

    t0 = time.perf_counter()
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "quarterline",
            "search",
            "--ticker",
            "AAPL",
            "--query",
            "what did management say about revenue and the March quarter",
            "--strategy",
            "section",
            "--retrieval",
            "hybrid",
            "--mode",
            "brief",
            "--top-k",
            "3",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO),
        timeout=300,
        check=False,
    )
    print(f"[search] rc={out.returncode} took {time.perf_counter() - t0:.1f}s")
    print(out.stdout.strip()[-1200:] or out.stderr[-800:])
    if out.returncode != 0:
        raise SystemExit("real-embedding search failed")


def real_brief() -> None:
    code = r"""
import json, time
from fastapi.testclient import TestClient
from quarterline.api.main import create_app
from quarterline.config import get_settings
from quarterline.store.db import session_scope
from quarterline.store.repositories.companies import CompaniesRepo
from quarterline.store.repositories.facts import FactsRepo
get_settings.cache_clear()
with session_scope() as sess:
    company = CompaniesRepo(sess).get_by_ticker("AAPL")
    quarters = FactsRepo(sess).quarters_available(company.id)
periods = sorted(str(q.period_end) for q in quarters)
print("available quarters:", periods)
target = "2026-03-28" if "2026-03-28" in periods else periods[-1]
client = TestClient(create_app())
t0 = time.perf_counter()
r = client.post("/api/c/AAPL/brief", json={"period_end": target})
elapsed = time.perf_counter() - t0
data = r.json()
print(f"Brief HTTP {r.status_code} for {target} in {elapsed:.1f}s")
print("status:", data.get("status"))
brief = data.get("brief") or {}
print("label_echo:", brief.get("label_echo"))
print("metric_mentions:", json.dumps(brief.get("metric_mentions"), indent=1))
for b in (brief.get("bullets") or [])[:3]:
    print("bullet:", b.get("text"), "| citations:", b.get("evidence_ids"))
for rk in (brief.get("risks") or [])[:2]:
    print("risk:", rk.get("text"), "| citations:", rk.get("evidence_ids"))
print("reasons:", data.get("reasons"))
"""
    env = dict(
        os.environ,
        EMBED_PROVIDER="ollama",
        OLLAMA_MODEL=os.environ.get("OLLAMA_MODEL", "qwen3:4b"),
        PYTHONPATH=f"{REPO / 'src'};{REPO / 'tests'}",
    )
    t0 = time.perf_counter()
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO),
        timeout=1500,
        check=False,
    )
    print(f"[brief] total {time.perf_counter() - t0:.1f}s rc={out.returncode}")
    print(out.stdout.strip() or out.stderr[-1500:])


if __name__ == "__main__":
    seed()
    index_and_search()
    if "--no-brief" not in sys.argv:
        real_brief()
