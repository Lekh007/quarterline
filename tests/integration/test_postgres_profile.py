"""PostgreSQL 16 + pgvector contract tests (SPEC §15 profile, §31 criterion 10).

Mirrors the SQLite retrieval contract suite for the PostgreSQL backend:

- migrate-equivalent schema init (ORM metadata + ``PostgresSearchIndexRepo.ensure_schema``
  raw DDL: ``CREATE EXTENSION vector``, tsvector table + GIN index, pgvector
  ``chunk_vectors``, ``index_manifest``);
- chunk + embedding roundtrip with native pgvector cosine ordering;
- lexical search over ``to_tsquery``/``ts_rank_cd`` (labeled ``postgres-tsrank``
  — PostgreSQL full-text ranking is NOT BM25, SPEC §4);
- C8 evidence-id resolution;
- embedding-model mismatch rejection (SPEC §15.7);
- idempotent rebuilds and index-version derivation.

SKIP CONTRACT (never failing CI): the module skips at import when no
PostgreSQL DBAPI is installed, and skips in fixtures when the server is
unreachable (fast <1 s TCP probe) or the ``vector`` extension is unavailable.
The default suite (``uv run pytest -q``, CI) therefore only ever sees skips
here. Run for real with ``make test-postgres`` (boots ``docker-compose.yml``)
and ``QUARTERLINE_TEST_POSTGRES_URL`` to override the target DSN.

Marker note: ``postgres`` is applied via ``pytestmark``. pyproject.toml
(wave-0 owned) defines the ``markers`` list without ``--strict-markers``, so an
unregistered marker is a harmless PytestUnknownMarkWarning, never an error;
registering it in pyproject is a one-line orchestrator follow-up.
"""

from __future__ import annotations

import importlib
import os
import socket
from datetime import date
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit

import numpy as np
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from quarterline.retrieve.embeddings import EmbeddingModelMismatchError
from quarterline.retrieve.lexical import LexicalFilters
from quarterline.retrieve.models import ChunkDraft, evidence_id_for, text_hash
from quarterline.retrieve.vector import DenseFilters
from quarterline.store.models import Base, Company, Document, Section
from quarterline.store.repositories.search_postgres import (
    LEXICAL_METHOD,
    PostgresSearchIndexRepo,
    ensure_embedding_model_match,
    search_dense_postgres,
    search_lexical_postgres,
)

pytestmark = pytest.mark.postgres

DEFAULT_PG_URL = "postgresql://quarterline:quarterline@127.0.0.1:5432/quarterline"

_DRIVERS: tuple[tuple[str, str], ...] = (
    ("psycopg", "postgresql+psycopg"),
    ("psycopg2", "postgresql+psycopg2"),
    ("pg8000", "postgresql+pg8000"),
)


def _import_driver() -> tuple[str | None, str | None]:
    for module_name, dialect in _DRIVERS:
        try:
            importlib.import_module(module_name)
        except ImportError:
            continue
        return module_name, dialect
    return None, None


_DRIVER_MODULE, _DRIVER_DIALECT = _import_driver()
if _DRIVER_MODULE is None:
    pytest.skip(
        "No PostgreSQL DBAPI installed (tried: " + ", ".join(d for d, _ in _DRIVERS) + "). "
        "The pgvector contract tests require a driver; they skip instead of failing "
        "(add psycopg to pyproject to enable — see implementation log wave F8).",
        allow_module_level=True,
    )


def _tcp_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _pg_target() -> tuple[str, str, int]:
    """(sqlalchemy_url, host, port) honoring QUARTERLINE_TEST_POSTGRES_URL."""
    raw = os.environ.get("QUARTERLINE_TEST_POSTGRES_URL", DEFAULT_PG_URL)
    parts = urlsplit(raw)
    scheme = parts.scheme if "+" in parts.scheme else _DRIVER_DIALECT
    url = urlunsplit((scheme, parts.netloc, parts.path or "/quarterline", parts.query, ""))
    return url, parts.hostname or "127.0.0.1", parts.port or 5432


