"""Retrieval-layer DTOs (SPEC §14–§16, PLAN contracts C7/C8).

- :class:`SearchQuery` is contract C7 (query, ticker, strategy, retrieval mode,
  optional filters, top_k).
- :class:`EvidenceItem` extends the ``core.models.EvidenceItem`` skeleton with
  retrieval metadata while keeping every base field; ``evidence_id`` follows
  contract C8 exactly (see :func:`evidence_id_for`).
- :class:`SearchResult` carries the result items plus index/degradation
  metadata required by SPEC §15/§16/§25.
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from quarterline.core.models import EvidenceItem as CoreEvidenceItem

#: Version marker of the deterministic insufficient-evidence policy (SPEC §16).
EVIDENCE_POLICY_VERSION = "evidence-policy-v1"

StrategyName = Literal["fixed", "section", "any"]
RetrievalName = Literal["lexical", "dense", "hybrid", "hybrid-rerank"]
ModeName = Literal["general", "brief", "risk"]
ChunkRole = Literal["chunk", "parent", "window"]


class SearchQuery(BaseModel):
    """One retrieval request (contract C7)."""

    query: str
    ticker: str
    strategy: StrategyName = "section"
    retrieval: RetrievalName = "hybrid"
    index_version: str | None = None
    period_end: date | None = None
    forms: list[str] | None = None
    sections: list[str] | None = None
    filed_before: date | None = None
    top_k: int = 10
    mode: ModeName = "general"


class EvidenceItem(CoreEvidenceItem):
    """Retrieved, resolvable evidence window (contracts C8; SPEC §16).

    ``evidence_id`` resolves to an actual ``chunks`` row via
    ``SearchIndexRepo.get_chunk_by_evidence_id``; ``text`` is the exact slice
    ``document_text[start_offset:end_offset]`` of the cleaned document, so the
    supplied window's exact text is always resolvable.
    """

    chunk_id: int
    strategy: str
    page: int | None = None
    section: str | None = None
    token_count: int | None = None
    embedding_model: str | None = None
    # scores (base class): {"lexical": bm25, "dense": cosine, "rrf": ..., "rerank": ...}


class SearchResult(BaseModel):
    """Response of :meth:`quarterline.retrieve.search.SearchService.search`."""

    items: list[EvidenceItem] = Field(default_factory=list)
    index_version: str | None = None
    embedding_model: str | None = None
    #: Why a requested capability ran in degraded mode (e.g. reranker absent,
    #: dense without embeddings). Empty dict = fully healthy request.
    degraded: dict[str, str] = Field(default_factory=dict)
    evidence_policy_version: str = EVIDENCE_POLICY_VERSION
    insufficient_evidence: bool = False
    insufficient_evidence_reason: str | None = None


class DocumentMeta(BaseModel):
    """Document-level metadata handed to chunk strategies and the indexer.

    ``content_hash`` is the ingested artifact's sha256 and participates in the
    deterministic chunk identity (SPEC §14 stable IDs).
    """

    document_id: int
    content_hash: str
    extraction_version: str | None = None
    form: str | None = None
    document_kind: str | None = None


class ChunkDraft(BaseModel):
    """One proposed chunk produced by a chunk strategy (SPEC §14).

    ``start_offset``/``end_offset`` index into the ``document_text`` passed to
    the strategy; the invariant ``document_text[start:end] == text`` always
    holds. ``key``/``parent_key`` are draft-local identifiers the indexer
    resolves to real ``chunks.id`` values when writing rows.

    ``role``:
    - ``chunk`` — a retrieval unit (embedded + FTS-indexed);
    - ``parent`` — the full parent section (stored, never embedded/indexed);
    - ``window`` — the bounded expansion of one child inside its parent
      (stored, never embedded/indexed; supplied as evidence for that child).
    """

    key: str
    role: ChunkRole = "chunk"
    text: str
    start_offset: int
    end_offset: int
    token_count: int
    text_hash: str
    section_id: int | None = None
    section_type: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    parent_key: str | None = None


class IndexStats(BaseModel):
    """Result summary of one ``index build`` run."""

    strategy: str
    strategy_version: str
    documents_considered: int = 0
    documents_indexed: int = 0
    documents_skipped: int = 0
    chunks_written: int = 0
    chunks_deduplicated: int = 0
    embeddings_written: int = 0
    embeddings_cache_hits: int = 0
    fts_rows: int = 0
    embedding_model: str | None = None
    dim: int | None = None
    corpus_hash: str | None = None
    index_version: str | None = None

    def summary(self) -> str:
        lines = [
            "Index build report (research only, not investment advice)",
            f"  strategy: {self.strategy} v{self.strategy_version}",
            (
                f"  documents: {self.documents_indexed} indexed, "
                f"{self.documents_skipped} skipped (no sections / filtered)"
            ),
            (
                f"  chunks: {self.chunks_written} written, "
                f"{self.chunks_deduplicated} deduplicated (identical text)"
            ),
            (
                f"  embeddings: {self.embeddings_written} written, "
                f"{self.embeddings_cache_hits} served from cache"
            ),
            f"  fts rows: {self.fts_rows}",
            f"  embedding model: {self.embedding_model} (dim={self.dim})",
            f"  corpus hash: {self.corpus_hash}",
            f"  index version: {self.index_version}",
        ]
        return "\n".join(lines)


def text_hash(text: str) -> str:
    """sha256 hex of chunk text (SPEC §14 stable-ID component)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evidence_id_for(
    document_id: int, strategy: str, start: int, end: int, chunk_text_hash: str
) -> str:
    """Contract C8: ``ev-`` + first 12 hex chars of the sha1 identity tuple.

    Resolvable from the ``chunks`` table (see
    ``SearchIndexRepo.get_chunk_by_evidence_id``) because every component is
    persisted on the row.
    """
    payload = f"{document_id}|{strategy}|{start}|{end}|{chunk_text_hash}"
    return "ev-" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def chunk_identity(
    *,
    content_hash: str,
    extraction_version: str | None,
    strategy: str,
    strategy_version: str,
    start: int,
    end: int,
    chunk_text_hash: str,
) -> str:
    """Deterministic chunk identity (SPEC §14 stable IDs).

    Building the same index twice yields the same identity; the DB write path
    makes it idempotent through the ``chunks`` unique key
    ``(document_id, strategy, strategy_version, text_hash)``. The extraction
    version is included so re-extraction changes identity.
    """
    payload = "|".join(
        [
            content_hash,
            extraction_version or "",
            strategy,
            strategy_version,
            str(start),
            str(end),
            chunk_text_hash,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "EVIDENCE_POLICY_VERSION",
    "ChunkDraft",
    "DocumentMeta",
    "EvidenceItem",
    "IndexStats",
    "ModeName",
    "RetrievalName",
    "SearchQuery",
    "SearchResult",
    "StrategyName",
    "chunk_identity",
    "evidence_id_for",
    "text_hash",
]
