"""Ollama generation provider (SPEC §4, §7, §17; contract C10).

Uses ``POST /api/chat`` against ``OLLAMA_BASE_URL`` with ``OLLAMA_MODEL`` and
``options.num_ctx`` from settings. Before every call the configured model is
verified against ``GET /api/tags`` — never assume a model identifier remains
available (SPEC §4). Provider-reported usage (``prompt_eval_count`` /
``eval_count``) is used when present; otherwise the token counts are explicitly
marked ``token_count_source='estimate'``.

Tested exclusively with ``httpx.MockTransport`` — no live Ollama in tests
(PLAN ground rule 8).
"""

from __future__ import annotations

import time

import httpx

from quarterline.llm.base import (
    GenerationProviderUnavailable,
    GenerationResult,
    estimate_tokens,
)

#: Default per-call timeout; generation on CPU can be slow but must stay bounded.
DEFAULT_TIMEOUT_SECONDS = 180.0


class OllamaGenerationProvider:
    """Local Ollama chat provider with availability verification."""

    provider_name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str = "qwen3:4b",
        *,
        num_ctx: int = 8192,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.timeout_seconds = timeout_seconds
        self._client = client

    @property
    def model_id(self) -> str:
        return self.model

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_seconds)
        return self._client

    # -- availability (SPEC §4: verify, never assume) -------------------------

    def verify_model(self) -> None:
        """Raise :class:`GenerationProviderUnavailable` unless the configured
        model is present in the local Ollama tag list."""
        try:
            response = self._http().get(f"{self.base_url}/api/tags")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GenerationProviderUnavailable(f"ollama unreachable: {exc}") from exc
        names = {str(entry.get("name") or "") for entry in (payload.get("models") or [])}
        base = self.model.split(":", 1)[0]
        if self.model not in names and not any(name.split(":", 1)[0] == base for name in names):
            raise GenerationProviderUnavailable(
                f"model {self.model!r} not present in ollama tags; run `ollama pull {self.model}`"
            )

    # -- generation ------------------------------------------------------------

    def generate(
        self,
        messages: list[dict],
        json_schema: dict | None = None,
    ) -> GenerationResult:
        self.verify_model()
        payload: dict = {
            "model": self.model,
            "messages": list(messages),
            "stream": False,
            "options": {"num_ctx": self.num_ctx},
        }
        if json_schema is not None:
            # Ollama structured-output mode: pass the full JSON schema for
            # constrained decoding (plain "json" mode still let small models
            # produce type-wrong fields). The validation gate still treats
            # the reply as untrusted (SPEC §18).
            payload["format"] = json_schema

        started = time.perf_counter()
        try:
            response = self._http().post(f"{self.base_url}/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GenerationProviderUnavailable(f"ollama generation failed: {exc}") from exc
        latency_ms = (time.perf_counter() - started) * 1000

        message = data.get("message") or {}
        text = str(message.get("content") or "")
        input_tokens = data.get("prompt_eval_count")
        output_tokens = data.get("eval_count")
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            input_tokens = None
            output_tokens = None
            token_source = "estimate"
        else:
            token_source = "provider"
        finish_reason = data.get("done_reason")
        if finish_reason is None:
            finish_reason = "stop" if data.get("done") else None

        return GenerationResult(
            text=text,
            provider=self.provider_name,
            model=self.model,
            input_tokens=(
                input_tokens if input_tokens is not None else estimate_tokens(str(messages))
            ),
            output_tokens=(output_tokens if output_tokens is not None else estimate_tokens(text)),
            token_count_source=token_source,
            latency_ms=round(latency_ms, 2),
            finish_reason=finish_reason,
        )


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "OllamaGenerationProvider"]