@pytest.fixture(scope="session")
def pg_engine():
    url, host, port = _pg_target()
    if not _tcp_reachable(host, port):
        pytest.skip(
            f"PostgreSQL not reachable at {host}:{port} — pgvector contract tests skipped "
            "(start it with: docker compose up -d quarterline-db, or make test-postgres)"
        )
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            has_vector = conn.execute(
                text("SELECT count(*) FROM pg_available_extensions WHERE name = 'vector'")
            ).scalar()
    except (SQLAlchemyError, OSError, ValueError) as exc:  # not usable -> skip, never fail CI
        engine.dispose()
        pytest.skip(f"PostgreSQL reachable at {host}:{port} but connection failed: {exc!r}")
    if not has_vector:
        engine.dispose()
        pytest.skip("PostgreSQL reachable but the pgvector extension is not available")
    yield engine
    engine.dispose()


@pytest.fixture()
def pg_session(pg_engine):
    """Fresh schema per test (drop + create of the ORM tables + repo DDL)."""
    Base.metadata.drop_all(pg_engine)
    Base.metadata.create_all(pg_engine)
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def seeded(pg_session):
    """One company + one 8-K document + one earnings-release section."""
    company = Company(ticker="AAPL", cik="0000320193", name="Apple Inc.")
    pg_session.add(company)
    pg_session.flush()
    document = Document(
        company_id=company.id,
        accession="0000320193-26-000011",
        form="8-K",
        document_kind="8-K-exhibit",
        period_end=date(2026, 3, 28),
        filed_at=date(2026, 4, 30),
    )
    pg_session.add(document)
    pg_session.flush()
    body = (
        "Revenue grew strongly in the March quarter driven by services. "
        "Operating cash flow remained robust while the board announced a "
        "dividend increase and an expanded buyback program for shareholders."
    )
    section = Section(
        document_id=document.id,
        section_type="earnings_release",
        text=body,
        start_offset=0,
        end_offset=len(body),
    )
    pg_session.add(section)
    pg_session.flush()
    return SimpleNamespace(company=company, document=document, section=section, body=body)


def _drafts(body: str) -> list[ChunkDraft]:
    """Two base chunks + one bounded expansion window (section-family shape).

    Draft keys use the row-family convention (`chunk:`/`window:` prefixes) so
    ``ChunkWriteReport.base_chunk_ids`` classifies them like the real indexer.
    """
    first, second = body[:100], body[100:200]
    window = body[:200]
    return [
        ChunkDraft(
            key="chunk:c1",
            role="chunk",
            text=first,
            start_offset=0,
            end_offset=len(first),
            token_count=len(first) // 4,
            text_hash=text_hash(first),
        ),
        ChunkDraft(
            key="chunk:c2",
            role="chunk",
            text=second,
            start_offset=100,
            end_offset=100 + len(second),
            token_count=len(second) // 4,
            text_hash=text_hash(second),
        ),
        ChunkDraft(
            key="window:w1",
            role="window",
            parent_key="chunk:c1",
            text=window,
            start_offset=0,
            end_offset=len(window),
            token_count=len(window) // 4,
            text_hash=text_hash(window),
        ),
    ]


def _repo(pg_session) -> PostgresSearchIndexRepo:
    repo = PostgresSearchIndexRepo(pg_session)
    repo.ensure_schema()
    return repo


def _dense_filters(strategy_version: str = "fixed-1") -> DenseFilters:
    return DenseFilters(
        ticker="AAPL",
        strategies=["fixed"],
        strategy_version_by_strategy={"fixed": strategy_version},
    )


def _unit_vector(dim: int, hot: int) -> np.ndarray:
    vec = np.zeros(dim, dtype=np.float32)
    vec[hot % dim] = 1.0
    return vec


# ---------------------------------------------------------------------------
# Contract cases
# ---------------------------------------------------------------------------


def test_chunk_write_rebuild_idempotent_and_evidence_ids_resolve(pg_session, seeded):
    repo = _repo(pg_session)
    first = repo.write_chunks(
        document_id=seeded.document.id,
        strategy="fixed",
        strategy_version="fixed-1",
        drafts=_drafts(seeded.body),
    )
    second = repo.write_chunks(
        document_id=seeded.document.id,
        strategy="fixed",
        strategy_version="fixed-1",
        drafts=_drafts(seeded.body),
    )
    assert first.written == 3 and second.written == 0 and second.reused == 3
    assert first.ids_by_key == second.ids_by_key
    base_ids = first.base_chunk_ids()
    assert len(base_ids) == 2  # window is stored but not a retrieval unit

    # C8 evidence id resolves to the exact chunk row.
    first_draft = _drafts(seeded.body)[0]
    evidence = evidence_id_for(
        seeded.document.id,
        "fixed",
        first_draft.start_offset,
        first_draft.end_offset,
        first_draft.text_hash,
    )
    resolved = repo.get_chunk_by_evidence_id(evidence)
    assert resolved is not None and resolved.text_hash == first_draft.text_hash
    assert repo.get_chunk_by_evidence_id("ev-000000000000") is None
    assert repo.get_chunk_by_evidence_id("not-an-evidence-id") is None

    # Bounded window expansion maps child -> window row.
    windows = repo.windows_for_children(base_ids, strategy="fixed", strategy_version="fixed-1")
    child = first.ids_by_key["chunk:c1"]
    assert set(windows) == {child}
    assert windows[child].parent_id == child


