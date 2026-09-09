"""Retrieval service integration tests — the SPEC §26 retrieval list over the
real fixture corpus (REAL Apple EX-99.1 exhibit + synthetic fixtures) in a tmp
SQLite DB, indexed with the deterministic FakeEmbeddingProvider. Fully
offline: no SEC, no Ollama."""

from __future__ import annotations

import hashlib
from datetime import date

import pytest
from retrieval_test_helpers import (
    DOC_ACME_PDF,
    DOC_INJECTION_8K,
    DOC_REAL_8K,
    DOC_TEN_Q,
    fixture_provider,
    load_fixture_embeddings,
    retrieval_db_fixture,  # noqa: F401 (registers the `retrieval_db` fixture)
)

from quarterline.retrieve.context import MAX_PASSAGE_TOKENS, MAX_PASSAGES, assemble_context
from quarterline.retrieve.embeddings import (
    EmbeddingModelMismatchError,
    FakeEmbeddingProvider,
)
from quarterline.retrieve.models import EVIDENCE_POLICY_VERSION, SearchQuery, evidence_id_for
from quarterline.retrieve.search import SearchService, build_index
from quarterline.store.db import session_scope
from quarterline.store.repositories.search_sqlite import SearchIndexRepo

AAPL_DOCS = {DOC_TEN_Q, DOC_REAL_8K, DOC_INJECTION_8K}


@pytest.fixture
def service(retrieval_db):
    with session_scope() as session:
        yield SearchService(session, fixture_provider())


# -- correct-company filtering (§26) -------------------------------------------


def test_correct_company_filtering_lexical(service) -> None:
    result = service.search(
        SearchQuery(query="revenue quarter", ticker="AAPL", retrieval="lexical")
    )
    assert result.items
    assert {item.document_id for item in result.items} <= AAPL_DOCS
    acme = service.search(
        SearchQuery(query="manufacturing review", ticker="ACME", retrieval="lexical")
    )
    assert acme.items
    assert {item.document_id for item in acme.items} == {DOC_ACME_PDF}


def test_correct_company_filtering_dense(service) -> None:
    """Metadata filtering happens BEFORE cosine scoring (SPEC §15)."""
    result = service.search(SearchQuery(query="revenue", ticker="AAPL", retrieval="dense"))
    assert {item.document_id for item in result.items} <= AAPL_DOCS
    acme = service.search(SearchQuery(query="revenue", ticker="ACME", retrieval="dense"))
    assert {item.document_id for item in acme.items} == {DOC_ACME_PDF}


def test_correct_company_filtering_hybrid_never_crosses_companies(service) -> None:
    result = service.search(
        SearchQuery(query="Northwind manufacturing review", ticker="AAPL", retrieval="hybrid")
    )
    assert {item.document_id for item in result.items} <= AAPL_DOCS


# -- correct-period filtering (§26) ----------------------------------------------


def test_correct_period_filtering(service) -> None:
    q1 = service.search(
        SearchQuery(
            query="Apple results",
            ticker="AAPL",
            strategy="any",
            retrieval="lexical",
            period_end=date(2026, 3, 28),
        )
    )
    q2 = service.search(
        SearchQuery(
            query="Apple results",
            ticker="AAPL",
            strategy="any",
            retrieval="lexical",
            period_end=date(2026, 6, 30),
        )
    )
    assert q1.items and q2.items
    docs_q1 = {item.document_id for item in q1.items}
    docs_q2 = {item.document_id for item in q2.items}
    assert docs_q1 <= {DOC_TEN_Q, DOC_REAL_8K}  # both discuss the March quarter
    assert docs_q2 == {DOC_INJECTION_8K}  # discussed period parsed from its text
    assert docs_q1.isdisjoint(docs_q2)


def test_form_filter_restricts_documents(service) -> None:
    result = service.search(
        SearchQuery(
            query="revenue", ticker="AAPL", strategy="any", retrieval="lexical", forms=["10-Q"]
        )
    )
    assert result.items
    assert {item.document_id for item in result.items} == {DOC_TEN_Q}


# -- FTS5 ordering (§26, sign handled and documented) -----------------------------


