"""Unit tests for Settings (SPEC §7, contract C1)."""

from __future__ import annotations

import pytest

from quarterline.config import (
    MIN_SEC_REQUEST_INTERVAL_MS,
    Settings,
    get_settings,
)


def make_settings(**overrides) -> Settings:
    """Settings ignoring any local .env file (env vars still apply)."""
    return Settings(_env_file=None, **overrides)


def test_spec_defaults_load() -> None:
    settings = make_settings()
    assert settings.app_env == "development"
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.database_url == "sqlite:///storage/quarterline.db"
    assert str(settings.storage_dir).replace("\\", "/") == "storage"
    assert settings.sec_min_request_interval_ms == MIN_SEC_REQUEST_INTERVAL_MS == 200
    assert settings.sec_max_retries == 4
    assert settings.llm_provider == "ollama"
    assert settings.ollama_base_url == "http://127.0.0.1:11434"
    assert settings.ollama_model == "qwen3:4b"
    assert settings.ollama_num_ctx == 8192
    assert settings.embed_provider == "ollama"
    assert settings.ollama_embed_model == "nomic-embed-text"
    assert settings.local_embed_model == "sentence-transformers/all-MiniLM-L6-v2"
    assert settings.reranker_enabled is True
    assert settings.reranker_model == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert settings.allow_remote_fallback is False
    assert settings.price_provider == "yfinance"
    assert settings.price_cache_hours == 24
    assert settings.brief_cache_hours == 24
    assert settings.max_generation_concurrency == 1
    assert settings.max_prompt_tokens == 7500
    assert settings.max_output_tokens == 900
    assert settings.agent_max_tool_calls == 4
    assert settings.agent_max_graph_transitions == 12
    assert settings.enable_llm_judge is False
    assert settings.log_level == "INFO"


def test_env_overrides_apply(monkeypatch) -> None:
    monkeypatch.setenv("SEC_MIN_REQUEST_INTERVAL_MS", "350")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.1:8b")
    monkeypatch.setenv("RERANKER_ENABLED", "false")
    settings = make_settings()
    assert settings.sec_min_request_interval_ms == 350
    assert settings.ollama_model == "llama3.1:8b"
    assert settings.reranker_enabled is False


def test_placeholder_identity_rejected() -> None:
    assert make_settings().edgar_identity_is_valid() is False
    assert make_settings(edgar_identity="").edgar_identity_is_valid() is False
    assert make_settings(edgar_identity="   ").edgar_identity_is_valid() is False
    assert (
        make_settings(
            edgar_identity="Quarterline your-real-contact@email.example"
        ).edgar_identity_is_valid()
        is False
    )


def test_real_identity_accepted() -> None:
    assert make_settings(
        edgar_identity="Quarterline research alice@mydomain.org"
    ).edgar_identity_is_valid()


def test_interval_clamped_up_never_down(monkeypatch) -> None:
    monkeypatch.setenv("SEC_MIN_REQUEST_INTERVAL_MS", "50")
    assert make_settings().sec_min_request_interval_ms == 200

    monkeypatch.setenv("SEC_MIN_REQUEST_INTERVAL_MS", "1")
    assert make_settings().sec_min_request_interval_ms == 200

    assert make_settings(sec_min_request_interval_ms=350).sec_min_request_interval_ms == 350


def test_negative_retries_clamped_to_zero() -> None:
    assert make_settings(sec_max_retries=-2).sec_max_retries == 0


def test_get_settings_is_cached(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "testing")
    get_settings.cache_clear()
    first = get_settings()
    monkeypatch.setenv("APP_ENV", "changed-after-cache")
    assert get_settings() is first
    assert get_settings().app_env == "testing"
    get_settings.cache_clear()
    assert get_settings().app_env == "changed-after-cache"


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        ("Quarterline research dev@quarterline.local", True),
        ("Jane Doe jane@example.org", True),
        ("contact@email.example", False),
    ],
)
def test_identity_matrix(identity: str, expected: bool) -> None:
    assert make_settings(edgar_identity=identity).edgar_identity_is_valid() is expected
