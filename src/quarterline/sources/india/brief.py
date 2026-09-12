"""India briefs: the metric-reference allowlist, the India constrained-writer
schema, the Python renderers, and the IND-8 validation gate + pipeline.

The US generation stack (``quarterline.llm.generation``) validates
``metric_id`` against the US ``METRIC_IDS`` frozenset inside ``llm.schemas``'s
pydantic validator — India briefs must reference INDIA quantities, and
``llm/**`` internals may not be modified. So this module mirrors contract C11
India-side (the duplication is deliberate and reported in
docs/india_briefs.md):

- :data:`INDIA_METRIC_REFERENCES` — the ONLY metric ids an India brief may
  reference: the ten canonical concepts (``data/tagmap_india.yml`` namespace)
  plus the six registered ``india_*`` metrics. Never a US ``METRIC_IDS`` id.
- :class:`IndiaMetricMention` / :class:`IndiaBrief` — the C11 schema with the
  India allowlist injected exactly the way ``llm.schemas`` injects the US one
  (pydantic ``field_validator`` + enum-constrained JSON schema for providers
  with structured decoding; the gate stays the backstop).
- Python-side renderers (``expand_india_metric_mentions``) — the model NEVER
  types a financial number; every rendered sentence comes from the India fact
  card (Decimal-exact): crore presentation + exact rupees for INR values,
  ``₹x/share`` for per-share values, percent for growth/margins with percent
  vs percentage-points labeled correctly (``reported_change`` is structurally
  valid ONLY for the three growth metrics; margins are levels, and no India
  metric is a pp-difference, so pp wording never renders).
- The India validation gate (``validate_india_brief``) — the SPEC §18 checks
  in order, with two documented India additions:
  check 11 ``scope_attribution`` (a statement naming the OPPOSITE reporting
  scope is dropped — deliberately strict deterministic confusion guard) and
  check 12 ``commentary_attribution`` (a statement whose citations are ALL
  management-commentary documents must carry an attribution marker such as
  "management stated"; otherwise dropped. The deeper question of whether an
  attributed claim is TRUE stays prompt-level by design).
- :func:`generate_india_brief` — the pipeline: India fact card + India
  narrative evidence (SearchService, ticker filter, brief-equivalent page
  preset) -> bounded context -> ONE generation + ONE repair pass -> gate ->
  statuses ok | partial | insufficient_evidence | provider_unavailable |
  refused. Cache keyed per the US §25 pattern including the India
  normalization/metrics formula versions. C12 events carry endpoint
  ``india_brief``.

There is NO quarter label and NO score for India issuers (no scores, no
recommendations). The check-3 echo is the APPLICATION PERIOD LABEL —
code-generated from the exact period dates only (``periods.period_label``) —
so the model proves it is writing about the requested period identity, not a
rating. Check 9 answers the missing-cash-flow rows from the FACT layer: a
numeric claim has to match card values AT the brief's period identity, and a
fabricated quarterly cash-flow number for a quarterly-CF-absent issuer has no
compatible target at that identity and drops the whole statement.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from quarterline.config import Settings, get_settings
from quarterline.core.advice_policy import (
    ADVICE_POLICY_VERSION,
    detect_advice_content,
    detect_advice_request,
)
from quarterline.core.citations import (
    CHECK_CONTEXT,
    CHECK_EXISTENCE,
    CHECK_FORMAT,
    CHECK_MEMBERSHIP,
    EvidenceRef,
    validate_statement_citations,
)
from quarterline.core.factcheck import (
    FACTCHECK_VERSION,
    ExpansionResult,
    RenderedFact,
    extract_numeric_claims,
)
from quarterline.llm.generation import (  # reusable US DTOs (import only)
    EvidenceSummary,
    MetricFactSummary,
    ValidationCheckResult,
    ValidationReport,
)
from quarterline.llm.prompts import PromptSpec, load_prompt, render_prompt
from quarterline.llm.repair import RepairOutcome, generate_and_parse
from quarterline.observability.events import emit_run_event, new_run_id
from quarterline.retrieve.context import ContextBundle, assemble_context
from quarterline.retrieve.context import (
    context_budget_from_settings as _context_budget,
)
from quarterline.retrieve.models import SearchQuery, SearchResult
from quarterline.sources.india.concept_map import (
    INDIA_CONCEPTS,
    PER_SHARE_CONCEPTS,
)
from quarterline.sources.india.factcard import (
    IndiaFactCard,
    build_india_fact_card,
)
from quarterline.sources.india.metrics import (
    FORMULA_VERSION_INDIA_METRICS,
    INDIA_METRIC_IDS,
    IndiaMetricResult,
)
from quarterline.sources.india.normalization import NORMALIZATION_VERSION
from quarterline.sources.india.periods import KIND_QUARTER, period_label
from quarterline.sources.india.units import CRORE, format_crores
from quarterline.store.models import BriefCache

#: Version of the India validation gate (mirrors US validation-v1; part of the
#: cache key). Bumped when the India checks change.
INDIA_VALIDATION_VERSION = "india-validation-v1"

#: The India metric-reference allowlist: canonical concepts + india_* metrics.
#: This (never the US METRIC_IDS) is what India brief mentions validate against.
INDIA_METRIC_REFERENCES: frozenset[str] = frozenset(INDIA_CONCEPTS).union(INDIA_METRIC_IDS)

#: Change-shaped India metrics: the ONLY ids for which ``reported_change`` is
#: structurally valid (they are relative changes rendered as PERCENT — never
#: percentage points, which would misstate a ratio-of-ratios).
INDIA_CHANGE_METRICS: frozenset[str] = frozenset(
    {"india_revenue_yoy", "india_revenue_qoq", "india_eps_growth_yoy"}
)

#: Ids that may use ``reported_value``: every canonical concept (levels) plus
#: the margin and exceptional-impact metrics.
INDIA_LEVEL_VALUE_IDS: frozenset[str] = frozenset(INDIA_CONCEPTS).union(
    {"india_pat_margin_owners", "india_pat_margin_group", "india_exceptional_impact_pbt"}
)

#: Ids with documented qualitative-level wording (the same documented scale
#: anchors the US gate uses: 30% high / 10% moderate / 0-30% low).
INDIA_LEVEL_WORDING_IDS: frozenset[str] = frozenset(
    {"india_pat_margin_owners", "india_pat_margin_group"}
)

#: Display names for rendered sentences.
INDIA_METRIC_DISPLAY_NAMES: dict[str, str] = {
    "revenue_from_operations": "Revenue from operations",
    "total_income": "Total income",
    "profit_before_tax": "Profit before tax",
    "profit_after_tax": "Profit after tax (group)",
    "profit_attributable_to_owners": "Profit attributable to owners",
    "exceptional_items": "Exceptional items (stored profit impact)",
    "eps_basic": "Basic EPS",
    "eps_diluted": "Diluted EPS",
    "cash_flow_operations": "Operating cash flow",
    "capex": "Capital expenditure",
    "india_revenue_yoy": "Revenue",
    "india_revenue_qoq": "Revenue",
    "india_eps_growth_yoy": "Diluted EPS",
    "india_pat_margin_owners": "PAT margin attributable to owners",
    "india_pat_margin_group": "PAT margin for the group",
    "india_exceptional_impact_pbt": "Exceptional items impact on profit before tax",
}

#: Deterministic retrieval query for the India brief.
INDIA_BRIEF_RETRIEVAL_QUERY = (
    "quarterly results revenue profit margin cash flow management commentary"
)

#: Brief-equivalent section preset: India narrative kinds the brief context is
#: drawn from (results / press-release / presentation kinds; transcripts stay
#: reachable through the memo search tool, which uses all narrative kinds).
INDIA_BRIEF_SECTIONS: tuple[str, ...] = (
    "financial_results",
    "results_notes",
    "earnings_presentation",
)

#: All India narrative kinds (the memo search tool's preset).
INDIA_NARRATIVE_SECTIONS: tuple[str, ...] = (
    "financial_results",
    "results_notes",
    "earnings_presentation",
    "annual_report",
    "management_transcript",
    "exchange_announcement",
)

#: Statements naming the OPPOSITE reporting scope are dropped (check 11) —
#: scope confusion is a documented India failure mode (methodology §2).
_OPPOSITE_SCOPE: dict[str, str] = {
    "consolidated": "standalone",
    "standalone": "consolidated",
}

#: Attribution markers check 12 accepts for commentary-only citations.
_ATTRIBUTION_MARKER_RE = re.compile(
    r"management\s+(stated|said|attributed|noted|indicated|identified|described|guided|expects?|"
    r"flagged|highlighted|commented)"
    r"|according\s+to\s+management"
    r"|management'?s\s+(view|commentary|presentation|guidance|explanation)"
    r"|per\s+the\s+(presentation|transcript|press\s+release|management)"
    r"|in\s+management'?s\s+words",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# India brief schema (C11 mirrored with the India allowlist)
# ---------------------------------------------------------------------------

BriefStatus = Literal[
    "ok",
    "partial",
    "insufficient_evidence",
    "provider_unavailable",
    "refused",
]

MetricTemplate = Literal["reported_change", "reported_value", "reported_level"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IndiaMetricMention(_StrictModel):
    """An allowlisted INDIA metric reference; Python renders the number."""

    metric_id: str
    template: MetricTemplate

    @field_validator("metric_id")
    @classmethod
    def _allowlisted(cls, value: str) -> str:
        if value not in INDIA_METRIC_REFERENCES:
            raise ValueError(f"metric_id {value!r} is not in the INDIA_METRIC_REFERENCES allowlist")
        return value


class IndiaBriefBullet(_StrictModel):
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class IndiaBriefRisk(_StrictModel):
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class IndiaOpenQuestion(_StrictModel):
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class IndiaBrief(_StrictModel):
    """The constrained-writer INDIA brief schema (C11, India allowlist)."""

    status: BriefStatus
    label_echo: str | None = None
    metric_mentions: list[IndiaMetricMention] = Field(default_factory=list)
    bullets: list[IndiaBriefBullet] = Field(default_factory=list)
    risks: list[IndiaBriefRisk] = Field(default_factory=list)
    open_questions: list[IndiaOpenQuestion] = Field(default_factory=list)


class IndiaMemoOutput(_StrictModel):
    """Unvalidated provider output for the India memo writer (agent graph)."""

    status: Literal["ok", "partial", "insufficient_evidence", "refused"]
    label_echo: str | None = None
    metric_mentions: list[IndiaMetricMention] = Field(default_factory=list)
    sections: list[IndiaMemoSectionOutput] = Field(default_factory=list)


class IndiaMemoSectionOutput(_StrictModel):
    heading: str = Field(
        pattern=r"^(overview|what_changed|management_explanation|risks_and_open_questions|"
        r"evidence_gaps)$"
    )
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


IndiaMemoOutput.model_rebuild()


def india_brief_json_schema(
    label_echo: str | None = None,
    evidence_ids: list[str] | None = None,
) -> dict:
    """JSON-schema description of :class:`IndiaBrief` for structured decoding.

    Enum-constrains metric_id to INDIA_METRIC_REFERENCES and citations to the
    supplied evidence set (advisory; the validation gate never trusts it).
    """
    citation_items = (
        {"type": "string", "enum": sorted(evidence_ids)} if evidence_ids else {"type": "string"}
    )
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
                        "metric_id": {
                            "type": "string",
                            "enum": sorted(INDIA_METRIC_REFERENCES),
                        },
                        "template": {
                            "type": "string",
                            "enum": [
                                "reported_change",
                                "reported_value",
                                "reported_level",
                            ],
                        },
                    },
                },
            },
            "bullets": _india_statement_items(citation_items),
            "risks": _india_statement_items(citation_items),
            "open_questions": _india_statement_items(citation_items),
        },
    }


def india_memo_json_schema() -> dict:
    """JSON-schema description of :class:`IndiaMemoOutput` (advisory)."""
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
                        "metric_id": {
                            "type": "string",
                            "enum": sorted(INDIA_METRIC_REFERENCES),
                        },
                        "template": {
                            "type": "string",
                            "enum": [
                                "reported_change",
                                "reported_value",
                                "reported_level",
                            ],
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
                            "enum": [
                                "overview",
                                "what_changed",
                                "management_explanation",
                                "risks_and_open_questions",
                                "evidence_gaps",
                            ],
                        },
                        "text": {"type": "string"},
                        "evidence_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
        },
    }


def _india_statement_items(citation_items: dict) -> dict:
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


# ---------------------------------------------------------------------------
# Structural template validity + renderers
# ---------------------------------------------------------------------------


def india_template_valid_for(metric_id: str, template: str) -> bool:
    """Structural check: a template must fit the India metric's shape.

    ``reported_change`` only for the three growth metrics (rendered as
    percent, never percentage points); ``reported_value`` for canonical
    concept levels and the margin/impact metrics; ``reported_level`` only for
    the margins (documented scale anchors).
    """
    if template == "reported_change":
        return metric_id in INDIA_CHANGE_METRICS
    if template == "reported_value":
        return metric_id in INDIA_LEVEL_VALUE_IDS
    if template == "reported_level":
        return metric_id in INDIA_LEVEL_WORDING_IDS
    return False


def primary_identity(card: IndiaFactCard) -> tuple[date | None, date, str]:
    """The brief's primary period identity: the LATEST quarter-kind identity,
    else the latest identity by (end, kind). Growth metrics exist only for
    quarter identities, so a quarter identity is preferred when present."""
    identities = {(p.period_start, p.period_end, p.period_kind) for p in card.period_identities}
    if not identities:  # pragma: no cover - build_india_fact_card guarantees >= 1
        raise LookupError("fact card carries no period identity")
    quarters = [i for i in identities if i[2] == KIND_QUARTER]
    pool = quarters or list(identities)
    return max(pool, key=lambda i: (i[1], i[2]))


def application_label_of(card: IndiaFactCard) -> str:
    """The code-generated application label of the primary identity (the
    check-3 echo; dates-derived, never a rating)."""
    start, end, kind = primary_identity(card)
    return period_label(start, end, kind)


def _facts_at(card: IndiaFactCard, identity: tuple) -> dict:
    start, end, kind = identity
    return {
        fact.concept: fact
        for fact in card.canonical_facts
        if (fact.period_start, fact.period_end, fact.period_kind) == (start, end, kind)
    }


def _metrics_at(card: IndiaFactCard, identity: tuple) -> dict:
    start, end, kind = identity
    return {
        result.metric_id: result
        for result in card.metrics
        if (result.period_start, result.period_end, result.period_kind) == (start, end, kind)
    }


def _quantize(value: Decimal, places: str) -> Decimal:
    return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _full_rupees(value: Decimal) -> str:
    return f"₹ {value:,.0f}"


def _format_inr_value(value: Decimal) -> str:
    """Crore presentation; the exact rupees travel in the rendered note."""
    return f"{format_crores(value)} ({_full_rupees(value)} exact filed amount)"


def _render_india_change(metric: IndiaMetricResult) -> str:
    assert metric.value is not None
    rendered = f"{_quantize(metric.value * 100, '0.1'):+}%" if metric.value != 0 else "0.0%"
    if metric.metric_id == "india_revenue_qoq":
        return f"changed {rendered} versus the immediately preceding fiscal quarter (percent, not percentage points)"
    return f"changed {rendered} versus the matching prior-year quarter (percent, not percentage points)"


def _render_india_level_wording(value: Decimal) -> str:
    percent = value * 100
    if percent >= Decimal(30):
        return "was at a high level (documented scale anchor 30%)"
    if percent >= Decimal(10):
        return "was at a moderate level (documented scale anchor 10%)"
    if percent >= 0:
        return "was positive but low (documented scale anchors 0-30%)"
    return "was negative for the period"


def _render_india_metric_value(metric_id: str, value: Decimal, identity: tuple) -> str:
    label = period_label(identity[0], identity[1], identity[2])
    name = INDIA_METRIC_DISPLAY_NAMES.get(metric_id, metric_id)
    if metric_id in PER_SHARE_CONCEPTS:
        return f"{name} was ₹{value}/share {label}."
    if metric_id == "india_pat_margin_owners":
        return (
            f"{name} was {_quantize(value * 100, '0.1')}% of revenue from operations "
            f"(profit attributable to owners / revenue from operations) {label}."
        )
    if metric_id == "india_pat_margin_group":
        return (
            f"{name} was {_quantize(value * 100, '0.1')}% of revenue from operations "
            f"(profit after tax / revenue from operations) {label}."
        )
    if metric_id == "india_exceptional_impact_pbt":
        return (
            f"{name} was {format_crores(value)} as stored (negative = expense reducing "
            f"profit before tax, positive = gain increasing it; sign convention "
            f"preserved per issuer) {label}."
        )
    return f"{name} was {_format_inr_value(value)} {label}."


def expand_india_metric_mentions(mentions: list, card: IndiaFactCard) -> ExpansionResult:
    """Render valid India mentions into Python-authored sentences.

    Invalid mentions (unknown template shape for the metric, metric not ok at
    the primary identity, value missing) are dropped with recorded issues —
    missing stays missing, never zero, never approximated.
    """
    result = ExpansionResult()
    identity = primary_identity(card)
    metrics = _metrics_at(card, identity)
    facts = _facts_at(card, identity)
    for mention in mentions or []:
        metric_id = mention.metric_id
        template = mention.template
        name = INDIA_METRIC_DISPLAY_NAMES.get(metric_id, metric_id)
        if not india_template_valid_for(metric_id, template):
            result.issues.append(
                f"metric_mention {metric_id!r} template {template!r} is not "
                "structurally valid for this metric"
            )
            continue
        metric = metrics.get(metric_id)
        fact = facts.get(metric_id)
        value: Decimal | None = None
        status: str | None = None
        if metric is not None:
            value, status = metric.value, metric.status
        elif fact is not None:
            value, status = fact.value, "ok"
        else:
            result.issues.append(
                f"metric_mention {metric_id!r} has no fact or metric value at the "
                f"brief's period identity; not present in the ingested sources for "
                "this identity"
            )
            continue
        if status != "ok" or value is None:
            result.issues.append(
                f"metric_mention {metric_id!r} is unavailable (status {status}); "
                "missing stays missing"
            )
            continue
        label = period_label(identity[0], identity[1], identity[2])
        if template == "reported_change":
            text = f"{name} {_render_india_change(metric)}."
        elif template == "reported_value":
            text = _render_india_metric_value(metric_id, value, identity)
        else:  # reported_level (margins only)
            text = f"{name} {_render_india_level_wording(value)} {label}."
        result.rendered.append(RenderedFact(metric_id=metric_id, template=template, text=text))
    return result


# ---------------------------------------------------------------------------
# Free-text numeric + scope checks (India card semantics)
# ---------------------------------------------------------------------------

#: India fiscal labels masked BEFORE numeric scanning (metadata digits are not
#: financial claims). "FY2026-27" and "2025-26" would otherwise leave a stray
#: "-27"/"-26" behind the US fiscal-label mask.
_INDIA_FISCAL_MASKS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bFY\s?(19|20)\d{2}\s?[-–]\s?\d{2}\b", re.IGNORECASE),
    re.compile(r"\bQ[1-4]\s+FY\s?(19|20)\d{2}\s?[-–]?\s?\d{2}\b", re.IGNORECASE),
    re.compile(r"\b(19|20)\d{2}\s?[-–]\s?\d{2}\b"),
)


def _matches_within_tolerance(claim: Decimal, target: Decimal) -> bool:
    """The documented US rule: <=0.5% relative after semantic matching, plus
    display-rounding respect (0.1/0.01 quantized equal within 5%)."""
    if claim == 0 or target == 0:
        return claim == target
    if (claim < 0) != (target < 0):
        return False
    relative = abs(claim - target) / abs(target)
    if relative <= Decimal("0.005"):
        return True
    for places in ("0.1", "0.01"):
        if _quantize(target, places) == _quantize(claim, places) and relative <= Decimal("0.05"):
            return True
    return False


def _india_numeric_targets(card: IndiaFactCard, identity: tuple) -> dict:
    """Semantically compatible targets at the brief's period identity.

    percent -> growth/margin ratios x100; eps -> per-share facts; money/plain
    -> INR values in {exact rupees, crores, millions}; ratio -> margin
    decimals. pp and shares have NO India targets (no pp-difference metric and
    no share-count fact), so such claims are unsupported by construction.
    """
    percent: list[Decimal] = []
    eps: list[Decimal] = []
    money: list[Decimal] = []
    ratio: list[Decimal] = []
    for fact in _facts_at(card, identity).values():
        if fact.value is None:
            continue
        if fact.concept in PER_SHARE_CONCEPTS or fact.unit == "INR/share":
            eps.append(fact.value)
        elif fact.unit in (None, "INR"):
            money.extend([fact.value, fact.value / CRORE, fact.value / Decimal(1_000_000)])
    for result in _metrics_at(card, identity).values():
        if result.status != "ok" or result.value is None:
            continue
        if result.metric_id in INDIA_CHANGE_METRICS:
            percent.append(result.value * 100)
        elif result.metric_id in ("india_pat_margin_owners", "india_pat_margin_group"):
            percent.append(result.value * 100)
            ratio.append(result.value)
        elif result.unit in (None, "INR"):
            money.extend([result.value, result.value / CRORE, result.value / Decimal(1_000_000)])
    return {"percent": percent, "eps": eps, "money": money, "ratio": ratio, "pp": [], "shares": []}


def check_india_statement_numbers(text: str, card: IndiaFactCard, identity: tuple) -> list[str]:
    """Validate every numeric claim against the India fact card AT the brief's
    period identity (check 9, India semantics).

    India fiscal labels are masked first, then the documented core normalizer
    extracts claims; a claim with no semantically compatible target at this
    identity is unsupported -> the caller drops the WHOLE statement. This is
    the mechanism that contradicts a fabricated quarterly cash-flow figure:
    where the fact layer carries no quarterly cash-flow fact at the identity
    (typed status not_present_in_ingested_sources), no money claim can match.
    """
    masked = text or ""
    for pattern in _INDIA_FISCAL_MASKS:
        masked = pattern.sub(lambda m: " " + "#" * len(m.group(0)) + " ", masked)
    reasons: list[str] = []
    targets = _india_numeric_targets(card, identity)
    for claim in extract_numeric_claims(masked):
        candidates = targets.get(claim.kind, [])
        if not candidates:
            reasons.append(
                f"numeric_consistency: {claim.raw!r} has no semantically compatible "
                f"India fact-card value at the brief's period identity "
                f"(kind {claim.kind}); unsupported claim"
            )
            continue
        if not any(_matches_within_tolerance(claim.value, target) for target in candidates):
            reasons.append(
                f"numeric_consistency: {claim.raw!r} matches no {claim.kind}-class "
                "India fact-card value within the documented tolerance; unsupported claim"
            )
    return reasons


def check_scope_attribution(text: str, scope: str) -> list[str]:
    """Check 11: a statement naming the OPPOSITE reporting scope is dropped.

    Deliberately strict and deterministic: prose naming the other scope is
    treated as value attribution to that scope, and values never cross scopes
    (methodology §2). The card's own scope wording is always fine.
    """
    opposite = _OPPOSITE_SCOPE.get(scope)
    if opposite and re.search(rf"\b{opposite}\b", text or "", re.IGNORECASE):
        return [
            (
                f"scope_attribution: statement names the {opposite!r} reporting scope but "
                f"this brief is scoped {scope!r}; values never cross scopes, statement dropped"
            )
        ]
    return []


def has_attribution_marker(text: str) -> bool:
    """True when the text carries a management-attribution marker (check 12)."""
    return bool(_ATTRIBUTION_MARKER_RE.search(text or ""))


def check_commentary_attribution(
    text: str, evidence_ids: list[str], commentary_flags: dict[str, bool]
) -> list[str]:
    """Check 12 (the deterministic half): a bullet/risk whose citations are ALL
    management-commentary documents must carry an attribution marker
    ("management stated..."); otherwise it states commentary as unattributed
    fact and is dropped. What remains prompt-level: whether an attributed
    claim is itself true — that judgement is deliberately not simulated here.
    """
    cited = [eid for eid in dict.fromkeys(evidence_ids or []) if eid in commentary_flags]
    if not cited or not all(commentary_flags[eid] for eid in cited):
        return []
    if has_attribution_marker(text):
        return []
    return [
        (
            "commentary_attribution: every cited document is management commentary "
            "(presentation/press release/transcript) but the statement has no "
            "management-stated attribution; commentary is never stated as "
            "unattributed fact, statement dropped"
        )
    ]


# ---------------------------------------------------------------------------
# Evidence map + commentary flags
# ---------------------------------------------------------------------------


def build_india_evidence_map(
    session: Session, bundle: ContextBundle, *, ticker: str, scoped_period: date | None
) -> dict[str, EvidenceRef]:
    """evidence_id -> company/period-qualified evidence reference (same
    semantics as the US brief: the document's own period travels with the
    reference so wrong-period citations fail check 6)."""
    from quarterline.store.repositories.documents import DocumentsRepo

    repo = DocumentsRepo(session)
    evidence_map: dict[str, EvidenceRef] = {}
    for passage in bundle.passages:
        document = repo.get_document(passage.document_id)
        document_period = document.period_end if document is not None else None
        evidence_map[passage.evidence_id] = EvidenceRef(
            evidence_id=passage.evidence_id,
            document_id=passage.document_id,
            ticker=ticker,
            period_end=document_period if scoped_period is not None else None,
            text=passage.text,
        )
    return evidence_map


def commentary_flags_for(session: Session, bundle: ContextBundle) -> dict[str, bool]:
    """evidence_id -> management_commentary flag of the underlying document."""
    from quarterline.sources.india.narrative import narrative_metadata
    from quarterline.store.repositories.documents import DocumentsRepo

    repo = DocumentsRepo(session)
    flags: dict[str, bool] = {}
    for passage in bundle.passages:
        document = repo.get_document(passage.document_id)
        metadata = narrative_metadata(document) if document is not None else {}
        flags[passage.evidence_id] = bool(metadata.get("management_commentary"))
    return flags


# ---------------------------------------------------------------------------
# Validation gate (SPEC §18 order; checks 1-10 + India 11-12)
# ---------------------------------------------------------------------------

_ABSTENTION_NOTE = "model returned an abstention status; no content is presented"

_CITATION_CHECKS: tuple[tuple[str, str, str], ...] = (
    (CHECK_EXISTENCE, "4", "citation_existence"),
    (CHECK_MEMBERSHIP, "5", "citation_supplied_context"),
    (CHECK_CONTEXT, "6", "citation_company_period"),
    (CHECK_FORMAT, "7", "citation_sentence_format"),
)

_IN_GATE_CHECK_IDS = (
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    "10",
    "11",
    "12",
)


def _add_check(
    checks: list[ValidationCheckResult],
    check_id: str,
    name: str,
    passed: bool,
    reasons: list[str] | None = None,
    dropped: int = 0,
) -> None:
    checks.append(
        ValidationCheckResult(
            check_id=check_id,
            name=name,
            passed=passed,
            dropped_count=dropped,
            reasons=reasons or [],
        )
    )


def validate_india_brief(
    parsed: IndiaBrief,
    *,
    card: IndiaFactCard,
    label: str,
    scope: str,
    evidence_map: dict[str, EvidenceRef],
    commentary_flags: dict[str, bool],
) -> tuple[IndiaBrief, ValidationReport]:
    """Run the India gate; return (surviving brief, report)."""
    report = ValidationReport(version=INDIA_VALIDATION_VERSION)
    checks = report.checks
    identity = primary_identity(card)

    # -- check 1: schema validity (pydantic IndiaBrief, extra=forbid) ----------
    _add_check(checks, "1", "schema_validity", True)

    # -- check 2: status validity / model abstention ----------------------------
    if parsed.status in ("insufficient_evidence", "provider_unavailable", "refused"):
        _add_check(checks, "2", "status_validity", True, [_ABSTENTION_NOTE])
        return IndiaBrief(status=parsed.status, label_echo=None), report
    _add_check(checks, "2", "status_validity", True)

    # -- check 3: application-period-label echo EQUALITY ------------------------
    echoed = (parsed.label_echo or "").strip()
    expected = (label or "").strip()
    label_ok = bool(expected) and echoed.lower() == expected.lower()
    if not label_ok:
        dropped_all = (
            len(parsed.bullets)
            + len(parsed.risks)
            + len(parsed.open_questions)
            + len(parsed.metric_mentions)
        )
        _add_check(
            checks,
            "3",
            "period_label_echo_equality",
            False,
            [
                (
                    f"label_echo {echoed!r} does not equal the code-generated "
                    f"application period label {expected!r}; all content dropped"
                )
            ],
            dropped=dropped_all,
        )
        return IndiaBrief(status="insufficient_evidence", label_echo=None), report
    _add_check(checks, "3", "period_label_echo_equality", True)

    # -- checks 4-6: citations, per statement -----------------------------------
    statements: list[tuple[str, int, str, list[str]]] = []
    for index, bullet in enumerate(parsed.bullets):
        statements.append(("bullet", index, bullet.text, list(bullet.evidence_ids)))
    for index, risk in enumerate(parsed.risks):
        statements.append(("risk", index, risk.text, list(risk.evidence_ids)))
    for index, question in enumerate(parsed.open_questions):
        statements.append(("open_question", index, question.text, list(question.evidence_ids)))

    per_check_reasons: dict[str, list[str]] = {}
    citation_kept: list[tuple[str, int, str, list[str]]] = []
    for kind, index, text, ids in statements:
        reasons: list[str] = []
        if kind in ("bullet", "risk") and not ids:
            reasons.append(
                f"{CHECK_EXISTENCE}: statement makes a filing-derived assertion "
                "with no supplied citation"
            )
        reasons.extend(
            validate_statement_citations(
                text,
                ids,
                evidence_map,
                expected_ticker=card.issuer.ticker_nse or card.issuer.issuer_id,
                expected_period_end=identity[1],
            )
        )
        if reasons:
            for reason in reasons:
                bucket, _, detail = reason.partition(":")
                per_check_reasons.setdefault(bucket, []).append(f"{kind}[{index}] {detail.strip()}")
        else:
            citation_kept.append((kind, index, text, ids))

    failed_statements = len(statements) - len(citation_kept)
    for bucket, check_id, name in _CITATION_CHECKS:
        reasons = per_check_reasons.get(bucket, [])
        _add_check(
            checks,
            check_id,
            name,
            not reasons,
            reasons,
            dropped=failed_statements if reasons else 0,
        )

    # -- check 8: metric-reference validity (India allowlist + structure) -------
    kept_mentions: list[IndiaMetricMention] = []
    mention_reasons: list[str] = []
    for mention in parsed.metric_mentions:
        if mention.metric_id not in INDIA_METRIC_REFERENCES:  # defense in depth
            mention_reasons.append(
                f"metric_id {mention.metric_id!r} is not in the INDIA_METRIC_REFERENCES allowlist"
            )
        elif not india_template_valid_for(mention.metric_id, mention.template):
            mention_reasons.append(
                f"template {mention.template!r} is not structurally valid for "
                f"metric {mention.metric_id!r}"
            )
        else:
            kept_mentions.append(mention)
    _add_check(
        checks,
        "8",
        "metric_reference_validity",
        not mention_reasons,
        mention_reasons,
        dropped=len(parsed.metric_mentions) - len(kept_mentions),
    )

    # -- check 9: numeric consistency vs the India fact card --------------------
    numeric_kept: list[tuple[str, int, str, list[str]]] = []
    numeric_reasons: list[str] = []
    for kind, index, text, ids in citation_kept:
        reasons = check_india_statement_numbers(text, card, identity)
        if reasons:
            numeric_reasons.extend(f"{kind}[{index}] {reason}" for reason in reasons)
        else:
            numeric_kept.append((kind, index, text, ids))
    _add_check(
        checks,
        "9",
        "numeric_consistency",
        not numeric_reasons,
        numeric_reasons,
        dropped=len(citation_kept) - len(numeric_kept),
    )

    # -- check 10: advice-policy compliance --------------------------------------
    advice_kept: list[tuple[str, int, str, list[str]]] = []
    advice_reasons: list[str] = []
    for kind, index, text, ids in numeric_kept:
        decision = detect_advice_content(text)
        if decision.is_advice:
            advice_reasons.append(
                f"{kind}[{index}] generated content issues investment advice "
                f"({', '.join(decision.matched)}); statement dropped"
            )
        else:
            advice_kept.append((kind, index, text, ids))
    _add_check(
        checks,
        "10",
        "advice_policy_compliance",
        not advice_reasons,
        advice_reasons,
        dropped=len(numeric_kept) - len(advice_kept),
    )

    # -- check 11: scope attribution (India addition) ----------------------------
    scope_kept: list[tuple[str, int, str, list[str]]] = []
    scope_reasons: list[str] = []
    for kind, index, text, ids in advice_kept:
        reasons = check_scope_attribution(text, scope)
        if reasons:
            scope_reasons.extend(f"{kind}[{index}] {reason}" for reason in reasons)
        else:
            scope_kept.append((kind, index, text, ids))
    _add_check(
        checks,
        "11",
        "scope_attribution_consistency",
        not scope_reasons,
        scope_reasons,
        dropped=len(advice_kept) - len(scope_kept),
    )

    # -- check 12: commentary attribution (India addition; bullets+risks only) ---
    final_kept: list[tuple[str, int, str, list[str]]] = []
    commentary_reasons: list[str] = []
    for kind, index, text, ids in scope_kept:
        if kind == "open_question":
            final_kept.append((kind, index, text, ids))
            continue
        reasons = check_commentary_attribution(text, ids, commentary_flags)
        if reasons:
            commentary_reasons.extend(f"{kind}[{index}] {reason}" for reason in reasons)
        else:
            final_kept.append((kind, index, text, ids))
    _add_check(
        checks,
        "12",
        "commentary_attribution",
        not commentary_reasons,
        commentary_reasons,
        dropped=len(scope_kept) - len(final_kept),
    )

    surviving = _surviving_india_brief(parsed, final_kept, kept_mentions, label_ok, label)
    _record_counts(report, parsed, final_kept, kept_mentions)
    return surviving, report


def _surviving_india_brief(
    parsed: IndiaBrief,
    kept: list[tuple[str, int, str, list[str]]],
    kept_mentions: list[IndiaMetricMention],
    label_ok: bool,
    label: str,
) -> IndiaBrief:
    bullets: list[IndiaBriefBullet] = []
    risks: list[IndiaBriefRisk] = []
    questions: list[IndiaOpenQuestion] = []
    for kind, index, text, ids in kept:
        if kind == "bullet":
            bullets.append(IndiaBriefBullet(text=text, evidence_ids=ids))
        elif kind == "risk":
            risks.append(IndiaBriefRisk(text=text, evidence_ids=ids))
        else:
            questions.append(IndiaOpenQuestion(text=text, evidence_ids=ids))
    return IndiaBrief(
        status="ok",
        label_echo=label if label_ok else None,
        metric_mentions=kept_mentions,
        bullets=bullets,
        risks=risks,
        open_questions=questions,
    )


def _record_counts(
    report: ValidationReport,
    parsed: IndiaBrief,
    kept: list[tuple[str, int, str, list[str]]],
    kept_mentions: list[IndiaMetricMention],
) -> None:
    kinds = [kind for kind, _index, _text, _ids in kept]
    report.retained_bullet_count = kinds.count("bullet")
    report.retained_risk_count = kinds.count("risk")
    report.dropped_bullet_count = len(parsed.bullets) - report.retained_bullet_count
    report.dropped_risk_count = len(parsed.risks) - report.retained_risk_count
    report.dropped_open_question_count = len(parsed.open_questions) - kinds.count("open_question")
    report.retained_mention_count = len(kept_mentions)
    report.dropped_mention_count = len(parsed.metric_mentions) - len(kept_mentions)


def finalize_india_status(surviving: IndiaBrief, report: ValidationReport) -> BriefStatus:
    """Same survival rule as the US gate (dropped -> partial; nothing useful ->
    insufficient_evidence; advice-driven total loss -> refused)."""
    has_content = bool(
        surviving.bullets
        or surviving.risks
        or surviving.open_questions
        or surviving.metric_mentions
    )
    if has_content:
        dropped_anything = (
            india_gate_failed(report)
            or (
                report.dropped_bullet_count
                + report.dropped_risk_count
                + report.dropped_open_question_count
                + report.dropped_mention_count
            )
            > 0
        )
        return "partial" if dropped_anything else "ok"
    if any(check.check_id == "10" and not check.passed for check in report.checks):
        return "refused"
    return "insufficient_evidence"


def india_gate_failed(report: ValidationReport) -> bool:
    """True when any India gate check (3-12) failed (the reused US report DTO
    only knows the US check ids, so the India set is explicit here)."""
    return any(not check.passed for check in report.checks if check.check_id in _IN_GATE_CHECK_IDS)


def india_gate_failed_checks(check_results: list[dict]) -> bool:
    """Dict-shaped variant of :func:`india_gate_failed` (the agent memo gate
    records its checks as plain JSON-safe dicts)."""
    return any(
        not bool(check.get("passed", True))
        for check in check_results
        if check.get("check_id") in _IN_GATE_CHECK_IDS
    )


# ---------------------------------------------------------------------------
# Outcome DTO + provider + cache
# ---------------------------------------------------------------------------


class IndiaBriefOutcome(BaseModel):
    """Everything the India brief API/UI needs; nothing unvalidated inside."""

    kind: Literal["india_brief"] = "india_brief"
    status: BriefStatus
    issuer_id: str
    ticker: str
    scope: str
    period_end: date | None = None
    application_label: str | None = None
    brief: IndiaBrief | None = None
    metric_facts: list[str] = Field(default_factory=list)
    facts: list[MetricFactSummary] = Field(default_factory=list)
    evidence: list[EvidenceSummary] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    validation: ValidationReport | None = None
    cache_hit: bool = False
    run_id: str = ""
    provider: str | None = None
    model: str | None = None
    advice_policy_version: str | None = None


def india_fact_summaries(card: IndiaFactCard) -> list[MetricFactSummary]:
    """Display rows: canonical concepts (at the primary identity) + metrics."""
    identity = primary_identity(card)
    rows: list[MetricFactSummary] = []
    facts = _facts_at(card, identity)
    for concept in INDIA_CONCEPTS:
        fact = facts.get(concept)
        rows.append(
            MetricFactSummary(
                metric_id=concept,
                display=fact.display if fact is not None else "",
                status="ok" if fact is not None else "not_present_in_ingested_sources",
            )
        )
    for result in card.metrics:
        if (result.period_start, result.period_end, result.period_kind) != identity:
            continue
        if result.metric_id not in INDIA_METRIC_IDS:
            continue
        if result.status == "ok" and result.value is not None:
            if result.unit == "ratio":
                display = f"{_quantize(result.value * 100, '0.01')}%"
            else:
                display = format_crores(result.value)
        else:
            display = ""
        rows.append(
            MetricFactSummary(metric_id=result.metric_id, display=display, status=result.status)
        )
    return rows


def _card_hash(card: IndiaFactCard) -> str:
    return hashlib.sha256(
        card.model_dump_json(exclude={"generated_at"}).encode("utf-8")
    ).hexdigest()


def _context_hash(bundle: ContextBundle) -> str:
    parts = [
        f"{p.evidence_id}|{p.document_id}|{p.start_offset}|{p.end_offset}|{p.text}"
        for p in bundle.passages
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _cache_key(
    *,
    issuer_id: str,
    period_end: date | None,
    scope: str,
    card_hash: str,
    context_hash: str,
    provider_name: str,
    model: str,
    prompt_version: str,
    prompt_hash: str,
    settings: Settings,
) -> str:
    material = json.dumps(
        {
            "market": "india",
            "company": issuer_id,
            "period": period_end.isoformat() if period_end else None,
            "scope": scope,
            "fact_card_hash": card_hash,
            "evidence_context_hash": context_hash,
            "provider": provider_name,
            "model": model,
            "prompt": prompt_version,
            "prompt_hash": prompt_hash,
            "generation_settings": {
                "num_ctx": settings.ollama_num_ctx,
                "max_output_tokens": settings.max_output_tokens,
                "max_prompt_tokens": settings.max_prompt_tokens,
            },
            "validation_version": INDIA_VALIDATION_VERSION,
            "factcheck_version": FACTCHECK_VERSION,
            "advice_policy_version": ADVICE_POLICY_VERSION,
            "india_normalization_version": NORMALIZATION_VERSION,
            "india_metrics_formula_version": FORMULA_VERSION_INDIA_METRICS,
        },
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _cache_get(session: Session, key: str) -> dict | None:

    from sqlalchemy import select as _select

    row = session.scalar(_select(BriefCache).where(BriefCache.cache_key == key).limit(1))
    if row is None or not row.response_json:
        return None
    expires = row.expires_at
    if expires is not None:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= datetime.now(UTC):
            return None
    try:
        return json.loads(row.response_json)
    except ValueError:
        return None


def _cache_put(
    session: Session,
    key: str,
    *,
    company_id: int,
    period_end: date | None,
    provider: str,
    model: str,
    prompt_version: str,
    payload: dict,
    settings: Settings,
) -> None:
    session.add(
        BriefCache(
            cache_key=key,
            company_id=company_id,
            period_end=period_end,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            response_json=json.dumps(payload, ensure_ascii=False),
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=settings.brief_cache_hours),
        )
    )


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def _catalog_lines(card: IndiaFactCard, identity: tuple) -> str:
    facts = _facts_at(card, identity)
    lines = [
        f"{concept}: {'present' if concept in facts else 'not present in the ingested sources at this period identity'}"
        for concept in INDIA_CONCEPTS
    ]
    lines.append("")
    lines.append("india_* metrics (registered formulas):")
    seen: set[str] = set()
    for result in card.metrics:
        if (
            result.metric_id in seen
            or (result.period_start, result.period_end, result.period_kind) != identity
        ):
            continue
        seen.add(result.metric_id)
        lines.append(
            f"{result.metric_id}: status {result.status}"
            + (f"; unit {result.unit}" if result.unit else "")
        )
    for metric_id in sorted(set(INDIA_METRIC_IDS) - seen):
        lines.append(f"{metric_id}: no result at this period identity")
    return "\n".join(lines)


def _india_brief_messages(
    spec: PromptSpec, card: IndiaFactCard, label: str, bundle: ContextBundle
) -> list[dict]:
    identity = primary_identity(card)
    system = render_prompt(
        spec,
        issuer=card.issuer.issuer_id,
        ticker=card.issuer.ticker_nse or card.issuer.issuer_id,
        period=label,
        scope=card.scope,
        allowlist=", ".join(sorted(INDIA_METRIC_REFERENCES)),
        catalog=_catalog_lines(card, identity),
    )
    payload = {
        "task": "india_quarter_brief",
        "issuer_id": card.issuer.issuer_id,
        "ticker": card.issuer.ticker_nse or card.issuer.issuer_id,
        "reporting_scope": card.scope,
        "period_end": identity[1].isoformat(),
        "required_label_echo": label,
        "metric_allowlist": sorted(INDIA_METRIC_REFERENCES),
        "evidence": [
            {"evidence_id": p.evidence_id, "section": p.section, "text": p.text}
            for p in bundle.passages
        ],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def default_generation_provider(settings: Settings | None = None):
    """The SAME provider factory as the US stack (Ollama local-first; remote
    OpenRouter only behind the explicit consent gate)."""
    from quarterline.llm.generation import default_generation_provider as _us_provider

    return _us_provider(settings)


def generate_india_brief(
    issuer_id: str,
    period_end: date | str | None = None,
    *,
    scope: str = "consolidated",
    focus: str | None = None,
    session: Session | None = None,
    provider=None,
    search_service=None,
    settings: Settings | None = None,
) -> IndiaBriefOutcome:
    """Grounded India quarter brief for one issuer/scope/period (IND-8)."""
    if session is not None:
        return _generate_india_brief(
            session,
            issuer_id,
            period_end,
            scope=scope,
            focus=focus,
            provider=provider,
            search_service=search_service,
            settings=settings,
        )
    from quarterline.store.db import session_scope

    with session_scope() as owned_session:
        return _generate_india_brief(
            owned_session,
            issuer_id,
            period_end,
            scope=scope,
            focus=focus,
            provider=provider,
            search_service=search_service,
            settings=settings,
        )


def _generate_india_brief(
    session: Session,
    issuer_id: str,
    period_end: date | str | None,
    *,
    scope: str,
    focus: str | None,
    provider,
    search_service,
    settings: Settings | None,
) -> IndiaBriefOutcome:
    settings = settings if settings is not None else get_settings()
    run_id = new_run_id()
    started = time.perf_counter()

    # Advice policy FIRST: advice-seeking focus text is refused without any
    # provider call (SPEC §26; the India release keeps the research-only line).
    if focus:
        decision = detect_advice_request(focus)
        if decision.is_advice:
            return IndiaBriefOutcome(
                status="refused",
                issuer_id=issuer_id,
                ticker=issuer_id,
                scope=scope,
                reasons=[
                    (
                        f"advice-policy {decision.policy_version} matched: "
                        f"{', '.join(decision.matched)}; research-only refusal "
                        "without a generation call"
                    )
                ],
                advice_policy_version=decision.policy_version,
                run_id=run_id,
            )

    end = date.fromisoformat(period_end) if isinstance(period_end, str) else period_end
    card = build_india_fact_card(session, issuer_id, period_end=end, scope=scope)
    label = application_label_of(card)
    ticker = card.issuer.ticker_nse or card.issuer.issuer_id
    identity = primary_identity(card)
    base = {
        "issuer_id": card.issuer.issuer_id,
        "ticker": ticker,
        "scope": scope,
        "period_end": identity[1],
        "application_label": label,
        "facts": india_fact_summaries(card),
        "run_id": run_id,
    }

    # 1. retrieval over the India narrative corpus + bounded context ----------
    retrieval_started = time.perf_counter()
    if search_service is None:
        from quarterline.retrieve.search import SearchService, provider_from_settings

        search_service = SearchService(session, provider_from_settings(settings), settings=settings)
    query = SearchQuery(
        query=INDIA_BRIEF_RETRIEVAL_QUERY,
        ticker=ticker,
        strategy="section",
        retrieval="hybrid",
        mode="general",
        sections=list(INDIA_BRIEF_SECTIONS),
        period_end=identity[1],
        top_k=12,
    )
    try:
        result: SearchResult = search_service.search(query)
    except Exception as exc:
        from quarterline.retrieve.embeddings import EmbeddingModelMismatchError

        if not isinstance(exc, EmbeddingModelMismatchError):
            raise
        query.retrieval = "lexical"  # SPEC §25: lexical remains available
        result = search_service.search(query)
    bundle: ContextBundle = assemble_context(result.items, _context_budget(settings))
    retrieval_ms = (time.perf_counter() - retrieval_started) * 1000
    evidence = [
        EvidenceSummary(
            evidence_id=p.evidence_id,
            document_id=p.document_id,
            section=p.section,
            text=p.text,
        )
        for p in bundle.passages
    ]
    if result.insufficient_evidence or not bundle.passages:
        reason = result.insufficient_evidence_reason or "no admissible evidence windows"
        return IndiaBriefOutcome(
            status="insufficient_evidence",
            reasons=[
                (
                    f"insufficient evidence ({result.evidence_policy_version}): {reason}; "
                    "abstained without a generation call"
                )
            ],
            evidence=evidence,
            **base,
        )

    evidence_map = build_india_evidence_map(
        session, bundle, ticker=ticker, scoped_period=identity[1]
    )
    commentary_flags = commentary_flags_for(session, bundle)
    spec: PromptSpec = load_prompt("brief_india", "v1")
    provider = provider if provider is not None else default_generation_provider(settings)
    key = _cache_key(
        issuer_id=card.issuer.issuer_id,
        period_end=identity[1],
        scope=scope,
        card_hash=_card_hash(card),
        context_hash=_context_hash(bundle),
        provider_name=provider.provider_name,
        model=provider.model_id,
        prompt_version=spec.prompt_version,
        prompt_hash=spec.sha256,
        settings=settings,
    )

    # 2. cache lookup -----------------------------------------------------------
    cached = _cache_get(session, key)
    if cached is not None:
        report = ValidationReport.model_validate(cached["validation"])
        return IndiaBriefOutcome(
            status=cached["status"],
            brief=IndiaBrief.model_validate(cached["brief"]),
            metric_facts=list(cached["metric_facts"]),
            reasons=list(cached["reasons"]),
            validation=report,
            evidence=evidence,
            cache_hit=True,
            provider=cached["provider"],
            model=cached["model"],
            **base,
        )

    # 3-4. single provider call + ONE repair pass + the India gate --------------
    messages = _india_brief_messages(spec, card, label, bundle)
    gen_started = time.perf_counter()
    try:
        repaired: RepairOutcome = generate_and_parse(
            provider,
            messages=messages,
            model_cls=IndiaBrief,
            json_schema=india_brief_json_schema(
                label_echo=label,
                evidence_ids=[p.evidence_id for p in bundle.passages],
            ),
        )
    except Exception as exc:
        from quarterline.llm.base import (
            GenerationProviderUnavailable,
            RemoteFallbackNotConsented,
        )
        from quarterline.llm.repair import GenerationFailure as _GenFailure

        if isinstance(exc, (GenerationProviderUnavailable, RemoteFallbackNotConsented)):
            return _provider_down(base, evidence, exc, provider)
        if isinstance(exc, _GenFailure):
            return _controlled_failure(base, evidence, exc, provider)
        raise
    generation_ms = (time.perf_counter() - gen_started) * 1000

    surviving, report = validate_india_brief(
        repaired.model,
        card=card,
        label=label,
        scope=scope,
        evidence_map=evidence_map,
        commentary_flags=commentary_flags,
    )
    expansion = expand_india_metric_mentions(surviving.metric_mentions, card)
    metric_facts = [fact.text for fact in expansion.rendered]
    reasons = report.reasons() + [f"metric_mention dropped: {issue}" for issue in expansion.issues]
    final_status = finalize_india_status(surviving, report)
    if expansion.issues and final_status == "ok":
        final_status = "partial"

    # 5. cache persist (successes only) + C12 run event --------------------------
    if final_status in ("ok", "partial"):
        _cache_put(
            session,
            key,
            company_id=_company_id(session, ticker),
            period_end=identity[1],
            provider=provider.provider_name,
            model=provider.model_id,
            prompt_version=spec.prompt_version,
            payload={
                "brief": surviving.model_dump(mode="json"),
                "validation": report.model_dump(),
                "status": final_status,
                "metric_facts": metric_facts,
                "reasons": reasons,
                "provider": provider.provider_name,
                "model": provider.model_id,
            },
            settings=settings,
        )
    _emit_event(
        run_id=run_id,
        provider=provider,
        spec=spec,
        repair_outcome=repaired,
        report=report,
        final_status=final_status,
        cache_hit=False,
        retrieval_ms=retrieval_ms,
        generation_ms=generation_ms,
        total_ms=(time.perf_counter() - started) * 1000,
        ticker=ticker,
    )
    return IndiaBriefOutcome(
        status=final_status,
        brief=surviving,
        metric_facts=metric_facts,
        reasons=reasons,
        validation=report,
        evidence=evidence,
        provider=provider.provider_name,
        model=provider.model_id,
        **base,
    )


def _company_id(session: Session, ticker: str) -> int:
    from quarterline.store.repositories.companies import CompaniesRepo

    company = CompaniesRepo(session).get_by_ticker(ticker)
    return company.id if company is not None else 0


def _provider_down(base: dict, evidence: list, exc: Exception, provider) -> IndiaBriefOutcome:
    """SPEC §25: generation unavailable -> facts and evidence, never prose."""
    _emit_failure_event(base, provider, exc, "provider_unavailable")
    return IndiaBriefOutcome(
        status="provider_unavailable",
        reasons=[f"generation provider unavailable: {exc}"],
        evidence=evidence,
        provider=getattr(provider, "provider_name", None),
        model=getattr(provider, "model_id", None),
        **base,
    )


def _controlled_failure(base: dict, evidence: list, exc: Exception, provider) -> IndiaBriefOutcome:
    _emit_failure_event(base, provider, exc, "controlled_failure")
    return IndiaBriefOutcome(
        status="provider_unavailable",
        reasons=[
            (
                f"controlled generation failure: {exc}; showing facts and evidence, "
                "not generated prose"
            )
        ],
        evidence=evidence,
        provider=getattr(provider, "provider_name", None),
        model=getattr(provider, "model_id", None),
        **base,
    )


def _emit_failure_event(base: dict, provider, exc: Exception, error_type: str) -> None:
    emit_run_event(
        base.get("run_id") or new_run_id(),
        {
            "endpoint": "india_brief",
            "issuer_id": base.get("issuer_id"),
            "ticker": base.get("ticker"),
            "provider": getattr(provider, "provider_name", None),
            "model": getattr(provider, "model_id", None),
            "answer_status": "provider_unavailable",
            "cache_hit": False,
            "error_type": f"{error_type}: {type(exc).__name__}",
            "estimated_api_cost": None,
            "cost_currency": None,
            "cost_basis": "local-first; no API cost model configured",
        },
    )


def _emit_event(
    *,
    run_id: str,
    ticker: str,
    provider,
    spec: PromptSpec,
    repair_outcome: RepairOutcome,
    report: ValidationReport,
    final_status: str,
    cache_hit: bool,
    retrieval_ms: float,
    generation_ms: float,
    total_ms: float,
) -> None:
    results = repair_outcome.results
    input_tokens = max((r.input_tokens or 0) for r in results) if results else 0
    output_tokens = sum((r.output_tokens or 0) for r in results)
    emit_run_event(
        run_id,
        {
            "endpoint": "india_brief",
            "ticker": ticker,
            "provider": provider.provider_name,
            "model": provider.model_id,
            "prompt_version": spec.prompt_version,
            "prompt_hash": spec.sha256,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "token_count_source": (results[-1].token_count_source if results else "estimate"),
            "retrieval_latency_ms": round(retrieval_ms, 2),
            "generation_latency_ms": round(generation_ms, 2),
            "total_latency_ms": round(total_ms, 2),
            "json_valid": True,
            "json_valid_first_pass": repair_outcome.first_pass_valid,
            "repair_used": repair_outcome.repair_used,
            "citation_valid": report.citation_valid,
            "factcheck_passed": report.factcheck_passed,
            "retained_bullet_count": report.retained_bullet_count,
            "dropped_bullet_count": report.dropped_bullet_count,
            "answer_status": final_status,
            "cache_hit": cache_hit,
            "error_type": None,
            "estimated_api_cost": None,
            "cost_currency": None,
            "cost_basis": "local-first; no API cost model configured",
        },
    )


__all__ = [
    "FORMULA_VERSION_INDIA_METRICS",
    "INDIA_BRIEF_RETRIEVAL_QUERY",
    "INDIA_BRIEF_SECTIONS",
    "INDIA_CHANGE_METRICS",
    "INDIA_LEVEL_VALUE_IDS",
    "INDIA_LEVEL_WORDING_IDS",
    "INDIA_METRIC_REFERENCES",
    "INDIA_NARRATIVE_SECTIONS",
    "INDIA_VALIDATION_VERSION",
    "IndiaBrief",
    "IndiaBriefBullet",
    "IndiaBriefOutcome",
    "IndiaBriefRisk",
    "IndiaMemoOutput",
    "IndiaMemoSectionOutput",
    "IndiaMetricMention",
    "IndiaOpenQuestion",
    "application_label_of",
    "check_commentary_attribution",
    "check_india_statement_numbers",
    "check_scope_attribution",
    "commentary_flags_for",
    "expand_india_metric_mentions",
    "finalize_india_status",
    "generate_india_brief",
    "has_attribution_marker",
    "india_brief_json_schema",
    "india_fact_summaries",
    "india_gate_failed",
    "india_gate_failed_checks",
    "india_memo_json_schema",
    "india_template_valid_for",
    "primary_identity",
    "validate_india_brief",
]
