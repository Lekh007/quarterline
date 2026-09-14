"""Retrieval orchestration: SearchService (SPEC §16 flow), index building,
the versioned insufficient-evidence policy, and the CLI handlers.

Flow (SPEC §16)::

    query -> metadata filter -> lexical top 20 -> dense top 20 -> RRF fusion
          -> top 15 candidates -> optional cross-encoder reranker
          -> select evidence (top_k) -> bounded context assembly (context.py)

Insufficient evidence (SPEC §16): a VERSIONED deterministic policy
(:data:`quarterline.retrieve.models.EVIDENCE_POLICY_VERSION`) decides
abstention. Its thresholds are engineering anchors, not learned constants,
and similarity scores are NOT calibrated probabilities — the policy never
interprets them as such.

Index versioning (SPEC §15.6): ``index_version`` is a deterministic function
of (embedding provider, model, revision, chunking strategy + version, corpus
content-hash set); changing the embedding model creates a new index version.
A dense/hybrid search whose provider does not match the recorded index model
raises :class:`EmbeddingModelMismatchError` (SPEC §15.7) instead of silently
querying a foreign index; lexical-only searches keep working (SPEC §25).

CLI: ``index build`` / ``search`` handlers are registered into the W0
``SUBCOMMAND_REGISTRY`` at import of ``quarterline.retrieve`` (cli.py is not
edited). The W0 parser surface for these subcommands is
``--strategy/--retrieval/--ticker/--query`` only; optional knobs (``--mode``,
``--tickers``) ride on ``getattr(args, ..., default)`` / the
``QUARTERLINE_SEARCH_MODE`` environment variable until the parser gains them.
"""

from __future__ import annotations

import argparse
import os
import re
import time

import numpy as np
from sqlalchemy.orm import Session

from quarterline.config import Settings
from quarterline.observability.events import emit_run_event, new_run_id
from quarterline.retrieve.chunk_fixed import FixedWindowChunker, SectionRef
from quarterline.retrieve.chunk_section import SectionParentChildChunker
from quarterline.retrieve.context import (
    ContextBundle,
    assemble_context,
    context_budget_from_settings,
)
from quarterline.retrieve.embeddings import (
    EmbeddingModelMismatchError,
    EmbeddingProviderUnavailable,
    FakeEmbeddingProvider,
    OllamaEmbeddingProvider,
)
from quarterline.retrieve.fusion import FusedHit, rrf_fuse
from quarterline.retrieve.lexical import LexicalFilters, LexicalHit, search_lexical
from quarterline.retrieve.models import (
    DocumentMeta,
    EvidenceItem,
    IndexStats,
    SearchQuery,
    SearchResult,
    evidence_id_for,
)
from quarterline.retrieve.models import text_hash as sha256_text
from quarterline.retrieve.vector import DenseFilters, DenseHit, search_dense
from quarterline.store.models import Chunk, Company, Document
from quarterline.store.repositories.documents import DocumentsRepo
from quarterline.store.repositories.search_postgres import (
    get_search_repo,
    search_dense_postgres,
    search_lexical_postgres,
)
from quarterline.store.repositories.search_sqlite import (
    SearchIndexRepo,
    provider_revision,
)

#: Section presets per question mode (SPEC §16 — an MD&A-only filter must NOT
#: be applied to every question).
MODE_SECTION_PRESETS: dict[str, list[str] | None] = {
    "brief": ["mda", "earnings_release"],
    "risk": ["risk_factors", "mda"],
    "general": None,
}

STRATEGY_REGISTRY: dict[str, type] = {
    FixedWindowChunker.STRATEGY_ID: FixedWindowChunker,
    SectionParentChildChunker.STRATEGY_ID: SectionParentChildChunker,
}

# Candidate pool sizes (SPEC §16).
_LEXICAL_TOP_K = 20
_DENSE_TOP_K = 20
_FUSE_TOP_K = 15


# ---------------------------------------------------------------------------
# Evidence policy v1 (deterministic, versioned; SPEC §16)
# ---------------------------------------------------------------------------

