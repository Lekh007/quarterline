"""PostgreSQL 16 + pgvector retrieval-index repository (SPEC §15 PostgreSQL profile).

Mirrors the :class:`~quarterline.store.repositories.search_sqlite.SearchIndexRepo`
contract over PostgreSQL so the same index-building / lookup semantics run on
either backend. Duck-typed sibling of ``SearchIndexRepo`` — same method names,
same chunk/evidence-id/index-version semantics — exposed through
:func:`get_search_repo`, which selects the backend from the ``DATABASE_URL``
scheme.

Backend-specific storage owned by this module (raw DDL at ``ensure_schema``;
same pattern as the SQLite module owning ``chunks_fts``/``index_manifest``
outside the wave-0 ORM):

- ``chunk_fts`` — PostgreSQL full-text-search analog of the FTS5 table:
  ``tsvector`` + GIN index keyed by chunk id (ON DELETE CASCADE);
- ``chunk_vectors`` — pgvector ``vector`` storage mirroring the ``embeddings``
  columns (provider/model/model_revision identity, text_hash, dim, normalized)
  so dense search uses native ``<=>`` cosine distance;
- ``index_manifest`` — identical columns/upsert semantics to the SQLite table.

HONESTY LABEL (SPEC §4 / §15): PostgreSQL full-text ranking with
``ts_rank_cd`` is **not BM25**. The lexical path exposed by this backend is
labeled ``postgres-tsrank`` everywhere user-visible (see
:attr:`PostgresSearchIndexRepo.lexical_method`). Scores are higher-is-better,
unlike SQLite FTS5's more-negative-is-better ``bm25()``.

SQL hygiene (SPEC §2.3.5): fixed statement fragments + bound parameters only;
list filters use per-item bound params — user input never interpolates into
SQL. User query text reaches ``to_tsquery`` only after tokenizing to
``[A-Za-z0-9_]{2,}`` words (operators are dropped), mirroring
``retrieve.lexical.sanitize_fts_query``.

Wiring note: :func:`get_search_repo` is the intended single selection point.
Current call sites construct ``SearchIndexRepo`` directly
(``retrieve/search.py``); switching them is the orchestrator's integration
step (see the wave-F8 implementation-log row).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar

import numpy as np
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from quarterline.retrieve.embeddings import EmbeddingModelMismatchError
from quarterline.retrieve.lexical import LEXICAL_TOP_K, LexicalFilters, LexicalHit
from quarterline.retrieve.vector import DENSE_TOP_K, DenseFilters, DenseHit
from quarterline.store.models import Chunk
from quarterline.store.repositories.search_sqlite import (
    ChunkWriteReport,
    EvidenceRow,
    SearchIndexRepo,
    compute_index_version,
    provider_revision,
)

if TYPE_CHECKING:  # pragma: no cover - import used only for typing
    from quarterline.config import Settings

#: User-visible label of this backend's lexical ranking method. NEVER "bm25":
#: PostgreSQL ts_rank_cd is a plain saturation ranking, not BM25 (SPEC §4).
LEXICAL_METHOD = "postgres-tsrank"

_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}", re.UNICODE)

_PG_EXTENSIONS_DDL = ("CREATE EXTENSION IF NOT EXISTS vector",)

_CHUNK_FTS_DDL = (
    """
    CREATE TABLE IF NOT EXISTS chunk_fts (
        chunk_id BIGINT PRIMARY KEY REFERENCES chunks (id) ON DELETE CASCADE,
        tsv tsvector NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_chunk_fts_tsv ON chunk_fts USING GIN (tsv)",
)

_CHUNK_VECTORS_DDL = (
    """
    CREATE TABLE IF NOT EXISTS chunk_vectors (
        chunk_id BIGINT NOT NULL REFERENCES chunks (id) ON DELETE CASCADE,
        text_hash VARCHAR(64),
        provider VARCHAR(32) NOT NULL,
        model VARCHAR(128) NOT NULL,
        model_revision VARCHAR(128) NOT NULL DEFAULT '',
        dimension INTEGER,
        normalized BOOLEAN,
        vec vector,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (chunk_id, provider, model, model_revision)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_chunk_vectors_text_identity
        ON chunk_vectors (text_hash, provider, model, model_revision)
    """,
)

# Same columns/upsert semantics as the SQLite table; BIGSERIAL/TIMESTAMPTZ types.
_MANIFEST_DDL = (
    """
    CREATE TABLE IF NOT EXISTS index_manifest (
        id BIGSERIAL PRIMARY KEY,
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
        created_at TIMESTAMPTZ NOT NULL,
        UNIQUE (strategy, strategy_version, embedding_provider, embedding_model,
                embedding_model_revision)
    )
    """,
)


