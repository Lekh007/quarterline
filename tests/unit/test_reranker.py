"""Reranker tests (SPEC §16/§25): import-guarded availability, real-score
attachment, and graceful degradation without fake scores."""

from __future__ import annotations

import importlib.util

import pytest

from quarterline.retrieve.models import EvidenceItem
from quarterline.retrieve.reranker import (
    CrossEncoderReranker,
    RerankerUnavailable,
    maybe_rerank,
)


def _items(*texts: str) -> list[EvidenceItem]:
    return [
        EvidenceItem(
            evidence_id=f"ev-{index:012d}",
            document_id=1,
            start_offset=0,
            end_offset=len(text),
            text=text,
            chunk_id=index,
            strategy="fixed",
        )
        for index, text in enumerate(texts, start=1)
    ]


def _sentence_transformers_installed() -> bool:
    return importlib.util.find_spec("sentence_transformers") is not None


def test_reranker_unavailable_without_the_optional_library() -> None:
    if _sentence_transformers_installed():
        pytest.skip("sentence-transformers installed; unavailable-path covered by injection")
    reranker = CrossEncoderReranker()
    items = _items("alpha beta", "gamma delta")
    with pytest.raises(RerankerUnavailable, match="rerank"):
        reranker.rerank("query", items)
    # Items are untouched — no fabricated scores.
    assert all("rerank" not in item.scores for item in items)


def test_reranker_attaches_real_scores_and_reorders() -> None:
    class StubCrossEncoder:
        def predict(self, pairs, batch_size=None):
            # Later input = higher score (reversed relevance for the test).
            return [float(index) for index, _ in enumerate(pairs)]

    reranker = CrossEncoderReranker("stub/model", _loader=lambda name: StubCrossEncoder())
    items = _items("first doc text", "second doc text", "third doc text")
    reranked = reranker.rerank("query", items, batch_size=2)
    assert [item.chunk_id for item in reranked] == [3, 2, 1]
    assert reranked[0].scores["rerank"] == pytest.approx(2.0)
    assert reranked[-1].scores["rerank"] == pytest.approx(0.0)


def test_maybe_rerank_degrades_to_hybrid_with_metadata() -> None:
    items = _items("alpha", "beta")

    def broken_loader(name: str):
        raise RerankerUnavailable("model download failed")

    reranker = CrossEncoderReranker("stub/model", _loader=broken_loader)
    result, reason = maybe_rerank(reranker, "query", items)
    assert result == items  # hybrid results still returned
    assert reason is not None and "model download failed" in reason
    assert all("rerank" not in item.scores for item in result)  # never fake scores


def test_maybe_rerank_disabled_by_configuration() -> None:
    items = _items("alpha")
    result, reason = maybe_rerank(None, "query", items, enabled=False)
    assert result == items
    assert reason == "reranker disabled by configuration"


def test_unavailable_reason_is_cached_not_retried() -> None:
    calls = {"n": 0}

    def counting_loader(name: str):
        calls["n"] += 1
        raise RerankerUnavailable("no library")

    reranker = CrossEncoderReranker("stub/model", _loader=counting_loader)
    for _ in range(2):
        with pytest.raises(RerankerUnavailable):
            reranker.rerank("q", _items("a"))
    assert calls["n"] == 1  # the failure is remembered, not re-attempted