#: Top evidence must match at least one query term (case-insensitive words).
POLICY_MIN_QUERY_TERM_OVERLAP = 1
#: Hybrid retrieval needs at least two passages before answering.
POLICY_MIN_HYBRID_ITEMS = 2
#: Top RRF score must be >= 1/(60+5): rank<=5 in at least one source list.
POLICY_MIN_TOP_RRF = 1.0 / 65.0
#: Lexical-only: the top hit must sit within the top 5 lexical ranks.
POLICY_LEXICAL_MAX_RANK = 5
#: Dense-only: top cosine must clear this anchor. NOT a calibrated
#: probability — a conservative engineering threshold (SPEC §16).
POLICY_MIN_TOP_COSINE = 0.20

_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}", re.UNICODE)


def _query_terms(text: str) -> set[str]:
    return {token.lower() for token in _WORD_RE.findall(text or "")}


def evaluate_evidence_policy(
    items: list[EvidenceItem], query_text: str, retrieval: str
) -> tuple[bool, str | None]:
    """Return (sufficient, reason). Deterministic and versioned by
    ``EVIDENCE_POLICY_VERSION``. Similarity scores are not probabilities."""
    if not items:
        return False, "no evidence retrieved"
    top = items[0]
    terms = _query_terms(query_text)
    if terms:
        overlap = len(terms & _query_terms(top.text))
        if overlap < POLICY_MIN_QUERY_TERM_OVERLAP:
            return False, "top evidence shares no query term (evidence-policy-v1)"
    if retrieval in ("hybrid", "hybrid-rerank"):
        if len(items) < POLICY_MIN_HYBRID_ITEMS:
            return False, f"fewer than {POLICY_MIN_HYBRID_ITEMS} evidence items"
        top_rrf = max(item.scores.get("rrf", 0.0) for item in items)
        if top_rrf < POLICY_MIN_TOP_RRF:
            return False, "top fused score below evidence-policy-v1 threshold"
    elif retrieval == "lexical":
        top_rank = min(item.scores.get("lexical_rank", 10**9) for item in items)
        if top_rank > POLICY_LEXICAL_MAX_RANK:
            return False, "top lexical rank worse than evidence-policy-v1 bound"
    elif retrieval == "dense":
        top_cosine = max(item.scores.get("dense", -1.0) for item in items)
        if top_cosine < POLICY_MIN_TOP_COSINE:
            return False, "top cosine below evidence-policy-v1 anchor (not a probability)"
    return True, None


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------


def provider_from_settings(settings):
    """Embedding provider from configuration.

    ``EMBED_PROVIDER=ollama`` (default) -> :class:`OllamaEmbeddingProvider`;
    ``EMBED_PROVIDER=fake`` -> deterministic test provider (NON-PRODUCTION;
    exists so index/search CLIs and the test suite run fully offline).
    """
    if settings.embed_provider == "fake":
        return FakeEmbeddingProvider()
    return OllamaEmbeddingProvider(
        settings.ollama_base_url,
        settings.ollama_embed_model,
        normalize=True,
        apply_nomic_conventions=True,
    )


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------


