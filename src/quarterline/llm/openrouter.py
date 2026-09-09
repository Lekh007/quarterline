"""OpenRouter remote fallback provider (SPEC §2.3.2, §25; contract C10).

CONSENT GATE (hard security requirement, SPEC §2.3.2 / §25): constructing this
provider requires ``ALLOW_REMOTE_FALLBACK=true`` AND a non-empty
``OPENROUTER_API_KEY`` AND a non-empty ``OPENROUTER_MODEL`` in the settings.
Without all three it raises :class:`RemoteFallbackNotConsented` — user data is
NEVER silently sent to a remote provider.
"""

from __future__ import annotations

import time

import httpx

from quarterline.config import Settings, get_settings
from quarterline.llm.base import (
    GenerationResult,
    RemoteFallbackNotConsented,
    estimate_tokens,
)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT_SECONDS = 120.0


class OpenRouterGenerationProvider:
    """Remote OpenRouter chat-completions provider behind the consent gate."""

    provider_name = "openrouter"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        settings = settings if settings is not None else get_settings()
        resolved_key = api_key if api_key is not None else settings.openrouter_api_key
        resolved_model = model if model is not None else settings.openrouter_model
        if not settings.allow_remote_fallback:
            raise RemoteFallbackNotConsented(
                "ALLOW_REMOTE_FALLBACK is not enabled: remote generation providers are "
                "opt-in only; user data is never sent to a remote provider without "
                "explicit consent (SPEC §2.3.2/§25)"
            )
        if not (resolved_key or "").strip() or not (resolved_model or "").strip():
            raise RemoteFallbackNotConsented(
                "ALLOW_REMOTE_FALLBACK is enabled but OPENROUTER_API_KEY and/or "
                "OPENROUTER_MODEL is empty; refusing to construct a unusable remote provider"
            )
        self.api_key = resolved_key.strip()
        self.model = resolved_model.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client = client

    @property
    def model_id(self) -> str:
        return self.model

    def verify_model(self) -> None:
        """No cheap remote preflight: consent + credentials are checked at
        construction; an invalid model surfaces as a failed call, never as a
        silent fallback to a different model."""

    def _post(self, body: dict, headers: dict) -> httpx.Response:
        if self._client is not None:
            return self._client.post(
                f"{self.base_url}/chat/completions", json=body, headers=headers
            )
        return httpx.post(
            f"{self.base_url}/chat/completions",
            json=body,
            headers=headers,
            timeout=self.timeout_seconds,
        )

    def generate(
        self,
        messages: list[dict],
        json_schema: dict | None = None,
    ) -> GenerationResult:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # OpenRouter attribution headers (optional, documented by the API).
            "HTTP-Referer": "http://127.0.0.1:8000/",
            "X-Title": "Quarterline",
        }
        body: dict = {"model": self.model, "messages": list(messages)}
        if json_schema is not None:
            body["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        try:
            response = self._post(body, headers)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"openrouter generation failed: {exc}") from exc
        latency_ms = (time.perf_counter() - started) * 1000

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("openrouter returned no choices")
        text = str((choices[0].get("message") or {}).get("content") or "")
        usage = data.get("usage") or {}
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
            input_tokens = None
            output_tokens = None
            token_source = "estimate"
        else:
            token_source = "provider"

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
            finish_reason=choices[0].get("finish_reason"),
        )


__all__ = ["DEFAULT_BASE_URL", "OpenRouterGenerationProvider"]