def format_pg_vector(vector: list[float] | np.ndarray) -> str:
    """Render a vector as pgvector's text literal ``'[1,2,3]'`` (bound param)."""
    array = np.asarray(vector, dtype=np.float64).ravel()
    return "[" + ",".join(f"{x:.9g}" for x in array) + "]"


def parse_pg_vector(value: object) -> np.ndarray:
    """Decode a pgvector value (text literal, list, or ndarray) to float32."""
    if isinstance(value, np.ndarray):
        return value.astype(np.float32)
    if isinstance(value, (list, tuple)):
        return np.asarray(value, dtype=np.float32)
    body = str(value).strip().strip("[]")
    if not body:
        return np.empty(0, dtype=np.float32)
    return np.asarray([float(part) for part in body.split(",")], dtype=np.float32)


def sanitize_tsquery_query(query: str) -> str:
    """Build a safe ``to_tsquery`` expression from untrusted user input.

    Mirrors ``retrieve.lexical.sanitize_fts_query``: tokens are limited to
    ``[A-Za-z0-9_]{2,}`` (so no tsquery operators survive) and joined with
    ``|`` (OR) for recall parity with the SQLite lexical arm. Empty output
    means nothing searchable — callers skip lexical search.
    """
    tokens = _WORD_RE.findall(query or "")
    return " | ".join(tokens)


def ensure_embedding_model_match(
    manifest: dict | None,
    *,
    provider: str,
    model: str,
    revision: str,
    strategy: str,
    strategy_version: str,
) -> dict:
    """Raise :class:`EmbeddingModelMismatchError` when the index belongs to a
    different embedding model (SPEC §15.7). Returns the manifest otherwise."""
    if manifest is None:
        return {}
    built_with = (
        f"{manifest.get('embedding_provider')}/{manifest.get('embedding_model')}"
        f":{manifest.get('embedding_model_revision', '')}"
    )
    asking = f"{provider}/{model}:{revision}"
    if manifest.get("embedding_provider") != provider or manifest.get("embedding_model") != model:
        raise EmbeddingModelMismatchError(
            f"index for [{strategy}] was built with embedding model {built_with}, "
            f"not {asking} (SPEC §15.7)"
        )
    return manifest


