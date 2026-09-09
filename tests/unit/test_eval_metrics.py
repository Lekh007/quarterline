"""Hand-computed metric-definition tests (SPEC §22) + regression/judge/report units.

The Hit@5 vs Recall@5 case below is the load-bearing one: they must not be
confusable (SPEC §22: "Do not call 'any relevant result found' Recall@5").
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest

from quarterline.eval.generation import (
    GenerationOutput,
    ScriptedFakeGenerationRunner,
    json_valid,
    repair_json,
)
from quarterline.eval.judge import (
    JUDGE_LABEL,
    JudgeConfig,
    LLMJudge,
    parse_judgment,
)
from quarterline.eval.regression import (
    BaselineWriteError,
    baseline_snapshot,
    check_regression,
    diff_snapshots,
    write_baseline,
)
from quarterline.eval.report import build_markdown
from quarterline.eval.retrieval import (
    QuestionRetrievalRecord,
    Span,
    aggregate,
    dataset_version,
)
from quarterline.observability.metrics import percentile

# ---------------------------------------------------------------------------
# Retrieval metric primitives (hand-computed)
# ---------------------------------------------------------------------------


def _record(
    question_id: str,
    retrieved: list[Span],
    gold: list[Span],
    *,
    ticker: str = "AAPL",
    companies: list[str] | None = None,
    periods: list[int | None] | None = None,
    question_period: int | None = 2026,
    has_gold: bool = True,
) -> QuestionRetrievalRecord:
    return QuestionRetrievalRecord(
        question_id=question_id,
        ticker=ticker,
        task_type="period_specific",
        expected_behavior="answer",
        has_gold=has_gold,
        question_period=question_period,
        retrieved=retrieved,
        gold=gold,
        retrieved_companies=companies or [ticker] * len(retrieved),
        # periods here are plain ints standing in for dates in hand-cases:
        retrieved_periods=periods or [question_period] * len(retrieved),
    )


DOC = 1


def test_hit_at_5_and_recall_at_5_diverge() -> None:
    # Two gold units; the top-5 retrieved covers only the second one.
    gold = [Span(DOC, 0, 10), Span(DOC, 100, 110)]
    retrieved = [
        Span(DOC, 500, 510),
        Span(DOC, 600, 610),
        Span(DOC, 95, 105),  # overlaps gold[1] only
        Span(DOC, 700, 710),
        Span(DOC, 800, 810),
    ]
    record = _record("q", retrieved, gold)
    assert record.hit_at_k(5) is True  # at least one relevant found
    assert record.recall_at_k(5) == 0.5  # ...but only half the gold units
    assert record.reciprocal_rank(5) == pytest.approx(1 / 3)


def test_hit_at_5_excludes_no_gold_questions() -> None:
    record = _record("q", [Span(DOC, 0, 5)], [], has_gold=False)
    assert record.hit_at_k(5) is None
    assert record.recall_at_k(5) is None


def test_mrr_uses_first_relevant_rank() -> None:
    gold = [Span(DOC, 50, 60)]
    retrieved = [Span(DOC, 0, 1), Span(DOC, 55, 65), Span(DOC, 2, 3)]
    record = _record("q", retrieved, gold)
    assert record.reciprocal_rank(5) == pytest.approx(0.5)  # first relevant at rank 2
    assert record.reciprocal_rank(1) == 0.0  # k=1 cuts the relevant unit off


def test_mrr_rank_cut_boundary() -> None:
    gold = [Span(DOC, 50, 60)]
    retrieved = [Span(DOC, 0, 1), Span(DOC, 2, 3), Span(DOC, 55, 65)]
    record = _record("q", retrieved, gold)
    assert record.reciprocal_rank(2) == 0.0  # relevant at rank 3, outside top-2
    assert record.reciprocal_rank(3) == pytest.approx(1 / 3)


def test_wrong_company_rate_counts_units() -> None:
    gold = [Span(DOC, 0, 10)]
    retrieved = [Span(DOC, 0, 5), Span(2, 10, 15), Span(3, 20, 25)]
    record = _record(
        "q",
        retrieved,
        gold,
        companies=["AAPL", "MSFT", "GOOG"],
    )
    assert record.wrong_company_units(5) == 2
    report = aggregate([record], strategy="section", retrieval="hybrid", embedding_model="fake")
    assert report.retrieved_units == 3
    assert report.wrong_company_rate == pytest.approx(2 / 3)


def test_aggregate_hit_vs_recall_on_hand_case() -> None:
    gold_two = [Span(DOC, 0, 10), Span(DOC, 100, 110)]
    partial = _record(
        "q-partial",
        [Span(DOC, 95, 105), Span(DOC, 500, 510), Span(DOC, 600, 610)],
        gold_two,
    )
    full = _record("q-full", [Span(DOC, 0, 5)], [Span(DOC, 0, 10)])
    report = aggregate([partial, full], strategy="fixed", retrieval="lexical", embedding_model="f")
    assert report.hit_at_5 == pytest.approx(1.0)  # both questions have >=1 hit
    assert report.recall_at_5 == pytest.approx((0.5 + 1.0) / 2)  # overlap-count mean
    assert report.mrr == pytest.approx((1 / 1 + 1 / 1) / 2)


def test_dataset_version_is_content_stable() -> None:
    assert dataset_version() == dataset_version()
    assert len(dataset_version()) == 12


# ---------------------------------------------------------------------------
# Regression thresholds (SPEC §23: 5pp passes, 6pp fails; wrong-co +1 fails)
# ---------------------------------------------------------------------------


def _baseline(recall: float, wrong: float = 0.0) -> dict:
    return {
        "dataset_version": "abc123",
        "configs": {
            "section/hybrid": {
                "recall_at_5": recall,
                "wrong_company_rate": wrong,
            }
        },
    }


def test_regression_passes_at_exactly_five_pp_drop() -> None:
    baseline = _baseline(0.80)
    current = {"section/hybrid": {"recall_at_5": 0.75, "wrong_company_rate": 0.0}}
    report = check_regression(current, baseline, dataset_version_current="abc123")
    assert report.status == "pass"


def test_regression_fails_at_six_pp_drop() -> None:
    baseline = _baseline(0.80)
    current = {"section/hybrid": {"recall_at_5": 0.74, "wrong_company_rate": 0.0}}
    report = check_regression(current, baseline, dataset_version_current="abc123")
    assert report.status == "fail"
    assert any("Recall@5" in v for v in report.configs[0].violations)


def test_regression_fails_on_any_wrong_company_increase() -> None:
    baseline = _baseline(0.90, wrong=0.0)
    current = {"section/hybrid": {"recall_at_5": 0.90, "wrong_company_rate": 0.01}}
    report = check_regression(current, baseline, dataset_version_current="abc123")
    assert report.status == "fail"
    assert any("wrong-company" in v for v in report.configs[0].violations)


def test_regression_flags_new_config() -> None:
    baseline = _baseline(0.90)
    current = {
        "section/hybrid": {"recall_at_5": 0.90, "wrong_company_rate": 0.0},
        "fixed/dense": {"recall_at_5": 0.10, "wrong_company_rate": 0.0},
    }
    report = check_regression(current, baseline, dataset_version_current="abc123")
    assert report.status == "fail"
    new_entry = next(c for c in report.configs if c.config_key == "fixed/dense")
    assert new_entry.status == "new_config"


def test_baseline_write_requires_explicit_confirm(tmp_path: Path) -> None:
    snapshot = baseline_snapshot({"section/hybrid": {}}, dataset_version="abc")
    target = tmp_path / "baseline.json"
    with pytest.raises(BaselineWriteError, match="never regenerated silently"):
        write_baseline(target, snapshot)
    assert not target.exists()
    written = write_baseline(target, snapshot, confirm=True)
    assert written.is_file()
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["configs"] == {"section/hybrid": {}}
    assert "fixture" in payload["labeled"].lower()


def test_baseline_diff_lists_changed_metrics(tmp_path: Path) -> None:
    previous = _baseline(0.90, wrong=0.0)
    previous["configs"]["section/hybrid"]["recall_at_5"] = 0.90
    next_snapshot = _baseline(0.80, wrong=0.02)
    diff = diff_snapshots(previous, next_snapshot)
    assert any("recall_at_5: 0.9 -> 0.8" in line for line in diff)
    assert any("wrong_company_rate" in line for line in diff)


# ---------------------------------------------------------------------------
# Generation harness units
# ---------------------------------------------------------------------------


class _QuestionStub:
    id = "q-stub"
    ticker = "AAPL"
    question = "What was revenue?"
    task_type = "period_specific"
    period_end = None
    answerable = True
    expected_behavior = "answer"
    required_concepts: ClassVar[list[str]] = ["revenue"]
    reviewed = True
    relevant_evidence: ClassVar[list] = []
    notes = None


def test_json_validity_and_repair() -> None:
    assert json_valid('{"a": 1}') is True
    assert json_valid("{bad json") is False
    repaired = repair_json('```json\n{"a": 1,}\n```')
    assert json_valid(repaired) is True


def test_scripted_runner_shapes_are_distinct_per_expectation() -> None:
    runner = ScriptedFakeGenerationRunner()

    answer_q = _QuestionStub()
    answer_q.expected_behavior = "answer"
    answer_out = runner.run(answer_q, [{"evidence_id": "ev-1", "text": "revenue grew"}])
    assert answer_out.status == "answer"

    abstain_q = _QuestionStub()
    abstain_q.expected_behavior = "insufficient_evidence"
    abstain_out = runner.run(abstain_q, [])
    assert abstain_out.status == "insufficient_evidence"

    refuse_q = _QuestionStub()
    refuse_q.expected_behavior = "refusal"
    refuse_out = runner.run(refuse_q, [])
    assert refuse_out.status == "refused"
    assert refuse_out.refusal_alternative is True

    statuses = {answer_out.status, abstain_out.status, refuse_out.status}
    assert len(statuses) == 3  # three distinct events, never merged


def test_generation_output_defaults_carry_provenance() -> None:
    out = GenerationOutput(raw_text="{}", repaired_text="{}", status="answer")
    assert out.provider == "scripted-fake"
    assert out.prompt_version == "harness-v1"


# ---------------------------------------------------------------------------
# Judge units (offline; mock transport)
# ---------------------------------------------------------------------------


def test_judge_disabled_by_default() -> None:
    class _Settings:
        enable_llm_judge = False
        judge_provider = ""
        judge_model = ""

    config = JudgeConfig.from_settings(_Settings())
    assert config.enabled is False
    judge = LLMJudge(config)
    result = judge.judge("q", "question", "evidence", "answer")
    assert result.enabled is False
    assert result.faithfulness is None


def test_judge_requires_full_configuration() -> None:
    class _HalfConfigured:
        enable_llm_judge = True
        judge_provider = "ollama"
        judge_model = ""

    assert JudgeConfig.from_settings(_HalfConfigured()).enabled is False


def test_judge_mock_transport_parses_rubric_json() -> None:
    class _Settings:
        enable_llm_judge = True
        judge_provider = "ollama"
        judge_model = "qwen3:4b"
        ollama_base_url = "http://127.0.0.1:9"

    judge = LLMJudge(
        JudgeConfig.from_settings(_Settings()),
        transport=lambda messages: (
            'Sure! {"faithfulness": 4, "relevance": 5, "rationale": "grounded"}'
        ),
    )
    result = judge.judge("q-1", "What was revenue?", "Revenue was 111,184.", "111,184")
    assert result.faithfulness == 4
    assert result.relevance == 5
    assert result.rationale == "grounded"
    assert result.judge_model == "qwen3:4b"
    assert result.prompt_version == "v1"
    assert result.human_reviewed is False
    assert "never ground truth" in result.label


def test_parse_judgment_rejects_out_of_scale_and_garbage() -> None:
    assert parse_judgment('{"faithfulness": 9, "relevance": 2}') == (
        None,
        2,
        None,
        None,
    )
    assert parse_judgment("no json here")[3] is not None
    assert parse_judgment('{"faithfulness": "high"}')[0] is None


def test_judge_prompt_file_is_pinned_and_has_placeholders() -> None:
    from quarterline.eval.judge import build_judge_messages, load_prompt

    prompt = load_prompt()
    assert "{question}" in prompt and "{evidence}" in prompt and "{answer}" in prompt
    assert "1-5" in prompt
    build = build_judge_messages("Q", "E", "A")
    assert "Q" in build[0]["content"] and "A" in build[0]["content"]
    assert JUDGE_LABEL


# ---------------------------------------------------------------------------
# Report rendering units
# ---------------------------------------------------------------------------


def test_report_markdown_renders_matrix_and_fixture_label() -> None:
    payload = {
        "generated_at": "2026-09-09T00:00:00+00:00",
        "dataset_version": "abc123",
        "corpus": "fixture",
        "matrix": [
            {
                "strategy": "section",
                "retrieval": "hybrid",
                "hit_at_5": 0.8,
                "recall_at_5": 0.7,
                "mrr": 0.6,
                "wrong_company_rate": 0.0,
                "period_filter_error_rate": 0.0,
                "retrieval_latency_p50_ms": 1.0,
                "retrieval_latency_p95_ms": 2.0,
                "n_questions": 11,
            }
        ],
        "regression": {"status": "pass", "summary": "all good"},
    }
    markdown = build_markdown(payload)
    assert "fixture" in markdown.lower()
    assert "| section | hybrid | 80.0% | 70.0% |" in markdown
    assert "NOT real-model quality" in markdown
    assert "all good" in markdown


def test_percentile_nearest_rank() -> None:
    assert percentile([], 50) is None
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 95) == 5.0
    assert percentile([10.0], 95) == 10.0
