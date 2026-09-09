"""Generation evaluation harness (SPEC §22).

The harness is decoupled from the generation implementation (F5 owns
``quarterline.llm``): it calls a :class:`GenerationRunner` PROTOCOL and never
imports F5 internals. In tests a scripted fake runner produces valid /
invalid / repair / abstain / refuse outputs so every metric is exercised
offline; with a real runner the same metrics measure that runner.

Metric definitions (exact):

- **JSON validity before / after repair** — fraction of runs whose raw output
  parses as JSON, respectively whose post-repair output parses.
- **Citation validity rate** — cited evidence ids that were actually supplied
  to the runner, over all citations (n reported; a run with zero citations
  contributes zero citations, not an error).
- **Numeric error rate before filtering** — claimed numeric tokens that do
  NOT occur in any supplied evidence text, over all claimed numbers.
- **Unsupported content remaining after filtering** — answered runs with at
  least one still-unbacked numeric claim after the normalization/filtering
  step. Deterministic proxy: it cannot catch paraphrased unsupported prose
  (that is the optional judge's job), only unbacked numbers.
- **Bullet retention rate** — retained bullets / (retained + dropped).
- **Empty-answer rate** — answered runs with blank answer text.
- **Answer relevancy** — documented PROXY, not semantic quality: mean of
  question-term overlap with the answer and required-concepts coverage.
- **Correct abstention rate** — expected ``insufficient_evidence`` questions
  that returned ``insufficient_evidence``.
- **Refusal accuracy** — expected ``refusal`` questions that returned
  ``refused`` WITH the research-only alternative offered.
- **Provider-failure rate** — runs returning ``provider_unavailable``.

The three non-answer events (insufficient evidence / refusal / provider
failure) are counted as DISTINCT outcomes (SPEC §21/§22) — never folded into
one "didn't answer" bucket.

Each run also emits a §24 observability event and, when a session is
supplied, ``eval_runs``/``eval_results`` rows are persisted (provider,
model, prompt version, per-question results).

The bundled :class:`ScriptedFakeGenerationRunner` is a NON-MEASUREMENT
fixture: it validates this harness offline. It does NOT measure generation
quality, and no report may present its numbers as such (SPEC §23).
"""

from __future__ import annotations

import json
import re
import time
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from quarterline.observability.events import emit_run_event, hash_text, new_run_id

#: Answer statuses a runner may report (aligned with the brief statuses, but
#: defined here so the eval harness never imports quarterline.llm).
STATUS_ANSWER = "answer"
STATUS_INSUFFICIENT = "insufficient_evidence"
STATUS_REFUSED = "refused"
STATUS_PROVIDER_UNAVAILABLE = "provider_unavailable"

RUNNER_PROMPT_VERSION = "harness-v1"

_NUMBER_RE = re.compile(r"\d[\d,\.]*")
_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}")


# ---------------------------------------------------------------------------
# Protocol + output contract
# ---------------------------------------------------------------------------


class GenerationOutput(BaseModel):
    """What a :class:`GenerationRunner` returns for one question.

    ``raw_text`` is the model-facing output BEFORE any repair; ``repaired_text``
    the output after the deterministic JSON repair step (equal to ``raw_text``
    when no repair was needed).
    """

    raw_text: str
    repaired_text: str
    status: str  # answer | insufficient_evidence | refused | provider_unavailable
    answer_text: str = ""
    citations: list[str] = Field(default_factory=list)  # evidence ids
    claimed_numbers: list[str] = Field(default_factory=list)
    bullets_total: int = 0
    bullets_retained: int = 0
    bullets_dropped: int = 0
    refusal_alternative: bool = False  # research-only alternative offered
    provider: str = "scripted-fake"
    model: str = "scripted-fake"
    prompt_version: str = RUNNER_PROMPT_VERSION
    input_tokens: int | None = None
    output_tokens: int | None = None
    token_count_source: str = "not_counted_scripted"
    latency_ms: float = 0.0
    error_type: str | None = None


@runtime_checkable
class GenerationRunner(Protocol):
    """The interface the harness calls — NEVER F5 internals directly."""

    def run(self, question, passages: list[dict]) -> GenerationOutput:
        """Generate for one EvalQuestion given supplied evidence passages."""
        ...  # pragma: no cover


