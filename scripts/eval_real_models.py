"""Real-model retrieval matrix (Milestone 9 / SPEC §22-§23 manual evaluation).

Runs the 2 strategies x 4 retrieval configurations matrix against the dev
store with the LIVE Ollama embedding provider (nomic-embed-text), i.e. real
768-dim query/index vectors rather than the deterministic fake provider used
for the frozen CI baseline. Writes a markdown + JSON report to
storage/eval_reports/ and prints the table.

Usage:
    uv run python scripts/eval_real_models.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from quarterline.config import get_settings
from quarterline.eval.dataset import load_dataset
from quarterline.eval.report import write_report  # noqa: F401  (report writing below)
from quarterline.eval.retrieval import run_matrix
from quarterline.retrieve.search import provider_from_settings
from quarterline.store.db import session_scope


def main() -> int:
    settings = get_settings()
    questions = load_dataset(Path("data/eval/questions.jsonl"))
    provider = provider_from_settings(settings)
    if hasattr(provider, "ensure_model"):
        provider.ensure_model()
    print(f"real embedding provider: {provider.model_id}")
    started = time.perf_counter()
    with session_scope() as session:
        reports = run_matrix(questions, session, provider=provider)
    elapsed = time.perf_counter() - started
    dim = getattr(provider, "_dim", None)
    print(f"(embedding dim: {dim})")

    rows = [
        "| strategy | retrieval | Hit@5 | Recall@5 | MRR | wrong-co | period-err | lat p50/p95 ms |",
        "|---|---|---|---|---|---|---|---|",
    ]
    payload = []
    for r in reports:
        rows.append(
            f"| {r.strategy} | {r.retrieval} | {r.hit_at_5:.1%} | {r.recall_at_5:.1%} "
            f"| {r.mrr:.3f} | {r.wrong_company_rate:.1%} | {r.period_filter_error_rate:.1%} "
            f"| {r.retrieval_latency_p50_ms:.1f}/{r.retrieval_latency_p95_ms:.1f} |"
        )
        payload.append(r.to_dict())
    table = "\n".join(rows)
    print(table)
    print(
        f"\nmeasured with real nomic-embed-text queries over the fixture corpus "
        f"(n={len(questions)}) in {elapsed:.1f}s"
    )

    out_dir = Path("storage/eval_reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    (out_dir / f"real_model_matrix_{stamp}.json").write_text(
        json.dumps(
            {"provider": provider.model_id, "n": len(questions), "matrix": payload}, indent=1
        ),
        encoding="utf-8",
    )
    (out_dir / f"real_model_matrix_{stamp}.md").write_text(
        f"# Real-model retrieval matrix (nomic-embed-text, n={len(questions)})\n\n{table}\n",
        encoding="utf-8",
    )
    print(f"reports written: storage/eval_reports/real_model_matrix_{stamp}.{{json,md}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
