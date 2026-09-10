"""The constrained-writer pipeline and the SPEC §18 validation gate.

Pipeline (SPEC §17, §25):

1. Build the fact card (label included) and retrieve evidence via
   ``SearchService`` (mode presets, company filter), then assemble a bounded
   context.
2. Look up the generation cache in ``brief_cache`` keyed by SHA-256 of
   (company, period, fact-card hash, evidence/context hash, provider, model,
   prompt version + hash, generation settings, validation version) — cache
   keys reflect content versions, never just ticker+model.
3. Call the provider ONCE with ``[system, user]`` messages.
4. Parse via :mod:`quarterline.llm.repair` (one repair pass maximum), then run
   the validation gate.
5. Persist the cache row (successes only) and emit C12 run events.

Nothing raw reaches the user: content that fails any gate check is dropped as
a WHOLE statement with recorded reasons; dropped-but-not-empty becomes
``partial``, nothing-useful becomes ``insufficient_evidence``, provider
failures become ``provider_unavailable``, and advice content becomes
``refused``. Numbers the user sees are rendered by Python from the fact card
(:mod:`quarterline.core.factcheck`), never typed by the model.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from quarterline.config import Settings, get_settings
from quarterline.core.advice_policy import (
    ADVICE_POLICY_VERSION,
    RESEARCH_ONLY_REFUSAL,
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
    check_statement_numbers,
    expand_metric_mentions,
    format_value,
    template_valid_for,
)
from quarterline.core.models import METRIC_IDS, FactCard, MetricStatus
from quarterline.core.provenance import build_fact_card
from quarterline.llm.base import (
    GenerationProvider,
    GenerationProviderUnavailable,
    RemoteFallbackNotConsented,
)
from quarterline.llm.ollama import OllamaGenerationProvider
from quarterline.llm.openrouter import OpenRouterGenerationProvider
from quarterline.llm.prompts import PromptSpec, load_prompt, render_prompt
from quarterline.llm.repair import GenerationFailure, RepairOutcome, generate_and_parse
from quarterline.llm.schemas import (
    Answer,
    Brief,
    BriefBullet,
    BriefRisks,
    BriefStatus,
    MetricMention,
    OpenQuestion,
    answer_json_schema,
    brief_json_schema,
)
from quarterline.observability.events import emit_run_event, new_run_id
from quarterline.retrieve.context import ContextBundle, assemble_context
from quarterline.retrieve.context import (
    context_budget_from_settings as _context_budget,
)
from quarterline.retrieve.models import SearchQuery, SearchResult
from quarterline.store.models import BriefCache
from quarterline.store.repositories.documents import DocumentsRepo

#: Version of the validation gate (SPEC §18; part of the cache key).
VALIDATION_VERSION = "validation-v1"

#: Model statuses that mean "do not present generated content".
ABSTENTION_STATUSES: frozenset[str] = frozenset(
    {"insufficient_evidence", "provider_unavailable", "refused"}
)

#: Deterministic retrieval query for the quarter brief (the mode preset
#: supplies the mda + earnings_release section filter; the company filter
#: comes from the ticker).
BRIEF_RETRIEVAL_QUERY = "quarterly results revenue operating margin cash flow management discussion"

#: Display names for rule-based quarter labels (mirrors routers/companies.py;
#: kept here because the llm layer never imports the api layer).
LABEL_DISPLAY_NAMES: dict[str, str] = {
    "strong": "Strong",
    "mixed": "Mixed",
    "weak": "Weak",
    "insufficient_data": "Insufficient data",
}

_GATE_CHECK_IDS = ("4", "5", "6", "7", "9", "10")


# ---------------------------------------------------------------------------
# Gate + outcome DTOs
# ---------------------------------------------------------------------------


class ValidationCheckResult(BaseModel):
    """One SPEC §18 check outcome (observable, testable)."""

    check_id: str
    name: str
    passed: bool
    dropped_count: int = 0
    reasons: list[str] = Field(default_factory=list)


class ValidationReport(BaseModel):
    """Every check result plus rejection reasons (versioned)."""

    version: str = VALIDATION_VERSION
    checks: list[ValidationCheckResult] = Field(default_factory=list)
    retained_bullet_count: int = 0
    dropped_bullet_count: int = 0
    retained_risk_count: int = 0
    dropped_risk_count: int = 0
    dropped_open_question_count: int = 0
    retained_mention_count: int = 0
    dropped_mention_count: int = 0

    @property
    def citation_valid(self) -> bool:
        return all(check.passed for check in self.checks if check.check_id in ("4", "5", "6", "7"))

    @property
    def factcheck_passed(self) -> bool:
        return all(check.passed for check in self.checks if check.check_id == "9")

    @property
    def gate_failed(self) -> bool:
        return any(not check.passed for check in self.checks if check.check_id in _GATE_CHECK_IDS)

    def reasons(self) -> list[str]:
        return [
            f"check[{check.check_id}]: {reason}"
            for check in self.checks
            for reason in check.reasons
        ]


class EvidenceSummary(BaseModel):
    """One supplied evidence window, for display and resolvable links."""

    evidence_id: str
    document_id: int
    section: str | None = None
    text: str


class MetricFactSummary(BaseModel):
    """Fact-card row for panels (display string; never a float)."""

    metric_id: str
    display: str
    status: str


class GenerationOutcome(BaseModel):
    """Everything the API/UI needs; nothing raw or unvalidated inside."""

    kind: Literal["brief", "ask"]
    status: BriefStatus
    ticker: str | None = None
    period_end: date | None = None
    label_value: str | None = None
    label_display: str | None = None
    brief: Brief | None = None
    answer: Answer | None = None
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


# ---------------------------------------------------------------------------
# Fact-card / label helpers
# ---------------------------------------------------------------------------


def label_value_of(card: FactCard) -> str | None:
    """The rule-based label value from the card's quarter_label metric."""
    for metric in card.metrics:
        if metric.metric_id != "quarter_label":
            continue
        for ref in metric.provenance.derived_from:
            if ref.startswith("label:"):
                return ref.split(":", 1)[1]
    return None