class ScriptedFakeGenerationRunner:
    """Deterministic scripted runner. FOR HARNESS TESTS ONLY — non-production.

    Scripts are keyed by expected behavior (+ an optional per-question
    override map), producing one output per shape so every metric has a
    known-value case offline. Numbers from this runner are harness checks,
    never generation-quality measurements (SPEC §23).
    """

    def __init__(self, overrides: dict[str, GenerationOutput] | None = None) -> None:
        self.overrides = overrides or {}

    def run(self, question, passages: list[dict]) -> GenerationOutput:
        if question.id in self.overrides:
            return self.overrides[question.id]
        evidence_ids = [passage["evidence_id"] for passage in passages]
        evidence_text = " ".join(passage.get("text", "") for passage in passages)
        if question.expected_behavior == "refusal":
            return GenerationOutput(
                raw_text='{"status": "refused", "answer": "", "citations": [], "bullets": []}',
                repaired_text='{"status": "refused", "answer": "", "citations": [], "bullets": []}',
                status=STATUS_REFUSED,
                refusal_alternative=True,
                provider="scripted-fake",
                model="scripted-fake",
                latency_ms=1.0,
            )
        if question.expected_behavior == "insufficient_evidence":
            return GenerationOutput(
                raw_text=(
                    '{"status": "insufficient_evidence", "answer": "", '
                    '"citations": [], "bullets": []}'
                ),
                repaired_text=(
                    '{"status": "insufficient_evidence", "answer": "", '
                    '"citations": [], "bullets": []}'
                ),
                status=STATUS_INSUFFICIENT,
                provider="scripted-fake",
                model="scripted-fake",
                latency_ms=1.0,
            )
        numbers = [token for token in _NUMBER_RE.findall(evidence_text)][:2]
        payload = {
            "status": "ok",
            "answer": " ".join(question.required_concepts[:1]) or "grounded answer",
            "citations": evidence_ids[:1],
            "bullets": [f"Grounded point citing {evidence_ids[0]}"] if evidence_ids else [],
        }
        text = json.dumps(payload)
        return GenerationOutput(
            raw_text=text,
            repaired_text=text,
            status=STATUS_ANSWER,
            answer_text=payload["answer"],
            citations=list(payload["citations"]),
            claimed_numbers=numbers,
            bullets_total=1,
            bullets_retained=1,
            provider="scripted-fake",
            model="scripted-fake",
            latency_ms=1.0,
        )


# ---------------------------------------------------------------------------
# Deterministic checkers
# ---------------------------------------------------------------------------


def json_valid(text: str) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def repair_json(text: str) -> str:
    """Deterministic minimal repair: strip code fences, trailing commas."""
    repaired = text.strip()
    if repaired.startswith("```"):
        repaired = re.sub(r"^```[a-zA-Z0-9]*\n", "", repaired)
        repaired = re.sub(r"\n```$", "", repaired)
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    return repaired


class GenerationQuestionRecord(BaseModel):
    """Per-question generation outcome with every metric's raw inputs."""

    question_id: str
    expected_behavior: str
    status: str
    json_valid_before: bool
    json_valid_after: bool
    repaired: bool
    citation_total: int
    citation_valid_count: int
    citation_valid: bool | None  # None when no citations at all
    numeric_total: int
    numeric_errors: int
    unsupported_content_remaining: bool
    bullets_total: int
    bullets_retained: int
    bullets_dropped: int
    empty_answer: bool
    relevancy_proxy: float | None
    correct_abstention: bool | None  # only for expected-insufficient questions
    refusal_accurate: bool | None  # only for expected-refusal questions
    provider_failure: bool
    latency_ms: float
    provider: str
    model: str
    prompt_version: str


