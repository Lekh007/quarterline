"""Persistent per-run checkpoints (SPEC §20 Memory/state).

Every node transition persists the FULL state snapshot + transition counter to
``agent_runs.state_json``; every tool call appends to the ``agent_tool_calls``
audit trail before/after execution. There is NO cross-user or conversational
memory: a run is exactly its checkpoint row, so a crashed process can resume
from the last snapshot (:func:`resume_run` in :mod:`quarterline.agent.graph`)
without re-calling tools.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from quarterline.agent.schemas import (
    AgentBudgets,
    AgentRunResult,
    BudgetUse,
    ToolCallLogEntry,
)
from quarterline.agent.state import AgentState
from quarterline.store.repositories.runs import RunsRepo


def persist_checkpoint(session: Session, state: AgentState) -> int:
    """Upsert the ``agent_runs`` row with the full state snapshot (post-node)."""
    row = RunsRepo(session).upsert_run(
        state["run_id"],
        company_id=state.get("company_id"),
        intent=state.get("intent"),
        status=state.get("status"),
        tool_call_count=int(state.get("tool_call_count", 0)),
        transition_count=int(state.get("transition_count", 0)),
        state_json=json.dumps(dict(state), ensure_ascii=False, default=str),
        error_type=_first_error_type(state),
    )
    return row.id


def _first_error_type(state: AgentState) -> str | None:
    for error in state.get("errors") or []:
        if isinstance(error, str) and error:
            return error[:120]
    return None


def ensure_run_row(session: Session, state: AgentState) -> int:
    """Create the ``agent_runs`` row before the first node runs."""
    return persist_checkpoint(session, state)


def load_state(session: Session, run_id: str) -> AgentState | None:
    """The last checkpoint snapshot for ``run_id`` (None when unknown)."""
    state = RunsRepo(session).state_of(run_id)
    return AgentState(**state) if state else None


def log_tool_call(
    session: Session, run_row_id: int, sequence: int, tool_name: str, arguments: dict[str, Any]
) -> int:
    """Insert the BEFORE-execution audit row; returns its id."""
    return RunsRepo(session).start_tool_call(run_row_id, sequence, tool_name, arguments)


def finish_tool_call(
    session: Session,
    call_id: int,
    *,
    status: str,
    result: dict[str, Any] | None,
    duration_ms: float,
    error: str | None = None,
) -> None:
    """Update the AFTER-execution audit row."""
    RunsRepo(session).finish_tool_call(
        call_id, status=status, result=result, duration_ms=duration_ms, error=error
    )


def list_tool_calls(session: Session, run_id: str) -> list[dict[str, Any]]:
    """The ordered audit trail as JSON-safe dicts (for results and failure docs)."""
    repo = RunsRepo(session)
    row = repo.get_run(run_id)
    if row is None:
        return []
    entries: list[dict[str, Any]] = []
    import json as _json

    for call in repo.list_tool_calls(row.id):
        args_hash = None
        if call.arguments_json:
            try:
                args_hash = _json.loads(call.arguments_json).get("args_sha256")
            except ValueError:
                args_hash = None
        duration = None
        error = None
        if call.result_json:
            try:
                payload = _json.loads(call.result_json)
                duration = payload.get("duration_ms")
                error = payload.get("error")
            except ValueError:
                pass
        entries.append(
            {
                "sequence": call.sequence,
                "tool_name": call.tool_name,
                "status": call.status,
                "duration_ms": duration,
                "error": error,
                "args_sha256": args_hash,
            }
        )
    return entries


def build_result(
    session: Session,
    state: AgentState,
    *,
    tool_call_limit: int,
    transition_limit: int,
) -> AgentRunResult:
    """Assemble the :class:`AgentRunResult` from a checkpoint state."""
    from quarterline.agent.schemas import MemoDraft

    draft = MemoDraft.model_validate(state["draft"]) if state.get("draft") else None
    budgets = AgentBudgets(
        tool_calls=BudgetUse(used=int(state.get("tool_call_count", 0)), limit=tool_call_limit),
        transitions=BudgetUse(used=int(state.get("transition_count", 0)), limit=transition_limit),
        memo_repair_attempts=BudgetUse(used=1 if state.get("memo_repair_used") else 0, limit=1),
        deadline_seconds=float(state.get("deadline_seconds", 60.0)),
    )
    tool_calls = list_tool_calls(session, state["run_id"])
    return AgentRunResult(
        run_id=state["run_id"],
        status=state.get("status") or "failed",
        ticker=(state.get("request") or {}).get("ticker"),
        intent=state.get("intent"),
        memo=draft,
        memo_content=state.get("memo_content"),
        metric_facts=list(state.get("metric_facts") or []),
        validation_results=state.get("validation_results"),
        tool_call_log=[ToolCallLogEntry.model_validate(entry) for entry in tool_calls],
        budgets=budgets,
        errors=list(state.get("errors") or []),
        approval_request_id=state.get("approval_request_id"),
        approval_status=state.get("approval_status"),
        exported=state.get("exported"),
        evidence=list(state.get("evidence") or []),
        trace_uri=state.get("trace_uri"),
    )


def load_result(
    session: Session,
    run_id: str,
    *,
    tool_call_limit: int | None = None,
    transition_limit: int | None = None,
) -> AgentRunResult | None:
    """Rebuild the run result from the last checkpoint (GET/API path)."""
    state = load_state(session, run_id)
    if state is None:
        return None
    if tool_call_limit is None or transition_limit is None:
        from quarterline.config import get_settings

        settings = get_settings()
        tool_call_limit = tool_call_limit or settings.agent_max_tool_calls
        transition_limit = transition_limit or settings.agent_max_graph_transitions
    return build_result(
        session,
        state,
        tool_call_limit=tool_call_limit,
        transition_limit=transition_limit,
    )


__all__ = [
    "build_result",
    "ensure_run_row",
    "finish_tool_call",
    "list_tool_calls",
    "load_result",
    "load_state",
    "log_tool_call",
    "persist_checkpoint",
]
