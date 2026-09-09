"""Evaluation dataset loading and validation (SPEC §21).

Dataset file: ``data/eval/questions.jsonl`` — one JSON object per line with
the SPEC §21 fields:

``id, ticker, question, task_type, period_end, answerable, expected_behavior,
relevant_evidence [{document_id, section, start_offset, end_offset}, ...],
required_concepts, reviewed``.

Conventions used by this implementation (documented deviations kept minimal):

- ``document_id`` is the STABLE EDGAR accession string (dashed form, e.g.
  ``0000320193-26-000011``), never the per-database integer row id. The
  harness resolves it to the ``documents`` row via the accession column, so
  gold evidence survives database rebuilds and is chunking-strategy
  independent (SPEC §21: spans, not chunk ids).
- Each span may carry an ``anchor`` — the exact quoted passage that must
  appear inside ``document_text[start:end]``. This makes "reviewed" mean
  machine-checkable: every reviewed answerable question's spans are verified
  against the ingested cleaned document text (see :func:`verify_spans`).
- A ``notes`` field is allowed for review provenance (how the span was
  verified, by whom, when).

The three abstention kinds are DISTINCT events (SPEC §21) and are modeled as
three distinct :class:`ExpectedBehavior` values:

- ``insufficient_evidence`` — evidence policy found no usable evidence;
- ``refusal`` — evidence exists but the request is out of policy (e.g.
  investment advice);
- provider failure is never an expected dataset behavior; it is an
  operational outcome measured by generation metrics (SPEC §22).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

#: The three expected behaviors (SPEC §21). Provider failure is NOT one.
ExpectedBehavior = str  # literal values: "answer" | "insufficient_evidence" | "refusal"
EXPECTED_BEHAVIORS = ("answer", "insufficient_evidence", "refusal")

#: SPEC §21 category coverage. Each category must appear at least once across
#: the reviewed dataset (checked via :func:`missing_categories`); task_type
#: and expected_behavior jointly tag the category.
REQUIRED_CATEGORIES: dict[str, set[str]] = {
    "management_explanation": {"answer"},
    "cash_flow": {"answer"},
    "capital_allocation": {"answer"},
    "risks": {"answer"},
    "period_specific": {"answer"},
    "cross_period_comparison": {"answer"},
    "wrong_company_trap": {"insufficient_evidence"},
    "unanswerable": {"insufficient_evidence"},
    "advice_request": {"refusal"},
}


class GoldSpan(BaseModel):
    """One span-anchored gold evidence unit (SPEC §21)."""

    model_config = ConfigDict(extra="forbid")

    document_id: str  # stable accession string (see module docstring)
    section: str | None = None
    start_offset: int
    end_offset: int
    #: Exact quoted passage that must occur inside text[start:end] — the
    #: machine-checkable review receipt for the offsets.
    anchor: str | None = None


class EvalQuestion(BaseModel):
    """One reviewed evaluation question (SPEC §21 schema)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    ticker: str
    question: str
    task_type: str
    period_end: date | None = None
    answerable: bool
    expected_behavior: ExpectedBehavior
    relevant_evidence: list[GoldSpan] = Field(default_factory=list)
    required_concepts: list[str] = Field(default_factory=list)
    reviewed: bool
    notes: str | None = None

    @model_validator(mode="after")
    def _validate_behavior(self) -> EvalQuestion:
        if self.expected_behavior not in EXPECTED_BEHAVIORS:
            raise ValueError(
                f"{self.id}: expected_behavior must be one of {EXPECTED_BEHAVIORS}, "
                f"got {self.expected_behavior!r}"
            )
        if self.answerable and self.expected_behavior != "answer":
            raise ValueError(f"{self.id}: answerable=true requires expected_behavior='answer'")
        if not self.answerable and self.expected_behavior == "answer":
            raise ValueError(f"{self.id}: answerable=false cannot expect 'answer'")
        if self.answerable and not self.relevant_evidence:
            raise ValueError(f"{self.id}: answerable questions need >=1 gold span")
        for span in self.relevant_evidence:
            if span.end_offset <= span.start_offset:
                raise ValueError(
                    f"{self.id}: span offsets inverted ({span.start_offset}>={span.end_offset})"
                )
        return self


class DatasetValidationError(ValueError):
    """The dataset file violates the SPEC §21 schema or review rules."""