def test_pgvector_roundtrip_and_native_cosine_ordering(pg_session, seeded):
    repo = _repo(pg_session)
    report = repo.write_chunks(
        document_id=seeded.document.id,
        strategy="fixed",
        strategy_version="fixed-1",
        drafts=_drafts(seeded.body),
    )
    ids = report.base_chunk_ids()
    v_near = _unit_vector(8, hot=0)
    v_far = _unit_vector(8, hot=1)

    assert (
        repo.store_embedding(
            chunk_id=ids[0],
            text_hash=text_hash(seeded.body[:100]),
            provider="fake",
            model="fake-embed-8d",
            revision="",
            vector=v_near,
            normalized=True,
            dim=8,
        )
        is True
    )
    # Same identity -> cached, not rewritten.
    assert (
        repo.store_embedding(
            chunk_id=ids[0],
            text_hash=text_hash(seeded.body[:100]),
            provider="fake",
            model="fake-embed-8d",
            revision="",
            vector=v_near,
            normalized=True,
            dim=8,
        )
        is False
    )
    assert (
        repo.store_embedding(
            chunk_id=ids[1],
            text_hash=text_hash(seeded.body[100:200]),
            provider="fake",
            model="fake-embed-8d",
            revision="",
            vector=v_far,
            normalized=True,
            dim=8,
        )
        is True
    )

    roundtrip = repo.get_vectors_for_chunks(
        ids, provider="fake", model="fake-embed-8d", revision=""
    )
    assert set(roundtrip) == set(ids)
    assert np.allclose(roundtrip[ids[0]], v_near, atol=1e-5)
    cached = repo.get_cached_vectors(
        [text_hash(seeded.body[:100])], provider="fake", model="fake-embed-8d", revision=""
    )
    assert text_hash(seeded.body[:100]) in cached

    query = (v_near * 0.5).astype(np.float32)  # same direction -> cosine 1.0
    hits = search_dense_postgres(
        pg_session,
        query,
        _dense_filters(),
        top_k=2,
        provider="fake",
        model="fake-embed-8d",
        revision="",
    )
    assert [h.chunk_id for h in hits] == [ids[0], ids[1]]
    assert hits[0].rank == 1 and hits[0].score == pytest.approx(1.0, abs=1e-5)
    assert hits[1].rank == 2 and hits[1].score < hits[0].score

    # Strict model identity: an unindexed model sees no rows.
    assert (
        search_dense_postgres(
            pg_session, query, _dense_filters(), provider="other", model="other", revision=""
        )
        == []
    )


def test_dense_dimension_mismatch_rejected(pg_session, seeded):
    repo = _repo(pg_session)
    report = repo.write_chunks(
        document_id=seeded.document.id,
        strategy="fixed",
        strategy_version="fixed-1",
        drafts=_drafts(seeded.body),
    )
    repo.store_embedding(
        chunk_id=report.base_chunk_ids()[0],
        text_hash="h1",
        provider="fake",
        model="fake-embed-8d",
        revision="",
        vector=_unit_vector(8, hot=0),
        normalized=True,
        dim=8,
    )
    with pytest.raises(ValueError, match="dimension mismatch"):
        search_dense_postgres(
            pg_session,
            np.ones(4, dtype=np.float32),
            _dense_filters(),
            provider="fake",
            model="fake-embed-8d",
            revision="",
        )


