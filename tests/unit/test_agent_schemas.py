"""Unit tests for agent schemas (SPEC §17 rules applied to memos, §20)."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from quarterline.agent.schemas import (
    AgentRunResult,
    MemoDraft,
    MemoOutput,
    MemoRequest,
    MemoSection,
    memo_content_markdown,
    memo_json_schema,
)


def test_memo_request_rejects_unknown_memo_type() -> None:
    with pytest.raises(ValidationError):
        MemoRequest.model_validate({"ticker": "AAPL", "memo_type": "shill_review"})


def test_memo_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        MemoRequest.model_validate(
            {"ticker": "AAPL", "memo_type": "quarter_review", "instructions": "do X"}
        )


def test_memo_request_accepts_question_or_topic() -> None:
    request = MemoRequest.model_validate(
        {"ticker": "AAPL", "memo_type": "risk_review", "topic": "supply chain"}
    )
    assert request.focus_text == "supply chain"
    assert (
        MemoRequest.model_validate(
            {"ticker": "AAPL", "memo_type": "quarter_review", "question": "What changed?"}
        ).focus_text
        == "What changed?"
    )


def test_memo_output_rejects_invented_metric_ids() -> None:
    with pytest.raises(ValidationError) as excinfo:
        MemoOutput.model_validate(
            {
                "status": "ok",
                "label_echo": "Strong",
                "metric_mentions": [
                    {"metric_id": "revenue_guess_42", "template": "reported_change"}
                ],
                "sections": [],
            }
        )
    assert "allowlist" in str(excinfo.value)


def test_memo_output_rejects_smuggled_fields() -> None:
    with pytest.raises(ValidationError):
        MemoOutput.model_validate(
            {
                "status": "ok",
                "label_echo": None,
                "metric_mentions": [],
                "sections": [],
                "raw_html": "<script>alert(1)</script>",
            }
        )


def test_memo_output_rejects_unknown_headings() -> None:
    with pytest.raises(ValidationError):
        MemoOutput.model_validate(
            {
                "status": "ok",
                "label_echo": None,
                "metric_mentions": [],
                "sections": [{"heading": "buy_now_analysis", "text": "x", "evidence_ids": []}],
            }
        )


def test_memo_json_schema_matches_documented_sections() -> None:
    schema = memo_json_schema()
    assert schema["properties"]["sections"]["items"]["properties"]["heading"]["enum"] == [
        "overview",
        "what_changed",
        "management_explanation",
        "risks_and_open_questions",
        "evidence_gaps",
    ]
    assert schema["additionalProperties"] is False


def test_memo_content_markdown_is_deterministic() -> None:
    draft = MemoDraft(
        status="ok",
        title="AAPL quarter review — 2026-06-27",
        label_echo="Strong",
        sections=[
            MemoSection(
                heading="overview",
                text="A statement. [ev-0123456789ab]",
                evidence_ids=["ev-0123456789ab"],
            )
        ],
    )
    first = memo_content_markdown(draft)
    second = memo_content_markdown(draft.model_copy())
    assert first == second
    assert hashlib.sha256(first.encode()).hexdigest() == hashlib.sha256(second.encode()).hexdigest()
    assert "A statement. [ev-0123456789ab]" in first


def test_agent_run_result_statuses() -> None:
    for status in ("completed", "failed", "awaiting_approval", "refused", "insufficient_evidence"):
        result = AgentRunResult(run_id="a" * 32, status=status)  # type: ignore[arg-type]
        assert result.status == status
    with pytest.raises(ValidationError):
        AgentRunResult(run_id="a" * 32, status="wing_it")  # type: ignore[arg-type]
