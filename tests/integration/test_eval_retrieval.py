"""Integration: retrieval evaluation over the REAL fixture corpus (SPEC §21/§22).

Reuses the wave-2 fixture corpus (REAL Apple EX-99.1 exhibit + synthetic
distractors) and the committed dataset. Fully offline: FakeEmbeddingProvider
only. Also verifies every gold span's anchor against the ingested cleaned
document text — the machine check behind `reviewed: true`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from retrieval_test_helpers import (
    DOC_REAL_8K,
    fixture_provider,
    retrieval_db_fixture,  # noqa: F401 (registers the `retrieval_db` fixture)
)
from sqlalchemy import select

from quarterline.eval.dataset import load_dataset, verify_spans
from quarterline.eval.retrieval import (
    RETRIEVAL_CONFIGS,
    STRATEGIES,
    evaluate_retrieval,
    run_matrix,
)
from quarterline.retrieve.embeddings import FakeEmbeddingProvider
from quarterline.retrieve.search import SearchService
from quarterline.store.db import session_scope
from quarterline.store.models import EvalResult, EvalRun

REPO_ROOT = Path(__file__).resolve().parents[2]
QUESTIONS_PATH = REPO_ROOT / "data" / "eval" / "questions.jsonl"


@pytest.fixture
def questions() -> list:
    return load_dataset(QUESTIONS_PATH)


def test_every_gold_span_anchor_resolves_in_ingested_text(retrieval_db, questions) -> None:
    """Offsets were verified in-session; the committed dataset must keep passing."""
    with session_scope() as session:
        receipts = verify_spans(session, questions)
    assert len(receipts) == sum(len(q.relevant_evidence) for q in questions)
    assert all("verified" in receipt for receipt in receipts)


def test_gold_spans_live_in_the_real_exhibit_document(retrieval_db, questions) -> None:
    with session_scope() as session:
        from quarterline.store.models import Document

        real_doc = session.get(Document, DOC_REAL_8K)
        assert real_doc is not None
        assert real_doc.accession == "000032019326000011"  # dash-stripped accession
        assert str(real_doc.period_end) == "2026-03-28"  # discussed period
    gold_docs = {span.document_id for q in questions for span in q.relevant_evidence}
    assert gold_docs == {"0000320193-26-000011"}


def test_answerable_questions_hit_gold_on_hybrid_section(retrieval_db, questions) -> None:
    """Frozen fixture-corpus retrieval levels (SPEC §23 frozen regression tests).

    Measured values (deterministic): section/hybrid reaches 6 of 7 gold
    questions in top-5. Levels raised from 4/7 (MRR 3/7) after the
    orchestrator's wave-3 fix of the ``_pack_chunks`` early-stop in
    ``retrieve/chunk_fixed.py`` (which had left offsets >~5639 of the exhibit
    unindexed for both strategies); baseline regenerated explicitly via
    QUARTERLINE_EVAL_WRITE_BASELINE with the diff in the implementation log.
    """
    with session_scope() as session:
        report = evaluate_retrieval(
            questions,
            session,
            strategy="section",
            retrieval="hybrid",
            provider=fixture_provider(),
        )
    assert report.n_questions == len(questions)
    assert report.n_with_gold == 7
    assert report.hit_at_5 == pytest.approx(6 / 7)
    assert report.recall_at_5 == pytest.approx(6 / 7)
    assert report.mrr == pytest.approx(5.0 / 7)
    assert report.wrong_company_rate == 0.0  # ticker filter must hold
    assert report.period_filter_error_rate == 0.0


def test_fixed_lexical_frozen_levels(retrieval_db, questions) -> None:
    with session_scope() as session:
        report = evaluate_retrieval(
            questions,
            session,
            strategy="fixed",
            retrieval="lexical",
            provider=fixture_provider(),
        )
    assert report.hit_at_5 == pytest.approx(7 / 7)
    assert report.recall_at_5 == pytest.approx(7 / 7)
    assert report.mrr == pytest.approx(6.5 / 7)


def test_wrong_company_trap_retrieves_nothing(retrieval_db, questions) -> None:
    with session_scope() as session:
        report = evaluate_retrieval(
            questions,
            session,
            strategy="section",
            retrieval="lexical",
            provider=fixture_provider(),
        )
    trap = next(r for r in report.records if r.task_type == "wrong_company_trap")
    assert trap.retrieved == []  # no MSFT docs in the corpus at all
    assert trap.insufficient_evidence is True


def test_period_trap_yields_insufficient_evidence(retrieval_db, questions) -> None:
    with session_scope() as session:
        report = evaluate_retrieval(
            questions,
            session,
            strategy="section",
            retrieval="lexical",
            provider=fixture_provider(),
        )
    trap = next(
        r
        for r in report.records
        if r.question_period is not None and not r.has_gold and r.task_type == "period_specific"
    )
    assert trap.retrieved == []
    assert trap.insufficient_evidence is True


def test_run_matrix_covers_2x4(retrieval_db, questions) -> None:
    with session_scope() as session:
        reports = run_matrix(questions, session, provider=FakeEmbeddingProvider())
    assert len(reports) == len(STRATEGIES) * len(RETRIEVAL_CONFIGS)
    keys = {(r.strategy, r.retrieval) for r in reports}
    assert keys == {(s, c) for s in STRATEGIES for c in RETRIEVAL_CONFIGS}
    # Fixture-environment honesty: the reranker arm degrades (no cross-encoder).
    rerank = next(r for r in reports if r.retrieval == "hybrid-rerank")
    assert rerank.rerank_degraded is True
    # Every report is serializable for baselines/reports.
    for report in reports:
        payload = report.to_dict()
        assert payload["corpus"] == "fixture"
        assert "fake" in payload["embedding_model"]


def test_eval_rows_persisted(retrieval_db, questions) -> None:
    with session_scope() as session:
        report = evaluate_retrieval(
            questions,
            session,
            strategy="fixed",
            retrieval="lexical",
            provider=fixture_provider(),
        )
        from quarterline.eval import _persist_retrieval_rows  # reuse the CLI persister

        _persist_retrieval_rows(session, questions, [report], QUESTIONS_PATH)
        row = session.execute(select(EvalRun).order_by(EvalRun.id.desc()).limit(1)).scalar_one()
        assert row.chunking_strategy == "fixed"
        assert row.retrieval_config == "lexical"
        assert row.dataset_version is not None and len(row.dataset_version) == 12
        results = (
            session.execute(select(EvalResult).where(EvalResult.eval_run_id == row.id))
            .scalars()
            .all()
        )
        assert {r.metric_name for r in results} == {"reciprocal_rank", "gold_covered"}
        assert len(results) == 2 * len(questions)


def test_unknown_ticker_service_degrades_cleanly(retrieval_db) -> None:
    """The wrong-company guarantee at the service level: no MSFT company row."""
    with session_scope() as session:
        service = SearchService(session, FakeEmbeddingProvider())
        from quarterline.retrieve.models import SearchQuery

        result = service.search(SearchQuery(query="cloud revenue", ticker="MSFT"))
    assert result.items == []
    assert result.insufficient_evidence is True
