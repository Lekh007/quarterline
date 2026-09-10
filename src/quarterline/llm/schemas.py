"""Brief and QA answer schemas (SPEC §17, PLAN contract C11).

The model output is parsed into these pydantic models; ``extra="forbid"`` on
every model rejects smuggled fields, and ``metric_id`` is validated against
``core.models.METRIC_IDS`` — the SAME frozenset the fact card is built from
(single source of truth, PLAN contract C7).

``template`` selects how Python renders the number from the fact card:
- ``reported_change`` — a signed change (year-over-year percent or percentage
  points), rendered by Python from the metric value;
- ``reported_value`` — the period value with its unit, rendered by Python;
- ``reported_level`` — deterministic qualitative level wording computed from
  the value by documented code thresholds (no model-typed numbers anywhere).

The model NEVER types a financial number into ``text``; numeric sentences the
user sees are rendered by Python from fact-card values (SPEC §17 "Safer
numerical output design").
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from quarterline.core.models import METRIC_IDS

#: Allowed response statuses (SPEC §17). ``provider_unavailable`` and
#: ``refused`` are produced by the pipeline/policy; a model may return
#: ok | partial | insufficient_evidence itself.
BriefStatus = Literal[
    "ok",
    "partial",
    "insufficient_evidence",
    "provider_unavailable",
    "refused",
]

MetricTemplate = Literal["reported_change", "reported_value", "reported_level"]

#: Templates that express a signed change; only change-shaped metrics may use
#: them (validated in :data:`TEMPLATE_VALID_METRICS`, factcheck module).
CHANGE_TEMPLATES: frozenset[str] = frozenset({"reported_change"})


class StrictModel(BaseModel):
    """Base for model-output schemas: unknown fields are a validation failure."""

    model_config = ConfigDict(extra="forbid")


class MetricMention(StrictModel):
    """An allowlisted metric reference; Python renders the number (SPEC §17)."""

    metric_id: str
    template: MetricTemplate

    @field_validator("metric_id")
    @classmethod
    def _allowlisted(cls, value: str) -> str:
        if value not in METRIC_IDS:
            raise ValueError(f"metric_id {value!r} is not in the METRIC_IDS allowlist")
        return value


class BriefBullet(StrictModel):
    """One brief statement; every filing-derived assertion cites evidence."""

    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class BriefRisks(StrictModel):
    """One risk statement identified by management in the supplied evidence."""

    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class OpenQuestion(StrictModel):
    """A research question for the user; must not carry unsupported premises.

    ``evidence_ids`` is usually empty; when supplied, every id is validated
    exactly like bullet citations by the validation gate.
    """

    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class Brief(StrictModel):
    """The constrained-writer brief schema (SPEC §17)."""

    status: BriefStatus
    label_echo: str | None = None
    metric_mentions: list[MetricMention] = Field(default_factory=list)
    bullets: list[BriefBullet] = Field(default_factory=list)
    risks: list[BriefRisks] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)


class Answer(StrictModel):
    """The QA answer schema: one grounded statement plus its citations."""

    status: BriefStatus
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


def brief_json_schema(
    label_echo: str | None = None,
    evidence_ids: list[str] | None = None,
) -> dict:
    """JSON-schema description of :class:`Brief` handed to providers that
    support structured decoding (advisory; the validation gate never trusts
    it). Values knowable before generation are enum-constrained: ``label_echo``
    to the code-generated label, citation ids to the supplied evidence set —
    invented citations become undecodable for schema-aware providers. The
    validation gate remains the backstop for providers without constrained
    decoding."""

    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "status",
            "label_echo",
            "metric_mentions",
            "bullets",
            "risks",
            "open_questions",
        ],
        "properties": {
            "status": {
                "type": "string",
                "enum": ["ok", "partial", "insufficient_evidence"],
            },
            "label_echo": (
                {"type": ["string", "null"], "enum": [label_echo, None]}
                if label_echo is not None
                else {"type": ["string", "null"]}
            ),
            "metric_mentions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["metric_id", "template"],
                    "properties": {
                        # Enum-constrain metric_id so schema-aware providers
                        # (Ollama structured outputs) cannot decode an
                        # off-allowlist identifier; the pydantic gate remains
                        # authoritative for providers without constrained
                        # decoding.
                        "metric_id": {"type": "string", "enum": sorted(METRIC_IDS)},
                        "template": {
                            "type": "string",
                            "enum": ["reported_change", "reported_value", "reported_level"],
                        },
                    },
                },
            },
            "bullets": _statement_items(evidence_ids),
            "risks": _statement_items(evidence_ids),
            "open_questions": _statement_items(evidence_ids),
        },
    }


def answer_json_schema() -> dict:
    """JSON-schema description of :class:`Answer`."""

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "text", "evidence_ids"],
        "properties": {
            "status": {"type": "string", "enum": ["ok", "insufficient_evidence"]},
            "text": {"type": "string"},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
        },
    }


def _statement_items(evidence_ids: list[str] | None = None) -> dict:
    citation_items = (
        {"type": "string", "enum": sorted(evidence_ids)} if evidence_ids else {"type": "string"}
    )
    return {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text", "evidence_ids"],
            "properties": {
                "text": {"type": "string"},
                "evidence_ids": {"type": "array", "items": citation_items},
            },
        },
    }


__all__ = [
    "Answer",
    "Brief",
    "BriefBullet",
    "BriefRisks",
    "BriefStatus",
    "MetricMention",
    "MetricTemplate",
    "OpenQuestion",
    "answer_json_schema",
    "brief_json_schema",
]
