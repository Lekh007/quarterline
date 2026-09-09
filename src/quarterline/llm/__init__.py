"""Grounded generation package (SPEC §17–§18, PLAN contracts C10/C11).

Modules:
- :mod:`quarterline.llm.base` — provider protocol + result DTO + exceptions.
- :mod:`quarterline.llm.ollama` — local Ollama provider (default).
- :mod:`quarterline.llm.openrouter` — remote fallback behind the consent gate.
- :mod:`quarterline.llm.schemas` — brief/answer output schemas (C11).
- :mod:`quarterline.llm.prompts` — versioned prompt loading + strict render.
- :mod:`quarterline.llm.repair` — generate-once + single JSON repair pass.
- :mod:`quarterline.llm.generation` — the constrained-writer pipeline and the
  SPEC §18 validation gate.

Importing this package has no side effects and requires no network; the
deterministic routes never import it (SPEC §25 graceful degradation).
"""

from __future__ import annotations

from quarterline.llm.base import (
    GenerationProvider,
    GenerationProviderUnavailable,
    GenerationResult,
    RemoteFallbackNotConsented,
    estimate_tokens,
)
from quarterline.llm.ollama import OllamaGenerationProvider
from quarterline.llm.openrouter import OpenRouterGenerationProvider
from quarterline.llm.prompts import PromptSpec, load_prompt, render_prompt
from quarterline.llm.schemas import (
    Answer,
    Brief,
    BriefBullet,
    BriefRisks,
    BriefStatus,
    MetricMention,
    OpenQuestion,
)

__all__ = [
    "Answer",
    "Brief",
    "BriefBullet",
    "BriefRisks",
    "BriefStatus",
    "GenerationProvider",
    "GenerationProviderUnavailable",
    "GenerationResult",
    "MetricMention",
    "OllamaGenerationProvider",
    "OpenQuestion",
    "OpenRouterGenerationProvider",
    "PromptSpec",
    "RemoteFallbackNotConsented",
    "estimate_tokens",
    "load_prompt",
    "render_prompt",
]
