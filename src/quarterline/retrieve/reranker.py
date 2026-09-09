"""Optional cross-encoder reranker (SPEC §16, §25).

``sentence-transformers`` lives behind the ``[rerank]`` extra and is NOT
installed by default. Everything here import-guards: when the library or the
model is unavailable, :class:`RerankerUnavailable` is raised and the search
service returns hybrid results with ``degraded={"reranker": reason}``
metadata — it never crashes and never fabricates scores (SPEC §25).
"""

from __future__ import annotations

import importlib
import importlib.util
from typing import Any

from quarterline.retrieve.models import EvidenceItem

#: Default reranker model (SPEC §4). CPU-supported; batch size configurable.
DEFAULT_RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
DEFAULT_BATCH_SIZE = 8


class RerankerUnavailable(RuntimeError):
    """The cross-encoder library or model could not be loaded."""


def _default_loader(model_name: str) -> Any:
    """Import-guarded ``CrossEncoder`` construction (importable extra only)."""
    if importlib.util.find_spec("sentence_transformers") is None:
        raise RerankerUnavailable(
            "sentence-transformers is not installed (install the [rerank] extra)"
        )
    try:
        module = importlib.import_module("sentence_transformers")
        return module.CrossEncoder(model_name)
    except Exception as exc:  # pragma: no cover - depends on optional lib
        raise RerankerUnavailable(f"cross-encoder {model_name!r} unavailable: {exc}") from exc


class CrossEncoderReranker:
    """Reranks a small candidate set with a cross-encoder (optional at runtime).

    The model loads lazily on the first :meth:`rerank` call so constructing
    the service stays cheap and works without the extra installed.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_RERANKER_MODEL,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        _loader: Any = _default_loader,
    ) -> None:
        self.model_name = model_name
        self.batch_size = batch_size
        self._loader = _loader
        self._model: Any = None
        self._load_error: str | None = None

    @property
    def model_id(self) -> str:
        return self.model_name

    def _ensure_model(self) -> Any:
        if self._model is None:
            if self._load_error is not None:
                raise RerankerUnavailable(self._load_error)
            try:
                self._model = self._loader(self.model_name)
            except RerankerUnavailable as exc:
                self._load_error = str(exc)
                raise
        return self._model

    def rerank(
        self, query: str, items: list[EvidenceItem], batch_size: int | None = None
    ) -> list[EvidenceItem]:
        """Return items with a real ``rerank`` score attached, best first.

        Raises :class:`RerankerUnavailable` when the model cannot be used; the
        caller decides how to degrade (SPEC §25).
        """
        if not items:
            return []
        model = self._ensure_model()
        size = batch_size or self.batch_size
        pairs = [[query, item.text] for item in items]
        try:
            scores = model.predict(pairs, batch_size=size)
        except TypeError:
            # Minimal CrossEncoder stubs may not accept batch_size.
            scores = model.predict(pairs)
        scored = []
        for item, score in zip(items, scores, strict=True):
            item.scores["rerank"] = float(score)
            scored.append(item)
        scored.sort(key=lambda item: (-item.scores["rerank"], item.chunk_id))
        return scored


def maybe_rerank(
    reranker: CrossEncoderReranker | None,
    query: str,
    items: list[EvidenceItem],
    *,
    enabled: bool = True,
) -> tuple[list[EvidenceItem], str | None]:
    """Rerank-or-degrade helper: never raises, never fakes scores.

    Returns ``(items, degraded_reason)``. ``degraded_reason`` is None on a
    successful rerank, else explains why the fused order was kept.
    """
    if not enabled:
        return items, "reranker disabled by configuration"
    if reranker is None:
        return items, "no reranker configured"
    try:
        return reranker.rerank(query, items), None
    except RerankerUnavailable as exc:
        return items, str(exc)


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_RERANKER_MODEL",
    "CrossEncoderReranker",
    "RerankerUnavailable",
    "maybe_rerank",
]