def build_index(
    session,
    strategy: str,
    provider,
    *,
    tickers: list[str] | None = None,
    settings=None,
    embed_batch_size: int = 32,
) -> IndexStats:
    """Chunk + embed + persist the ingested corpus for one strategy.

    Idempotent: rebuilding produces identical chunk rows/ids (unique-key
    reuse), re-serves cached embeddings by (text hash, model identity), and
    refreshes the FTS + index-manifest bookkeeping.
    """
    if strategy not in STRATEGY_REGISTRY:
        raise ValueError(f"unknown chunking strategy {strategy!r}")
    chunker = STRATEGY_REGISTRY[strategy]()
    strategy_version = chunker.STRATEGY_VERSION
    settings = settings or _get_settings()
    repo = _repo_for(session, settings)
    repo.ensure_schema()
    docs_repo = DocumentsRepo(session)

    stats = IndexStats(strategy=strategy, strategy_version=strategy_version)
    wanted_tickers = {t.upper() for t in (tickers or [])}

    documents: list[tuple[Document, DocumentMeta]] = []
    considered = 0
    for document in docs_repo.list_documents(extraction_status="ok"):
        company_row = session.get(Company, document.company_id)
        ticker = company_row.ticker if company_row else ""
        if not docs_repo.list_sections(document.id):
            continue  # nothing extracted (e.g. needs_ocr) — nothing to index
        considered += 1
        if wanted_tickers and ticker not in wanted_tickers:
            stats.documents_skipped += 1
            continue
        documents.append((document, _document_meta(docs_repo, document)))
    stats.documents_considered = considered

    total_fts_synced = 0
    for document, meta in documents:
        reports = []
        for section in docs_repo.list_sections(document.id):
            if not section.text or not section.text.strip():
                continue
            section_ref = SectionRef(
                section_id=section.id,
                section_type=section.section_type,
                heading=section.heading,
                # Offsets relative to the text handed to the chunker (the
                # section text); the indexer shifts into document coordinates.
                start_offset=0,
                end_offset=len(section.text),
                page_start=section.page_start,
                page_end=section.page_end,
            )
            drafts = chunker.chunk(section.text, [section_ref], meta)
            shift = section.start_offset or 0
            for draft in drafts:
                draft.start_offset += shift
                draft.end_offset += shift
            reports.append(
                repo.write_chunks(
                    document_id=document.id,
                    strategy=strategy,
                    strategy_version=strategy_version,
                    drafts=drafts,
                )
            )
        if not reports:
            stats.documents_skipped += 1
            continue
        stats.documents_indexed += 1
        # Sync FTS for ALL of the document's retrieval units (every section),
        # not just the last section's.
        chunk_ids: list[int] = []
        for report in reports:
            stats.chunks_written += report.written
            stats.chunks_deduplicated += report.reused
            chunk_ids.extend(report.base_chunk_ids())
        total_fts_synced += repo.sync_fts(chunk_ids)
    stats.fts_rows = total_fts_synced

    # -- embeddings (batched, cache-first by text hash + model identity) ------
    provider_name = getattr(provider, "provider_name", type(provider).__name__)
    revision = provider_revision(provider)
    stats.embedding_model = provider.model_id
    unit_ids = repo.retrieval_unit_ids(strategy=strategy, strategy_version=strategy_version)
    chunks: list[Chunk] = [c for cid in unit_ids if (c := repo.get_chunk(cid)) and c.text]
    hashes = [c.text_hash or sha256_text(c.text or "") for c in chunks]
    cached = repo.get_cached_vectors(
        sorted(set(hashes)), provider=provider_name, model=provider.model_id, revision=revision
    )
    stats.embeddings_cache_hits = sum(1 for h in hashes if h in cached)

    hash_to_text: dict[str, str] = {}
    for chunk, digest in zip(chunks, hashes, strict=True):
        hash_to_text.setdefault(digest, chunk.text or "")
    pending: dict[str, list[float]] = {}
    missing = [h for h in dict.fromkeys(hashes) if h not in cached]
    for start in range(0, len(missing), embed_batch_size):
        batch = missing[start : start + embed_batch_size]
        texts = [f"{provider.document_prefix}{hash_to_text[h]}" for h in batch]
        for digest, vector in zip(batch, provider.embed(texts), strict=True):
            pending[digest] = vector
    for chunk, digest in zip(chunks, hashes, strict=True):
        vector = cached.get(digest)
        if vector is None:
            vector = pending.get(digest)
        if vector is None:
            continue
        if repo.store_embedding(
            chunk_id=chunk.id,
            text_hash=digest,
            provider=provider_name,
            model=provider.model_id,
            revision=revision,
            vector=np.asarray(vector, dtype=np.float32),
            normalized=bool(getattr(provider, "normalize", True)),
            dim=len(vector),
        ):
            stats.embeddings_written += 1
    try:
        stats.dim = provider.dim
    except EmbeddingProviderUnavailable:
        stats.dim = None

    corpus_hash = repo.compute_corpus_hash(strategy=strategy, strategy_version=strategy_version)
    stats.corpus_hash = corpus_hash
    stats.index_version = repo.record_manifest(
        strategy=strategy,
        strategy_version=strategy_version,
        embedding_provider=provider_name,
        embedding_model=provider.model_id,
        embedding_model_revision=revision,
        dim=stats.dim,
        corpus_hash=corpus_hash,
        chunk_count=len(chunks),
        embedded_chunk_count=len(cached) + len(pending),
    )
    return stats