class PostgresSearchIndexRepo(SearchIndexRepo):
    """Indexer + lookup over ``chunks`` with pgvector vectors and tsvector FTS.

    Subclasses the SQLite repo and overrides only the backend-specific
    pieces (schema DDL, FTS sync, vector storage); chunk writes, evidence-id
    resolution, window expansion, manifest reads, corpus hashing, and stats
    are inherited unchanged because they are database-portable.
    """

    #: Honesty label surfaced to callers/reports (never "bm25").
    lexical_method: ClassVar[str] = LEXICAL_METHOD
    storage_backend: ClassVar[str] = "postgres-pgvector"

    # -- schema ---------------------------------------------------------------

    def ensure_schema(self) -> None:
        for statement in (
            *_PG_EXTENSIONS_DDL,
            *_CHUNK_FTS_DDL,
            *_CHUNK_VECTORS_DDL,
            *_MANIFEST_DDL,
        ):
            self.session.execute(text(statement))
        self.session.flush()

    def create_vector_index(self, dim: int, *, lists: int = 100) -> None:
        """Opt-in ANN index (SPEC §15: "add appropriate indexes AFTER
        correctness tests"). Pins the column dimension, then builds an
        IVFFlat cosine index. Not called by ``ensure_schema``."""
        self.session.execute(
            text("ALTER TABLE chunk_vectors ALTER COLUMN vec TYPE vector(:dim)"),
            {"dim": dim},
        )
        self.session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_chunk_vectors_vec "
                "ON chunk_vectors USING ivfflat (vec vector_cosine_ops) "
                f"WITH (lists = {int(lists)})"
            )
        )
        self.session.flush()

    # -- FTS sync (tsvector) -----------------------------------------------------

    def sync_fts(self, chunk_ids: list[int]) -> int:
        """Rebuild tsvector rows for the given retrieval units (delete + insert)."""
        if not chunk_ids:
            return 0
        rows = (
            self.session.query(Chunk.id, Chunk.text)  # type: ignore[misc]
            .filter(Chunk.id.in_(list(chunk_ids)))
            .all()
        )
        for chunk_id in chunk_ids:
            self.session.execute(
                text("DELETE FROM chunk_fts WHERE chunk_id = :chunk_id"),
                {"chunk_id": int(chunk_id)},
            )
        count = 0
        for chunk_id, chunk_text in rows:
            if chunk_text:
                self.session.execute(
                    text(
                        "INSERT INTO chunk_fts (chunk_id, tsv) VALUES "
                        "(:chunk_id, to_tsvector('english', :chunk_text))"
                    ),
                    {"chunk_id": int(chunk_id), "chunk_text": chunk_text},
                )
                count += 1
        self.session.flush()
        return count

    # -- vectors in pgvector (SPEC §15.2 cache contract) --------------------------

    def get_cached_vectors(
        self,
        text_hashes: list[str],
        *,
        provider: str,
        model: str,
        revision: str,
    ) -> dict[str, np.ndarray]:
        if not text_hashes:
            return {}
        stmt = text(
            """
            SELECT text_hash, vec FROM chunk_vectors
            WHERE text_hash IN :hashes
              AND provider = :provider AND model = :model
              AND model_revision = :revision
            """
        ).bindparams(bindparam("hashes", expanding=True))
        rows = self.session.execute(
            stmt,
            {
                "hashes": list(text_hashes),
                "provider": provider,
                "model": model,
                "revision": revision,
            },
        ).all()
        out: dict[str, np.ndarray] = {}
        for text_hash, vec in rows:
            if text_hash is not None and vec is not None and text_hash not in out:
                out[str(text_hash)] = parse_pg_vector(vec)
        return out

    def get_vectors_for_chunks(
        self, chunk_ids: list[int], *, provider: str, model: str, revision: str
    ) -> dict[int, np.ndarray]:
        if not chunk_ids:
            return {}
        stmt = text(
            """
            SELECT chunk_id, vec FROM chunk_vectors
            WHERE chunk_id IN :ids
              AND provider = :provider AND model = :model
              AND model_revision = :revision
            """
        ).bindparams(bindparam("ids", expanding=True))
        rows = self.session.execute(
            stmt,
            {
                "ids": [int(i) for i in chunk_ids],
                "provider": provider,
                "model": model,
                "revision": revision,
            },
        ).all()
        return {int(chunk_id): parse_pg_vector(vec) for chunk_id, vec in rows if vec is not None}

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
        """Persist one vector into ``chunk_vectors``; False when already present."""
        existing = self.session.execute(
            text(
                """
                SELECT chunk_id FROM chunk_vectors
                WHERE chunk_id = :chunk_id AND provider = :provider
                  AND model = :model AND model_revision = :revision
                """
            ),
            {
                "chunk_id": int(chunk_id),
                "provider": provider,
                "model": model,
                "revision": revision,
            },
        ).first()
        if existing is not None:
            return False
        self.session.execute(
            text(
                """
                INSERT INTO chunk_vectors (
                    chunk_id, text_hash, provider, model, model_revision,
                    dimension, normalized, vec
                ) VALUES (
                    :chunk_id, :text_hash, :provider, :model, :revision,
                    :dim, :normalized, CAST(:vec AS vector)
                )
                """
            ),
            {
                "chunk_id": int(chunk_id),
                "text_hash": text_hash,
                "provider": provider,
                "model": model,
                "revision": revision,
                "dim": dim,
                "normalized": normalized,
                "vec": format_pg_vector(vector),
            },
        )
        self.session.flush()
        return True

    # -- manifest write (inherited reads are portable; DDL differs) ---------------

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
        """Upsert the manifest row for one build; returns the index version.

        The version formula is identical to the SQLite backend
        (``compute_index_version``), so the same corpus + embedding identity
        yields the same index version on either backend.
        """
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
                    :chunk_count, :embedded_chunk_count, now()
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
            },
        )
        self.session.flush()
        return index_version


# ---------------------------------------------------------------------------
# Retrieval functions (PostgreSQL analogs of retrieve/lexical.py + vector.py)
# ---------------------------------------------------------------------------