def load_dataset(path: str | Path) -> list[EvalQuestion]:
    """Load + validate a questions JSONL file.

    Raises :class:`DatasetValidationError` on: malformed JSON, duplicate ids,
    unreviewed answerable questions, unreviewed questions without verified
    spans, or answerable questions that lack gold spans.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise DatasetValidationError(f"dataset file not found: {file_path}")

    questions: list[EvalQuestion] = []
    seen_ids: set[str] = set()
    for line_number, raw in enumerate(file_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise DatasetValidationError(f"line {line_number}: invalid JSON ({exc})") from exc
        try:
            question = EvalQuestion.model_validate(payload)
        except ValueError as exc:
            raise DatasetValidationError(f"line {line_number}: {exc}") from exc
        if question.id in seen_ids:
            raise DatasetValidationError(
                f"line {line_number}: duplicate question id {question.id!r}"
            )
        if not question.reviewed:
            raise DatasetValidationError(
                f"line {line_number}: {question.id!r} is not reviewed — released gold "
                "examples must all be reviewed (SPEC §21)"
            )
        seen_ids.add(question.id)
        questions.append(question)

    if not questions:
        raise DatasetValidationError(f"dataset is empty: {file_path}")
    return questions


def missing_categories(questions: list[EvalQuestion]) -> list[str]:
    """SPEC §21 categories not represented in the reviewed dataset."""
    present = {(question.task_type, question.expected_behavior) for question in questions}
    return sorted(
        category
        for category, behaviors in REQUIRED_CATEGORIES.items()
        if not any((category, behavior) in present for behavior in behaviors)
    )


def resolve_document_ids(session, questions: list[EvalQuestion]) -> dict[str, int]:
    """Map dataset accession strings to ``documents.id`` values.

    Accessions are compared dash-stripped (the pipeline stores them without
    dashes). Raises :class:`DatasetValidationError` when a gold document is
    not present in the store — never silently skip gold evidence.
    """
    from quarterline.store.models import Document

    wanted = {
        span.document_id.replace("-", "")
        for question in questions
        for span in question.relevant_evidence
    }
    found: dict[str, int] = {}
    for accession in wanted:
        row = session.execute(
            select(Document.id).where(Document.accession == accession).limit(1)
        ).scalar_one_or_none()
        if row is None:
            raise DatasetValidationError(
                f"gold document {accession!r} not found in the store — "
                "the fixture corpus must be ingested before evaluation"
            )
        found[accession] = int(row)
    return found


def span_text(session, document_id: int, start_offset: int, end_offset: int) -> str:
    """Return ``cleaned_document_text[start:end]`` for a gold span.

    Section rows store the exact slice ``cleaned.text[section.start:section.end]``
    (roundtrip invariant of the ingestion pipeline), so the containing
    section's local slice reconstructs the span exactly — no full-document
    reconstruction or contiguity assumption needed.
    """
    from quarterline.store.models import Section

    row = session.execute(
        select(Section.start_offset, Section.end_offset, Section.text)
        .where(
            Section.document_id == document_id,
            Section.start_offset <= start_offset,
            Section.end_offset >= end_offset,
        )
        .order_by(Section.start_offset.asc())
        .limit(1)
    ).first()
    if row is None or row.text is None:
        raise DatasetValidationError(
            f"no section of document {document_id} covers span [{start_offset}:{end_offset})"
        )
    section_start, section_end, text = row
    if not (section_start <= start_offset <= end_offset <= section_end):
        raise DatasetValidationError(
            f"section [{section_start}:{section_end}) does not contain "
            f"[{start_offset}:{end_offset})"
        )
    return text[start_offset - section_start : end_offset - section_start]


def verify_spans(session, questions: list[EvalQuestion]) -> list[str]:
    """Assert every gold span resolves and its anchor occurs in the span.

    Returns a list of human-readable verification receipts (one per span).
    Raises :class:`DatasetValidationError` on any unverifiable span — this is
    the machine check that makes ``reviewed: true`` meaningful.
    """
    doc_ids = resolve_document_ids(session, questions)
    receipts: list[str] = []
    for question in questions:
        for span in question.relevant_evidence:
            document_id = doc_ids[span.document_id.replace("-", "")]
            text = span_text(session, document_id, span.start_offset, span.end_offset)
            if span.anchor and span.anchor not in text:
                raise DatasetValidationError(
                    f"{question.id}: anchor not found inside "
                    f"{span.document_id}[{span.start_offset}:{span.end_offset}]"
                )
            receipts.append(
                f"{question.id}: {span.document_id}[{span.start_offset}:{span.end_offset}] verified"
            )
    return receipts


__all__ = [
    "EXPECTED_BEHAVIORS",
    "REQUIRED_CATEGORIES",
    "DatasetValidationError",
    "EvalQuestion",
    "ExpectedBehavior",
    "GoldSpan",
    "load_dataset",
    "missing_categories",
    "resolve_document_ids",
    "span_text",
    "verify_spans",
]
