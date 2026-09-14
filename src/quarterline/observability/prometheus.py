"""Prometheus text exposition of the SPEC §24 metrics aggregates.

This is a *pure rendering* of :func:`quarterline.observability.metrics.metrics_summary`
— the same DB-derived numbers the ``/dashboard`` page shows, in the format a
Prometheus scraper understands. No new dependency and no second source of
truth: if the dashboard and this endpoint ever disagree, one of them is a bug.

Honesty carries over from the dashboard: every rate is exported next to its
sample size (``*_samples``), and a rate with no samples is *omitted* rather
than exported as a zero it has not earned.
"""

from __future__ import annotations

# (metric name, help text, type)
_LATENCY_STAGES = {
    "retrieval_latency_ms": "retrieval",
    "rerank_latency_ms": "rerank",
    "generation_latency_ms": "generation",
    "validation_latency_ms": "validation",
    "total_latency_ms": "total",
}


def _escape_label(value: str) -> str:
    """Escape a Prometheus label value (backslash, quote, newline)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\n")


def _labels(**pairs: str) -> str:
    if not pairs:
        return ""
    inner = ",".join(f'{k}="{_escape_label(str(v))}"' for k, v in pairs.items())
    return "{" + inner + "}"


class _Writer:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._declared: set[str] = set()

    def declare(self, name: str, help_text: str, metric_type: str) -> None:
        if name in self._declared:
            return
        self._declared.add(name)
        self.lines.append(f"# HELP {name} {help_text}")
        self.lines.append(f"# TYPE {name} {metric_type}")

    def sample(self, name: str, value: float, **labels: str) -> None:
        self.lines.append(f"{name}{_labels(**labels)} {value}")

    def rate_block(self, name: str, help_text: str, rate: dict, **labels: str) -> None:
        """Emit ``<name>`` (ratio) and ``<name>_samples`` (n) from a _rate dict.

        The ratio line is skipped when n == 0 — an unmeasured rate is missing,
        never zero.
        """
        self.declare(name, help_text, "gauge")
        self.declare(f"{name}_samples", f"Sample size behind {name}.", "gauge")
        if rate.get("rate") is not None:
            self.sample(name, rate["rate"], **labels)
        self.sample(f"{name}_samples", int(rate.get("n") or 0), **labels)

    def render(self) -> str:
        return "\n".join(self.lines) + "\n"


def render_prometheus(summary: dict) -> str:
    """Render a :func:`metrics_summary` dict as Prometheus exposition text."""
    w = _Writer()

    w.declare("quarterline_runs_total", "Distinct runs recorded.", "gauge")
    w.sample("quarterline_runs_total", int(summary.get("total_runs") or 0))

    w.declare("quarterline_events_total", "Run events recorded.", "gauge")
    w.sample("quarterline_events_total", int(summary.get("total_events") or 0))

    w.declare(
        "quarterline_endpoint_events_total",
        "Run events recorded, by endpoint.",
        "gauge",
    )
    for endpoint, count in sorted((summary.get("endpoints") or {}).items()):
        w.sample("quarterline_endpoint_events_total", int(count), endpoint=endpoint)

    w.declare(
        "quarterline_latency_milliseconds",
        "Stage latency percentiles computed from persisted run events.",
        "gauge",
    )
    w.declare(
        "quarterline_latency_samples",
        "Sample size behind quarterline_latency_milliseconds.",
        "gauge",
    )
    for field, stage in _LATENCY_STAGES.items():
        block = (summary.get("latency_ms") or {}).get(field) or {}
        for quantile, key in (("0.5", "p50_ms"), ("0.95", "p95_ms")):
            value = block.get(key)
            if value is not None:
                w.sample(
                    "quarterline_latency_milliseconds",
                    value,
                    stage=stage,
                    quantile=quantile,
                )
        w.sample("quarterline_latency_samples", int(block.get("n") or 0), stage=stage)

    w.declare(
        "quarterline_provider_events_total",
        "Run events recorded, by provider/model.",
        "gauge",
    )
    for key, count in sorted((summary.get("provider_model_breakdown") or {}).items()):
        provider, _, model = key.partition("/")
        w.sample(
            "quarterline_provider_events_total",
            int(count),
            provider=provider,
            model=model,
        )

    w.rate_block(
        "quarterline_cache_hit_ratio",
        "Provider cache-hit rate.",
        summary.get("cache_hit") or {},
    )

    json_block = summary.get("json") or {}
    w.rate_block(
        "quarterline_json_valid_ratio",
        "Generated JSON valid on first parse.",
        json_block.get("valid") or {},
    )
    w.rate_block(
        "quarterline_json_repair_ratio",
        "Generated JSON that needed the repair pass.",
        json_block.get("repaired") or {},
    )
    w.rate_block(
        "quarterline_numeric_rejection_ratio",
        "Answers rejected by the numeric-consistency check.",
        summary.get("numeric_rejection") or {},
    )

    bullets = summary.get("bullet_retention") or {}
    w.declare(
        "quarterline_bullets_total",
        "Generated bullets, by validation outcome.",
        "gauge",
    )
    w.sample("quarterline_bullets_total", int(bullets.get("retained") or 0), outcome="retained")
    w.sample("quarterline_bullets_total", int(bullets.get("dropped") or 0), outcome="dropped")
    w.declare(
        "quarterline_bullet_retention_ratio",
        "Retained bullets / (retained + dropped).",
        "gauge",
    )
    if bullets.get("rate") is not None:
        w.sample("quarterline_bullet_retention_ratio", bullets["rate"])

    answer = summary.get("answer_status") or {}
    for status in ("insufficient_evidence", "refusal", "provider_failure"):
        block = answer.get(status)
        if isinstance(block, dict):
            w.rate_block(
                "quarterline_answer_status_ratio",
                "Answer outcomes as a share of their sample.",
                block,
                status=status,
            )

    return w.render()
