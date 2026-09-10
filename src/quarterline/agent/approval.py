"""Approval-gated export (SPEC §20 Approval; §26 security tests).

An approval is tied to ALL of:

- the run id;
- the SHA-256 of the EXACT validated memo content (the canonical markdown
  rendering of the memo — the same bytes the export writes);
- the export type (``md`` | ``json``);
- an expiry timestamp.

:meth:`approve` recomputes the hash over the content presented for approval —
if the memo changed since :meth:`request_approval`, the previous approval is
INVALID and the export is refused. Expired requests are rejected. Only a valid
approval lets the ``export_memo`` tool write (verified again inside the tool
handler — defense in depth).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from quarterline.store.repositories.runs import RunsRepo

#: Default approval lifetime (no SPEC constant; documented engineering default).
DEFAULT_APPROVAL_TTL_SECONDS = 3600.0


def memo_content_hash(content: str) -> str:
    """SHA-256 hex of the exact memo content (SPEC §20: content hash)."""
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


@dataclass
class ApprovalDecision:
    """Outcome of an approval request/approval/verification (testable)."""

    approved: bool
    reason: str | None = None
    request_id: int | None = None
    export_type: str | None = None
    memo_content_hash: str | None = None


def _expired(expires_at: datetime | None, now: datetime) -> bool:
    if expires_at is None:
        return True  # no expiry recorded -> refuse (cannot validate)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= now


def request_approval(
    session: Session,
    run_id: str,
    memo_content: str,
    export_type: str,
    *,
    ttl_seconds: float = DEFAULT_APPROVAL_TTL_SECONDS,
    now: datetime | None = None,
) -> object:
    """Create one approval request bound to run + content hash + type + expiry."""
    moment = now or datetime.now(UTC)
    repo = RunsRepo(session)
    return repo.create_approval(
        run_id,
        memo_content_hash(memo_content),
        export_type,
        expires_at=moment + timedelta(seconds=ttl_seconds),
    )


def approve(
    session: Session,
    run_id: str,
    request_id: int,
    memo_content: str,
    export_type: str,
    *,
    now: datetime | None = None,
) -> ApprovalDecision:
    """Validate and grant one approval request (SPEC §20 Approval).

    The approval is INVALID when the request is unknown, belongs to another
    run, was already decided, is expired, targets a different export type, or
    the memo content hash no longer matches (the memo changed since the
    request).
    """
    moment = now or datetime.now(UTC)
    repo = RunsRepo(session)
    row = repo.get_approval(request_id)
    if row is None or row.run_id != run_id:
        return ApprovalDecision(approved=False, reason="approval request not found for this run")
    if row.status == "approved":
        # Idempotent re-approval of the SAME content is fine; different content
        # is not (the hash check below still applies).
        pass
    elif row.status != "pending":
        return ApprovalDecision(
            approved=False, reason=f"approval request is {row.status}, not pending"
        )
    if _expired(row.expires_at, moment):
        return ApprovalDecision(
            approved=False, reason="approval request expired", request_id=request_id
        )
    if export_type and row.export_type != export_type:
        return ApprovalDecision(
            approved=False,
            reason=f"approval was requested for export type {row.export_type!r}, "
            f"not {export_type!r}",
            request_id=request_id,
        )
    presented = memo_content_hash(memo_content)
    if presented != row.memo_content_hash:
        return ApprovalDecision(
            approved=False,
            reason="memo content changed since the approval request; previous approval "
            "is invalid (content-hash mismatch)",
            request_id=request_id,
        )
    repo.decide_approval(request_id, "approved", now=moment)
    return ApprovalDecision(
        approved=True,
        request_id=request_id,
        export_type=row.export_type,
        memo_content_hash=row.memo_content_hash,
    )


def verify_export_approval(
    session: Session,
    run_id: str,
    export_type: str,
    *,
    now: datetime | None = None,
) -> ApprovalDecision:
    """The check the ``export_memo`` tool runs before writing any file.

    An approved, unexpired request must exist for (run_id, export_type) and its
    content hash must still equal the hash of the checkpointed memo content —
    so a memo tampered with AFTER approval still cannot be exported.
    """
    from quarterline.agent.checkpoints import load_state

    moment = now or datetime.now(UTC)
    repo = RunsRepo(session)
    state = load_state(session, run_id)
    if state is None or not state.get("memo_content_hash"):
        return ApprovalDecision(approved=False, reason="no checkpointed memo for this run")
    stored_hash = state["memo_content_hash"]
    for row in repo.list_approvals(run_id):
        if row.export_type != export_type:
            continue
        if row.status != "approved":
            continue
        if _expired(row.expires_at, moment):
            return ApprovalDecision(approved=False, reason="approval expired", request_id=row.id)
        if row.memo_content_hash != stored_hash:
            return ApprovalDecision(
                approved=False,
                reason="memo content changed since the approval request; previous "
                "approval is invalid",
                request_id=row.id,
            )
        return ApprovalDecision(
            approved=True,
            request_id=row.id,
            export_type=row.export_type,
            memo_content_hash=row.memo_content_hash,
        )
    return ApprovalDecision(
        approved=False,
        reason=f"no approved, unexpired approval for run {run_id} / export type {export_type!r}",
    )


__all__ = [
    "DEFAULT_APPROVAL_TTL_SECONDS",
    "ApprovalDecision",
    "approve",
    "memo_content_hash",
    "request_approval",
    "verify_export_approval",
]