def search_lexical_postgres(
    session: Session, query: str, filters: LexicalFilters, top_k: int = LEXICAL_TOP_K
) -> list[LexicalHit]:
    """Full-text search via ``to_tsquery`` + ``ts_rank_cd`` (NOT BM25).

    ``LexicalHit.score`` is the raw ``ts_rank_cd`` value — HIGHER is better
    (the SQLite backend's bm25 sign convention is the opposite; RRF fusion
    only uses ranks, so this never leaks into hybrid ordering).
    """
    match_expr = sanitize_tsquery_query(query)
    if not match_expr:
        return []

    params: dict[str, object] = {"match": match_expr, "k": top_k}
    bindparams_spec = [bindparam("match"), bindparam("k")]
    arms: list[str] = []
    for index, strategy in enumerate(filters.strategies):
        version = filters.strategy_version_by_strategy.get(strategy)
        if version is None:
            continue
        suffixed = {name: f"{name}_{index}" for name in ("strategy", "strategy_version")}
        arm_params: dict[str, object] = {
            suffixed["strategy"]: strategy,
            suffixed["strategy_version"]: version,
        }
        bindparams_spec.extend(
            [bindparam(suffixed["strategy"]), bindparam(suffixed["strategy_version"])]
        )
        extra_clauses: list[str] = []
        for column, values, transform in (
            ("secs.section_type", filters.sections, str.lower),
            ("documents.form", filters.forms, str.upper),
        ):
            if not values:
                continue
            placeholders = []
            for pos, value in enumerate(transform(v) for v in values):
                name = f"{column.split('.')[-1]}_{index}_{pos}"
                arm_params[name] = value
                bindparams_spec.append(bindparam(name))
                placeholders.append(f":{name}")
            extra_clauses.append(f"{column} IN ({', '.join(placeholders)})")
        if filters.period_end is not None:
            arm_params[f"period_end_{index}"] = filters.period_end.isoformat()
            bindparams_spec.append(bindparam(f"period_end_{index}"))
            extra_clauses.append(f"documents.period_end = CAST(:period_end_{index} AS DATE)")
        if filters.filed_before is not None:
            arm_params[f"filed_before_{index}"] = filters.filed_before.isoformat()
            bindparams_spec.append(bindparam(f"filed_before_{index}"))
            extra_clauses.append(f"documents.filed_at <= CAST(:filed_before_{index} AS DATE)")
        extra = (" AND " + " AND ".join(extra_clauses)) if extra_clauses else ""
        ticker_param = f"ticker_{index}"
        arm_params[ticker_param] = filters.ticker
        bindparams_spec.append(bindparam(ticker_param))
        arm_sql = f"""
        SELECT chunk_fts.chunk_id AS chunk_id,
               ts_rank_cd(chunk_fts.tsv, to_tsquery('english', :match)) AS score
        FROM chunk_fts
        JOIN chunks ON chunks.id = chunk_fts.chunk_id
        JOIN documents ON documents.id = chunks.document_id
        JOIN companies ON companies.id = documents.company_id
        LEFT JOIN sections secs ON secs.id = chunks.section_id
        WHERE chunk_fts.tsv @@ to_tsquery('english', :match)
          AND companies.ticker = :{ticker_param}
          AND chunks.strategy = :{suffixed["strategy"]}
          AND chunks.strategy_version = :{suffixed["strategy_version"]}{extra}
        """
        params.update(arm_params)
        arms.append(arm_sql)

    if not arms:
        return []

    union_sql = "\nUNION ALL\n".join(arms)
    stmt = text(
        f"""
        SELECT chunk_id, score
        FROM ( {union_sql} ) AS arms
        ORDER BY score DESC, chunk_id ASC
        LIMIT :k
        """
    ).bindparams(*bindparams_spec)
    rows = session.execute(stmt, params).all()
    return [
        LexicalHit(chunk_id=int(row.chunk_id), score=float(row.score), rank=i + 1)
        for i, row in enumerate(rows)
    ]