def _document_meta(docs_repo: DocumentsRepo, document: Document) -> DocumentMeta:
    content_hash = f"doc-{document.id}"
    if document.source_artifact_id is not None:
        artifact = docs_repo.get_source_artifact(document.source_artifact_id)
        if artifact is not None and artifact.content_hash:
            content_hash = artifact.content_hash
    return DocumentMeta(
        document_id=document.id,
        content_hash=content_hash,
        extraction_version=document.extraction_version,
        form=document.form,
        document_kind=document.document_kind,
    )


# ---------------------------------------------------------------------------
# Search service
# ---------------------------------------------------------------------------


def _is_postgres(settings: Settings) -> bool:
    """True when DATABASE_URL targets the PostgreSQL + pgvector profile."""
    url = str(getattr(settings, "database_url", "") or "")
    return url.startswith(("postgresql://", "postgres://", "postgresql+"))


def _repo_for(session: Session, settings: Settings):
    """Select the search backend for the configured DATABASE_URL (SQLite
    default; PostgreSQL + pgvector profile when the URL targets Postgres)."""
    if _is_postgres(settings):
        return get_search_repo(session, settings)
    return SearchIndexRepo(session)


class SearchService:
    """Query orchestration per SPEC §16 (SQLite profile)."""

    def __init__(
        self,
        session,
        embedding_provider=None,
        *,
        reranker=None,
        reranker_enabled: bool = True,
        settings=None,
    ) -> None:
        self.session = session
        self.provider = embedding_provider
        self.reranker = reranker
        self.reranker_enabled = reranker_enabled
        self.settings = settings or _get_settings()
        self.repo = _repo_for(session, self.settings)
        # The repo and the query functions must agree on the backend: binding
        # them from one decision is what stops a Postgres session from running
        # SQLite FTS5 syntax (`chunks_fts MATCH ...`), which psycopg rejects.
        postgres = _is_postgres(self.settings)
        self._search_lexical = search_lexical_postgres if postgres else search_lexical
        self._search_dense = search_dense_postgres if postgres else search_dense
        self.repo.ensure_schema()

    # -- public API -----------------------------------------------------------

    def search(self, query: SearchQuery) -> SearchResult:
        started = time.perf_counter()
        run_id = new_run_id()
        result = self._search(query)
        emit_run_event(
            run_id,
            {
                "endpoint": "search",
                "provider": getattr(self.provider, "provider_name", None),
                "model": getattr(self.provider, "model_id", None),
                "embedding_model": result.embedding_model,
                "corpus_version": result.index_version,
                "chunking_strategy": query.strategy,
                "retrieval_strategy": query.retrieval,
                "retrieval_latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "evidence_count": len(result.items),
                "answer_status": (
                    "insufficient_evidence" if result.insufficient_evidence else "ok"
                ),
                "degraded": sorted(result.degraded),
                "error_type": None,
            },
        )
        return result

    def search_and_assemble(
        self, query: SearchQuery, *, budget_tokens: int | None = None
    ) -> tuple[SearchResult, ContextBundle]:
        """Search + bounded context assembly (tail of the SPEC §16 flow)."""
        result = self.search(query)
        budget = budget_tokens or context_budget_from_settings(self.settings)
        return result, assemble_context(result.items, budget)

    # -- internals ------------------------------------------------------------

    def _search(self, query: SearchQuery) -> SearchResult:
        sections = query.sections
        if sections is None:
            sections = MODE_SECTION_PRESETS.get(query.mode)
        strategies = [query.strategy] if query.strategy != "any" else ["fixed", "section"]
        strategies = [s for s in strategies if s in STRATEGY_REGISTRY]
        version_by_strategy = {s: STRATEGY_REGISTRY[s].STRATEGY_VERSION for s in strategies}

        result = SearchResult()
        docs_repo = DocumentsRepo(self.session)
        company = docs_repo.get_company_by_ticker(query.ticker)
        if company is None:
            result.insufficient_evidence = True
            result.insufficient_evidence_reason = f"unknown company {query.ticker!r}"
            result.degraded["corpus"] = "company not present in the store"
            return result

        needs_dense = query.retrieval in ("dense", "hybrid", "hybrid-rerank")
        index_version = self._resolve_index_version(
            query, strategies, version_by_strategy, needs_dense, result
        )
        result.index_version = index_version
        if result.embedding_model is None and self.provider is not None:
            result.embedding_model = (
                f"{getattr(self.provider, 'provider_name', 'unknown')}/{self.provider.model_id}"
            )
        if query.index_version is not None and query.index_version != index_version:
            result.degraded["index_version"] = (
                f"requested {query.index_version}, current is {index_version}"
            )
            result.insufficient_evidence = True
            result.insufficient_evidence_reason = "index version mismatch"
            return result

        lexical_filters = LexicalFilters(
            ticker=company.ticker,
            strategies=strategies,
            strategy_version_by_strategy=version_by_strategy,
            sections=sections,
            forms=query.forms,
            period_end=query.period_end,
            filed_before=query.filed_before,
        )

        # -- lexical top 20 -------------------------------------------------------
        lexical_hits: list[LexicalHit] = []
        if query.retrieval in ("lexical", "hybrid", "hybrid-rerank"):
            lexical_hits = self._search_lexical(self.session, query.query, lexical_filters)

        # -- dense top 20 ---------------------------------------------------------
        dense_hits: list[DenseHit] = []
        if needs_dense:
            if self.provider is None:
                result.degraded["dense"] = "no embedding provider configured"
            else:
                try:
                    vectors = self.provider.embed([f"{self.provider.query_prefix}{query.query}"])
                except EmbeddingProviderUnavailable as exc:
                    # SPEC §25: embedding provider unavailable -> lexical
                    # search remains available.
                    result.degraded["dense"] = f"embedding provider unavailable: {exc}"
                    vectors = None
                if vectors is not None:
                    dense_hits = self._search_dense(
                        self.session,
                        np.asarray(vectors[0], dtype=np.float32),
                        _dense_filters_from(lexical_filters),
                        _DENSE_TOP_K,
                        provider=getattr(self.provider, "provider_name", "unknown"),
                        model=self.provider.model_id,
                        revision=provider_revision(self.provider),
                    )

        # -- RRF fusion -> top 15 -------------------------------------------------
        fused = self._fuse(query.retrieval, lexical_hits, dense_hits)
        if not fused:
            result.insufficient_evidence = True
            result.insufficient_evidence_reason = "no evidence retrieved"
            if needs_dense and not dense_hits:
                result.degraded.setdefault(
                    "dense", result.degraded.get("dense", "no dense hits for this corpus/query")
                )
            return result

        items = self._build_items(query, fused, lexical_hits, dense_hits, result)

        # -- optional cross-encoder rerank ----------------------------------------
        if query.retrieval == "hybrid-rerank":
            from quarterline.retrieve.reranker import maybe_rerank

            items, reason = maybe_rerank(
                self.reranker, query.query, items, enabled=self.reranker_enabled
            )
            if reason:
                result.degraded["reranker"] = reason

        result.items = items[: query.top_k]
        sufficient, reason = evaluate_evidence_policy(result.items, query.query, query.retrieval)
        result.insufficient_evidence = not sufficient
        result.insufficient_evidence_reason = reason
        return result

    def _resolve_index_version(
        self,
        query: SearchQuery,
        strategies: list[str],
        version_by_strategy: dict[str, str],
        needs_dense: bool,
        result: SearchResult,
    ) -> str | None:
        """Model-mismatch gate + version resolution (SPEC §15.6/§15.7)."""
        versions: list[str] = []
        any_manifest = None
        for strategy in strategies:
            manifest = self.repo.get_any_manifest(
                strategy=strategy, strategy_version=version_by_strategy[strategy]
            )
            if manifest is None:
                continue
            if any_manifest is None:
                any_manifest = manifest
            if self.provider is not None:
                matches = (
                    manifest["embedding_provider"] == getattr(self.provider, "provider_name", None)
                    and manifest["embedding_model"] == self.provider.model_id
                    and manifest["embedding_model_revision"] == provider_revision(self.provider)
                )
                if matches:
                    versions.append(manifest["index_version"])
            else:
                versions.append(manifest["index_version"])
        if needs_dense and self.provider is not None and any_manifest is not None and not versions:
            raise EmbeddingModelMismatchError(
                "index for "
                f"{strategies} was built with embedding model "
                f"{any_manifest['embedding_provider']}/{any_manifest['embedding_model']}, "
                f"not {getattr(self.provider, 'provider_name', 'unknown')}/"
                f"{self.provider.model_id} (SPEC §15.7)"
            )
        if self.provider is None and any_manifest is not None:
            result.embedding_model = (
                f"{any_manifest['embedding_provider']}/{any_manifest['embedding_model']}"
            )
        return versions[0] if versions else None

    def _fuse(self, retrieval, lexical_hits, dense_hits) -> list[FusedHit]:
        if retrieval == "lexical":
            return [
                FusedHit(chunk_id=hit.chunk_id, score=0.0, lexical_rank=hit.rank)
                for hit in lexical_hits[:_FUSE_TOP_K]
            ]
        if retrieval == "dense":
            return [
                FusedHit(chunk_id=hit.chunk_id, score=0.0, dense_rank=hit.rank)
                for hit in dense_hits[:_FUSE_TOP_K]
            ]
        return rrf_fuse(
            [(hit.chunk_id, hit.rank) for hit in lexical_hits],
            [(hit.chunk_id, hit.rank) for hit in dense_hits],
            top_k=_FUSE_TOP_K,
        )

    def _build_items(self, query, fused, lexical_hits, dense_hits, result) -> list[EvidenceItem]:
        lexical_by_id = {hit.chunk_id: hit for hit in lexical_hits}
        dense_by_id = {hit.chunk_id: hit for hit in dense_hits}

        # Section-strategy children expand to their bounded window row; the
        # fixed strategy's chunks stand alone (SPEC §14: never send a whole
        # long section).
        strategies = [query.strategy] if query.strategy != "any" else ["fixed", "section"]
        window_by_child: dict[int, Chunk] = {}
        for strategy in strategies:
            if strategy not in STRATEGY_REGISTRY:
                continue
            version = STRATEGY_REGISTRY[strategy].STRATEGY_VERSION
            child_ids = []
            for hit in fused:
                if hit.chunk_id in window_by_child or hit.chunk_id in child_ids:
                    continue
                chunk = self.repo.get_chunk(hit.chunk_id)
                if (
                    chunk is not None
                    and chunk.strategy == strategy
                    and chunk.strategy_version == version
                ):
                    child_ids.append(hit.chunk_id)
            window_by_child.update(
                self.repo.windows_for_children(
                    child_ids, strategy=strategy, strategy_version=version
                )
            )

        display_ids = [
            window_by_child[hit.chunk_id].id if hit.chunk_id in window_by_child else hit.chunk_id
            for hit in fused
        ]
        rows = self.repo.evidence_rows(list(dict.fromkeys(display_ids)))
        items: list[EvidenceItem] = []
        for hit, display_id in zip(fused, display_ids, strict=True):
            row = rows.get(display_id)
            if row is None or row.text is None:
                continue
            scores: dict[str, float] = {}
            lexical = lexical_by_id.get(hit.chunk_id)
            if lexical is not None:
                scores["lexical"] = lexical.score
                scores["lexical_rank"] = float(lexical.rank)
            dense = dense_by_id.get(hit.chunk_id)
            if dense is not None:
                scores["dense"] = dense.score
                scores["dense_rank"] = float(dense.rank)
            if hit.score:
                scores["rrf"] = hit.score
            items.append(
                EvidenceItem(
                    evidence_id=evidence_id_for(
                        row.document_id,
                        row.strategy,
                        row.start_offset or 0,
                        row.end_offset or 0,
                        row.text_hash or "",
                    ),
                    document_id=row.document_id,
                    section=row.section_type,
                    start_offset=row.start_offset or 0,
                    end_offset=row.end_offset or 0,
                    text=row.text,
                    scores=scores,
                    chunk_id=row.chunk_id,
                    strategy=row.strategy,
                    page=row.page_start,
                    token_count=row.token_count,
                    embedding_model=result.embedding_model,
                )
            )
        return items


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _dense_filters_from(lexical_filters: LexicalFilters) -> DenseFilters:
    return DenseFilters(
        ticker=lexical_filters.ticker,
        strategies=lexical_filters.strategies,
        strategy_version_by_strategy=lexical_filters.strategy_version_by_strategy,
        sections=lexical_filters.sections,
        forms=lexical_filters.forms,
        period_end=lexical_filters.period_end,
        filed_before=lexical_filters.filed_before,
    )


