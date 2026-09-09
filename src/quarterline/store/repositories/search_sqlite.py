"""SQLite retrieval-index repository: chunks, FTS5, embeddings, manifest.

Contract C5 (typed boundary): raw SQL lives only here, uses fixed statement
fragments and bound parameters — user input never interpolates into SQL
(SPEC §2.3.5).

Schema additions owned by this module (created lazily by
:meth:`SearchIndexRepo.ensure_schema`; outside the W0 ORM metadata because
``store/models.py`` is wave-0 owned — a future migration can formalize them):

- ``chunks_fts`` — contentful FTS5 virtual table over chunk text with an
  UNINDEXED ``chunk_id`` column mapping row -> ``chunks.id`` (a contentful
  table is deliberately chosen over a contentless one: deletes by chunk id
  stay simple and the text duplication is bounded);
- ``index_manifest`` — one row per (strategy, strategy_version, embedding
  provider/model/revision) recording corpus hash, counts, dim, and the
  derived ``index_version`` (SPEC §15.6: switching embedding models creates a
  new index version).

Row-family convention (SPEC §14, see ``chunk_section``): for the section
strategy one build writes base children with the bare ``strategy_version`` and
parents/windows with ``-parent``/``-window`` suffixes; only base rows are
embedded, FTS-indexed, and retrievable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import Session

from quarterline.retrieve.models import ChunkDraft, evidence_id_for
from quarterline.store.models import Chunk, Embedding, Section

FTS_CREATE = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    chunk_id UNINDEXED
)
"""

MANIFEST_CREATE = """
CREATE TABLE IF NOT EXISTS index_manifest (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    index_version TEXT NOT NULL,
    strategy TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    embedding_provider TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_model_revision TEXT NOT NULL DEFAULT '',
    dim INTEGER,
    corpus_hash TEXT NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    embedded_chunk_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (strategy, strategy_version, embedding_provider, embedding_model,
            embedding_model_revision)
)
"""

_VECTOR_DTYPE = "<f4"


def encode_vector(vector: list[float] | np.ndarray) -> bytes:
    """float32 little-endian BLOB encoding for ``embeddings.vector``."""
    return np.asarray(vector, dtype=_VECTOR_DTYPE).tobytes()


def decode_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=_VECTOR_DTYPE).astype(np.float32)