def search_dense_postgres(
    session: Session,
    query_embedding: np.ndarray,
    filters: DenseFilters,
    top_k: int = DENSE_TOP_K,
    *,
    provider: str,
    model: str,
    revision: str,
) -> list[DenseHit]:
    """pgvector cosine search over ``chunk_vectors`` joined to metadata filters.

    Candidates are filtered by model identity + metadata IN SQL, scored with
    the native ``<=>`` cosine-distance operator, and returned as
    ``retrieve.vector.DenseHit`` rows (score = 1 - distance, best first).
    A stored vector whose dimension differs from the query raises
    ``ValueError`` (SPEC §15.7 defense in depth, mirroring ``vector.py``).
    """
    version_by_strategy = filters.strategy_version_by_strategy
    armed = [s for s in filters.strategies if s in version_by_strategy]
    if not armed:
        return []

    params: dict[str, object] = {
        "qvec": format_pg_vector(query_embedding),
        "dim": int(np.asarray(query_embedding).ravel().shape[0]),
        "provider": provider,
        "model": model,
        "revision": revision,
        "k": top_k,
        "ticker": filters.ticker,
    }
    version_clauses = []
    for strategy in armed:
        params[f"strategy_{strategy}"] = strategy
        params[f"version_{strategy}"] = version_by_strategy[strategy]
        version_clauses.append(
            f"(chunks.strategy = :strategy_{strategy} "
            f"AND chunks.strategy_version = :version_{strategy})"
        )

    base_from = """
        FROM chunk_vectors
        JOIN chunks ON chunks.id = chunk_vectors.chunk_id
        JOIN documents ON documents.id = chunks.document_id
        JOIN companies ON companies.id = documents.company_id
        LEFT JOIN sections secs ON secs.id = chunks.section_id
    """
    where = [
        "chunk_vectors.provider = :provider",
        "chunk_vectors.model = :model",
        "chunk_vectors.model_revision = :revision",
        "chunk_vectors.dimension = :dim",
        "(" + " OR ".join(version_clauses) + ")",
        "companies.ticker = :ticker",
    ]
    if filters.sections:
        placeholders = []
        for pos, section in enumerate(s.lower() for s in filters.sections):
            params[f"section_{pos}"] = section
            placeholders.append(f":section_{pos}")
        where.append(f"secs.section_type IN ({', '.join(placeholders)})")
    if filters.forms:
        placeholders = []
        for pos, form in enumerate(f.upper() for f in filters.forms):
            params[f"form_{pos}"] = form
            placeholders.append(f":form_{pos}")
        where.append(f"documents.form IN ({', '.join(placeholders)})")
    if filters.period_end is not None:
        params["period_end"] = filters.period_end.isoformat()
        where.append("documents.period_end = CAST(:period_end AS DATE)")
    if filters.filed_before is not None:
        params["filed_before"] = filters.filed_before.isoformat()
        where.append("documents.filed_at <= CAST(:filed_before AS DATE)")

    # Dimension guard (§15.7): rows for the same model identity with a
    # different dim must not be silently skipped — raise like vector.py does.
    mismatch_filters = (
        "chunk_vectors.provider = :provider AND chunk_vectors.model = :model "
        "AND chunk_vectors.model_revision = :revision AND chunk_vectors.dimension <> :dim"
    )
    mismatch = session.execute(
        text(f"SELECT COUNT(*) AS n {base_from} WHERE {mismatch_filters}"),
        {
            "provider": provider,
            "model": model,
            "revision": revision,
            "dim": int(np.asarray(query_embedding).ravel().shape[0]),
        },
    ).first()
    if mismatch is not None and int(mismatch.n) > 0:
        raise ValueError(
            f"embedding dimension mismatch: stored vectors exist with dim <> {params['dim']} "
            f"for {provider}/{model}:{revision}"
        )

    stmt = text(
        f"""
        SELECT chunk_vectors.chunk_id AS chunk_id,
               1 - (chunk_vectors.vec <=> CAST(:qvec AS vector)) AS score
        {base_from}
        WHERE {" AND ".join(where)}
        ORDER BY chunk_vectors.vec <=> CAST(:qvec AS vector) ASC,
                 chunk_vectors.chunk_id ASC
        LIMIT :k
        """
    )
    rows = session.execute(stmt, params).all()
    return [
        DenseHit(chunk_id=int(row.chunk_id), score=float(row.score), rank=i + 1)
        for i, row in enumerate(rows)
    ]


def get_search_repo(session: Session, settings: Settings | None = None) -> SearchIndexRepo:
    """Select the search-index backend from ``DATABASE_URL`` (contract C5).

    Returns :class:`PostgresSearchIndexRepo` for ``postgres(ql)://`` /
    ``postgres(ql)+driver://`` URLs and the SQLite :class:`SearchIndexRepo`
    otherwise. Callers currently construct ``SearchIndexRepo`` directly
    (``retrieve/search.py``); routing them through this factory is the
    remaining integration step for the PostgreSQL profile.
    """
    if settings is None:
        from quarterline.config import get_settings

        settings = get_settings()
    url = (settings.database_url or "").strip().lower()
    if url.startswith(("postgres://", "postgresql://", "postgres+", "postgresql+")):
        return PostgresSearchIndexRepo(session)
    return SearchIndexRepo(session)


__all__ = [
    "LEXICAL_METHOD",
    "ChunkWriteReport",
    "EvidenceRow",
    "PostgresSearchIndexRepo",
    "compute_index_version",
    "ensure_embedding_model_match",
    "format_pg_vector",
    "get_search_repo",
    "parse_pg_vector",
    "provider_revision",
    "sanitize_tsquery_query",
    "search_dense_postgres",
    "search_lexical_postgres",
]