def test_fts5_rank_ordering_with_bm25_sign_convention(service) -> None:
    result = service.search(
        SearchQuery(
            query="Apple reports second quarter results",
            ticker="AAPL",
            strategy="any",
            retrieval="lexical",
        )
    )
    assert len(result.items) >= 2
    scores = [item.scores["lexical"] for item in result.items]
    # Documented FTS5 convention: more negative bm25 = better; ranks follow.
    assert scores == sorted(scores)
    assert result.items[0].scores["lexical_rank"] == 1.0
    best_contains_query = any(
        token in result.items[0].text.lower() for token in ("apple", "quarter", "results")
    )
    assert best_contains_query


# -- RRF calculation (§26; hand-computed case lives in tests/unit/test_fusion) ----


def test_hybrid_rrf_score_recomputed_from_source_ranks(service) -> None:
    result = service.search(
        SearchQuery(
            query="Apple revenue March quarter", ticker="AAPL", strategy="fixed", retrieval="hybrid"
        )
    )
    assert result.items
    for item in result.items:
        expected = 0.0
        if item.scores.get("lexical_rank"):
            expected += 1.0 / (60 + item.scores["lexical_rank"])
        if item.scores.get("dense_rank"):
            expected += 1.0 / (60 + item.scores["dense_rank"])
        assert item.scores["rrf"] == pytest.approx(expected, abs=1e-9)


# -- embedding-model mismatch rejection (§26, §15.7) --------------------------------


def test_embedding_model_mismatch_rejected(retrieval_db) -> None:
    with session_scope() as session:
        rogue = SearchService(session, FakeEmbeddingProvider(dim=64, seed=999))
        with pytest.raises(EmbeddingModelMismatchError, match="15.7"):
            rogue.search(
                SearchQuery(query="revenue", ticker="AAPL", strategy="fixed", retrieval="dense")
            )
        with pytest.raises(EmbeddingModelMismatchError):
            rogue.search(
                SearchQuery(query="revenue", ticker="AAPL", strategy="section", retrieval="hybrid")
            )


def test_lexical_survives_embedding_provider_absence(retrieval_db) -> None:
    """SPEC §25: embedding provider unavailable -> lexical remains available."""
    with session_scope() as session:
        service = SearchService(session, None)
        result = service.search(
            SearchQuery(query="revenue", ticker="AAPL", strategy="fixed", retrieval="lexical")
        )
        assert result.items
        assert result.degraded == {}  # nothing dense was requested


def test_index_version_changes_with_embedding_model(retrieval_db) -> None:
    with session_scope() as session:
        repo = SearchIndexRepo(session)
        before = repo.get_any_manifest(strategy="fixed", strategy_version="fixed-1")
        assert before is not None
        from quarterline.retrieve.search import build_index

        build_index(session, "fixed", FakeEmbeddingProvider(dim=64, seed=424242))
        after = repo.get_any_manifest(strategy="fixed", strategy_version="fixed-1")
        assert after["index_version"] != before["index_version"]  # SPEC §15.6
        # Both model identities remain queryable individually.
        assert (
            repo.get_manifest(
                strategy="fixed",
                strategy_version="fixed-1",
                provider="fake",
                model="fake-embed-64d-seed20260909",
                revision="",
            )
            is not None
        )


# -- bounded parent expansion (§26, §14) ----------------------------------------------


def test_bounded_parent_expansion_never_returns_whole_section(service) -> None:
    result = service.search(
        SearchQuery(
            query="Apple revenue March quarter",
            ticker="AAPL",
            strategy="section",
            retrieval="hybrid",
        )
    )
    assert result.items
    for item in result.items:
        assert item.token_count <= 560  # window cap (documented choice, <= §16's 600)
    # The real exhibit's earnings_release section is ~11k chars (>1,300 tokens):
    # no supplied window may be the whole section.
    assert all(len(item.text) < 4000 for item in result.items)


def test_fixed_strategy_items_stay_under_chunk_cap(service) -> None:
    result = service.search(
        SearchQuery(query="revenue quarter", ticker="AAPL", strategy="fixed", retrieval="lexical")
    )
    assert result.items
    assert max(item.token_count for item in result.items) <= 460


# -- context budget enforcement (§26, §16) ---------------------------------------------


def test_context_budget_enforcement(service) -> None:
    result = service.search(
        SearchQuery(
            query="Apple revenue March quarter",
            ticker="AAPL",
            strategy="section",
            retrieval="hybrid",
        )
    )
    bundle = assemble_context(result.items, budget_tokens=2_000)
    assert len(bundle.passages) <= MAX_PASSAGES
    assert all(p.token_count <= MAX_PASSAGE_TOKENS for p in bundle.passages)
    assert bundle.total_tokens <= bundle.effective_budget_tokens


