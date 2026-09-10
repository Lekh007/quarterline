"""Agent run state (SPEC §20 State block).

A plain JSON-serializable TypedDict so the full snapshot can be checkpointed to
``agent_runs.state_json`` after EVERY node transition (:mod:`..agent.checkpoints`)
and reloaded for resume without any runtime-specific handles. Nodes return
partial updates; LangGraph merges them over this shape.

Every field is JSON-safe by construction: fact cards and memos travel as
``model_dump(mode="json")`` dicts, evidence as plain dicts, budgets as ints.
"""

from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    """One bounded agent run (SPEC §20 State)."""

    run_id: str
    #: The parsed MemoRequest (model_dump(mode="json") of agent.schemas.MemoRequest).
    request: dict[str, Any]
    company_id: int | None
    #: quarter_review | risk_review
    intent: str | None
    #: Ordered allowlist tool names the planner selected (read tools only;
    #: export is handled by the approval-gated tail of the graph).
    plan: list[str]
    tool_budget: dict[str, int]
    #: tool name -> JSON-safe tool result (facts / evidence / prices / export).
    tool_results: dict[str, Any]
    #: Executed tool names in order (the read-once rule reads this).
    tools_executed: list[str]
    #: Fact card (FactCard model_dump(mode="json")) or None.
    facts: dict[str, Any] | None
    #: Bounded evidence windows: evidence_id, document_id, section, text, period_end.
    evidence: list[dict[str, Any]]
    #: Validated memo (MemoDraft model_dump(mode="json")) or None.
    draft: dict[str, Any] | None
    #: UNVALIDATED provider output (MemoOutput dump) between write and gate.
    draft_output: dict[str, Any] | None
    #: Whether the single allowed memo repair pass was used.
    memo_repair_used: bool
    #: Canonical memo content (markdown) + its sha256; approval binds to this.
    memo_content: str | None
    memo_content_hash: str | None
    #: Python-rendered metric sentences (never model-typed numbers).
    metric_facts: list[str]
    #: ValidationReport-ish dict (check results + reasons).
    validation_results: dict[str, Any] | None
    #: fresh | pending | granted | refused_by_policy
    approval_status: str | None
    approval_request_id: int | None
    #: completed | failed | awaiting_approval | refused | insufficient_evidence
    status: str | None
    errors: list[str]
    transition_count: int
    tool_call_count: int
    #: Request deadline (seconds) + monotonic start; checked BETWEEN nodes.
    deadline_seconds: float
    started_at_monotonic: float
    #: Test hook: raise a simulated crash after this node completes.
    crash_after: str | None
    #: Where the memo review page can find the run trace.
    trace_uri: str | None
    #: Export output (path/filename) once the approved export ran.
    exported: dict[str, Any] | None
    #: Whether the request asked for informational market context (prices).
    market_context_requested: bool
    #: Export type chosen by the approving user (md | json); set on approval.
    export_type_requested: str | None


def initial_state(
    run_id: str,
    request: dict[str, Any],
    *,
    tool_budget: dict[str, int],
    deadline_seconds: float,
    started_at_monotonic: float,
    company_id: int | None = None,
    crash_after: str | None = None,
) -> AgentState:
    """A fresh run state (SPEC §20 defaults; missing stays missing)."""
    return AgentState(
        run_id=run_id,
        request=request,
        company_id=company_id,
        intent=None,
        plan=[],
        tool_budget=tool_budget,
        tool_results={},
        tools_executed=[],
        facts=None,
        evidence=[],
        draft=None,
        draft_output=None,
        memo_repair_used=False,
        memo_content=None,
        memo_content_hash=None,
        metric_facts=[],
        validation_results=None,
        approval_status=None,
        approval_request_id=None,
        status=None,
        errors=[],
        transition_count=0,
        tool_call_count=0,
        deadline_seconds=deadline_seconds,
        started_at_monotonic=started_at_monotonic,
        crash_after=crash_after,
        trace_uri=f"/api/runs/{run_id}",
        exported=None,
        market_context_requested=False,
        export_type_requested=None,
    )


def is_terminal(state: AgentState) -> bool:
    """True when the run reached a terminal status (no further nodes run)."""
    return state.get("status") in {
        "completed",
        "failed",
        "awaiting_approval",
        "refused",
        "insufficient_evidence",
    }


__all__ = ["AgentState", "initial_state", "is_terminal"]
