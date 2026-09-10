"""Unit tests for approval-gated export (SPEC §20 Approval, §26 security list).

An approval binds run id + exact memo content hash + export type + expiry;
changed content, expiry, or a wrong export type all invalidate it.
"""

from __future__ import annotations

import pytest
from agent_test_helpers import create_schema

from quarterline.agent import approval
from quarterline.agent.approval import (
    ApprovalDecision,
    approve,
    memo_content_hash,
    request_approval,
)
from quarterline.agent.checkpoints import persist_checkpoint
from quarterline.agent.state import initial_state
from quarterline.store.db import session_scope
from quarterline.store.repositories.runs import RunsRepo


@pytest.fixture
def approval_env(offline_env):
    create_schema()
    return offline_env


def _seed_run(session, run_id: str, memo_content: str) -> None:
    state = initial_state(
        run_id,
        {"ticker": "AAPL", "memo_type": "quarter_review"},
        tool_budget={"max_tool_calls": 4, "max_graph_transitions": 12},
        deadline_seconds=60.0,
        started_at_monotonic=0.0,
    )
    state["memo_content"] = memo_content
    state["memo_content_hash"] = memo_content_hash(memo_content)
    persist_checkpoint(session, state)


def test_content_hash_is_deterministic_sha256() -> None:
    assert memo_content_hash("memo body") == memo_content_hash("memo body")
    assert memo_content_hash("memo body") != memo_content_hash("memo body ")
    assert len(memo_content_hash("x")) == 64


def test_approval_flow_pending_then_approved(approval_env) -> None:
    run_id = "b" * 32
    with session_scope() as session:
        _seed_run(session, run_id, "# memo\n\nBody.")
        row = request_approval(session, run_id, "# memo\n\nBody.", "md")
        decision = approve(session, run_id, row.id, "# memo\n\nBody.", "md")

    assert decision.approved is True
    assert decision.export_type == "md"
    with session_scope() as session:
        assert RunsRepo(session).get_approval(row.id).status == "approved"


def test_approval_invalidated_after_content_change(approval_env) -> None:
    run_id = "c" * 32
    with session_scope() as session:
        _seed_run(session, run_id, "# memo\n\nOriginal validated content.")
        row = request_approval(session, run_id, "# memo\n\nOriginal validated content.", "md")
        decision = approve(session, run_id, row.id, "# memo\n\nTAMPERED content.", "md")

    assert decision.approved is False
    assert "content changed" in (decision.reason or "")


def test_expired_approval_rejected(approval_env) -> None:
    run_id = "d" * 32
    with session_scope() as session:
        _seed_run(session, run_id, "# memo\n\nBody.")
        row = request_approval(
            session,
            run_id,
            "# memo\n\nBody.",
            "md",
            ttl_seconds=-1.0,  # already expired at creation
        )
        decision = approve(session, run_id, row.id, "# memo\n\nBody.", "md")

    assert decision.approved is False
    assert "expired" in (decision.reason or "")


def test_wrong_export_type_rejected(approval_env) -> None:
    run_id = "e" * 32
    with session_scope() as session:
        _seed_run(session, run_id, "# memo\n\nBody.")
        row = request_approval(session, run_id, "# memo\n\nBody.", "md")
        decision = approve(session, run_id, row.id, "# memo\n\nBody.", "json")

    assert decision.approved is False
    assert "export type" in (decision.reason or "")


def test_unknown_or_foreign_request_rejected(approval_env) -> None:
    run_id = "f" * 32
    other_run = "1" * 32
    with session_scope() as session:
        _seed_run(session, run_id, "# memo\n\nBody.")
        row = request_approval(session, run_id, "# memo\n\nBody.", "md")
        foreign = approve(session, other_run, row.id, "# memo\n\nBody.", "md")
        unknown = approve(session, run_id, 999999, "# memo\n\nBody.", "md")

    assert foreign.approved is False and "not found for this run" in (foreign.reason or "")
    assert unknown.approved is False


def test_verify_export_approval_requires_approved_matching_hash(approval_env) -> None:
    run_id = "a" * 32
    content = "# memo\n\nBody."
    with session_scope() as session:
        _seed_run(session, run_id, content)
        none_yet = approval.verify_export_approval(session, run_id, "md")
        row = request_approval(session, run_id, content, "md")
        unapproved = approval.verify_export_approval(session, run_id, "md")
        approve(session, run_id, row.id, content, "md")
        approved = approval.verify_export_approval(session, run_id, "md")
        wrong_type = approval.verify_export_approval(session, run_id, "json")

    assert none_yet.approved is False
    assert unapproved.approved is False
    assert approved.approved is True
    assert approved.memo_content_hash == memo_content_hash(content)
    assert wrong_type.approved is False


def test_verify_rejects_when_stored_memo_changed_after_approval(approval_env) -> None:
    """Memo tampered AFTER approval: hash no longer matches the checkpoint."""
    run_id = "9" * 32
    content = "# memo\n\nOriginal."
    with session_scope() as session:
        _seed_run(session, run_id, content)
        row = request_approval(session, run_id, content, "md")
        approve(session, run_id, row.id, content, "md")
    with session_scope() as session:
        from quarterline.agent.checkpoints import load_state

        state = load_state(session, run_id)
        rewritten = "# memo\n\nRewritten after approval."
        state["memo_content"] = rewritten
        state["memo_content_hash"] = memo_content_hash(rewritten)
        persist_checkpoint(session, state)
        decision = approval.verify_export_approval(session, run_id, "md")

    assert decision.approved is False
    assert "content changed" in (decision.reason or "")


def test_request_without_expiry_information_rejects(approval_env) -> None:
    """A row with no expiry cannot be validated -> refused (defensive)."""
    run_id = "7" * 32
    with session_scope() as session:
        _seed_run(session, run_id, "# memo\n\nBody.")
        repo = RunsRepo(session)
        row = repo.create_approval(
            run_id, memo_content_hash("# memo\n\nBody."), "md", expires_at=None
        )  # type: ignore[arg-type]
        decision = approve(session, run_id, row.id, "# memo\n\nBody.", "md")

    assert decision.approved is False
    assert "expired" in (decision.reason or "")
    assert isinstance(decision, ApprovalDecision)