# -- evidence/citation ID resolution (§26, contract C8) ---------------------------------


def test_evidence_id_resolution_roundtrip(service) -> None:
    result = service.search(
        SearchQuery(
            query="Apple revenue March quarter",
            ticker="AAPL",
            strategy="section",
            retrieval="hybrid",
        )
    )
    assert result.items
    with session_scope() as session:
        repo = SearchIndexRepo(session)
        for item in result.items[:5]:
            chunk = repo.get_chunk_by_evidence_id(item.evidence_id)
            assert chunk is not None
            assert chunk.id == item.chunk_id
            assert chunk.document_id == item.document_id
            # The id is a pure function of the persisted identity tuple.
            assert item.evidence_id == evidence_id_for(
                chunk.document_id,
                chunk.strategy,
                chunk.start_offset or 0,
                chunk.end_offset or 0,
                chunk.text_hash or "",
            )


def test_offset_slice_invariant_on_real_fixture_evidence(service) -> None:
    """text[start:end] must reproduce the chunk text from the document text."""
    result = service.search(
        SearchQuery(
            query="Apple reports second quarter", ticker="AAPL", strategy="any", retrieval="lexical"
        )
    )
    assert result.items
    hit = next(item for item in result.items if item.document_id == DOC_REAL_8K)
    with session_scope() as session:
        assert hit.text == _slice_document_text(
            session, DOC_REAL_8K, hit.start_offset, hit.end_offset
        )
        assert "Apple" in hit.text


def _slice_document_text(session, document_id: int, start: int, end: int) -> str:
    """Reconstruct [start, end) of the cleaned document text from section rows.

    Section rows store exact slices at document offsets; sections may leave
    gaps (un-sectioned interstitial text), which cannot be stitched and stop
    the reconstruction honestly.
    """
    from quarterline.store.repositories.documents import DocumentsRepo

    repo = DocumentsRepo(session)
    pieces: list[str] = []
    covered_until = start
    for section in repo.list_sections(document_id):
        s_start = section.start_offset or 0
        s_end = section.end_offset or 0
        if s_end <= covered_until:
            continue
        if s_start > covered_until:
            break  # gap: the span reaches un-sectioned text
        take_end = min(end, s_end)
        pieces.append((section.text or "")[covered_until - s_start : take_end - s_start])
        covered_until = take_end
        if covered_until >= end:
            break
    return "".join(pieces)


# -- reranker failure fallback (§26, §25) ------------------------------------------------


def test_reranker_failure_fallback_returns_hybrid_with_metadata(retrieval_db) -> None:
    from quarterline.retrieve.reranker import CrossEncoderReranker

    with session_scope() as session:
        # A real (library-absent) reranker attempt, not just "none configured".
        service = SearchService(session, fixture_provider(), reranker=CrossEncoderReranker())
        result = service.search(
            SearchQuery(
                query="Apple revenue March quarter",
                ticker="AAPL",
                strategy="section",
                retrieval="hybrid-rerank",
            )
        )
        assert result.items  # hybrid results still returned
        assert "reranker" in result.degraded  # degraded-mode metadata present
        assert "sentence-transformers" in result.degraded["reranker"]
        assert all("rerank" not in item.scores for item in result.items)  # no fake scores


# -- chunking idempotence + strategy coexistence (§26, §14) --------------------------------


def test_rebuild_is_idempotent_and_strategies_coexist(retrieval_db) -> None:
    from quarterline.store.models import Chunk

    ids_before = _all_chunk_ids()
    with session_scope() as session:
        stats_fixed = build_index(session, "fixed", fixture_provider())
        stats_section = build_index(session, "section", fixture_provider())
    assert stats_fixed.chunks_written == 0
    assert stats_fixed.chunks_deduplicated > 0
    assert stats_section.chunks_written == 0
    assert _all_chunk_ids() == ids_before  # identical ids on rebuild

    with session_scope() as session:
        stats = SearchIndexRepo(session).corpus_stats()
    assert "fixed/fixed-1" in stats["chunks"]
    assert "section/section-1" in stats["chunks"]
    assert "section/section-1-parent" in stats["chunks"]
    assert "section/section-1-window" in stats["chunks"]
    # Only retrieval units (base rows) carry embeddings.
    base_rows = stats["chunks"]["fixed/fixed-1"] + stats["chunks"]["section/section-1"]
    assert stats["embeddings"] == {"fake/fake-embed-64d-seed20260909": base_rows}
    # Building one strategy again did not disturb the other's corpus.
    with session_scope() as session:
        count = (
            session.query(Chunk)
            .filter(Chunk.strategy == "fixed", Chunk.strategy_version == "fixed-1")
            .count()
        )
    assert count == stats["chunks"]["fixed/fixed-1"]


