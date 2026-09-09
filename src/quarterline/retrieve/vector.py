"""Dense retrieval: metadata filter first, then NumPy cosine (SPEC §15/§16).

Candidates are filtered by company / strategy / section / form / period in
SQL *before* any scoring (the §26 correct-company requirement), then cosine
similarity runs over the BLOB-decoded float32 vectors. Vectors are selected
strictly by (provider, model, revision) so cross-model mixing is impossible
at this layer; the service-level model check lives in ``search.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
from sqlalchemy.orm import Session

from quarterline.store.models import Chunk, Company, Document, Embedding, Section

#: Candidate pool size for the hybrid flow (SPEC §16: dense top 20).
DENSE_TOP_K = 20


@dataclass(frozen=True)
class DenseHit:
    chunk_id: int
    score: float  # cosine similarity in [-1, 1]; NOT a calibrated probability
    rank: int  # 1-based, best first


@dataclass(frozen=True)
class DenseFilters:
    ticker: str
    strategies: list[str]
    strategy_version_by_strategy: dict[str, str]
    sections: list[str] | None = None
    forms: list[str] | None = None
    period_end: date | None = None
    filed_before: date | None = None


def _cosine_many(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    if matrix.size == 0:
        return np.empty(0, dtype=np.float32)
    q = query / (np.linalg.norm(query) or 1.0)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return (matrix / norms) @ q


def search_dense(
    session: Session,
    query_embedding: np.ndarray,
    filters: DenseFilters,
    top_k: int = DENSE_TOP_K,
    *,
    provider: str,
    model: str,
    revision: str,
) -> list[DenseHit]:
    """Metadata-first candidate selection, then NumPy cosine, best-first."""
    version_by_strategy = filters.strategy_version_by_strategy
    query = (
        session.query(Chunk.id, Embedding.vector)
        .join(Document, Document.id == Chunk.document_id)
        .join(Company, Company.id == Document.company_id)
        .join(Embedding, Embedding.chunk_id == Chunk.id)
        .outerjoin(Section, Section.id == Chunk.section_id)
        .filter(
            Company.ticker == filters.ticker,
            Embedding.provider == provider,
            Embedding.model == model,
            Embedding.model_revision == revision,
        )
    )
    version_clauses = []
    for strategy in filters.strategies:
        version = version_by_strategy.get(strategy)
        if version is None:
            continue
        version_clauses.append((Chunk.strategy == strategy) & (Chunk.strategy_version == version))
    if version_clauses:
        clause = version_clauses[0]
        for extra in version_clauses[1:]:
            clause = clause | extra
        query = query.filter(clause)
    else:
        return []
    if filters.sections:
        query = query.filter(Section.section_type.in_([s.lower() for s in filters.sections]))
    if filters.forms:
        query = query.filter(Document.form.in_([f.upper() for f in filters.forms]))
    if filters.period_end is not None:
        query = query.filter(Document.period_end == filters.period_end)
    if filters.filed_before is not None:
        # Point-in-time safety: nothing filed after the timestamp is used.
        query = query.filter(Document.filed_at <= filters.filed_before)

    rows = query.all()
    if not rows:
        return []

    ids = [int(chunk_id) for chunk_id, _ in rows]
    matrix = np.stack(
        [np.frombuffer(blob, dtype="<f4").astype(np.float32) for _, blob in rows if blob]
    )
    # Dimension guard: a vector whose dim differs from the query cannot be
    # scored (SPEC §15.7 defense in depth; the service-level check raises).
    if matrix.shape[1] != query_embedding.shape[0]:
        raise ValueError(
            f"embedding dimension mismatch: stored dim={matrix.shape[1]} "
            f"query dim={query_embedding.shape[0]}"
        )
    scores = _cosine_many(query_embedding.astype(np.float32), matrix)
    order = sorted(range(len(ids)), key=lambda i: (-float(scores[i]), ids[i]))
    return [
        DenseHit(chunk_id=ids[i], score=float(scores[i]), rank=rank + 1)
        for rank, i in enumerate(order[:top_k])
    ]


__all__ = [
    "DENSE_TOP_K",
    "DenseFilters",
    "DenseHit",
    "search_dense",
]
