"""Reciprocal-rank fusion (SPEC §16).

    score(chunk) = sum over sources of 1 / (60 + rank)

Ranks are 1-based within each source list (``search_lexical`` / ``search_dense``
ordering). Only ranks feed the score, so the FTS5 bm25 sign convention never
affects fusion. Ties break deterministically on chunk id.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Standard RRF constant (SPEC §16).
RRF_K = 60

#: Fusion output size (SPEC §16: top 15 candidates).
FUSE_TOP_K = 15


@dataclass
class FusedHit:
    chunk_id: int
    score: float
    #: 1-based rank within each source list; 0 = absent from that source.
    lexical_rank: int = 0
    dense_rank: int = 0


def rrf_fuse(
    lexical_hits: list[tuple[int, int]] | list,
    dense_hits: list[tuple[int, int]] | list,
    *,
    k: int = RRF_K,
    top_k: int = FUSE_TOP_K,
) -> list[FusedHit]:
    """Fuse (chunk_id, rank) pairs from both sources into one ranked list.

    ``lexical_hits``/``dense_hits`` accept any sequence of (chunk_id, rank)
    pairs — :class:`LexicalHit` / :class:`DenseHit` unpack directly.
    """
    scores: dict[int, float] = {}
    lexical_rank_by_id: dict[int, int] = {}
    dense_rank_by_id: dict[int, int] = {}

    for chunk_id, rank in lexical_hits:
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
        lexical_rank_by_id[chunk_id] = rank
    for chunk_id, rank in dense_hits:
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
        dense_rank_by_id[chunk_id] = rank

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [
        FusedHit(
            chunk_id=chunk_id,
            score=score,
            lexical_rank=lexical_rank_by_id.get(chunk_id, 0),
            dense_rank=dense_rank_by_id.get(chunk_id, 0),
        )
        for chunk_id, score in ordered[:top_k]
    ]


__all__ = ["FUSE_TOP_K", "RRF_K", "FusedHit", "rrf_fuse"]
