"""Embedding provider + cache tests (SPEC §15, contract C9).

Ollama is exercised ONLY through httpx.MockTransport request shapes — no
network, no local model (plan ground rule 8)."""

from __future__ import annotations

import json

import httpx
import numpy as np
import pytest

from quarterline.retrieve.embeddings import (
    NOMIC_DOCUMENT_PREFIX,
    NOMIC_QUERY_PREFIX,
    EmbeddingModelMismatchError,
    EmbeddingProviderUnavailable,
    FakeEmbeddingProvider,
    OllamaEmbeddingProvider,
)

# -- FakeEmbeddingProvider (deterministic test artifact) -----------------------


def test_fake_provider_is_deterministic_and_normalized() -> None:
    provider = FakeEmbeddingProvider(dim=32, seed=7)
    vectors_a = provider.embed(["hello world", "another text"])
    vectors_b = provider.embed(["hello world", "another text"])
    assert vectors_a == vectors_b  # same text -> same vector, any process
    assert len(vectors_a) == 2 and len(vectors_a[0]) == 32
    for vector in vectors_a:
        norm = np.linalg.norm(vector)
        assert norm == pytest.approx(1.0, abs=1e-5)  # L2-normalized


def test_fake_provider_identity_changes_with_dim_and_seed() -> None:
    a = FakeEmbeddingProvider(dim=32, seed=7)
    b = FakeEmbeddingProvider(dim=64, seed=7)
    c = FakeEmbeddingProvider(dim=32, seed=8)
    assert a.model_id != b.model_id and a.model_id != c.model_id
    v1 = a.embed(["same text"])[0]
    v2 = FakeEmbeddingProvider(dim=32, seed=7).embed(["same text"])[0]
    assert v1 == v2


# -- OllamaEmbeddingProvider (request shape via MockTransport) -----------------


def _transport_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_ollama_embed_batches_and_applies_conventions() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        vectors = [[0.5] * 4 for _ in payload["input"]]
        return httpx.Response(200, json={"embeddings": vectors})

    provider = OllamaEmbeddingProvider(
        "http://127.0.0.1:11434",
        "nomic-embed-text",
        client=_transport_client(handler),
    )
    # Prefixes are applied by the CALLER (indexer/search), per contract C9.
    assert provider.document_prefix == NOMIC_DOCUMENT_PREFIX
    assert provider.query_prefix == NOMIC_QUERY_PREFIX
    texts = [f"{provider.document_prefix}doc one", f"{provider.document_prefix}doc two"]
    result = provider.embed(texts)

    assert len(requests) == 1  # one batched call
    assert requests[0]["model"] == "nomic-embed-text"
    assert requests[0]["input"] == texts
    assert provider.dim == 4  # learned from the response
    for vector in result:
        assert len(vector) == 4
        assert abs(np.linalg.norm(vector) - 1.0) < 1e-5  # normalized flag honored


def test_ollama_ensure_model_verifies_availability() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(
            200, json={"models": [{"name": "nomic-embed-text:latest"}, {"name": "qwen3:4b"}]}
        )

    provider = OllamaEmbeddingProvider(
        "http://127.0.0.1:11434", "nomic-embed-text", client=_transport_client(handler)
    )
    provider.ensure_model()  # base name match, no tag suffix needed

    missing = OllamaEmbeddingProvider(
        "http://127.0.0.1:11434",
        "all-MiniLM-L6-v2",
        client=_transport_client(handler),
    )
    with pytest.raises(EmbeddingProviderUnavailable, match="ollama pull"):
        missing.ensure_model()


def test_ollama_unreachable_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    provider = OllamaEmbeddingProvider(
        "http://127.0.0.1:9", "nomic-embed-text", client=_transport_client(handler)
    )
    with pytest.raises(EmbeddingProviderUnavailable):
        provider.embed(["text"])
    with pytest.raises(EmbeddingProviderUnavailable):
        provider.ensure_model()


def test_ollama_dim_unknown_until_first_embed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[0.1] * 8]})

    provider = OllamaEmbeddingProvider(
        "http://127.0.0.1:11434", "nomic-embed-text", client=_transport_client(handler)
    )
    with pytest.raises(EmbeddingProviderUnavailable, match="dimension unknown"):
        _ = provider.dim
    provider.embed(["now the dim is known"])
    assert provider.dim == 8


def test_ollama_short_payload_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [[0.1]]})

    provider = OllamaEmbeddingProvider(
        "http://127.0.0.1:11434", "nomic-embed-text", client=_transport_client(handler)
    )
    with pytest.raises(EmbeddingProviderUnavailable, match="embeddings payload"):
        provider.embed(["a", "b"])


# -- model-mismatch discipline (SPEC §15.7) -------------------------------------


def test_mismatch_error_is_raised_type_for_cross_model_queries() -> None:
    # The service-level gate raises this; here we pin the contract type.
    assert issubclass(EmbeddingModelMismatchError, RuntimeError)
    with pytest.raises(EmbeddingModelMismatchError):
        raise EmbeddingModelMismatchError("never query a Nomic index with MiniLM vectors")