def evaluate_generation(
    questions,
    runner: GenerationRunner,
    *,
    service=None,
    session=None,
    top_k: int = 5,
    corpus: str = "fixture",
) -> dict:
    """Run the generation harness; returns the aggregate metrics dict.

    When ``service`` (a :class:`SearchService`) is supplied, evidence is
    retrieved per question and assembled into passages; otherwise the runner
    is called with no evidence (abstention paths only).
    """
    from quarterline.eval.dataset import EvalQuestion
    from quarterline.retrieve.context import assemble_context

    records: list[GenerationQuestionRecord] = []
    for question in questions:
        assert isinstance(question, EvalQuestion)
        passages: list[dict] = []
        if service is not None:
            from quarterline.retrieve.context import context_budget_from_settings
            from quarterline.retrieve.models import SearchQuery

            result = service.search(
                SearchQuery(
                    query=question.question,
                    ticker=question.ticker,
                    period_end=question.period_end,
                    top_k=top_k,
                )
            )
            bundle = assemble_context(result.items, context_budget_from_settings(service.settings))
            passages = [passage.model_dump() for passage in bundle.passages]

        started = time.perf_counter()
        output = runner.run(question, passages)
        latency = (time.perf_counter() - started) * 1000 + output.latency_ms

        valid_before = json_valid(output.raw_text)
        valid_after = json_valid(output.repaired_text)
        supplied_ids = {passage.get("evidence_id") for passage in passages}
        citation_valid_count = sum(1 for cid in output.citations if cid in supplied_ids)
        citation_total = len(output.citations)
        evidence_text = " ".join(passage.get("text", "") for passage in passages)
        numeric_errors = sum(1 for number in output.claimed_numbers if number not in evidence_text)
        numeric_total = len(output.claimed_numbers)

        if question.expected_behavior == "insufficient_evidence":
            correct_abstention: bool | None = output.status == STATUS_INSUFFICIENT
        else:
            correct_abstention = None
        if question.expected_behavior == "refusal":
            refusal_accurate: bool | None = (
                output.status == STATUS_REFUSED and output.refusal_alternative
            )
        else:
            refusal_accurate = None

        answered = output.status == STATUS_ANSWER
        empty_answer = answered and not output.answer_text.strip()
        if answered and question.required_concepts:
            answer_terms = {token.lower() for token in _WORD_RE.findall(output.answer_text)}
            question_terms = {token.lower() for token in _WORD_RE.findall(question.question)}
            overlap = (
                len(answer_terms & question_terms) / len(question_terms) if question_terms else 0.0
            )
            concept_coverage = sum(
                1
                for concept in question.required_concepts
                if all(word.lower() in answer_terms for word in _WORD_RE.findall(concept))
            ) / len(question.required_concepts)
            relevancy: float | None = round((overlap + concept_coverage) / 2, 4)
        else:
            relevancy = None

        record = GenerationQuestionRecord(
            question_id=question.id,
            expected_behavior=question.expected_behavior,
            status=output.status,
            json_valid_before=valid_before,
            json_valid_after=valid_after,
            repaired=valid_after and not valid_before,
            citation_total=citation_total,
            citation_valid_count=citation_valid_count,
            citation_valid=(citation_valid_count == citation_total) if citation_total else None,
            numeric_total=numeric_total,
            numeric_errors=numeric_errors,
            unsupported_content_remaining=bool(answered and numeric_errors),
            bullets_total=output.bullets_total,
            bullets_retained=output.bullets_retained,
            bullets_dropped=output.bullets_dropped,
            empty_answer=empty_answer,
            relevancy_proxy=relevancy,
            correct_abstention=correct_abstention,
            refusal_accurate=refusal_accurate,
            provider_failure=output.status == STATUS_PROVIDER_UNAVAILABLE,
            latency_ms=round(latency, 2),
            provider=output.provider,
            model=output.model,
            prompt_version=output.prompt_version,
        )
        records.append(record)
        emit_run_event(
            new_run_id(),
            {
                "endpoint": "eval:generation",
                "company_id": question.ticker,
                "provider": output.provider,
                "model": output.model,
                "prompt_version": output.prompt_version,
                "corpus_version": corpus,
                "question_hash": hash_text(question.question),
                "question_length": len(question.question),
                "input_tokens": output.input_tokens,
                "output_tokens": output.output_tokens,
                "token_count_source": output.token_count_source,
                "generation_latency_ms": record.latency_ms,
                "json_valid": valid_after,
                "json_repaired": record.repaired,
                "citation_valid": record.citation_valid,
                "numeric_error": bool(numeric_errors),
                "retained_bullet_count": output.bullets_retained,
                "dropped_bullet_count": output.bullets_dropped,
                "answer_status": output.status,
                "cache_hit": False,
                "error_type": output.error_type,
                "estimated_api_cost": None,  # scripted fake: no credible cost basis
                "cost_currency": None,
                "cost_basis": (
                    "scripted local fixture runner; no API charge and compute "
                    "cost not estimated (never labelled free, SPEC §24)"
                ),
            },
        )

    return _aggregate_generation(records, corpus=corpus, session=session)


def _rate(count: int, n: int) -> dict:
    return {"n": n, "count": count, "rate": round(count / n, 4) if n else None}