def _get_settings():
    from quarterline.config import get_settings

    return get_settings()


# ---------------------------------------------------------------------------
# CLI handlers (registered at import; cli.py is not edited)
# ---------------------------------------------------------------------------


def handle_index_build(args: argparse.Namespace) -> int:
    strategy = args.strategy
    tickers = getattr(args, "tickers", None)  # W0 parser has no --tickers flag yet
    settings = _get_settings()
    provider = provider_from_settings(settings)
    if isinstance(provider, OllamaEmbeddingProvider):
        provider.ensure_model()
    from quarterline.store.db import session_scope

    with session_scope() as session:
        stats = build_index(session, strategy, provider, tickers=tickers, settings=settings)
    print(stats.summary())
    return 0


def handle_search(args: argparse.Namespace) -> int:
    settings = _get_settings()
    provider = provider_from_settings(settings)
    if isinstance(provider, OllamaEmbeddingProvider):
        try:
            provider.ensure_model()
        except EmbeddingProviderUnavailable as exc:
            print(f"embedding provider unavailable: {exc}", flush=True)
    mode = getattr(args, "mode", None) or os.environ.get("QUARTERLINE_SEARCH_MODE", "general")
    query = SearchQuery(
        query=args.query,
        ticker=args.ticker,
        strategy=args.strategy,
        retrieval=args.retrieval,
        mode=mode,  # type: ignore[arg-type]
    )
    from quarterline.store.db import session_scope

    with session_scope() as session:
        service = SearchService(session, provider, settings=settings)
        try:
            result = service.search(query)
        except EmbeddingModelMismatchError as exc:
            print(f"embedding model mismatch: {exc}")
            return 1
        print(_format_search_result(result))
    return 0


def _format_search_result(result: SearchResult) -> str:
    lines = [
        "Search results (research only, not investment advice)",
        f"  index version: {result.index_version}",
        f"  embedding model: {result.embedding_model}",
        f"  evidence policy: {result.evidence_policy_version}"
        + (
            f" — INSUFFICIENT EVIDENCE ({result.insufficient_evidence_reason})"
            if result.insufficient_evidence
            else ""
        ),
    ]
    for flag, reason in sorted(result.degraded.items()):
        lines.append(f"  degraded[{flag}]: {reason}")
    for rank, item in enumerate(result.items, start=1):
        preview = " ".join(item.text.split())[:160]
        scores = ", ".join(f"{k}={v:.4f}" for k, v in sorted(item.scores.items()))
        lines.append(
            f"  [{rank}] {item.evidence_id} doc={item.document_id} "
            f"section={item.section} offsets=[{item.start_offset},{item.end_offset}) "
            f"scores{{{scores}}}"
        )
        lines.append(f"      {preview}")
    if not result.items:
        lines.append("  (no evidence)")
    return "\n".join(lines)


def register_cli() -> None:
    from quarterline.cli import register_subcommand

    register_subcommand("index:build", handle_index_build)
    register_subcommand("search", handle_search)


register_cli()