def label_display_of(card: FactCard) -> str | None:
    value = label_value_of(card)
    return LABEL_DISPLAY_NAMES.get(value, value) if value else None


def fact_summaries(card: FactCard) -> list[MetricFactSummary]:
    """Display rows for every card metric (missing stays missing, SPEC §2.1)."""
    rows: list[MetricFactSummary] = []
    for metric in card.metrics:
        if metric.status is MetricStatus.ok and metric.value is not None:
            display = format_value(metric)
        else:
            display = ""
        rows.append(
            MetricFactSummary(
                metric_id=metric.metric_id, display=display, status=metric.status.value
            )
        )
    return rows


def _fact_card_hash(card: FactCard) -> str:
    """Content hash of the card excluding its build timestamp."""
    return hashlib.sha256(
        card.model_dump_json(exclude={"generated_at"}).encode("utf-8")
    ).hexdigest()


def _context_hash(bundle: ContextBundle) -> str:
    parts = [
        f"{p.evidence_id}|{p.document_id}|{p.start_offset}|{p.end_offset}|{p.text}"
        for p in bundle.passages
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Evidence map (the only citable material)
# ---------------------------------------------------------------------------


def build_evidence_map(
    session: Session, bundle: ContextBundle, *, ticker: str, scoped_period: date | None
) -> dict[str, EvidenceRef]:
    """evidence_id -> company/period-qualified evidence reference.

    Period identity comes from the ``documents`` row (the reporting context the
    window was filed under). When ``scoped_period`` is given (the brief is
    period-scoped) the document's period travels with the reference so the
    gate can reject wrong-period citations; QA is not period-scoped, so its
    references carry the document period when known and the gate checks only
    company identity.
    """
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


# ---------------------------------------------------------------------------
# Validation gate (SPEC §18, checks 1–10 in order)
# ---------------------------------------------------------------------------

_ABSTENTION_NOTE = "model returned an abstention status; no content is presented"

#: (citation reason bucket, check id, check name) in SPEC §18 order.
_CITATION_CHECKS: tuple[tuple[str, str, str], ...] = (
    (CHECK_EXISTENCE, "4", "citation_existence"),
    (CHECK_MEMBERSHIP, "5", "citation_supplied_context"),
    (CHECK_CONTEXT, "6", "citation_company_period"),
    (CHECK_FORMAT, "7", "citation_sentence_format"),
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


def validate_generation(
    parsed: Brief,
    *,
    card: FactCard,
    label_display: str | None,
    evidence_map: dict[str, EvidenceRef],
) -> tuple[Brief, ValidationReport]:
    """Run checks 1–10 in order; return (surviving brief, report)."""
    report = ValidationReport()
    checks = report.checks

    # -- check 1: schema validity (pydantic Brief, extra=forbid) ---------------
    _add_check(checks, "1", "schema_validity", True)

    # -- check 2: status validity / model abstention ----------------------------
    if parsed.status in ABSTENTION_STATUSES:
        _add_check(checks, "2", "status_validity", True, [_ABSTENTION_NOTE])
        return Brief(status=parsed.status, label_echo=None), report
    _add_check(checks, "2", "status_validity", True)

    # -- check 3: label echo EQUALITY against the code-generated label ----------
    echoed = (parsed.label_echo or "").strip()
    expected = (label_display or "").strip()
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
            "label_echo_equality",
            False,
            [
                (
                    f"label_echo {echoed!r} does not equal the code-generated label "
                    f"{expected!r}; all content dropped"
                )
            ],
            dropped=dropped_all,
        )
        return Brief(status="insufficient_evidence", label_echo=None), report
    _add_check(checks, "3", "label_echo_equality", True)

    # -- checks 4–7: citations, per statement -----------------------------------
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
            # SPEC §2.2.1: filing-derived assertions require citations.
            reasons.append(
                f"{CHECK_EXISTENCE}: statement makes a filing-derived assertion "
                "with no supplied citation"
            )
        reasons.extend(
            validate_statement_citations(
                text,
                ids,
                evidence_map,
                expected_ticker=card.ticker,
                expected_period_end=card.period_end,
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

    # -- check 8: metric-reference validity (allowlist + structural template) ---
    kept_mentions: list[MetricMention] = []
    mention_reasons: list[str] = []
    for mention in parsed.metric_mentions:
        if mention.metric_id not in METRIC_IDS:  # defense in depth (pydantic gates too)
            mention_reasons.append(
                f"metric_id {mention.metric_id!r} is not in the METRIC_IDS allowlist"
            )
        elif not template_valid_for(mention.metric_id, mention.template):
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

    # -- check 9: numeric consistency on surviving free text ---------------------
    numeric_kept: list[tuple[str, int, str, list[str]]] = []
    numeric_reasons: list[str] = []
    for kind, index, text, ids in citation_kept:
        reasons = check_statement_numbers(text, card)
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

    # -- check 10: advice-policy compliance ---------------------------------------
    final_kept: list[tuple[str, int, str, list[str]]] = []
    advice_reasons: list[str] = []
    for kind, index, text, ids in numeric_kept:
        decision = detect_advice_content(text)
        if decision.is_advice:
            advice_reasons.append(
                f"{kind}[{index}] generated content issues investment advice "
                f"({', '.join(decision.matched)}); statement dropped"
            )
        else:
            final_kept.append((kind, index, text, ids))
    _add_check(
        checks,
        "10",
        "advice_policy_compliance",
        not advice_reasons,
        advice_reasons,
        dropped=len(numeric_kept) - len(final_kept),
    )

    surviving = _surviving_brief(parsed, final_kept, kept_mentions, label_ok, label_display)
    _record_counts(report, parsed, final_kept, kept_mentions)
    return surviving, report


def _surviving_brief(
    parsed: Brief,
    kept: list[tuple[str, int, str, list[str]]],
    kept_mentions: list[MetricMention],
    label_ok: bool,
    label_display: str | None,
) -> Brief:
    bullets: list[BriefBullet] = []
    risks: list[BriefRisks] = []
    questions: list[OpenQuestion] = []
    for kind, index, text, ids in kept:
        if kind == "bullet":
            bullets.append(BriefBullet(text=text, evidence_ids=ids))
        elif kind == "risk":
            risks.append(BriefRisks(text=text, evidence_ids=ids))
        else:
            questions.append(OpenQuestion(text=text, evidence_ids=ids))
    return Brief(
        status="ok",
        label_echo=label_display if label_ok else None,
        metric_mentions=kept_mentions,
        bullets=bullets,
        risks=risks,
        open_questions=questions,
    )


def _record_counts(
    report: ValidationReport,
    parsed: Brief,
    kept: list[tuple[str, int, str, list[str]]],
    kept_mentions: list[MetricMention],
) -> None:
    kinds = [kind for kind, _index, _text, _ids in kept]
    report.retained_bullet_count = kinds.count("bullet")
    report.retained_risk_count = kinds.count("risk")
    report.dropped_bullet_count = len(parsed.bullets) - report.retained_bullet_count
    report.dropped_risk_count = len(parsed.risks) - report.retained_risk_count
    report.dropped_open_question_count = len(parsed.open_questions) - kinds.count("open_question")
    report.retained_mention_count = len(kept_mentions)
    report.dropped_mention_count = len(parsed.metric_mentions) - len(kept_mentions)


def finalize_status(surviving: Brief, report: ValidationReport) -> BriefStatus:
    """Survival rule (SPEC §18): dropped content -> ``partial``; nothing useful
    -> ``insufficient_evidence``; advice-driven total loss -> ``refused``."""
    has_content = bool(
        surviving.bullets
        or surviving.risks
        or surviving.open_questions
        or surviving.metric_mentions
    )
    if has_content:
        dropped_anything = (
            report.gate_failed
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


# ---------------------------------------------------------------------------
# Provider + retrieval defaults
# ---------------------------------------------------------------------------


def default_generation_provider(settings: Settings | None = None) -> GenerationProvider:
    """Provider from configuration. Selecting ``openrouter`` constructs the
    remote provider, whose consent gate raises
    :class:`RemoteFallbackNotConsented` unless ALLOW_REMOTE_FALLBACK, an API
    key and a model are ALL configured."""
    settings = settings if settings is not None else get_settings()
    if settings.llm_provider == "openrouter":
        return OpenRouterGenerationProvider(settings)
    return OllamaGenerationProvider(
        settings.ollama_base_url,
        settings.ollama_model,
        num_ctx=settings.ollama_num_ctx,
        timeout_seconds=settings.generation_timeout_seconds,
    )


def _default_search_service(session: Session, settings: Settings):
    from quarterline.retrieve.search import SearchService, provider_from_settings

    return SearchService(session, provider_from_settings(settings), settings=settings)


def _retrieve(
    search_service, query_text: str, ticker: str, mode: str, period_end: date | None
) -> SearchResult:
    """Hybrid retrieval with company + mode filters; an embedding-model
    mismatch (SPEC §15.7) degrades to lexical instead of failing the request
    (SPEC §25)."""
    from quarterline.retrieve.embeddings import EmbeddingModelMismatchError

    query = SearchQuery(
        query=query_text,
        ticker=ticker,
        strategy="section",
        retrieval="hybrid",
        mode=mode,  # type: ignore[arg-type]
        period_end=period_end,
        top_k=12,
    )
    try:
        return search_service.search(query)
    except EmbeddingModelMismatchError:
        query.retrieval = "lexical"
        return search_service.search(query)


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def _catalog_lines(card: FactCard) -> str:
    lines = []
    for metric in card.metrics:
        unit = metric.unit or "n/a"
        lines.append(f"{metric.metric_id} (unit: {unit}; availability: {metric.status.value})")
    return "\n".join(lines)


def _brief_messages(
    spec: PromptSpec, card: FactCard, label_display: str | None, bundle: ContextBundle
) -> list[dict]:
    system = render_prompt(
        spec,
        ticker=card.ticker,
        period=card.period_end.isoformat(),
        label=label_display or "Insufficient data",
        allowlist=", ".join(sorted(METRIC_IDS)),
        catalog=_catalog_lines(card),
    )
    payload = {
        "task": "quarter_brief",
        "ticker": card.ticker,
        "period_end": card.period_end.isoformat(),
        "required_label_echo": label_display or "Insufficient data",
        "metric_allowlist": sorted(METRIC_IDS),
        "evidence": [
            {"evidence_id": p.evidence_id, "section": p.section, "text": p.text}
            for p in bundle.passages
        ],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _ask_messages(
    spec: PromptSpec, question: str, ticker: str | None, bundle: ContextBundle
) -> list[dict]:
    scope = (
        f"{ticker} filings only (company-scoped retrieval)"
        if ticker
        else "the supplied evidence only"
    )
    system = render_prompt(spec, scope=scope, question=question)
    payload = {
        "task": "answer_question",
        "question": question,
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
# Cache (SPEC §25 generation cache)
# ---------------------------------------------------------------------------


def _cache_key(
    *,
    ticker: str,
    period_end: date | None,
    card_hash: str,
    context_hash: str,
    provider_name: str,
    model: str,
    prompt_version: str,
    prompt_hash: str,
    settings: Settings,
    mode: str,
) -> str:
    material = json.dumps(
        {
            "company": ticker,
            "period": period_end.isoformat() if period_end else None,
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
                "mode": mode,
            },
            "validation_version": VALIDATION_VERSION,
            "factcheck_version": FACTCHECK_VERSION,
            "advice_policy_version": ADVICE_POLICY_VERSION,
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
# Public entry points
# ---------------------------------------------------------------------------


def generate_brief(
    ticker: str,
    period_end: date | None = None,
    *,
    mode: str = "brief",
    session: Session | None = None,
    provider: GenerationProvider | None = None,
    search_service=None,
    settings: Settings | None = None,
) -> GenerationOutcome:
    """Grounded quarter brief for one company-period (SPEC §17)."""
    if session is not None:
        return _generate_brief(
            session,
            ticker,
            period_end,
            mode=mode,
            provider=provider,
            search_service=search_service,
            settings=settings,
        )
    from quarterline.store.db import session_scope

    with session_scope() as owned_session:
        return _generate_brief(
            owned_session,
            ticker,
            period_end,
            mode=mode,
            provider=provider,
            search_service=search_service,
            settings=settings,
        )


def answer_question(
    question: str,
    ticker: str | None = None,
    *,
    mode: str = "general",
    session: Session | None = None,
    provider: GenerationProvider | None = None,
    search_service=None,
    settings: Settings | None = None,
) -> GenerationOutcome:
    """Grounded QA answer (SPEC §17 qa path + advice policy)."""
    if session is not None:
        return _answer_question(
            session,
            question,
            ticker,
            mode=mode,
            provider=provider,
            search_service=search_service,
            settings=settings,
        )
    from quarterline.store.db import session_scope

    with session_scope() as owned_session:
        return _answer_question(
            owned_session,
            question,
            ticker,
            mode=mode,
            provider=provider,
            search_service=search_service,
            settings=settings,
        )


# ---------------------------------------------------------------------------
# Internals: brief
# ---------------------------------------------------------------------------


def _generate_brief(
    session: Session,
    ticker: str,
    period_end: date | None,
    *,
    mode: str,
    provider: GenerationProvider | None,
    search_service,
    settings: Settings | None,
) -> GenerationOutcome:
    settings = settings if settings is not None else get_settings()
    run_id = new_run_id()
    started = time.perf_counter()

    card = build_fact_card(session, ticker, period_end)
    label_display = label_display_of(card)
    base = {
        "kind": "brief",
        "ticker": card.ticker,
        "period_end": card.period_end,
        "label_value": label_value_of(card),
        "label_display": label_display,
        "facts": fact_summaries(card),
        "run_id": run_id,
    }

    # 1. retrieval + bounded context -------------------------------------------
    retrieval_started = time.perf_counter()
    if search_service is None:
        search_service = _default_search_service(session, settings)
    result = _retrieve(search_service, BRIEF_RETRIEVAL_QUERY, card.ticker, mode, card.period_end)
    bundle = assemble_context(result.items, _context_budget(settings))
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
        # Evidence policy says abstain: no generation call at all (SPEC §25).
        reason = result.insufficient_evidence_reason or "no admissible evidence windows"
        return GenerationOutcome(
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

    evidence_map = build_evidence_map(
        session, bundle, ticker=card.ticker, scoped_period=card.period_end
    )
    # v2: added a concrete metric_mentions example after qwen3:4b and
    # qwen2.5:7b both systematically swapped metric_id/template (measured
    # 2026-09-09); see docs/retrieval_experiments.md and the wave-5 log.
    spec = load_prompt("brief", "v2")
    provider = provider if provider is not None else default_generation_provider(settings)
    key = _cache_key(
        ticker=card.ticker,
        period_end=card.period_end,
        card_hash=_fact_card_hash(card),
        context_hash=_context_hash(bundle),
        provider_name=provider.provider_name,
        model=provider.model_id,
        prompt_version=spec.prompt_version,
        prompt_hash=spec.sha256,
        settings=settings,
        mode=mode,
    )

    # 2. cache lookup -----------------------------------------------------------
    cached = _cache_get(session, key)
    if cached is not None:
        report = ValidationReport.model_validate(cached["validation"])
        return GenerationOutcome(
            status=cached["status"],
            brief=Brief.model_validate(cached["brief"]),
            metric_facts=list(cached["metric_facts"]),
            reasons=list(cached["reasons"]),
            validation=report,
            evidence=evidence,
            cache_hit=True,
            provider=cached["provider"],
            model=cached["model"],
            **base,
        )

    # 3–4. single provider call + repair + validation gate -----------------------
    messages = _brief_messages(spec, card, label_display, bundle)
    gen_started = time.perf_counter()
    try:
        repaired: RepairOutcome = generate_and_parse(
            provider,
            messages=messages,
            model_cls=Brief,
            json_schema=brief_json_schema(
                label_echo=label_display,
                evidence_ids=[p.evidence_id for p in bundle.passages],
            ),
        )
    except GenerationProviderUnavailable as exc:
        return _provider_down(base, evidence, exc, provider)
    except RemoteFallbackNotConsented as exc:
        return _provider_down(base, evidence, exc, provider)
    except GenerationFailure as exc:
        return _controlled_failure(base, evidence, exc, provider)
    generation_ms = (time.perf_counter() - gen_started) * 1000

    surviving, report = validate_generation(
        repaired.model, card=card, label_display=label_display, evidence_map=evidence_map
    )
    expansion = expand_metric_mentions(surviving.metric_mentions, card)
    metric_facts = [fact.text for fact in expansion.rendered]
    reasons = report.reasons() + [f"metric_mention dropped: {issue}" for issue in expansion.issues]
    final_status = finalize_status(surviving, report)
    if expansion.issues and final_status == "ok":
        final_status = "partial"

    # 5. cache persist (successes only) + run events ------------------------------
    if final_status in ("ok", "partial"):
        _cache_put(
            session,
            key,
            company_id=_company_id(session, card.ticker),
            period_end=card.period_end,
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
        endpoint="brief",
        ticker=card.ticker,
        provider=provider,
        spec=spec,
        repair_outcome=repaired,
        report=report,
        final_status=final_status,
        cache_hit=False,
        retrieval_ms=retrieval_ms,
        generation_ms=generation_ms,
        total_ms=(time.perf_counter() - started) * 1000,
    )
    return GenerationOutcome(
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


# ---------------------------------------------------------------------------
# Internals: ask
# ---------------------------------------------------------------------------


def _answer_question(
    session: Session,
    question: str,
    ticker: str | None,
    *,
    mode: str,
    provider: GenerationProvider | None,
    search_service,
    settings: Settings | None,
) -> GenerationOutcome:
    settings = settings if settings is not None else get_settings()
    run_id = new_run_id()
    started = time.perf_counter()

    # Advice policy FIRST: advice-seeking requests are refused without any
    # provider call (SPEC §26 "Advice refusal"; §12.2 "best stocks" language).
    decision = detect_advice_request(question)
    if decision.is_advice:
        return GenerationOutcome(
            kind="ask",
            status="refused",
            ticker=ticker,
            answer=Answer(status="refused", text=RESEARCH_ONLY_REFUSAL, evidence_ids=[]),
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
    if not ticker:
        return GenerationOutcome(
            kind="ask",
            status="insufficient_evidence",
            answer=Answer(
                status="insufficient_evidence",
                text=(
                    "Evidence retrieval is company-scoped; supply a ticker so the "
                    "question can be matched against that company's filings."
                ),
                evidence_ids=[],
            ),
            reasons=["no ticker supplied; retrieval requires a company scope"],
            run_id=run_id,
        )

    if search_service is None:
        search_service = _default_search_service(session, settings)
    retrieval_started = time.perf_counter()
    result = _retrieve(search_service, question, ticker, mode, None)
    bundle = assemble_context(result.items, _context_budget(settings))
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
        return GenerationOutcome(
            kind="ask",
            status="insufficient_evidence",
            ticker=ticker,
            answer=Answer(
                status="insufficient_evidence",
                text=(
                    "The supplied filings do not contain enough evidence to answer this question."
                ),
                evidence_ids=[],
            ),
            reasons=[f"insufficient evidence ({result.evidence_policy_version}): {reason}"],
            evidence=evidence,
            run_id=run_id,
        )

    evidence_map = {
        passage.evidence_id: EvidenceRef(
            evidence_id=passage.evidence_id,
            document_id=passage.document_id,
            ticker=ticker,
            # QA is not period-scoped (mode section presets still apply), so no
            # period equality is enforced; company identity still is.
            period_end=None,
            text=passage.text,
        )
        for passage in bundle.passages
    }
    spec = load_prompt("qa", "v1")
    provider = provider if provider is not None else default_generation_provider(settings)
    messages = _ask_messages(spec, question, ticker, bundle)
    gen_started = time.perf_counter()
    base = {"kind": "ask", "ticker": ticker, "run_id": run_id}
    try:
        repaired: RepairOutcome = generate_and_parse(
            provider, messages=messages, model_cls=Answer, json_schema=answer_json_schema()
        )
    except GenerationProviderUnavailable as exc:
        return _provider_down(base, evidence, exc, provider)
    except RemoteFallbackNotConsented as exc:
        return _provider_down(base, evidence, exc, provider)
    except GenerationFailure as exc:
        return _controlled_failure(base, evidence, exc, provider)
    generation_ms = (time.perf_counter() - gen_started) * 1000

    answer = repaired.model
    report = ValidationReport()
    _validate_answer(answer, ticker, evidence_map, report)
    final_status = _finalize_answer_status(answer, report)
    _emit_event(
        run_id=run_id,
        endpoint="ask",
        ticker=ticker,
        provider=provider,
        spec=spec,
        repair_outcome=repaired,
        report=report,
        final_status=final_status,
        cache_hit=False,
        retrieval_ms=retrieval_ms,
        generation_ms=generation_ms,
        total_ms=(time.perf_counter() - started) * 1000,
    )
    return GenerationOutcome(
        kind="ask",
        status=final_status,
        ticker=ticker,
        answer=answer,
        reasons=report.reasons(),
        validation=report,
        evidence=evidence,
        provider=provider.provider_name,
        model=provider.model_id,
        run_id=run_id,
    )


def _validate_answer(
    answer: Answer,
    ticker: str,
    evidence_map: dict[str, EvidenceRef],
    report: ValidationReport,
) -> None:
    """Answer-side gate: schema (parsed), status, citations, numbers, advice."""
    checks = report.checks

    _add_check(checks, "1", "schema_validity", True)
    if answer.status in ABSTENTION_STATUSES:
        _add_check(checks, "2", "status_validity", True, [_ABSTENTION_NOTE])
        return
    _add_check(checks, "2", "status_validity", True)

    citation_reasons = validate_statement_citations(
        answer.text,
        answer.evidence_ids,
        evidence_map,
        expected_ticker=ticker,
        expected_period_end=None,
    )
    if answer.status == "ok" and not answer.evidence_ids:
        citation_reasons.insert(
            0,
            f"{CHECK_EXISTENCE}: an ok answer must cite at least one supplied evidence id",
        )
    per_check: dict[str, list[str]] = {}
    for reason in citation_reasons:
        bucket, _, detail = reason.partition(":")
        per_check.setdefault(bucket, []).append(detail.strip())
    for bucket, check_id, name in _CITATION_CHECKS:
        reasons = per_check.get(bucket, [])
        _add_check(checks, check_id, name, not reasons, reasons, dropped=1 if reasons else 0)

    # QA answers carry no fact card: a typed number has no semantically
    # compatible metric and is unsupported (strict by design).
    numeric_reasons = check_statement_numbers(answer.text, _empty_card(ticker))
    _add_check(
        checks,
        "9",
        "numeric_consistency",
        not numeric_reasons,
        numeric_reasons,
        dropped=1 if numeric_reasons else 0,
    )
    advice = detect_advice_content(answer.text)
    _add_check(
        checks,
        "10",
        "advice_policy_compliance",
        not advice.is_advice,
        [f"generated content issues investment advice ({', '.join(advice.matched)})"]
        if advice.is_advice
        else None,
        dropped=1 if advice.is_advice else 0,
    )
    gate_failed = any(not check.passed for check in checks if check.check_id in _GATE_CHECK_IDS)
    report.retained_bullet_count = 0 if gate_failed else 1
    report.dropped_bullet_count = 1 if gate_failed else 0


def _finalize_answer_status(answer: Answer, report: ValidationReport) -> BriefStatus:
    if answer.status in ABSTENTION_STATUSES:
        return answer.status  # type: ignore[return-value]
    if report.gate_failed:
        advice_failed = any(check.check_id == "10" and not check.passed for check in report.checks)
        return "refused" if advice_failed else "insufficient_evidence"
    return "ok"


def _empty_card(ticker: str) -> FactCard:
    """An empty quarter card for QA numeric checks (no metrics -> no free-text
    number can ever be supported there; qualitative answers only)."""
    now = datetime.now(UTC)
    return FactCard(ticker=ticker, cik="", period_end=now.date(), generated_at=now)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _company_id(session: Session, ticker: str) -> int:
    from quarterline.store.repositories.companies import CompaniesRepo

    company = CompaniesRepo(session).get_by_ticker(ticker)
    return company.id if company is not None else 0


def _provider_down(
    base: dict, evidence: list[EvidenceSummary], exc: Exception, provider: GenerationProvider
) -> GenerationOutcome:
    """SPEC §25: generation unavailable -> show facts and evidence, not prose."""
    _emit_failure_event(base, provider, exc, "provider_unavailable")
    return GenerationOutcome(
        kind=base.get("kind", "brief"),  # type: ignore[arg-type]
        status="provider_unavailable",
        ticker=base.get("ticker"),
        period_end=base.get("period_end"),
        label_value=base.get("label_value"),
        label_display=base.get("label_display"),
        facts=base.get("facts", []),
        reasons=[f"generation provider unavailable: {exc}"],
        evidence=evidence,
        provider=provider.provider_name,
        model=provider.model_id,
        run_id=base.get("run_id", ""),
    )


def _controlled_failure(
    base: dict, evidence: list[EvidenceSummary], exc: Exception, provider: GenerationProvider
) -> GenerationOutcome:
    """SPEC §25: JSON invalid after repair -> controlled failure response."""
    _emit_failure_event(base, provider, exc, "controlled_failure")
    return GenerationOutcome(
        kind=base.get("kind", "brief"),  # type: ignore[arg-type]
        status="provider_unavailable",
        ticker=base.get("ticker"),
        period_end=base.get("period_end"),
        label_value=base.get("label_value"),
        label_display=base.get("label_display"),
        facts=base.get("facts", []),
        reasons=[
            f"controlled generation failure: {exc}; showing facts and evidence, not generated prose"
        ],
        evidence=evidence,
        provider=provider.provider_name,
        model=provider.model_id,
        run_id=base.get("run_id", ""),
    )


def _emit_failure_event(
    base: dict, provider: GenerationProvider | None, exc: Exception, error_type: str
) -> None:
    emit_run_event(
        base.get("run_id") or new_run_id(),
        {
            "endpoint": base.get("kind", "brief"),
            "company_id": None,
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
    endpoint: str,
    ticker: str,
    provider: GenerationProvider,
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
            "endpoint": endpoint,
            "company_id": None,
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
            "validation_latency_ms": 0.0,
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
    "ABSTENTION_STATUSES",
    "BRIEF_RETRIEVAL_QUERY",
    "LABEL_DISPLAY_NAMES",
    "VALIDATION_VERSION",
    "GenerationOutcome",
    "MetricFactSummary",
    "ValidationCheckResult",
    "ValidationReport",
    "answer_question",
    "build_evidence_map",
    "default_generation_provider",
    "fact_summaries",
    "finalize_status",
    "generate_brief",
    "label_display_of",
    "label_value_of",
    "validate_generation",
]
