"""Provider adapter tests — httpx.MockTransport only, no live Ollama."""

from __future__ import annotations

import json

import httpx
import pytest

from quarterline.config import Settings
from quarterline.llm.base import (
    GenerationProviderUnavailable,
    RemoteFallbackNotConsented,
)
from quarterline.llm.ollama import OllamaGenerationProvider
from quarterline.llm.openrouter import OpenRouterGenerationProvider

MESSAGES = [
    {"role": "system", "content": "system prompt"},
    {"role": "user", "content": "user payload"},
]


def _tags_payload(model: str) -> dict:
    return {"models": [{"name": model}, {"name": "nomic-embed-text:latest"}]}


def _chat_payload(text: str, *, with_usage: bool = True) -> dict:
    payload = {
        "model": "qwen3:4b",
        "message": {"role": "assistant", "content": text},
        "done": True,
        "done_reason": "stop",
    }
    if with_usage:
        payload["prompt_eval_count"] = 111
        payload["eval_count"] = 22
    return payload


def test_ollama_unreachable_raises_provider_unavailable() -> None:
    provider = OllamaGenerationProvider("http://127.0.0.1:9", client=_dead_transport_client())
    with pytest.raises(GenerationProviderUnavailable):
        provider.generate(MESSAGES)


def _dead_transport_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_ollama_missing_model_raises_provider_unavailable() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=_tags_payload("llama3:8b"))
        return httpx.Response(200, json=_chat_payload("{}"))

    provider = OllamaGenerationProvider(
        "http://ollama.local",
        model="qwen3:4b",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GenerationProviderUnavailable, match="not present"):
        provider.generate(MESSAGES)
    # the tags check happens BEFORE any generation request (SPEC §4)
    assert [request.url.path for request in seen] == ["/api/tags"]


def test_ollama_generation_request_shape_and_provider_usage() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=_tags_payload("qwen3:4b"))
        return httpx.Response(200, json=_chat_payload('{"status": "ok"}'))

    provider = OllamaGenerationProvider(
        "http://ollama.local/",
        model="qwen3:4b",
        num_ctx=4096,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.generate(MESSAGES, json_schema={"type": "object"})

    assert result.text == '{"status": "ok"}'
    assert result.provider == "ollama"
    assert result.model == "qwen3:4b"
    assert result.input_tokens == 111 and result.output_tokens == 22
    assert result.token_count_source == "provider"
    assert result.finish_reason == "stop"
    assert result.latency_ms >= 0.0

    chat_request = seen[-1]
    body = json.loads(chat_request.content)
    assert chat_request.url.path == "/api/chat"
    assert body["model"] == "qwen3:4b"
    assert body["messages"] == MESSAGES
    assert body["stream"] is False
    assert body["options"]["num_ctx"] == 4096
    assert body["format"] == {"type": "object"}  # full schema: constrained decoding


def test_ollama_estimates_tokens_when_usage_absent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=_tags_payload("qwen3:4b"))
        return httpx.Response(200, json=_chat_payload("x" * 40, with_usage=False))

    provider = OllamaGenerationProvider(
        "http://ollama.local",
        model="qwen3:4b",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.generate(MESSAGES)
    assert result.token_count_source == "estimate"
    assert result.output_tokens == 10  # documented chars/4 estimator


# ---------------------------------------------------------------------------
# OpenRouter consent gate + request shape
# ---------------------------------------------------------------------------


def _remote_settings(**overrides) -> Settings:
    values = {
        "allow_remote_fallback": False,
        "openrouter_api_key": "",
        "openrouter_model": "",
        "edgar_identity": "x",
    }
    values.update(overrides)
    return Settings(**values)


def test_openrouter_refused_without_opt_in_even_with_key() -> None:
    with pytest.raises(RemoteFallbackNotConsented, match="ALLOW_REMOTE_FALLBACK"):
        OpenRouterGenerationProvider(
            _remote_settings(openrouter_api_key="sk-secret", openrouter_model="m")
        )


def test_openrouter_refused_with_opt_in_but_missing_key_or_model() -> None:
    with pytest.raises(RemoteFallbackNotConsented, match="API_KEY"):
        OpenRouterGenerationProvider(
            _remote_settings(allow_remote_fallback=True, openrouter_model="m")
        )
    with pytest.raises(RemoteFallbackNotConsented, match="API_KEY"):
        OpenRouterGenerationProvider(
            _remote_settings(allow_remote_fallback=True, openrouter_api_key="   ")
        )
    with pytest.raises(RemoteFallbackNotConsented, match="API_KEY"):
        OpenRouterGenerationProvider(
            _remote_settings(allow_remote_fallback=True, openrouter_api_key="k")
        )


def test_openrouter_request_shape_with_consent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": '{"status": "ok"}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 9},
            },
        )

    provider = OpenRouterGenerationProvider(
        _remote_settings(
            allow_remote_fallback=True,
            openrouter_api_key="sk-test",
            openrouter_model="vendor/model",
        ),
        base_url="https://remote.example/api/v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.generate(MESSAGES, json_schema={"type": "object"})

    request = seen[0]
    assert request.url == "https://remote.example/api/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["model"] == "vendor/model"
    assert body["messages"] == MESSAGES
    assert body["response_format"] == {"type": "json_object"}
    assert result.provider == "openrouter"
    assert result.token_count_source == "provider"
    assert result.finish_reason == "stop"


def test_openrouter_http_failure_raises_not_silent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("remote down", request=request)

    provider = OpenRouterGenerationProvider(
        _remote_settings(
            allow_remote_fallback=True, openrouter_api_key="sk-test", openrouter_model="m"
        ),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RuntimeError, match="openrouter generation failed"):
        provider.generate(MESSAGES)