def _aggregate_generation(
    records: list[GenerationQuestionRecord], *, corpus: str, session=None
) -> dict:
    n = len(records)
    abstain_expected = [r for r in records if r.correct_abstention is not None]
    refusal_expected = [r for r in records if r.refusal_accurate is not None]
    citation_events = [r for r in records if r.citation_total > 0]
    numeric_events = [r for r in records if r.numeric_total > 0]
    retained = sum(r.bullets_retained for r in records)
    dropped = sum(r.bullets_dropped for r in records)
    relevancies = [r.relevancy_proxy for r in records if r.relevancy_proxy is not None]

    summary = {
        "corpus": corpus,
        "provider": records[0].provider if records else None,
        "model": records[0].model if records else None,
        "prompt_version": records[0].prompt_version if records else None,
        "n_questions": n,
        "json_validity_before_repair": _rate(sum(1 for r in records if r.json_valid_before), n),
        "json_validity_after_repair": _rate(sum(1 for r in records if r.json_valid_after), n),
        "json_repair_rate": _rate(sum(1 for r in records if r.repaired), n),
        "citation_validity": _rate(
            sum(r.citation_valid_count for r in citation_events),
            sum(r.citation_total for r in citation_events),
        ),
        "numeric_error_rate_before_filtering": _rate(
            sum(r.numeric_errors for r in numeric_events),
            sum(r.numeric_total for r in numeric_events),
        ),
        "unsupported_content_remaining_after_filtering": _rate(
            sum(1 for r in records if r.unsupported_content_remaining), n
        ),
        "bullet_retention": {
            "retained": retained,
            "dropped": dropped,
            "rate": round(retained / (retained + dropped), 4) if retained + dropped else None,
        },
        "empty_answer_rate": _rate(sum(1 for r in records if r.empty_answer), n),
        "answer_relevancy_proxy": {
            "n": len(relevancies),
            "mean": round(sum(relevancies) / len(relevancies), 4) if relevancies else None,
            "definition": (
                "PROXY ONLY: mean of question-term overlap with the answer and "
                "required-concepts coverage. NOT semantic quality (SPEC §22)."
            ),
        },
        "correct_abstention_rate": _rate(
            sum(1 for r in abstain_expected if r.correct_abstention), len(abstain_expected)
        ),
        "refusal_accuracy": _rate(
            sum(1 for r in refusal_expected if r.refusal_accurate), len(refusal_expected)
        ),
        "provider_failure_rate": _rate(sum(1 for r in records if r.provider_failure), n),
        "status_breakdown": {
            status: sum(1 for r in records if r.status == status)
            for status in sorted({r.status for r in records})
        },
        "note": (
            "insufficient_evidence, refusal and provider failure are distinct "
            "outcomes and are never merged (SPEC §21/§22)."
        ),
        "records": [record.model_dump() for record in records],
    }
    if session is not None:
        summary["eval_run_id"] = _persist_eval_run(session, records, summary)
    return summary


def _persist_eval_run(session, records: list[GenerationQuestionRecord], summary: dict) -> int:
    """Write eval_runs + per-question eval_results rows (SPEC §9.9)."""
    from datetime import UTC, datetime

    from quarterline.store.models import EvalResult, EvalRun

    now = datetime.now(UTC)
    eval_run = EvalRun(
        dataset_version=records[0].prompt_version if records else None,
        chunking_strategy=None,
        retrieval_config=None,
        config_json=json.dumps(
            {key: summary.get(key) for key in ("provider", "model", "prompt_version", "corpus")},
            default=str,
        ),
        status="complete",
        started_at=now,
        finished_at=now,
        notes=(
            "generation harness run; scripted-fake runner measures the harness "
            "only, not generation quality (SPEC §23)"
            if summary.get("provider") == "scripted-fake"
            else "generation harness run"
        ),
    )
    session.add(eval_run)
    session.flush()
    for record in records:
        for metric_name, value in (
            ("status", record.status),
            ("json_valid_before", record.json_valid_before),
            ("json_valid_after", record.json_valid_after),
            ("citation_valid", record.citation_valid),
            ("numeric_errors", record.numeric_errors),
            ("unsupported_content_remaining", record.unsupported_content_remaining),
            ("empty_answer", record.empty_answer),
            ("relevancy_proxy", record.relevancy_proxy),
            ("correct_abstention", record.correct_abstention),
            ("refusal_accurate", record.refusal_accurate),
            ("provider_failure", record.provider_failure),
        ):
            session.add(
                EvalResult(
                    eval_run_id=eval_run.id,
                    question_id=record.question_id,
                    metric_name=metric_name,
                    value=None if value is None else str(value),
                )
            )
    session.flush()
    return eval_run.id


__all__ = [
    "RUNNER_PROMPT_VERSION",
    "STATUS_ANSWER",
    "STATUS_INSUFFICIENT",
    "STATUS_PROVIDER_UNAVAILABLE",
    "STATUS_REFUSED",
    "GenerationOutput",
    "GenerationQuestionRecord",
    "GenerationRunner",
    "ScriptedFakeGenerationRunner",
    "evaluate_generation",
    "json_valid",
    "repair_json",
]
