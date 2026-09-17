"""Real-model retrieval matrix (Milestone 9 / SPEC §22-§23 manual evaluation).

Runs the 2 strategies x 4 retrieval configurations matrix against the dev
store with the LIVE Ollama embedding provider (nomic-embed-text), i.e. real
768-dim query/index vectors rather than the deterministic fake provider used
for the frozen CI baseline. When the ``[rerank]`` extra is installed, the
hybrid-rerank arms run the REAL cross-encoder (ms-marco-MiniLM-L-6-v2);
without it those arms degrade to hybrid and every output labels the run
"degraded (no cross-encoder)" - the degraded label is carried into the
report file, never dropped. Writes a markdown + JSON report to
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
from quarterline.eval.retrieval import RETRIEVAL_CONFIGS, STRATEGIES, evaluate_retrieval
from quarterline.retrieve.search import SearchService, provider_from_settings
from quarterline.store.db import session_scope


def _real_reranker_if_available(settings):
    """Return a timing-wrapped CrossEncoderReranker when the [rerank] extra
    is installed.

    Raises RerankerUnavailable when it is not: this script exists to measure
    the real reranker, so silently degrading here would reproduce the old
    degraded-only numbers under a misleading label. The timing wrapper
    records per-call rerank durations (the harness itself does not
    instrument them; SPEC §22 reports them here as p50/p95 over calls).
    """
    import time as _time

    from quarterline.retrieve.reranker import CrossEncoderReranker, RerankerUnavailable

    if not settings.reranker_enabled:
        raise RerankerUnavailable("settings.reranker_enabled is false")
    inner = CrossEncoderReranker(settings.reranker_model)
    inner._ensure_model()  # warm the lazy load before timing anything

    class _TimedReranker:
        """Proxy that records wall time of every real rerank() call."""

        model_id = inner.model_id

        def __init__(self) -> None:
            self.durations_ms: list[float] = []

        def rerank(self, query, items, batch_size=None):
            started = _time.perf_counter()
            try:
                return inner.rerank(query, items, batch_size=batch_size)
            finally:
                self.durations_ms.append((_time.perf_counter() - started) * 1000)

    return _TimedReranker()


def main() -> int:
    settings = get_settings()
    questions = load_dataset(Path("data/eval/questions.jsonl"))
    provider = provider_from_settings(settings)
    if hasattr(provider, "ensure_model"):
        provider.ensure_model()
    print(f"real embedding provider: {provider.model_id}")

    rerank_note = "degraded (no cross-encoder)"
    reranker = None
    try:
        reranker = _real_reranker_if_available(settings)
        rerank_note = f"real cross-encoder {settings.reranker_model}"
    except (OSError, ValueError, RuntimeError) as exc:
        # Includes RerankerUnavailable (a RuntimeError subclass raised by the
        # loader) and model-download failures (HF hub errors surface as
        # OSError/ValueError).
        print(f"reranker unavailable: {exc}")
    print(f"reranker arm: {rerank_note}")

    started = time.perf_counter()
    with session_scope() as session:
        service = SearchService(
            session, provider, reranker=reranker, reranker_enabled=settings.reranker_enabled
        )
        reports = [
            evaluate_retrieval(
                questions,
                session,
                strategy=strategy,
                retrieval=retrieval,
                service=service,
            )
            for strategy in STRATEGIES
            for retrieval in RETRIEVAL_CONFIGS
        ]
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
    rerank_summary = ""
    if reranker is not None and reranker.durations_ms:
        from quarterline.observability.metrics import percentile

        durs = sorted(reranker.durations_ms)
        rerank_summary = (
            f" real rerank calls: n={len(durs)}, "
            f"p50={percentile(durs, 50):.1f} ms, p95={percentile(durs, 95):.1f} ms"
        )
    print(
        f"\nmeasured with real nomic-embed-text queries over the fixture corpus "
        f"(n={len(questions)}), reranker arm: {rerank_note}, in {elapsed:.1f}s" + rerank_summary
    )

    out_dir = Path("storage/eval_reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    (out_dir / f"real_model_matrix_{stamp}.json").write_text(
        json.dumps(
            {
                "provider": provider.model_id,
                "reranker": rerank_note,
                "rerank_call_durations_ms": reranker.durations_ms if reranker else [],
                "n": len(questions),
                "matrix": payload,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    (out_dir / f"real_model_matrix_{stamp}.md").write_text(
        f"# Real-model retrieval matrix (nomic-embed-text, n={len(questions)}, "
        f"reranker: {rerank_note})\n\n{table}\n",
        encoding="utf-8",
    )
    print(f"reports written: storage/eval_reports/real_model_matrix_{stamp}.{{json,md}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
