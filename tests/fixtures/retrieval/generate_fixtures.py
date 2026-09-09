"""Regenerates tests/fixtures/retrieval/fixture_embeddings.json.

NOT real model output — a deterministic test artifact (SPEC §26 allows
committed fixture embeddings with documented provenance for offline CI).

The vectors come from :class:`quarterline.retrieve.embeddings.FakeEmbeddingProvider`
(hash-based, seeded). Regenerate with (from the repo root)::

    uv run python tests/fixtures/retrieval/generate_fixtures.py

The run is fully offline: document bytes come from ``tests/fixtures/documents/``
(the REAL Apple EX-99.1 exhibit and the clearly synthetic fixtures) and are
ingested through an in-file fake SEC client into a throwaway SQLite database.
Determinism: identical fixture bytes + pinned chunking strategies + pinned
(seed, dim) => identical text hashes and vectors; the committed JSON changes
only when the pipeline or the provider algorithm changes (which is exactly
what the fixture regression test must catch).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))

FIXTURE_DIR = Path(__file__).resolve().parent
DOCUMENTS_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "documents"
SEED = 20260909
DIM = 64

# Canonical search queries pinned alongside the chunk vectors.
QUERY_TEXTS = [
    "Apple revenue March quarter",
    "revenue",
    "risk factors",
    "Northwind manufacturing",
    "operating expenses",
]


class FakeSecClient:
    """Offline stand-in for SecClient (same shape as the wave-1 test fake)."""

    def __init__(self, json_by_url: dict[str, object], bytes_by_url: dict[str, bytes]) -> None:
        self.json_by_url = json_by_url
        self.bytes_by_url = bytes_by_url

    def get_json(self, url: str) -> object:
        return self.json_by_url[url]

    def download(self, url: str):
        from quarterline.sources.sec.client import SecDownloadResult

        return SecDownloadResult(
            content=self.bytes_by_url[url],
            source_url=url,
            etag='"fixture-etag"',
            last_modified="Wed, 09 Sep 2026 00:00:00 GMT",
            cache_hit=False,
        )

    def close(self) -> None:
        pass


def _build_corpus(tmp: Path):
    from quarterline.ingest.documents import FormsConfig, ingest_documents
    from quarterline.store.db import get_engine
    from quarterline.store.models import Base

    os.environ["STORAGE_DIR"] = str(tmp / "storage")
    os.environ["DATABASE_URL"] = f"sqlite:///{(tmp / 'app.db').as_posix()}"
    os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"
    from quarterline.store.db import reset_db_caches

    reset_db_caches()
    Base.metadata.create_all(get_engine())

    real = (DOCUMENTS_FIXTURES / "real_aapl_8k_ex991_q2fy26.htm").read_bytes()
    injection = (DOCUMENTS_FIXTURES / "injection_filing.html").read_bytes()
    base = "https://www.sec.gov/Archives/edgar/data/0000320193"
    ten_q_html = (
        b"<!DOCTYPE html>\n<html><head><title>10-Q</title></head><body>\n"
        b"<h1>Part I - Financial Information</h1>\n<table>\n"
        b"<tr><td>Item 1.</td><td>Financial Statements</td><td>3</td></tr>\n"
        b"<tr><td>Item 2.</td><td>Management's Discussion and Analysis of Financial "
        b"Condition and Results of Operations</td><td>12</td></tr>\n</table>\n"
        b"<h2>Item 2. Management's Discussion and Analysis of Financial Condition and "
        b"Results of Operations</h2>\n"
        b"<p>Revenue increased 12 percent driven by strong services growth across every "
        b"segment during the quarter, while operating expenses grew more slowly than "
        b"revenue and gross margin expanded. The company continued returning capital to "
        b"shareholders while operating cash flow remained robust.</p>\n"
        b"<h1>Part II - Other Information</h1>\n"
        b"<h2>Item 1A. Risk Factors</h2>\n"
        b"<p>The risk factors previously disclosed in the annual report have not changed "
        b"materially, although supply chain concentration and evolving data regulation "
        b"remain areas of continued attention for the business this year.</p>\n"
        b"<h2>Item 6. Exhibits</h2>\n"
        b"<p>Exhibit 31 certification rules apply.</p>\n</body></html>"
    )
    json_by_url: dict[str, object] = {
        "https://data.sec.gov/submissions/CIK0000320193.json": {
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
    bytes_by_url = {
        f"{base}/000032019326000099/a10-q.htm": ten_q_html,
        f"{base}/000032019326000011/a8-kex991q2202603282026.htm": real,
        f"{base}/000032019326000013/exhibit-99-pressrelease.htm": injection,
    }
    watchlist = tmp / "watchlist.csv"
    watchlist.write_text(
        "ticker,cik,name,sector,country\nAAPL,0000320193,Apple Inc.,Information Technology,US\n",
        encoding="utf-8",
    )
    report = ingest_documents(
        watchlist, FormsConfig(), client=FakeSecClient(json_by_url, bytes_by_url)
    )
    assert report.documents_failed == 0, report.errors


def main() -> int:
    from quarterline.ingest.documents import ingest_pdf_file
    from quarterline.retrieve.chunk_fixed import FixedWindowChunker
    from quarterline.retrieve.chunk_section import SectionParentChildChunker
    from quarterline.retrieve.embeddings import FakeEmbeddingProvider
    from quarterline.retrieve.models import DocumentMeta
    from quarterline.retrieve.search import build_index
    from quarterline.store.db import session_scope
    from quarterline.store.repositories.documents import DocumentsRepo

    tmp = Path(tempfile.mkdtemp())
    _build_corpus(tmp)
    # The ACME PDF exercises document_kind=pdf rows.
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{(tmp / 'app.db').as_posix()}")
    ingest_pdf_file(
        "ACME",
        "https://ir.example.invalid/reports/q2-review.pdf",
        date(2026, 7, 15),
        DOCUMENTS_FIXTURES / "synthetic_mda.pdf",
    )

    provider = FakeEmbeddingProvider(dim=DIM, seed=SEED)
    with session_scope() as session:
        fixed_stats = build_index(session, "fixed", provider)
        section_stats = build_index(session, "section", provider)

        repo = DocumentsRepo(session)
        texts: dict[str, str] = {}
        for document in repo.list_documents():
            for section in repo.list_sections(document.id):
                for chunker in (FixedWindowChunker(), SectionParentChildChunker()):
                    meta = DocumentMeta(document_id=document.id, content_hash=f"doc-{document.id}")
                    from quarterline.retrieve.chunk_fixed import SectionRef

                    section_ref = SectionRef(
                        section_id=section.id,
                        section_type=section.section_type,
                        heading=section.heading,
                        start_offset=0,
                        end_offset=len(section.text or ""),
                    )
                    drafts = chunker.chunk(section.text or "", [section_ref], meta)
                    for draft in drafts:
                        digest = hashlib.sha256(draft.text.encode("utf-8")).hexdigest()
                        texts.setdefault(digest, draft.text)

        vectors: dict[str, list[float]] = {}
        hashes = sorted(texts)
        for start in range(0, len(hashes), 32):
            batch = hashes[start : start + 32]
            # The Fake provider's document_prefix is "" — exactly what
            # build_index stores — so no Nomic prefix is applied here.
            embedded = provider.embed([texts[h] for h in batch])
            for digest, vector in zip(batch, embedded, strict=True):
                vectors[digest] = vector
        query_vectors = {
            hashlib.sha256(q.encode("utf-8")).hexdigest(): vector
            for q, vector in zip(QUERY_TEXTS, provider.embed(QUERY_TEXTS), strict=True)
        }

    payload = {
        "manifest": {
            "artifact": "fixture_embeddings.json",
            "generated": "2026-09-09",
            "generator": "tests/fixtures/retrieval/generate_fixtures.py",
            "regenerate_command": "uv run python tests/fixtures/retrieval/generate_fixtures.py",
            "provenance": (
                "NOT real model output — deterministic test artifact. Vectors "
                "are hash-based outputs of FakeEmbeddingProvider and carry no "
                "semantic signal; they exist so retrieval tests run offline "
                "with committed, reproducible fixtures (SPEC 26)."
            ),
            "provider": {
                "class": "quarterline.retrieve.embeddings.FakeEmbeddingProvider",
                "model_id": provider.model_id,
                "seed": SEED,
                "dim": DIM,
                "normalized": True,
            },
            "chunking": {
                "fixed": FixedWindowChunker.STRATEGY_VERSION,
                "section": SectionParentChildChunker.STRATEGY_VERSION,
            },
            "source_fixtures": [
                "tests/fixtures/documents/real_aapl_8k_ex991_q2fy26.htm (real SEC exhibit)",
                "tests/fixtures/documents/injection_filing.html (synthetic)",
                "tests/fixtures/documents/synthetic_mda.pdf (synthetic)",
            ],
            "chunk_texts": len(vectors),
            "query_texts": len(query_vectors),
        },
        "chunk_vectors": [
            {"text_hash": digest, "preview": texts[digest][:80], "vector": vectors[digest]}
            for digest in hashes
        ],
        "query_vectors": [
            {"text_hash": digest, "text": text, "vector": query_vectors[digest]}
            for text, digest in (
                (q, hashlib.sha256(q.encode("utf-8")).hexdigest()) for q in QUERY_TEXTS
            )
        ],
    }
    out = FIXTURE_DIR / "fixture_embeddings.json"
    out.write_text(json.dumps(payload, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(vectors)} chunk vectors, {len(query_vectors)} query vectors)")
    print(f"fixed index version: {fixed_stats.index_version}")
    print(f"section index version: {section_stats.index_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
