"""Unit tests for the evaluation dataset loader/validator (SPEC §21)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from quarterline.eval.dataset import (
    DatasetValidationError,
    EvalQuestion,
    load_dataset,
    missing_categories,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
QUESTIONS_PATH = REPO_ROOT / "data" / "eval" / "questions.jsonl"


def _write_dataset(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "questions.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
    return path


def _base_entry(**overrides) -> dict:
    entry = {
        "id": "q-1",
        "ticker": "AAPL",
        "question": "What drove revenue?",
        "task_type": "management_explanation",
        "period_end": "2026-03-28",
        "answerable": True,
        "expected_behavior": "answer",
        "relevant_evidence": [
            {
                "document_id": "0000320193-26-000011",
                "section": "earnings_release",
                "start_offset": 447,
                "end_offset": 1055,
            }
        ],
        "required_concepts": ["revenue"],
        "reviewed": True,
    }
    entry.update(overrides)
    return entry


def test_committed_dataset_loads_and_is_fully_reviewed() -> None:
    questions = load_dataset(QUESTIONS_PATH)
    assert len(questions) >= 10  # SPEC §21: initial development = 10 reviewed
    assert all(question.reviewed for question in questions)
    ids = [question.id for question in questions]
    assert len(ids) == len(set(ids))

    behaviors = {question.expected_behavior for question in questions}
    assert behaviors == {"answer", "insufficient_evidence", "refusal"}
    # The three abstention kinds are distinct events, all present in the set.
    answerable = [q for q in questions if q.answerable]
    assert all(q.relevant_evidence for q in answerable)
    wrong_company = [q for q in questions if q.task_type == "wrong_company_trap"]
    assert wrong_company and wrong_company[0].ticker != "AAPL"


def test_committed_dataset_covers_every_spec_category() -> None:
    questions = load_dataset(QUESTIONS_PATH)
    assert missing_categories(questions) == []


def test_duplicate_ids_rejected(tmp_path: Path) -> None:
    path = _write_dataset(tmp_path, [_base_entry(), _base_entry()])
    with pytest.raises(DatasetValidationError, match="duplicate question id"):
        load_dataset(path)


def test_unreviewed_question_rejected(tmp_path: Path) -> None:
    path = _write_dataset(tmp_path, [_base_entry(reviewed=False)])
    with pytest.raises(DatasetValidationError, match="not reviewed"):
        load_dataset(path)


def test_answerable_question_without_spans_rejected(tmp_path: Path) -> None:
    path = _write_dataset(tmp_path, [_base_entry(relevant_evidence=[])])
    with pytest.raises(DatasetValidationError, match="need >=1 gold span"):
        load_dataset(path)


def test_answerable_expectation_mismatch_rejected(tmp_path: Path) -> None:
    path = _write_dataset(tmp_path, [_base_entry(expected_behavior="refusal")])
    with pytest.raises(DatasetValidationError, match="answerable=true"):
        load_dataset(path)


def test_unknown_expected_behavior_rejected(tmp_path: Path) -> None:
    # Provider failure is an OUTCOME, never a dataset expectation (SPEC §21).
    path = _write_dataset(tmp_path, [_base_entry(expected_behavior="provider_failure")])
    with pytest.raises(DatasetValidationError, match="expected_behavior must be one of"):
        load_dataset(path)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(DatasetValidationError, match="not found"):
        load_dataset(tmp_path / "nope.jsonl")


def test_missing_category_detection() -> None:
    questions = [EvalQuestion.model_validate(_base_entry())]
    missing = missing_categories(questions)
    assert "advice_request" in missing
    assert "management_explanation" not in missing


def test_extra_fields_are_forbidden(tmp_path: Path) -> None:
    entry = _base_entry(unexpected_field=1)
    path = _write_dataset(tmp_path, [entry])
    with pytest.raises(DatasetValidationError):
        load_dataset(path)
