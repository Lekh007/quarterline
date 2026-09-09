"""Application configuration (SPEC §7, contract C1).

All settings load from environment variables and an optional ``.env`` file at the
repository root. Field names match the documented environment variable names
(case-insensitive), e.g. ``EDGAR_IDENTITY`` -> ``Settings.edgar_identity``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: SEC fair-access minimum spacing between request starts (SPEC §2.4.2).
MIN_SEC_REQUEST_INTERVAL_MS = 200

#: Fragments that mark an EDGAR identity as an unfilled placeholder. ``.example``
#: is a reserved TLD, so any real contact string cannot contain it.
EDGAR_PLACEHOLDER_FRAGMENTS: tuple[str, ...] = (
    "your-real-contact@email.example",
    "email.example",
)


class Settings(BaseSettings):
    """Every configuration field from SPEC §7."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Application
    app_env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000

    # Storage
    database_url: str = "sqlite:///storage/quarterline.db"
    storage_dir: Path = Path("storage")

    # SEC access
    edgar_identity: str = "Quarterline your-real-contact@email.example"
    sec_min_request_interval_ms: int = 200
    sec_max_retries: int = 4

    # Generation provider
    llm_provider: str = "ollama"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:4b"
    ollama_num_ctx: int = 8192

    # Embedding provider (independent of generation selection, SPEC §7)
    embed_provider: str = "ollama"
    ollama_embed_model: str = "nomic-embed-text"
    local_embed_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    # Reranker
    reranker_enabled: bool = True
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    # Remote fallback
    allow_remote_fallback: bool = False
    openrouter_api_key: str = ""
    openrouter_model: str = ""

    # Prices
    price_provider: str = "yfinance"
    price_cache_hours: int = 24

    # Generation budget
    brief_cache_hours: int = 24
    max_generation_concurrency: int = 1
    max_prompt_tokens: int = 7500
    max_output_tokens: int = 900

    # Agent budgets
    agent_max_tool_calls: int = 4
    agent_max_graph_transitions: int = 12

    # Evaluation judge
    enable_llm_judge: bool = False
    judge_provider: str = ""
    judge_model: str = ""

    # Logging
    log_level: str = "INFO"

    @field_validator("sec_min_request_interval_ms", mode="after")
    @classmethod
    def _clamp_min_interval(cls, value: int) -> int:
        # SPEC §2.4.2: at least 200 ms between SEC request starts. Clamp up, never down.
        return max(value, MIN_SEC_REQUEST_INTERVAL_MS)

    @field_validator("sec_max_retries", mode="after")
    @classmethod
    def _clamp_min_retries(cls, value: int) -> int:
        return max(value, 0)

    def edgar_identity_is_valid(self) -> bool:
        """True when the SEC identity is present and not a known placeholder (SPEC §7)."""
        identity = (self.edgar_identity or "").strip()
        if not identity:
            return False
        lowered = identity.lower()
        return not any(fragment in lowered for fragment in EDGAR_PLACEHOLDER_FRAGMENTS)


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor (contract C1)."""
    return Settings()
