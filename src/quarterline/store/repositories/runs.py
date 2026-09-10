"""Agent-run persistence (SPEC §9.9 ``agent_runs`` / ``agent_tool_calls`` /
``approval_requests``; SPEC §20 memory/state).

The agent wave's audit trail: one ``agent_runs`` row per run carries the FULL
checkpointed state snapshot (``state_json``) plus the transition and tool-call
counters; every tool call appends an ``agent_tool_calls`` row written BEFORE
execution (status ``running``) and updated AFTER it (status, result, duration);
export approvals are ``approval_requests`` rows tied to run id + memo content
hash + export type + expiry (SPEC §20 Approval).

Like every repo (contract C5), this is a thin typed data-access object: no raw
SQL escapes past this layer.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from quarterline.store.models import AgentRun, AgentToolCall, ApprovalRequest


class RunsRepo:
    """Typed persistence for agent runs, tool-call audit rows and approvals."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # -- agent_runs -----------------------------------------------------------

    def get_run(self, run_id: str) -> AgentRun | None:
        return self.session.scalar(select(AgentRun).where(AgentRun.run_id == run_id).limit(1))

    def upsert_run(
        self,
        run_id: str,
        *,
        company_id: int | None,
        intent: str | None,
        status: str | None,
        tool_call_count: int,
        transition_count: int,
        state_json: str,
        error_type: str | None = None,
    ) -> AgentRun:
        """Insert or refresh the single checkpoint row for ``run_id``."""
        row = self.get_run(run_id)
        if row is None:
            row = AgentRun(run_id=run_id)
            self.session.add(row)
        row.company_id = company_id
        row.intent = intent
        row.status = status
        row.tool_call_count = tool_call_count
        row.transition_count = transition_count
        row.state_json = state_json
        row.error_type = error_type
        self.session.flush()
        return row

    def state_of(self, run_id: str) -> dict[str, Any] | None:
        """The persisted checkpoint snapshot for ``run_id`` (or None)."""
        row = self.get_run(run_id)
        if row is None or not row.state_json:
            return None
        try:
            parsed = json.loads(row.state_json)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None

    # -- agent_tool_calls (audit trail) ----------------------------------------

    def start_tool_call(
        self,
        agent_run_id: int,
        sequence: int,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        status: str = "running",
    ) -> int:
        """Insert the audit row BEFORE execution (or at rejection time).

        ``arguments_json`` embeds the sha256 args hash + the typed arguments so
        the audit trail can prove what the tool was asked to do even when the
        run dies mid-call. Rejected attempts (unknown tool, budget exceeded)
        are recorded with their rejection status and never execute.
        """
        import hashlib

        payload = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
        args_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        row = AgentToolCall(
            agent_run_id=agent_run_id,
            sequence=sequence,
            tool_name=tool_name,
            arguments_json=json.dumps(
                {"args_sha256": args_hash, "arguments": arguments},
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            ),
            status=status,
        )
        self.session.add(row)
        self.session.flush()
        return row.id

    def finish_tool_call(
        self,
        call_id: int,
        *,
        status: str,
        result: dict[str, Any] | None,
        duration_ms: float,
        error: str | None = None,
    ) -> None:
        """Update the audit row AFTER execution with outcome + duration.

        ``result_json`` carries the JSON-safe result, ``duration_ms`` and the
        error text (the table has no dedicated duration column, so the timing
        travels inside ``result_json`` — reported as a wave gap).
        """
        row = self.session.get(AgentToolCall, call_id)
        if row is None:  # pragma: no cover - only if the row was deleted
            return
        row.status = status
        row.result_json = json.dumps(
            {"duration_ms": round(duration_ms, 3), "error": error, "result": result},
            ensure_ascii=False,
            default=str,
        )
        self.session.flush()

    def list_tool_calls(self, agent_run_id: int) -> list[AgentToolCall]:
        """Ordered audit trail for one run (sequence ASC)."""
        return list(
            self.session.scalars(
                select(AgentToolCall)
                .where(AgentToolCall.agent_run_id == agent_run_id)
                .order_by(AgentToolCall.sequence.asc(), AgentToolCall.id.asc())
            ).all()
        )

    # -- approval_requests ------------------------------------------------------

    def create_approval(
        self, run_id: str, memo_content_hash: str, export_type: str, expires_at: datetime
    ) -> ApprovalRequest:
        row = ApprovalRequest(
            run_id=run_id,
            memo_content_hash=memo_content_hash,
            export_type=export_type,
            status="pending",
            expires_at=expires_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get_approval(self, request_id: int) -> ApprovalRequest | None:
        return self.session.get(ApprovalRequest, request_id)

    def list_approvals(self, run_id: str) -> list[ApprovalRequest]:
        return list(
            self.session.scalars(
                select(ApprovalRequest)
                .where(ApprovalRequest.run_id == run_id)
                .order_by(ApprovalRequest.id.asc())
            ).all()
        )

    def decide_approval(self, request_id: int, status: str, *, now: datetime | None) -> None:
        row = self.session.get(ApprovalRequest, request_id)
        if row is None:  # pragma: no cover - callers resolve the row first
            return
        row.status = status
        row.decided_at = now or datetime.now(UTC)
        self.session.flush()


__all__ = ["RunsRepo"]
