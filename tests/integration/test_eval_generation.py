"""Integration: generation harness over the fixture corpus with the scripted
fake runner (SPEC §22). All metrics exercised offline; every non-answer
event classified distinctly. The scripted runner measures the HARNESS, not
generation quality (SPEC §23)."""

from __future__ import annotations

from pathlib import Path

import pytest
from retrieval_test_helpers import fixture_provider, retrieval_db_fixture  # noqa: F401
from sqlalchemy import select

from quarterline.eval.dataset import load_dataset
from quarterline.eval.generation import (
    GenerationOutput,
    ScriptedFakeGenerationRunner,
    evaluate_generation,
)
from quarterline.retrieve.search import SearchService
from quarterline.store.db import session_scope
from quarterline.store.models import EvalResult, EvalRun

REPO_ROOT = Path(__file__).resolve().parents[2]
QUESTIONS_PATH = REPO_ROOT / "data" / "eval" / "questions.jsonl"


@pytest.fixture
def questions() -> list:
    return load_dataset(QUESTIONS_PATH)


def test_all_generation_metrics_offline(retrieval_db, questions) -> None:
    runner = ScriptedFakeGenerationRunner()
    with session_scope() as session:
        service = SearchService(session, fixture_provider())
        summary = evaluate_generation(questions, runner, service=service)

    assert summary["n_questions"] == len(questions)
    # Scripted answer path emits valid JSON with valid citations.
    assert summary["json_validity_before_repair"]["rate"] == pytest.approx(1.0)
    assert summary["json_validity_after_repair"]["rate"] == pytest.approx(1.0)
    assert summary["citation_validity"]["rate"] == pytest.approx(1.0)
    assert summary["citation_validity"]["n"] > 0
    # Scripted numbers come from the supplied evidence text -> no numeric errors.
    assert summary["numeric_error_rate_before_filtering"]["rate"] == pytest.approx(0.0)
    assert summary["unsupported_content_remaining_after_filtering"]["count"] == 0
    # Scripted answer keeps its bullet.
    assert summary["bullet_retention"]["rate"] == pytest.approx(1.0)
    assert summary["empty_answer_rate"]["count"] == 0
    # Abstention/refusal/prov-failure all correct on the scripted paths.
    abstain = summary["correct_abstention_rate"]
    assert abstain["n"] > 0 and abstain["rate"] == pytest.approx(1.0)
    refusal = summary["refusal_accuracy"]
    assert refusal["n"] > 0 and refusal["rate"] == pytest.approx(1.0)
    assert summary["provider_failure_rate"]["rate"] == pytest.approx(0.0)
    # The three non-answer events stay distinct in the status breakdown.
    breakdown = summary["status_breakdown"]
    assert breakdown.get("insufficient_evidence", 0) >= 1
    assert breakdown.get("refused", 0) >= 1
    assert breakdown.get("answer", 0) >= 1


def test_generation_metrics_distinguish_failure_classes(retrieval_db, questions) -> None:
    """Override two question classes: every advice_request gets a provider
    failure, every wrong_company_trap gets bad JSON needing repair. Counts
    track the committed dataset (30 questions: 20 answer, 6 expected
    insufficient, 4 expected refusal, 2 wrong-company traps)."""
    overrides = {}
    for question in questions:
        if question.task_type == "advice_request":
            overrides[question.id] = GenerationOutput(
                raw_text="",  # provider died: no JSON at all
                repaired_text="",
                status="provider_unavailable",
                error_type="provider_unavailable",
                latency_ms=1.0,
            )
        if question.task_type == "wrong_company_trap":
            overrides[question.id] = GenerationOutput(
                raw_text='{"status": "insufficient_evidence",,}',  # malformed
                repaired_text='{"status": "insufficient_evidence"}',  # repaired
                status="insufficient_evidence",
                latency_ms=1.0,
            )
    runner = ScriptedFakeGenerationRunner(overrides=overrides)
    with session_scope() as session:
        service = SearchService(session, fixture_provider())
        summary = evaluate_generation(questions, runner, service=service)

        assert summary["provider_failure_rate"]["count"] == 4
        # The malformed-JSON and dead-provider outputs are all invalid before
        # repair; only the malformed ones are recovered by repair.
        assert summary["json_validity_before_repair"]["count"] == len(questions) - 6
        assert summary["json_repair_rate"]["count"] == 2
        assert summary["status_breakdown"].get("provider_unavailable", 0) == 4
        # The three non-answer outcomes stay distinct: with every
        # expected-refusal question overridden to provider_unavailable, the
        # remaining expected-insufficient questions still abstain - and the
        # provider failures are never merged into either bucket.
        statuses = summary["status_breakdown"]
        assert statuses == {
            "answer": 20,
            "insufficient_evidence": 6,
            "provider_unavailable": 4,
        }


def test_generation_eval_rows_persisted(retrieval_db, questions) -> None:
    runner = ScriptedFakeGenerationRunner()
    with session_scope() as session:
        service = SearchService(session, fixture_provider())
        summary = evaluate_generation(questions, runner, service=service, session=session)
        eval_run_id = summary["eval_run_id"]
        row = session.get(EvalRun, eval_run_id)
        assert row is not None
        assert "scripted" in (row.notes or "").lower()
        assert "scripted-fake" in (row.config_json or "")
        results = (
            session.execute(select(EvalResult).where(EvalResult.eval_run_id == eval_run_id))
            .scalars()
            .all()
        )
        assert len(results) >= len(questions)  # >=1 metric row per question
        assert {r.question_id for r in results} == {q.id for q in questions}
