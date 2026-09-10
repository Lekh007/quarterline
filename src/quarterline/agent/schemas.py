"""Agent request/memo/result schemas (SPEC §17 rules applied to memos, §20).

The memo schema validates like the brief (SPEC §17, contract C11):

- statuses ``ok | partial | insufficient_evidence | provider_unavailable | refused``;
- ``label_echo`` must equal the code-generated label (checked by the gate);
- ``metric_id`` is validated against ``core.models.METRIC_IDS`` (contract C7)
  and the template must be structurally valid for the metric (factcheck);
- every filing-derived assertion in a section must cite supplied evidence;
- the model NEVER types a financial number — numeric sentences the user sees
  are rendered by Python from the fact card.

``extra="forbid"`` everywhere rejects smuggled fields.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from quarterline.llm.schemas import MetricMention

MemoType = Literal["quarter_review", "risk_review"]

#: The five memo sections the versioned memo prompt describes.
MEMO_HEADINGS: tuple[str, ...] = (
    "overview",
    "what_changed",
    "management_explanation",
    "risks_and_open_questions",
    "evidence_gaps",
)

MemoHeading = Literal[
    "overview",
    "what_changed",
    "management_explanation",
    "risks_and_open_questions",
    "evidence_gaps",
]

#: Run-level terminal statuses (SPEC §20 AgentRunResult).
RunStatus = Literal[
    "completed",
    "failed",
    "awaiting_approval",
    "refused",
    "insufficient_evidence",
]

ExportType = Literal["md", "json"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemoRequest(StrictModel):
    """One memo run request (SPEC §20): a ticker plus optional focus text."""

    ticker: str = Field(min_length=1, max_length=16)
    memo_type: MemoType
    question: str | None = Field(default=None, max_length=2000)
    topic: str | None = Field(default=None, max_length=2000)
    period_end: date | None = None
    #: Explicit opt-in for the informational get_prices tool; the planner also
    #: auto-detects market-context language in question/topic.
    market_context: bool = False

    @property
    def focus_text(self) -> str:
        parts = [p for p in (self.question, self.topic) if p]
        return " ".join(parts)


class MemoSection(StrictModel):
    """One validated memo section (statements survived the whole gate)."""

    heading: str
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class MemoDraft(StrictModel):
    """The validated memo (the run's ``draft``; nothing raw inside)."""

    status: Literal["ok", "partial", "insufficient_evidence", "refused"]
    title: str
    label_echo: str | None = None
    sections: list[MemoSection] = Field(default_factory=list)
    metric_mentions: list[MetricMention] = Field(default_factory=list)


class ToolCallLogEntry(StrictModel):
    """One audit-trail row as reported in ``AgentRunResult.tool_call_log``."""

    sequence: int
    tool_name: str
    status: str | None
    duration_ms: float | None = None
    error: str | None = None
    args_sha256: str | None = None


class BudgetUse(StrictModel):
    used: int
    limit: int


class AgentBudgets(StrictModel):
    """Budgets as reported in every result (SPEC §20 Budgets, tested)."""

    tool_calls: BudgetUse
    transitions: BudgetUse
    memo_repair_attempts: BudgetUse
    deadline_seconds: float


class AgentRunResult(StrictModel):
    """Everything the API/UI needs about one run (SPEC §20)."""

    run_id: str
    status: RunStatus
    ticker: str | None = None
    intent: str | None = None
    memo: MemoDraft | None = None
    memo_content: str | None = None
    metric_facts: list[str] = Field(default_factory=list)
    validation_results: dict | None = None
    tool_call_log: list[ToolCallLogEntry] = Field(default_factory=list)
    budgets: AgentBudgets | None = None
    errors: list[str] = Field(default_factory=list)
    approval_request_id: int | None = None
    approval_status: str | None = None
    exported: dict | None = None
    evidence: list[dict] = Field(default_factory=list)
    trace_uri: str | None = None


# ---------------------------------------------------------------------------
# Provider-output schema (what the constrained writer must return)
# ---------------------------------------------------------------------------


class MemoSectionOutput(StrictModel):
    """One drafted section straight from the model (UNVALIDATED)."""

    heading: MemoHeading
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class MemoOutput(StrictModel):
    """The unvalidated provider output for the memo writer."""

    status: Literal["ok", "partial", "insufficient_evidence", "refused"]
    label_echo: str | None = None
    metric_mentions: list[MetricMention] = Field(default_factory=list)
    sections: list[MemoSectionOutput] = Field(default_factory=list)


def memo_json_schema() -> dict:
    """JSON-schema description of :class:`MemoOutput` (advisory for providers)."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "label_echo", "metric_mentions", "sections"],
        "properties": {
            "status": {
                "type": "string",
                "enum": ["ok", "partial", "insufficient_evidence", "refused"],
            },
            "label_echo": {"type": ["string", "null"]},
            "metric_mentions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["metric_id", "template"],
                    "properties": {
                        "metric_id": {"type": "string"},
                        "template": {
                            "type": "string",
                            "enum": ["reported_change", "reported_value", "reported_level"],
                        },
                    },
                },
            },
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["heading", "text", "evidence_ids"],
                    "properties": {
                        "heading": {
                            "type": "string",
                            "enum": list(MEMO_HEADINGS),
                        },
                        "text": {"type": "string"},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
    }


def memo_content_markdown(draft: MemoDraft) -> str:
    """Deterministic canonical memo content (the approval-bound bytes).

    The export writes EXACTLY this string, and the SHA-256 approval hash is
    computed over EXACTLY this string, so an approval can never be replayed
    against changed memo content (SPEC §20 Approval).
    """
    lines: list[str] = [f"# {draft.title}", ""]
    if draft.label_echo:
        lines += [
            f"Quarter label (rule-based): {draft.label_echo}. Not a recommendation or forecast.",
            "",
        ]
    for section in draft.sections:
        heading = section.heading.replace("_", " ").title()
        lines += [f"## {heading}", "", section.text, ""]
    return "\n".join(lines)


__all__ = [
    "MEMO_HEADINGS",
    "AgentBudgets",
    "AgentRunResult",
    "BudgetUse",
    "ExportType",
    "MemoDraft",
    "MemoOutput",
    "MemoRequest",
    "MemoSection",
    "MemoSectionOutput",
    "MemoType",
    "RunStatus",
    "ToolCallLogEntry",
    "memo_content_markdown",
    "memo_json_schema",
]