def test_lexical_tsrank_search_metadata_filters_and_sanitization(pg_session, seeded):
    repo = _repo(pg_session)
    report = repo.write_chunks(
        document_id=seeded.document.id,
        strategy="fixed",
        strategy_version="fixed-1",
        drafts=_drafts(seeded.body),
    )
    assert repo.sync_fts(report.base_chunk_ids()) == 2
    assert repo.lexical_method == LEXICAL_METHOD == "postgres-tsrank"

    filters = LexicalFilters(
        ticker="AAPL", strategies=["fixed"], strategy_version_by_strategy={"fixed": "fixed-1"}
    )
    hits = search_lexical_postgres(pg_session, "revenue growth", filters)
    assert hits, "expected tsquery hits for corpus terms"
    assert hits[0].rank == 1
    top_chunk = repo.get_chunk(hits[0].chunk_id)
    assert top_chunk is not None and "Revenue" in (top_chunk.text or "")

    # FTS operators / punctuation are inert: tokenized to plain words, no crash.
    hostile = "revenue AND (growth*) NEAR dividend; DROP TABLE chunks--"
    hostile_hits = search_lexical_postgres(pg_session, hostile, filters)
    assert hostile_hits and all(h.rank >= 1 for h in hostile_hits)

    # Nothing searchable -> empty result, not an error.
    assert search_lexical_postgres(pg_session, "!!! @@ …", filters) == []
    # Wrong strategy version -> no arms -> empty.
    empty_filters = LexicalFilters(
        ticker="AAPL", strategies=["fixed"], strategy_version_by_strategy={}
    )
    assert search_lexical_postgres(pg_session, "revenue", empty_filters) == []
    # Metadata filters apply: another company's sections never leak.
    section_filters = LexicalFilters(
        ticker="AAPL",
        strategies=["fixed"],
        strategy_version_by_strategy={"fixed": "fixed-1"},
        sections=["mda"],
    )
    assert search_lexical_postgres(pg_session, "revenue", section_filters) == []


def test_manifest_index_version_and_embedding_model_mismatch_gate(pg_session, seeded):
    repo = _repo(pg_session)
    repo.write_chunks(
        document_id=seeded.document.id,
        strategy="fixed",
        strategy_version="fixed-1",
        drafts=_drafts(seeded.body),
    )
    corpus_hash = repo.compute_corpus_hash(strategy="fixed", strategy_version="fixed-1")
    version_a = repo.record_manifest(
        strategy="fixed",
        strategy_version="fixed-1",
        embedding_provider="fake",
        embedding_model="fake-embed-8d",
        embedding_model_revision="",
        dim=8,
        corpus_hash=corpus_hash,
        chunk_count=2,
        embedded_chunk_count=2,
    )
    assert len(version_a) == 16
    manifest = repo.get_manifest(
        strategy="fixed",
        strategy_version="fixed-1",
        provider="fake",
        model="fake-embed-8d",
        revision="",
    )
    assert manifest is not None and manifest["index_version"] == version_a

    any_manifest = repo.get_any_manifest(strategy="fixed", strategy_version="fixed-1")
    assert any_manifest is not None and any_manifest["embedding_model"] == "fake-embed-8d"
    assert (
        ensure_embedding_model_match(
            any_manifest,
            provider="fake",
            model="fake-embed-8d",
            revision="",
            strategy="fixed",
            strategy_version="fixed-1",
        )
        == any_manifest
    )
    with pytest.raises(EmbeddingModelMismatchError, match="SPEC"):
        ensure_embedding_model_match(
            any_manifest,
            provider="other",
            model="other-model",
            revision="",
            strategy="fixed",
            strategy_version="fixed-1",
        )

    # SPEC §15.6: switching embedding models derives a NEW index version.
    version_b = repo.record_manifest(
        strategy="fixed",
        strategy_version="fixed-1",
        embedding_provider="other",
        embedding_model="other-model",
        embedding_model_revision="",
        dim=8,
        corpus_hash=corpus_hash,
        chunk_count=2,
        embedded_chunk_count=2,
    )
    assert version_b != version_a
    by_version = repo.get_manifest_by_version(version_a)
    assert by_version is not None and by_version["embedding_model"] == "fake-embed-8d"


def test_corpus_stats_and_backend_labels(pg_session, seeded):
    repo = _repo(pg_session)
    repo.write_chunks(
        document_id=seeded.document.id,
        strategy="fixed",
        strategy_version="fixed-1",
        drafts=_drafts(seeded.body),
    )
    stats = repo.corpus_stats()
    assert stats["documents"] == 1
    # 2 base chunks carry strategy_version "fixed-1"; the bounded expansion
    # window is its own row family ("fixed-1-window") per the wave-2
    # row-family convention.
    assert stats["chunks"]["fixed/fixed-1"] == 2
    assert stats["chunks"]["fixed/fixed-1-window"] == 1
    assert repo.storage_backend == "postgres-pgvector"
    assert repo.lexical_method == "postgres-tsrank"