def compute_index_version(
    *,
    embedding_provider: str,
    embedding_model: str,
    embedding_model_revision: str,
    strategy: str,
    strategy_version: str,
    corpus_hash: str,
) -> str:
    """Deterministic index version (SPEC §15.6).

    Changing the embedding model, the chunking strategy/version, or the corpus
    content-hash set changes the version.
    """
    payload = (
        f"{embedding_provider}|{embedding_model}|{embedding_model_revision}"
        f"|{strategy}|{strategy_version}|{corpus_hash}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def provider_revision(provider) -> str:
    """Normalized model-revision key (unknown -> empty string)."""
    return getattr(provider, "model_revision", None) or ""


@dataclass
class ChunkWriteReport:
    """Outcome of writing one document's drafts."""

    ids_by_key: dict[str, int] = field(default_factory=dict)
    written: int = 0
    reused: int = 0  # identical identity already present (idempotent rebuild)

    def base_chunk_ids(self) -> list[int]:
        """Ids of retrieval units (role ``chunk``; keys are strategy-prefixed)."""
        return [
            chunk_id
            for key, chunk_id in self.ids_by_key.items()
            if not key.startswith("parent:") and not key.startswith("window:")
        ]


@dataclass(frozen=True)
class EvidenceRow:
    """Everything needed to build an :class:`EvidenceItem` for one chunk."""

    chunk_id: int
    document_id: int
    strategy: str
    strategy_version: str
    text: str | None
    text_hash: str | None
    token_count: int | None
    start_offset: int | None
    end_offset: int | None
    section_type: str | None
    page_start: int | None
    page_end: int | None


class SearchIndexRepo:
    """Indexer + lookup over the ``chunks``/``embeddings`` tables (SQLite)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- schema ---------------------------------------------------------------

    def ensure_schema(self) -> None:
        self.session.execute(text(FTS_CREATE))
        self.session.execute(text(MANIFEST_CREATE))
        self.session.flush()

    # -- chunk writes -----------------------------------------------------------

    def write_chunks(
        self,
        *,
        document_id: int,
        strategy: str,
        strategy_version: str,
        drafts: list[ChunkDraft],
    ) -> ChunkWriteReport:
        """Idempotently persist drafts (parents -> children -> windows).

        Identity is the ``chunks`` unique key; drafts whose text hash already
        exists for the same (document, strategy, row version) are reused, so
        rebuilding the same index yields identical rows and ids (SPEC §14).
        """
        report = ChunkWriteReport()
        existing: dict[tuple[str, str], int] = {}
        for row in self.session.execute(
            text(
                "SELECT id, strategy_version, text_hash FROM chunks "
                "WHERE document_id = :document_id AND strategy = :strategy"
            ),
            {"document_id": document_id, "strategy": strategy},
        ):
            existing[(row.strategy_version, row.text_hash)] = int(row.id)

        ordered = [d for d in drafts if d.role == "parent"]
        ordered += [d for d in drafts if d.role == "chunk"]
        ordered += [d for d in drafts if d.role == "window"]

        for draft in ordered:
            row_version = (
                strategy_version if draft.role == "chunk" else f"{strategy_version}-{draft.role}"
            )
            found = existing.get((row_version, draft.text_hash))
            if found is None:
                chunk = Chunk(
                    document_id=document_id,
                    section_id=draft.section_id,
                    parent_id=None,
                    strategy=strategy,
                    strategy_version=row_version,
                    text=draft.text,
                    text_hash=draft.text_hash,
                    token_count=draft.token_count,
                    start_offset=draft.start_offset,
                    end_offset=draft.end_offset,
                    page_start=draft.page_start,
                    page_end=draft.page_end,
                )
                self.session.add(chunk)
                self.session.flush()  # assign the id before dependents reference it
                chunk_id = int(chunk.id)
                report.written += 1
            else:
                chunk_id = found
                report.reused += 1
            existing[(row_version, draft.text_hash)] = chunk_id
            report.ids_by_key[draft.key] = chunk_id

        # Parent linkage (parent rows were persisted first, so ids exist).
        for draft in ordered:
            if draft.parent_key is None:
                continue
            parent_id = report.ids_by_key.get(draft.parent_key)
            chunk_id = report.ids_by_key.get(draft.key)
            if parent_id is not None and chunk_id is not None and chunk_id != parent_id:
                self.session.execute(
                    text("UPDATE chunks SET parent_id = :parent_id WHERE id = :chunk_id"),
                    {"parent_id": parent_id, "chunk_id": chunk_id},
                )
        self.session.flush()
        return report

    def sync_fts(self, chunk_ids: list[int]) -> int:
        """Rebuild FTS rows for the given retrieval units (delete + insert)."""
        if not chunk_ids:
            return 0
        rows = (
            self.session.query(Chunk.id, Chunk.text)  # type: ignore[misc]
            .filter(Chunk.id.in_(list(chunk_ids)))
            .all()
        )
        delete_stmt = text("DELETE FROM chunks_fts WHERE chunk_id = :chunk_id")
        insert_stmt = text("INSERT INTO chunks_fts (chunk_id, text) VALUES (:chunk_id, :text)")
        count = 0
        for chunk_id in chunk_ids:
            self.session.execute(delete_stmt, {"chunk_id": chunk_id})
        for chunk_id, chunk_text in rows:
            if chunk_text:
                self.session.execute(insert_stmt, {"chunk_id": int(chunk_id), "text": chunk_text})
                count += 1
        self.session.flush()
        return count

    # -- lookups ---------------------------------------------------------------

    def get_chunk(self, chunk_id: int) -> Chunk | None:
        return self.session.get(Chunk, chunk_id)

    def get_chunk_by_evidence_id(self, evidence_id: str) -> Chunk | None:
        """Resolve a C8 evidence id to its chunk row.

        Evidence ids are content-derived (contract C8), so resolution scans
        the corpus once computing the identity per row — bounded and fine at
        local scale. A persisted ``evidence_id`` column would need a schema
        migration (reported as a wave gap).
        """
        if not evidence_id.startswith("ev-"):
            return None
        for chunk in self.session.query(Chunk).all():
            candidate = evidence_id_for(
                chunk.document_id,
                chunk.strategy,
                chunk.start_offset or 0,
                chunk.end_offset or 0,
                chunk.text_hash or "",
            )
            if candidate == evidence_id:
                return chunk
        return None

    def windows_for_children(
        self, child_ids: list[int], *, strategy: str, strategy_version: str
    ) -> dict[int, Chunk]:
        """Map each section-strategy child to its bounded expansion window.

        Windows are rows with ``strategy_version = version + "-window"`` whose
        ``parent_id`` is the child id.
        """
        if not child_ids:
            return {}
        rows = (
            self.session.query(Chunk)
            .filter(
                Chunk.strategy == strategy,
                Chunk.strategy_version == f"{strategy_version}-window",
                Chunk.parent_id.in_(child_ids),
            )
            .all()
        )
        return {int(row.parent_id): row for row in rows}

    def evidence_rows(self, chunk_ids: list[int]) -> dict[int, EvidenceRow]:
        """Joined chunk + section metadata for building ``EvidenceItem``s."""
        if not chunk_ids:
            return {}
        rows = (
            self.session.query(Chunk, Section.section_type)
            .outerjoin(Section, Section.id == Chunk.section_id)
            .filter(Chunk.id.in_(chunk_ids))
            .all()
        )
        out: dict[int, EvidenceRow] = {}
        for chunk, section_type in rows:
            out[int(chunk.id)] = EvidenceRow(
                chunk_id=int(chunk.id),
                document_id=int(chunk.document_id),
                strategy=chunk.strategy,
                strategy_version=chunk.strategy_version,
                text=chunk.text,
                text_hash=chunk.text_hash,
                token_count=chunk.token_count,
                start_offset=chunk.start_offset,
                end_offset=chunk.end_offset,
                section_type=section_type,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
            )
        return out

    def retrieval_unit_ids(
        self, *, strategy: str, strategy_version: str, document_id: int | None = None
    ) -> list[int]:
        """Ids of embedded/indexable rows for one strategy build (base rows)."""
        query = self.session.query(Chunk.id).filter(
            Chunk.strategy == strategy, Chunk.strategy_version == strategy_version
        )
        if document_id is not None:
            query = query.filter(Chunk.document_id == document_id)
        return [int(row) for (row,) in query.all()]

    def strategy_chunk_count(self, *, strategy: str, strategy_version: str) -> int:
        return (
            self.session.query(Chunk)
            .filter(Chunk.strategy == strategy, Chunk.strategy_version == strategy_version)
            .count()
        )

    # -- embeddings cache (SPEC §15.2) ------------------------------------------

    def get_cached_vectors(
        self,
        text_hashes: list[str],
        *,
        provider: str,
        model: str,
        revision: str,
    ) -> dict[str, np.ndarray]:
        """Cached vectors keyed by text hash for one model identity."""
        if not text_hashes:
            return {}
        rows = (
            self.session.query(Embedding.text_hash, Embedding.vector)
            .filter(
                Embedding.text_hash.in_(text_hashes),
                Embedding.provider == provider,
                Embedding.model == model,
                Embedding.model_revision == revision,
            )
            .all()
        )
        out: dict[str, np.ndarray] = {}
        for text_hash, blob in rows:
            if text_hash is not None and blob is not None and text_hash not in out:
                out[text_hash] = decode_vector(blob)
        return out

    def get_vectors_for_chunks(
        self, chunk_ids: list[int], *, provider: str, model: str, revision: str
    ) -> dict[int, np.ndarray]:
        if not chunk_ids:
            return {}
        rows = (
            self.session.query(Embedding.chunk_id, Embedding.vector)
            .filter(
                Embedding.chunk_id.in_(chunk_ids),
                Embedding.provider == provider,
                Embedding.model == model,
                Embedding.model_revision == revision,
            )
            .all()
        )
        return {int(chunk_id): decode_vector(blob) for chunk_id, blob in rows if blob is not None}

    def store_embedding(
        self,
        *,
        chunk_id: int,
        text_hash: str,
        provider: str,
        model: str,
        revision: str,
        vector: np.ndarray,
        normalized: bool,
        dim: int,
    ) -> bool:
        """Persist one vector; returns False when the identity row exists."""
        existing = (
            self.session.query(Embedding.id)
            .filter(
                Embedding.chunk_id == chunk_id,
                Embedding.provider == provider,
                Embedding.model == model,
                Embedding.model_revision == revision,
            )
            .first()
        )
        if existing is not None:
            return False
        self.session.add(
            Embedding(
                chunk_id=chunk_id,
                text_hash=text_hash,
                provider=provider,
                model=model,
                model_revision=revision,
                dimension=dim,
                normalized=normalized,
                vector=encode_vector(vector),
            )
        )
        self.session.flush()
        return True

    # -- index manifest -----------------------------------------------------------

    def record_manifest(
        self,
        *,
        strategy: str,
        strategy_version: str,
        embedding_provider: str,
        embedding_model: str,
        embedding_model_revision: str,
        dim: int | None,
        corpus_hash: str,
        chunk_count: int,
        embedded_chunk_count: int,
    ) -> str:
        """Upsert the manifest row for one build; returns the index version."""
        index_version = compute_index_version(
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            embedding_model_revision=embedding_model_revision,
            strategy=strategy,
            strategy_version=strategy_version,
            corpus_hash=corpus_hash,
        )
        self.session.execute(
            text(
                """
                INSERT INTO index_manifest (
                    index_version, strategy, strategy_version, embedding_provider,
                    embedding_model, embedding_model_revision, dim, corpus_hash,
                    chunk_count, embedded_chunk_count, created_at
                ) VALUES (
                    :index_version, :strategy, :strategy_version, :embedding_provider,
                    :embedding_model, :embedding_model_revision, :dim, :corpus_hash,
                    :chunk_count, :embedded_chunk_count, :created_at
                )
                ON CONFLICT (strategy, strategy_version, embedding_provider,
                             embedding_model, embedding_model_revision)
                DO UPDATE SET index_version = excluded.index_version,
                              dim = excluded.dim,
                              corpus_hash = excluded.corpus_hash,
                              chunk_count = excluded.chunk_count,
                              embedded_chunk_count = excluded.embedded_chunk_count,
                              created_at = excluded.created_at
                """
            ),
            {
                "index_version": index_version,
                "strategy": strategy,
                "strategy_version": strategy_version,
                "embedding_provider": embedding_provider,
                "embedding_model": embedding_model,
                "embedding_model_revision": embedding_model_revision,
                "dim": dim,
                "corpus_hash": corpus_hash,
                "chunk_count": chunk_count,
                "embedded_chunk_count": embedded_chunk_count,
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        self.session.flush()
        return index_version

    def get_manifest(
        self, *, strategy: str, strategy_version: str, provider: str, model: str, revision: str
    ) -> dict | None:
        row = self.session.execute(
            text(
                """
                SELECT index_version, dim, corpus_hash, chunk_count, embedded_chunk_count
                FROM index_manifest
                WHERE strategy = :strategy AND strategy_version = :strategy_version
                  AND embedding_provider = :provider AND embedding_model = :model
                  AND embedding_model_revision = :revision
                """
            ),
            {
                "strategy": strategy,
                "strategy_version": strategy_version,
                "provider": provider,
                "model": model,
                "revision": revision,
            },
        ).first()
        if row is None:
            return None
        return {
            "index_version": row.index_version,
            "dim": row.dim,
            "corpus_hash": row.corpus_hash,
            "chunk_count": row.chunk_count,
            "embedded_chunk_count": row.embedded_chunk_count,
        }

    def get_any_manifest(self, *, strategy: str, strategy_version: str) -> dict | None:
        """Latest manifest row for a strategy build, regardless of model.

        Used by the search service's model-mismatch gate (SPEC §15.7): a row
        for a DIFFERENT embedding model proves the index exists but must not
        be queried with the requesting provider.
        """
        row = self.session.execute(
            text(
                """
                SELECT index_version, embedding_provider, embedding_model,
                       embedding_model_revision, dim, corpus_hash, created_at
                FROM index_manifest
                WHERE strategy = :strategy AND strategy_version = :strategy_version
                ORDER BY id DESC LIMIT 1
                """
            ),
            {"strategy": strategy, "strategy_version": strategy_version},
        ).first()
        if row is None:
            return None
        return {
            "index_version": row.index_version,
            "embedding_provider": row.embedding_provider,
            "embedding_model": row.embedding_model,
            "embedding_model_revision": row.embedding_model_revision,
            "dim": row.dim,
            "corpus_hash": row.corpus_hash,
            "created_at": row.created_at,
        }

    def get_manifest_by_version(self, index_version: str) -> dict | None:
        row = self.session.execute(
            text(
                """
                SELECT strategy, strategy_version, embedding_provider, embedding_model,
                       embedding_model_revision, dim, corpus_hash
                FROM index_manifest WHERE index_version = :index_version
                """
            ),
            {"index_version": index_version},
        ).first()
        if row is None:
            return None
        return {
            "strategy": row.strategy,
            "strategy_version": row.strategy_version,
            "embedding_provider": row.embedding_provider,
            "embedding_model": row.embedding_model,
            "embedding_model_revision": row.embedding_model_revision,
            "dim": row.dim,
            "corpus_hash": row.corpus_hash,
        }

    def compute_corpus_hash(self, *, strategy: str, strategy_version: str) -> str:
        """sha256 over the sorted text hashes of one strategy build's rows."""
        hashes = sorted(
            row.text_hash
            for row in self.session.execute(
                text(
                    "SELECT text_hash FROM chunks WHERE strategy = :strategy "
                    "AND strategy_version = :strategy_version AND text_hash IS NOT NULL"
                ),
                {"strategy": strategy, "strategy_version": strategy_version},
            )
            if row.text_hash
        )
        payload = "\n".join(hashes)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # -- stats ----------------------------------------------------------------

    def corpus_stats(self) -> dict:
        """Counts per strategy / embedding model for ``index`` reporting."""
        chunks: dict[str, int] = {}
        for row in self.session.execute(
            text(
                "SELECT strategy, strategy_version, COUNT(*) AS n FROM chunks "
                "GROUP BY strategy, strategy_version ORDER BY strategy"
            )
        ):
            chunks[f"{row.strategy}/{row.strategy_version}"] = int(row.n)
        embeddings: dict[str, int] = {}
        for row in self.session.execute(
            text(
                "SELECT provider, model, COUNT(*) AS n FROM embeddings "
                "GROUP BY provider, model ORDER BY provider"
            )
        ):
            embeddings[f"{row.provider}/{row.model}"] = int(row.n)
        documents = int(self.session.execute(text("SELECT COUNT(*) AS n FROM documents")).first().n)
        return {"documents": documents, "chunks": chunks, "embeddings": embeddings}


__all__ = [
    "ChunkWriteReport",
    "EvidenceRow",
    "SearchIndexRepo",
    "compute_index_version",
    "decode_vector",
    "encode_vector",
    "provider_revision",
]