def _all_chunk_ids() -> list[int]:
    from quarterline.store.db import session_scope
    from quarterline.store.models import Chunk

    with session_scope() as session:
        return sorted(int(row) for (row,) in session.query(Chunk.id).order_by(Chunk.id).all())


# -- mode-specific section presets (§16) ---------------------------------------------------


def test_mode_section_presets(service) -> None:
    brief = service.search(
        SearchQuery(
            query="growth revenue quarter",
            ticker="AAPL",
            strategy="any",
            retrieval="lexical",
            mode="brief",
        )
    )
    assert brief.items
    assert {item.section for item in brief.items} <= {"mda", "earnings_release"}

    risk = service.search(
        SearchQuery(
            query="risk factors supply chain",
            ticker="AAPL",
            strategy="any",
            retrieval="lexical",
            mode="risk",
        )
    )
    assert risk.items
    assert {item.section for item in risk.items} <= {"risk_factors", "mda"}

    general = service.search(
        SearchQuery(
            query="Apple reports second quarter",
            ticker="AAPL",
            strategy="any",
            retrieval="lexical",
            mode="general",
        )
    )
    assert general.items
    assert "earnings_release" in {item.section for item in general.items}


def test_explicit_sections_override_mode(service) -> None:
    result = service.search(
        SearchQuery(
            query="supply chain regulation risk factors",
            ticker="AAPL",
            strategy="any",
            retrieval="lexical",
            mode="brief",
            sections=["risk_factors"],
        )
    )
    assert result.items
    assert {item.section for item in result.items} == {"risk_factors"}


# -- insufficient evidence (§26, §16) --------------------------------------------------------


def test_insufficient_evidence_on_empty_corpus_and_no_match(service) -> None:
    unknown = service.search(SearchQuery(query="anything", ticker="MSFT", retrieval="hybrid"))
    assert unknown.insufficient_evidence
    assert unknown.items == []
    assert unknown.evidence_policy_version == EVIDENCE_POLICY_VERSION == "evidence-policy-v1"

    no_match = service.search(
        SearchQuery(
            query="zzzzqx nonexistingterm", ticker="AAPL", strategy="fixed", retrieval="hybrid"
        )
    )
    assert no_match.insufficient_evidence
    assert no_match.insufficient_evidence_reason


def test_sufficient_query_passes_policy(service) -> None:
    result = service.search(
        SearchQuery(
            query="Apple revenue quarter", ticker="AAPL", strategy="fixed", retrieval="hybrid"
        )
    )
    assert not result.insufficient_evidence
    assert result.insufficient_evidence_reason is None
    assert len(result.items) >= 2


# -- committed fixture embeddings (documented provenance, SPEC §26) ---------------------------


def test_fixture_embeddings_match_deterministic_provider(service) -> None:
    fixture = load_fixture_embeddings()
    manifest = fixture["manifest"]
    assert manifest["provenance"].startswith("NOT real model output")
    assert manifest["provider"]["seed"] == 20260909
    assert manifest["provider"]["dim"] == 64
    assert manifest["provider"]["model_id"] == "fake-embed-64d-seed20260909"

    vectors = {entry["text_hash"]: entry["vector"] for entry in fixture["chunk_vectors"]}
    provider = fixture_provider()
    result = service.search(
        SearchQuery(
            query="Apple revenue March quarter",
            ticker="AAPL",
            strategy="section",
            retrieval="hybrid",
        )
    )
    assert result.items
    checked = 0
    for item in result.items:
        digest = hashlib.sha256(item.text.encode("utf-8")).hexdigest()
        assert digest in vectors, "corpus chunk text missing from committed fixture"
        fresh = provider.embed([item.text])[0]
        assert fresh == vectors[digest], (
            "FakeEmbeddingProvider output drifted from the committed fixture; "
            "regenerate via tests/fixtures/retrieval/generate_fixtures.py only "
            "after a reviewed change"
        )
        checked += 1
    assert checked >= 3
