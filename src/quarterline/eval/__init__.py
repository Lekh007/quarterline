"""Evaluation + observability toolkit (F6; SPEC §21–§24).

Importing this package registers the wave-3 CLI handlers into the W0
``SUBCOMMAND_REGISTRY`` (``cli.py`` lazy-loads ``quarterline.eval``):

- ``quarterline eval retrieval --dataset data/eval/questions.jsonl``
  Bootstraps the frozen fixture corpus (offline), runs the 2x4 experiment
  matrix, checks regression against the committed baseline, and writes a
  report into ``storage/eval_reports/``. Exit code 1 on regression FAIL.
  With ``QUARTERLINE_EVAL_WRITE_BASELINE=1`` the run instead writes/updates
  the baseline — printing a diff against the previous one first. Baselines
  are never regenerated silently (SPEC §23).
- ``quarterline eval generation --dataset data/eval/questions.jsonl``
  Runs the generation harness against the SCRIPTED fake runner. This
  validates the evaluation harness only; it does NOT measure generation
  quality (SPEC §23) — real-model generation evaluation is opt-in, local.

All numbers produced here are labeled fixture-corpus-only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from quarterline.eval.dataset import (
    DatasetValidationError,
    EvalQuestion,
    load_dataset,
    missing_categories,
)
from quarterline.eval.regression import (
    DEFAULT_BASELINE_PATH,
    BaselineWriteError,
    RegressionReport,
    baseline_snapshot,
    check_regression,
    config_key,
    diff_snapshots,
    load_baseline,
    write_baseline,
)
from quarterline.eval.report import write_report
from quarterline.eval.retrieval import (
    RetrievalEvalReport,
    dataset_version,
    ensure_fixture_corpus,
    evaluate_retrieval,
    run_matrix,
)

__all__ = [
    "DEFAULT_BASELINE_PATH",
    "WRITE_BASELINE_ENV",
    "BaselineWriteError",
    "DatasetValidationError",
    "EvalQuestion",
    "RegressionReport",
    "RetrievalEvalReport",
    "baseline_snapshot",
    "check_regression",
    "config_key",
    "dataset_version",
    "diff_snapshots",
    "ensure_fixture_corpus",
    "evaluate_retrieval",
    "handle_eval_generation",
    "handle_eval_retrieval",
    "load_baseline",
    "load_dataset",
    "missing_categories",
    "run_matrix",
    "write_baseline",
    "write_report",
]

WRITE_BASELINE_ENV = "QUARTERLINE_EVAL_WRITE_BASELINE"


def _dataset_sha(dataset_path: str) -> str:
    import hashlib
    from pathlib import Path

    return hashlib.sha256(Path(dataset_path).read_bytes()).hexdigest()[:12]


def handle_eval_retrieval(args: argparse.Namespace) -> int:
    from quarterline.store.db import session_scope

    dataset_path = getattr(args, "dataset", "data/eval/questions.jsonl")
    try:
        questions = load_dataset(dataset_path)
    except DatasetValidationError as exc:
        print(f"dataset invalid: {exc}", file=sys.stderr)
        return 2

    missing = missing_categories(questions)
    bootstrap = ensure_fixture_corpus()
    with session_scope() as session:
        reports = run_matrix(questions, session)
        _persist_retrieval_rows(session, questions, reports, dataset_path)

    version = _dataset_sha(dataset_path)
    current = {
        config_key(report.strategy, report.retrieval): report.to_dict() for report in reports
    }

    baseline_path = DEFAULT_BASELINE_PATH
    baseline = load_baseline(baseline_path)
    regression = check_regression(
        current,
        baseline,  # None when no baseline committed yet -> status "no_baseline"
        dataset_version_current=version,
        baseline_path=str(baseline_path),
    )

    print(
        "Fixture-corpus retrieval evaluation (deterministic fake embeddings; "
        "NOT real-model quality — SPEC §22/§23)"
    )
    print(f"dataset: {dataset_path} (version {version}); questions: {len(questions)}")
    print(
        f"corpus bootstrap: {bootstrap['documents_ingested']} ingested, "
        f"{bootstrap['documents_skipped']} skipped (idempotent)"
    )
    print()
    header = (
        f"{'strategy':>7} | {'retrieval':>13} | {'Hit@5':>7} | {'Recall@5':>8} | "
        f"{'MRR':>6} | {'wrong-co':>8} | {'period-err':>10} | {'lat p50/p95':>12} | n"
    )
    print(header)
    print("-" * len(header))
    for report in reports:
        print(report.summary_row())
    print()
    if missing:
        print(f"category coverage gaps (SPEC §21): {', '.join(missing)}")
    print(regression.summary())

    json_path, md_path = write_report(
        {
            "kind": "retrieval",
            "dataset_version": version,
            "dataset_path": str(dataset_path),
            "matrix": [report.to_dict() for report in reports],
            "regression": {
                "status": regression.status,
                "summary": regression.summary(),
                "configs": [config.model_dump() for config in regression.configs],
            },
            "category_coverage_missing": missing,
        }
    )
    print(f"report written: {json_path}")
    print(f"report written: {md_path}")

    if os.environ.get(WRITE_BASELINE_ENV) == "1":
        snapshot = baseline_snapshot(
            current,
            dataset_version=version,
            embedding_model=reports[0].embedding_model if reports else None,
        )
        diff = diff_snapshots(baseline or {"configs": {}}, snapshot)
        written = write_baseline(baseline_path, snapshot, confirm=True, previous=baseline)
        print(f"baseline written: {written}")
        print("baseline diff vs previous:")
        for line in diff:
            print(f"  {line}")

    return 1 if regression.status == "fail" else 0


def _persist_retrieval_rows(session, questions, reports, dataset_path) -> None:
    """Persist eval_runs/eval_results rows for the retrieval matrix run."""
    from datetime import UTC, datetime

    from quarterline.store.models import EvalResult, EvalRun

    now = datetime.now(UTC)
    version = _dataset_sha(dataset_path)
    for report in reports:
        eval_run = EvalRun(
            dataset_version=version,
            chunking_strategy=report.strategy,
            retrieval_config=report.retrieval,
            config_json=json.dumps(report.to_dict(), default=str),
            status="complete",
            started_at=now,
            finished_at=now,
            notes="fixture-corpus retrieval matrix (fake embeddings; not real-model quality)",
        )
        session.add(eval_run)
        session.flush()
        for record in report.records:
            session.add(
                EvalResult(
                    eval_run_id=eval_run.id,
                    question_id=record.question_id,
                    metric_name="reciprocal_rank",
                    value=str(record.reciprocal_rank(5)),
                )
            )
            covered = record.covered_gold(5)
            session.add(
                EvalResult(
                    eval_run_id=eval_run.id,
                    question_id=record.question_id,
                    metric_name="gold_covered",
                    value=f"{covered}/{len(record.gold)}",
                )
            )
    session.flush()


def handle_eval_generation(args: argparse.Namespace) -> int:
    from quarterline.eval.generation import (
        ScriptedFakeGenerationRunner,
        evaluate_generation,
    )
    from quarterline.retrieve.embeddings import FakeEmbeddingProvider
    from quarterline.retrieve.search import SearchService
    from quarterline.store.db import session_scope

    dataset_path = getattr(args, "dataset", "data/eval/questions.jsonl")
    try:
        questions = load_dataset(dataset_path)
    except DatasetValidationError as exc:
        print(f"dataset invalid: {exc}", file=sys.stderr)
        return 2

    ensure_fixture_corpus()
    runner = ScriptedFakeGenerationRunner()
    print(
        "GENERATION HARNESS CHECK — scripted fake provider. This does NOT "
        "measure generation quality (SPEC §23); real-model evaluation is "
        "opt-in and local."
    )
    with session_scope() as session:
        service = SearchService(session, FakeEmbeddingProvider())
        summary = evaluate_generation(questions, runner, service=service, session=session)

    print(f"dataset: {dataset_path}; questions: {summary['n_questions']}")
    print(f"  JSON validity after repair:  {summary['json_validity_after_repair']}")
    print(f"  citation validity:           {summary['citation_validity']}")
    print(f"  numeric error (pre-filter):  {summary['numeric_error_rate_before_filtering']}")
    print(
        f"  unsupported content remain.: {summary['unsupported_content_remaining_after_filtering']}"
    )
    print(f"  bullet retention:            {summary['bullet_retention']}")
    print(f"  correct abstention:          {summary['correct_abstention_rate']}")
    print(f"  refusal accuracy:            {summary['refusal_accuracy']}")
    print(f"  provider-failure rate:       {summary['provider_failure_rate']}")
    print(f"  answer relevancy (PROXY):    {summary['answer_relevancy_proxy']}")
    print(f"  status breakdown:            {summary['status_breakdown']}")

    json_path, md_path = write_report({"kind": "generation", "generation": summary})
    print(f"report written: {json_path}")
    print(f"report written: {md_path}")
    return 0


def register_cli() -> None:
    from quarterline.cli import register_subcommand

    register_subcommand("eval:retrieval", handle_eval_retrieval)
    register_subcommand("eval:generation", handle_eval_generation)


register_cli()
