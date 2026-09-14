"""Prometheus exposition of the SPEC §24 aggregates.

The contract under test is the honesty one: every rate ships with its sample
size, and a rate with no samples is *absent* rather than exported as zero.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from ui_test_helpers import configure_app_db, make_client

from quarterline.api.routers.metrics import CONTENT_TYPE
from quarterline.observability.metrics import metrics_summary
from quarterline.observability.prometheus import render_prometheus


def _samples(text: str) -> dict[str, str]:
    """Parse exposition text into {line-without-value: value}, ignoring comments."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, value = line.rpartition(" ")
        out[key] = value
    return out


EMPTY_SUMMARY = {
    "total_runs": 0,
    "total_events": 0,
    "endpoints": {},
    "latency_ms": {f: {"n": 0, "p50_ms": None, "p95_ms": None} for f in ("total_latency_ms",)},
    "provider_model_breakdown": {},
    "cache_hit": {"n": 0, "count": 0, "rate": None},
    "json": {
        "valid": {"n": 0, "count": 0, "rate": None},
        "repaired": {"n": 0, "count": 0, "rate": None},
    },
    "numeric_rejection": {"n": 0, "count": 0, "rate": None},
    "bullet_retention": {"retained": 0, "dropped": 0, "rate": None},
    "answer_status": {
        "n": 0,
        "insufficient_evidence": {"n": 0, "count": 0, "rate": None},
        "refusal": {"n": 0, "count": 0, "rate": None},
        "provider_failure": {"n": 0, "count": 0, "rate": None},
    },
}


def test_empty_store_exports_zero_counts_but_no_unearned_rates() -> None:
    samples = _samples(render_prometheus(EMPTY_SUMMARY))

    assert samples["quarterline_runs_total"] == "0"
    assert samples["quarterline_events_total"] == "0"
    # Sample sizes are always present...
    assert samples["quarterline_cache_hit_ratio_samples"] == "0"
    assert samples['quarterline_latency_samples{stage="total"}'] == "0"
    # ...but an unmeasured rate must not appear at all.
    assert "quarterline_cache_hit_ratio" not in samples
    assert "quarterline_bullet_retention_ratio" not in samples
    assert not any(k.startswith("quarterline_latency_milliseconds") for k in samples)


def test_measured_values_render_with_labels_and_sample_sizes() -> None:
    summary = {
        **EMPTY_SUMMARY,
        "total_runs": 3,
        "total_events": 7,
        "endpoints": {"/brief": 5, "/ask": 2},
        "latency_ms": {"total_latency_ms": {"n": 4, "p50_ms": 120.0, "p95_ms": 900.0}},
        "provider_model_breakdown": {"ollama/qwen3:4b": 6},
        "cache_hit": {"n": 6, "count": 3, "rate": 0.5},
        "bullet_retention": {"retained": 9, "dropped": 1, "rate": 0.9},
        "answer_status": {
            **EMPTY_SUMMARY["answer_status"],
            "refusal": {"n": 4, "count": 1, "rate": 0.25},
        },
    }
    samples = _samples(render_prometheus(summary))

    assert samples["quarterline_runs_total"] == "3"
    assert samples['quarterline_endpoint_events_total{endpoint="/brief"}'] == "5"
    assert samples['quarterline_latency_milliseconds{stage="total",quantile="0.95"}'] == "900.0"
    assert samples['quarterline_latency_samples{stage="total"}'] == "4"
    assert samples['quarterline_provider_events_total{provider="ollama",model="qwen3:4b"}'] == "6"
    assert samples["quarterline_cache_hit_ratio"] == "0.5"
    assert samples["quarterline_cache_hit_ratio_samples"] == "6"
    assert samples['quarterline_bullets_total{outcome="dropped"}'] == "1"
    assert samples["quarterline_bullet_retention_ratio"] == "0.9"
    assert samples['quarterline_answer_status_ratio{status="refusal"}'] == "0.25"
    assert samples['quarterline_answer_status_ratio_samples{status="refusal"}'] == "4"


def test_label_values_are_escaped() -> None:
    summary = {**EMPTY_SUMMARY, "endpoints": {r'we"ird\path': 1}}
    text = render_prometheus(summary)
    assert r'endpoint="we\"ird\\path"' in text


def test_every_metric_is_declared_once() -> None:
    lines = render_prometheus(EMPTY_SUMMARY).splitlines()
    types = [line for line in lines if line.startswith("# TYPE ")]
    assert len(types) == len(set(types)), "duplicate # TYPE declaration"


@pytest.fixture
def api_client(tmp_path, monkeypatch) -> TestClient:
    configure_app_db(tmp_path, monkeypatch)
    with make_client() as client:
        yield client


def test_metrics_endpoint_serves_scrapeable_text(api_client: TestClient) -> None:
    response = api_client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE
    assert "# TYPE quarterline_runs_total gauge" in response.text
    assert response.text.endswith("\n")


def test_endpoint_and_dashboard_read_the_same_numbers(api_client: TestClient) -> None:
    """One source of truth: the scrape is a rendering of metrics_summary()."""
    from quarterline.store.db import session_scope

    with session_scope() as session:
        expected = render_prometheus(metrics_summary(session))

    body = api_client.get("/metrics").text
    strip = lambda t: [ln for ln in t.splitlines() if not ln.startswith("#")]
    assert strip(body) == strip(expected)
