"""Boot smoke check for the deterministic UI/API (wave F4).

Seeds a throwaway SQLite database with the AAPL fixture, boots the app with
Ollama unreachable, and exercises every non-LLM route. Run:

    uv run python scripts/ui_smoke.py
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

from sqlalchemy import select

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests"))

from facts_test_helpers import (
    FakeCompanyFactsClient,
    create_schema,
    load_aapl_fixture,
)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="quarterline-smoke-"))
    os.environ["DATABASE_URL"] = f"sqlite:///{(tmp / 'app.db').as_posix()}"
    os.environ["STORAGE_DIR"] = str(tmp / "storage")
    os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"  # nothing listens: app must not care

    from quarterline.ingest.facts import ingest_facts
    from quarterline.store.db import session_scope
    from quarterline.store.models import Chunk, Company
    from quarterline.store.repositories.documents import DocumentsRepo, SectionInput

    create_schema()
    report = ingest_facts(
        tickers=["AAPL"],
        watchlist=str(REPO_ROOT / "data" / "watchlist_us.csv"),
        client=FakeCompanyFactsClient(load_aapl_fixture()),
    )
    print(f"fixture ingest errors: {report.errors or 'none'}")

    with session_scope() as session:
        company = session.execute(select(Company).where(Company.ticker == "AAPL")).scalar_one()
        repo = DocumentsRepo(session)
        document, _ = repo.upsert_document(
            company_id=company.id,
            accession="000032019326000099",
            form="10-Q",
            document_kind="10-Q",
            filed_at=date(2026, 5, 1),
            source_url="https://www.sec.gov/Archives/edgar/data/320193/000032019326000099/a10-q.htm",
            extraction_version="smoke",
            extraction_status="ok",
        )
        repo.replace_sections(
            document.id,
            [SectionInput(section_type="mda", heading="Item 2. MD&A", text="Cleaned text only.")],
        )
        text = "smoke evidence chunk"
        session.add(
            Chunk(
                document_id=document.id,
                strategy="section",
                strategy_version="sections-1",
                text=text,
                text_hash=hashlib.sha256(text.encode()).hexdigest(),
                start_offset=0,
                end_offset=len(text),
            )
        )
        document_id = document.id

    from quarterline.api.routers.filings import evidence_id_for

    with session_scope() as session:
        chunk = session.execute(select(Chunk).where(Chunk.document_id == document_id)).scalar_one()
        evidence_id = evidence_id_for(chunk)

    from fastapi.testclient import TestClient

    from quarterline.api.main import create_app

    failures = 0
    with TestClient(create_app()) as client:
        paths = [
            "/",
            "/screener",
            "/screener?revenue_yoy_gt=0.1&minimum_data_coverage=0.8",
            "/c/AAPL",
            "/c/AAPL?period_end=2025-09-27",
            "/c/MSFT",  # registered on the watchlist, no ingested data
            "/c/NOPE",  # unknown -> 404
            "/c/AAPL/provenance/revenue_yoy",
            "/c/AAPL/provenance/revenue_yoy?period_end=2026-06-27",
            "/c/AAPL/provenance/bogus_metric",  # allowlist -> 404
            "/api/c/AAPL/facts",
            "/api/c/AAPL/facts?period_end=2026-06-27",
            "/api/c/AAPL/metrics",
            "/api/c/AAPL/provenance/revenue_yoy?period_end=2026-06-27",
            "/api/c/NOPE/metrics",  # -> 404 JSON
            f"/filings/{document_id}",
            "/filings/999999",  # -> 404
            f"/evidence/{evidence_id}",
            "/evidence/ev-0123456789ab",  # index not built -> 404
            "/health",
        ]
        for path in paths:
            response = client.get(path)
            print(f"{response.status_code}  GET {path}")
            if response.status_code >= 500:
                failures += 1
        post = client.post("/screener", data={"revenue_yoy_gt": "0.1", "fcf_positive": "on"})
        print(f"{post.status_code}  POST /screener")
        if post.status_code >= 500:
            failures += 1

    print("smoke:", "FAIL" if failures else "OK")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
