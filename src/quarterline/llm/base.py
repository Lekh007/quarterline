"""Generation-provider interface (SPEC §17, PLAN contract C10).

Every provider returns a :class:`GenerationResult` carrying provider-reported
usage when available; when the provider does not report token usage the
count is explicitly marked ``token_count_source='estimate'`` (SPEC §17: never
label an estimate as measured).

Providers must verify that the configured model actually exists before use
(SPEC §4: "Verify configured model availability rather than assuming every
model identifier remains available") and raise
:class:`GenerationProviderUnavailable` otherwise. Remote providers additionally
require explicit user consent (SPEC §2.3.2) and raise
:class:`RemoteFallbackNotConsented` without it.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

TokenCountSource = Literal["provider", "estimate"]


class GenerationProviderUnavailable(RuntimeError):
    """The configured generation provider or model cannot be reached.

    Raised when the endpoint is unreachable, returns an error, or the
    configured model is not present in the provider's model list.
    """


class RemoteFallbackNotConsented(RuntimeError):
    """A remote generation provider was requested without explicit consent.

    SPEC §2.3.2 / §25: remote model fallback requires explicit opt-in; data is
    NEVER silently sent to a remote provider. Raised when the fallback is used
    without ``ALLOW_REMOTE_FALLBACK=true`` plus a non-empty API key and model.
    """


class GenerationResult(BaseModel):
    """One completed provider call (contract C10)."""

    text: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    token_count_source: TokenCountSource = "estimate"
    latency_ms: float = 0.0
    finish_reason: str | None = None


@runtime_checkable
class GenerationProvider(Protocol):
    """Contract C10 provider protocol (SPEC §17).

    ``messages`` are OpenAI-style chat messages
    (``[{"role": ..., "content": ...}, ...]``). ``json_schema`` is the JSON
    schema the output must satisfy; providers that support a JSON mode should
    enable it, and all providers must still be treated as untrusted output by
    the caller (the validation gate never trusts the provider).
    """

    provider_name: str
    model_id: str

    def verify_model(self) -> None:
        """Verify the configured model is available; raise if not (SPEC §4)."""
        ...

    def generate(
        self,
        messages: list[dict],
        json_schema: dict | None = None,
    ) -> GenerationResult: ...


def estimate_tokens(text: str) -> int:
    """Documented token estimate (chars/4, rounded up); the SAME estimator the
    retrieval layer uses, so prompt budgets and token accounting agree. Always
    reported with ``token_count_source='estimate'``."""

    if not text:
        return 0
    return max(1, -(-len(text) // 4))


__all__ = [
    "GenerationProvider",
    "GenerationProviderUnavailable",
    "GenerationResult",
    "RemoteFallbackNotConsented",
    "TokenCountSource",
    "estimate_tokens",
]
