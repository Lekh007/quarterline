"""Embedding providers and model-identity discipline (SPEC §15, contract C9).

Protocol (C9): ``model_id: str``, ``dim: int``, ``embed(texts) -> vectors``.

Model-specific conventions (SPEC §15.5): Nomic-family models prefix documents
with ``search_document: `` and queries with ``search_query: ``. The prefixes
are applied by the *caller* (indexer / search service) via
:attr:`EmbeddingProvider.document_prefix` / :attr:`EmbeddingProvider.query_prefix`
so the C9 ``embed(texts)`` signature stays unchanged.

Never mix models (SPEC §15.7): vectors are cached and stored keyed by
(provider, model, model_revision); the search service rejects a query whose
provider does not match the index's recorded embedding model
(:class:`EmbeddingModelMismatchError`).

L2 normalization: when ``normalize=True`` (the default and the recommended
configuration), vectors are L2-normalized before storage/search and the
``embeddings.normalized`` flag is stored alongside the dimension.
"""

from __future__ import annotations

import hashlib
from typing import Protocol, runtime_checkable

import httpx
import numpy as np

#: Prefix conventions for Nomic embedding models (SPEC §15.5).
NOMIC_DOCUMENT_PREFIX = "search_document: "
NOMIC_QUERY_PREFIX = "search_query: "


class EmbeddingProviderUnavailable(RuntimeError):
    """The configured embedding provider or model cannot be reached."""


class EmbeddingModelMismatchError(RuntimeError):
    """Query embedding model does not match the index's embedding model.

    SPEC §15.7: never silently query a Nomic index with MiniLM vectors (or any
    other cross-model combination).
    """


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Contract C9."""

    model_id: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return matrix / norms


class FakeEmbeddingProvider:
    """Deterministic hash-based vectors. FOR TESTS ONLY — non-production.

    Same text + same (dim, seed) => identical vector in any process; different
    text => pseudo-random unit vectors with no semantic signal. ``model_id``
    embeds dim and seed so mismatch detection works in tests.
    """

    def __init__(self, *, dim: int = 64, seed: int = 20260909, normalize: bool = True) -> None:
        self.dim = dim
        self.seed = seed
        self.normalize = normalize
        self.model_id = f"fake-embed-{dim}d-seed{seed}"
        self.provider_name = "fake"
        self.document_prefix = ""
        self.query_prefix = ""

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = np.empty((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            digest = hashlib.sha256(f"{self.model_id}|{text}".encode()).digest()
            rng_seed = int.from_bytes(digest[:8], "little")
            vectors[i] = np.random.default_rng(rng_seed).standard_normal(self.dim)
        if self.normalize:
            vectors = _l2_normalize(vectors)
        return [row.astype(np.float32).tolist() for row in vectors]


class OllamaEmbeddingProvider:
    """Ollama ``/api/embed`` client (batch calls) with availability checking.

    Applies the Nomic prefix conventions when ``apply_nomic_conventions`` is
    set (the default for ``nomic-embed-text``); the normalize flag controls
    L2 normalization before storage/search.
    """

    def __init__(
        self,
        base_url: str,
        model: str = "nomic-embed-text",
        *,
        normalize: bool = True,
        apply_nomic_conventions: bool = True,
        timeout_seconds: float = 60.0,
        client: httpx.Client | None = None,
        model_revision: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.normalize = normalize
        self.apply_nomic_conventions = apply_nomic_conventions
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._dim: int | None = None
        #: Ollama does not expose a revision; identity = (ollama, model, "").
        self.model_revision = model_revision

    # -- protocol surface -----------------------------------------------------

    provider_name = "ollama"

    @property
    def model_id(self) -> str:
        return self.model

    @property
    def dim(self) -> int:
        if self._dim is None:
            raise EmbeddingProviderUnavailable(
                "embedding dimension unknown until the model is verified; call ensure_model() first"
            )
        return self._dim

    @property
    def document_prefix(self) -> str:
        return NOMIC_DOCUMENT_PREFIX if self.apply_nomic_conventions else ""

    @property
    def query_prefix(self) -> str:
        return NOMIC_QUERY_PREFIX if self.apply_nomic_conventions else ""

    # -- availability ---------------------------------------------------------

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_seconds)
        return self._client

    def ensure_model(self) -> None:
        """Verify the configured model exists locally (SPEC §4: verify, never
        assume every model identifier remains available)."""
        try:
            response = self._http().get(f"{self.base_url}/api/tags")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingProviderUnavailable(f"ollama unreachable: {exc}") from exc
        names = {str(entry.get("name") or "") for entry in (payload.get("models") or [])}
        base = self.model.split(":", 1)[0]
        if self.model not in names and not any(name.split(":", 1)[0] == base for name in names):
            raise EmbeddingProviderUnavailable(
                f"model {self.model!r} not present in ollama tags; run `ollama pull {self.model}`"
            )

    # -- embedding ------------------------------------------------------------

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = self._http().post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": list(texts)},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EmbeddingProviderUnavailable(f"ollama embed failed: {exc}") from exc
        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(texts):
            raise EmbeddingProviderUnavailable(
                "ollama /api/embed returned no/short embeddings payload"
            )
        matrix = np.asarray(embeddings, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(texts):
            raise EmbeddingProviderUnavailable("ollama embeddings payload malformed")
        if self.normalize:
            matrix = _l2_normalize(matrix)
        self._dim = int(matrix.shape[1])
        return [row.astype(np.float32).tolist() for row in matrix]


__all__ = [
    "NOMIC_DOCUMENT_PREFIX",
    "NOMIC_QUERY_PREFIX",
    "EmbeddingModelMismatchError",
    "EmbeddingProvider",
    "EmbeddingProviderUnavailable",
    "FakeEmbeddingProvider",
    "OllamaEmbeddingProvider",
]
