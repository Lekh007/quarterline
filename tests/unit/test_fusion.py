"""RRF fusion tests (SPEC §16): hand-computed small case, rank preservation,
deterministic tie-breaks."""

from __future__ import annotations

import pytest

from quarterline.retrieve.fusion import RRF_K, FusedHit, rrf_fuse


def test_rrf_hand_computed_small_case() -> None:
    # lexical ranks: A=1, B=2   dense ranks: B=1, C=2
    lexical = [(101, 1), (102, 2)]
    dense = [(102, 1), (103, 2)]
    fused = rrf_fuse(lexical, dense, k=60, top_k=10)

    scores = {hit.chunk_id: hit.score for hit in fused}
    assert scores[101] == pytest.approx(1.0 / (60 + 1))  # lexical rank 1 only
    assert scores[102] == pytest.approx(1.0 / (60 + 2) + 1.0 / (60 + 1))  # both lists
    assert scores[103] == pytest.approx(1.0 / (60 + 2))  # dense rank 2 only
    # Order: the doc on both lists wins; then the two single-list hits by score.
    assert [hit.chunk_id for hit in fused] == [102, 101, 103]
    # Per-source ranks preserved (0 = absent from that source).
    by_id = {hit.chunk_id: hit for hit in fused}
    assert by_id[102].lexical_rank == 2 and by_id[102].dense_rank == 1
    assert by_id[101].lexical_rank == 1 and by_id[101].dense_rank == 0
    assert by_id[103].lexical_rank == 0 and by_id[103].dense_rank == 2


def test_rrf_ignores_raw_scores_uses_ranks_only() -> None:
    """The FTS5 bm25 sign convention never leaks into fusion."""
    lexical = [(1, 2), (2, 1)]  # note: deliberately not sorted by id
    dense: list = []
    fused = rrf_fuse(lexical, dense, top_k=5)
    assert [hit.chunk_id for hit in fused] == [2, 1]  # rank 1 before rank 2
    assert all(hit.score > 0 for hit in fused)


def test_rrf_top_k_and_tie_break_are_deterministic() -> None:
    lexical = [(i, i) for i in range(1, 6)]  # ids 1..5 with ranks 1..5
    dense = [(i, i) for i in range(10, 15)]
    fused = rrf_fuse(lexical, dense, k=RRF_K, top_k=4)
    assert len(fused) == 4
    assert all(isinstance(hit, FusedHit) for hit in fused)
    # Same input -> same output order, always.
    again = rrf_fuse(lexical, dense, k=RRF_K, top_k=4)
    assert [(h.chunk_id, h.score) for h in fused] == [(h.chunk_id, h.score) for h in again]
