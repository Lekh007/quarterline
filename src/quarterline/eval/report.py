"""Evaluation reports (markdown + JSON) into ``storage/eval_reports/``.

Reports are local research artifacts (gitignored storage); every report
carries: timestamps, sample sizes, config metadata (provider / model /
chunking strategy / retrieval config / prompt versions / dataset version),
the fixture-corpus 2x4 experiment matrix, and explicit labels that the
numbers are fixture-corpus-only unless stated otherwise. The dashboard
surfaces the latest report summary (see :func:`latest_report`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from quarterline.config import get_settings

REPORTS_DIRNAME = "eval_reports"

FIXTURE_LABEL = (
    "Fixture-corpus-only measurements: deterministic fake embedding provider "
    "(no semantic signal) over the frozen test fixture corpus. These numbers "
    "exercise the pipeline; they are NOT real-model quality. The real-model "
    "matrix is Wave 5 (SPEC §22/§23)."
)


def reports_dir(settings=None) -> Path:
    settings = settings or get_settings()
    path = Path(settings.storage_dir) / REPORTS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _matrix_table(reports: list[dict]) -> list[str]:
    lines = [
        "| strategy | retrieval | Hit@5 | Recall@5 | MRR | wrong-co | period-err | lat p50/p95 (ms) | n |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for report in reports:

        def pct(value: float | None) -> str:
            return f"{value * 100:.1f}%" if value is not None else "n/a"

        lat = (
            f"{report['retrieval_latency_p50_ms']:.1f}/{report['retrieval_latency_p95_ms']:.1f}"
            if report.get("retrieval_latency_p50_ms") is not None
            else "n/a"
        )
        mrr = f"{report['mrr']:.3f}" if report.get("mrr") is not None else "n/a"
        lines.append(
            f"| {report['strategy']} | {report['retrieval']} | {pct(report.get('hit_at_5'))} | "
            f"{pct(report.get('recall_at_5'))} | {mrr} | {pct(report.get('wrong_company_rate'))} | "
            f"{pct(report.get('period_filter_error_rate'))} | {lat} | {report['n_questions']} |"
        )
    return lines


def build_markdown(payload: dict) -> str:
    lines = [
        "# Quarterline evaluation report",
        "",
        f"Generated: {payload['generated_at']}",
        f"Dataset version: `{payload.get('dataset_version')}`",
        f"Corpus: **{payload.get('corpus', 'fixture')}** (frozen test fixtures)",
        "",
        f"> {FIXTURE_LABEL}",
        "",
        "## Experiment matrix (2 chunking strategies x 4 retrieval configurations)",
        "",
        *_matrix_table(payload.get("matrix", [])),
        "",
        "## Notes",
        "",
        ("- Sample sizes are shown per row; low-N numbers are noisy."),
        "- Wrong-company rate and period-filter error rate are regression-guard metrics.",
        (
            "- Reranker arm degrades to hybrid in the fixture environment (no cross-encoder "
            "installed) — see `rerank_degraded` in the JSON report."
        ),
        (
            "- Deterministic citation-validation and normalization invariants are enforced by "
            "the unit test suite (SPEC §23), not by this report."
        ),
        (
            "- Real-model generation evaluation runs opt-in locally; a scripted provider never "
            "measures generation quality (SPEC §23)."
        ),
    ]
    generation = payload.get("generation")
    if generation is not None:
        lines += [
            "",
            "## Generation harness (scripted fake — NOT a quality measurement)",
            "",
            f"- provider/model: {generation.get('provider')}/{generation.get('model')}",
            f"- JSON validity after repair: {generation.get('json_validity_after_repair')}",
            f"- citation validity: {generation.get('citation_validity')}",
            f"- correct abstention: {generation.get('correct_abstention_rate')}",
            f"- refusal accuracy: {generation.get('refusal_accuracy')}",
            f"- provider-failure rate: {generation.get('provider_failure_rate')}",
        ]
    regression = payload.get("regression")
    if regression is not None:
        lines += ["", "## Regression check", "", "```", regression.get("summary", ""), "```"]
    return "\n".join(lines) + "\n"


def write_report(
    payload: dict,
    *,
    out_dir: str | Path | None = None,
    settings=None,
) -> tuple[Path, Path]:
    """Write ``eval_<timestamp>.md`` + ``.json``; returns (json_path, md_path)."""
    directory = Path(out_dir) if out_dir else reports_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        **payload,
    }
    json_path = directory / f"eval_{stamp}.json"
    md_path = directory / f"eval_{stamp}.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(build_markdown(payload), encoding="utf-8")
    return json_path, md_path


def latest_report(settings=None) -> dict | None:
    """Parse the most recent report JSON (by generated_at), or None."""
    directory = reports_dir(settings)
    candidates = sorted(directory.glob("eval_*.json"))
    best: tuple[str, dict] | None = None
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except ValueError:
            continue
        generated = str(payload.get("generated_at", ""))
        if best is None or generated > best[0]:
            best = (generated, payload)
    return best[1] if best else None


__all__ = [
    "FIXTURE_LABEL",
    "build_markdown",
    "latest_report",
    "reports_dir",
    "write_report",
]
