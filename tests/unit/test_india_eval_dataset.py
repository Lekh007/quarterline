"""India evaluation dataset tests (IND-7, offline).

The promoted dataset (``data/eval/india_questions.jsonl``) must:

- load through the US loader (:func:`quarterline.eval.dataset.load_dataset`)
  unchanged — same schema, all rows reviewed;
- carry anchors that RE-VERIFY against their coordinate systems: narrative
  gold spans slice the ingested extracted page text (mirroring the US
  ``span_text`` contract), fact gold spans slice the committed fixture XML /
  sidecar text exactly;
- keep the India category coverage, with every NOT-promoted draft question
  explicitly recorded with its verified reason (honest failures are findings).

The sidecar file (``*.filing.json``) is storage-only (gitignored), so its
span check follows the IND-6 cache-absent skip convention; the revision
status is ALSO verified through the canonical metadata path, which needs only
committed fixtures.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from india_test_helpers import create_schema
from sqlalchemy import select

from quarterline.eval.dataset import load_dataset
from quarterline.sources.india.narrative import (
    ingest_narratives,
    narrative_entries,
)
from quarterline.store.db import session_scope
from quarterline.store.models import Document, FactObservation, Section, SourceArtifact

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_PATH = REPO_ROOT / "data" / "eval" / "india_questions.jsonl"
DRAFTS_PATH = REPO_ROOT / "data" / "eval" / "india_draft_questions.jsonl"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "india"


def _load_runner_module():
    """Import scripts/eval_india.py for its NOT_PROMOTED registry."""
    spec = importlib.util.spec_from_file_location(
        "eval_india_module", REPO_ROOT / "scripts" / "eval_india.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cache_present() -> bool:
    return all((REPO_ROOT / entry["path"]).is_file() for entry in narrative_entries())


def test_promoted_dataset_loads_via_us_loader():
    questions = load_dataset(DATASET_PATH)
    assert len(questions) == 13
    assert all(q.reviewed for q in questions)


def test_every_promoted_row_traces_to_a_draft():
    drafts = [
        json.loads(line)
        for line in DRAFTS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith('{"id": "_dataset_meta"')
    ]
    draft_ids = {d["id"] for d in drafts}
    promoted = {q.id for q in load_dataset(DATASET_PATH)}
    runner = _load_runner_module()
    unpromoted_ids = {entry["id"] for entry in runner.NOT_PROMOTED}
    assert promoted | unpromoted_ids == draft_ids
    assert not (promoted & unpromoted_ids)
    for entry in runner.NOT_PROMOTED:
        assert len(entry["reason"]) > 40, "an unpromoted row must carry its verified reason"


def _document_by_file(session, file_name: str) -> Document | None:
    return session.scalars(
        select(Document)
        .join(SourceArtifact, SourceArtifact.id == Document.source_artifact_id)
        .where(SourceArtifact.local_path.like(f"%{file_name}"))
    ).first()


def _ingested_document_text(session, file_name: str) -> str | None:
    """Assembled extracted text (the pdf_extract coordinate system), or None."""
    document = _document_by_file(session, file_name)
    if document is None:
        return None
    sections = list(
        session.scalars(
            select(Section).where(Section.document_id == document.id).order_by(Section.start_offset)
        )
    )
    assembled = ""
    for number, section in enumerate(sections):
        if number:
            assembled += "\n"  # pdf_extract's page separator
        assert section.start_offset == len(assembled)
        assembled += section.text or ""
    return assembled


@pytest.mark.skipif(
    not cache_present(),
    reason="India narrative storage cache absent (gitignored); the IND-7 "
    "ingestion run verified these spans against the live extraction",
)
def test_narrative_gold_spans_reverify_against_ingested_extracted_text(offline_env):
    create_schema()
    ingest_narratives()
    questions = load_dataset(DATASET_PATH)
    verified = 0
    with session_scope() as session:
        for question in questions:
            for span in question.relevant_evidence:
                if not span.document_id.endswith(".pdf"):
                    continue
                text = _ingested_document_text(session, span.document_id)
                assert text is not None, f"{span.document_id} not ingested"
                slice_ = text[span.start_offset : span.end_offset]
                assert span.anchor in slice_, (
                    f"{question.id}: anchor not inside {span.document_id}"
                    f"[{span.start_offset}:{span.end_offset}]"
                )
                verified += 1
    assert verified >= 3, "the narrative questions' spans must all re-verify"


def test_fact_gold_spans_reverify_against_committed_fixture_text():
    questions = load_dataset(DATASET_PATH)
    verified = 0
    for question in questions:
        for span in question.relevant_evidence:
            if span.document_id.endswith(".pdf"):
                continue
            if span.document_id.endswith(".filing.json"):
                path = (
                    REPO_ROOT
                    / "storage"
                    / "raw"
                    / "india"
                    / "infosys"
                    / "q1_fy2026-27"
                    / span.document_id
                )
                if not path.is_file():
                    pytest.skip(
                        "import sidecar absent (gitignored storage); canonical "
                        "metadata check covers the revision status"
                    )
            else:
                path = FIXTURES_DIR / span.document_id
            text = path.read_text(encoding="utf-8")
            assert text[span.start_offset : span.end_offset] == span.anchor, (
                f"{question.id}: fixture anchor drift at "
                f"{span.document_id}[{span.start_offset}:{span.end_offset}]"
            )
            verified += 1
    assert verified >= 12, "the fact questions' spans must all re-verify"


def test_revision_status_verified_through_canonical_metadata():
    """The revision-status row's load-bearing verification: the real import
    path carries Original + seq 177385 on the INFY Q1 consolidated observations
    (committed fixtures only; no storage cache needed)."""
    import datetime as dt

    from india_test_helpers import import_all_fixtures

    create_schema()
    import_all_fixtures()
    from quarterline.sources.india.pipeline import ingest_observations

    ingest_observations("IN-INFY")
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(FactObservation).where(
                    FactObservation.reporting_scope == "consolidated",
                    FactObservation.period_end == dt.date(2026, 6, 30),
                    FactObservation.accession == "177385",
                )
            )
        )
        assert rows, "no INFY Q1 FY27 consolidated observations under seq 177385"
        for obs in rows:
            metadata = json.loads(obs.context_metadata_json)
            assert metadata["revision_status"] == "Original"
            assert metadata["seq_id"] == "177385"


def test_india_category_coverage():
    """India plan categories present in the promoted set; the two coverage
    gaps (wrong-company trap, standalone direction) are explicitly unpromoted
    with verified reasons, never silently missing."""
    questions = load_dataset(DATASET_PATH)
    present = {(q.task_type, q.expected_behavior) for q in questions}
    required = {
        ("scope_retrieval", "answer"),  # consolidated direction
        ("quarter_vs_cumulative", "answer"),
        ("unit_normalization", "answer"),
        ("exceptional_items", "answer"),
        ("missing_quarterly_cash_flow", "insufficient_evidence"),
        ("advice_request", "refusal"),
        ("cash_flow", "answer"),
        ("management_explanation", "answer"),
        ("cross_period_comparison", "answer"),
        ("revision_status", "answer"),
    }
    assert required <= present
    task_types = {q.task_type for q in questions}
    assert "wrong_company_trap" not in task_types
    runner = _load_runner_module()
    reasons = {entry["id"]: entry["reason"] for entry in runner.NOT_PROMOTED}
    assert "itc-q1fy27-revenue-wrong-company-trap" in reasons
    assert "infy-q1fy27-standalone-revenue-scope-trap" in reasons
