"""Optional LLM judge (SPEC §22 "Optional LLM judge").

Enabled ONLY when all of ``ENABLE_LLM_JUDGE=true``, ``JUDGE_PROVIDER`` and
``JUDGE_MODEL`` are configured; otherwise the judge is disabled and every
entry point returns a disabled result without any model call.

Design constraints from the SPEC:
- one pinned rubric prompt, versioned as a file (``prompts/judge/v1.txt``);
- the judge scores faithfulness-to-supplied-evidence and relevance only,
  each on a 1-5 integer scale with a rationale;
- provider, model, prompt version, raw judgment and parsed score are all
  stored on :class:`JudgeResult`, with ``human_reviewed`` defaulting to False;
- LLM judge output is a MEASUREMENT WITH LIMITATIONS, never ground truth —
  every result carries that label.

Offline tests inject a mock ``transport`` callable; no network anywhere.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

JUDGE_PROMPT_VERSION = "v1"
REPO_ROOT = Path(__file__).resolve().parents[3]
JUDGE_PROMPT_PATH = REPO_ROOT / "prompts" / "judge" / f"{JUDGE_PROMPT_VERSION}.txt"

JUDGE_LABEL = (
    "LLM judge measurements with limitations — never ground truth "
    "(SPEC §22). Scores depend on the judge model and prompt version."
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class JudgeResult(BaseModel):
    """One judge verdict with full provenance."""

    enabled: bool = True
    question_id: str
    faithfulness: int | None = None  # 1-5
    relevance: int | None = None  # 1-5
    rationale: str | None = None
    raw_judgment: str | None = None
    judge_provider: str | None = None
    judge_model: str | None = None
    prompt_version: str = JUDGE_PROMPT_VERSION
    prompt_hash: str | None = None
    human_reviewed: bool = False  # never assumed; set only by a human later
    parse_error: str | None = None
    label: str = JUDGE_LABEL


@dataclass
class JudgeConfig:
    """Judge configuration resolved from settings (disabled by default)."""

    enabled: bool = False
    provider: str = ""
    model: str = ""
    base_url: str = "http://127.0.0.1:11434"

    @classmethod
    def from_settings(cls, settings) -> JudgeConfig:
        return cls(
            enabled=bool(
                getattr(settings, "enable_llm_judge", False)
                and getattr(settings, "judge_provider", "")
                and getattr(settings, "judge_model", "")
            ),
            provider=getattr(settings, "judge_provider", ""),
            model=getattr(settings, "judge_model", ""),
            base_url=getattr(settings, "ollama_base_url", "http://127.0.0.1:11434"),
        )


def load_prompt() -> str:
    """The pinned rubric prompt text (single source: prompts/judge/v1.txt)."""
    return JUDGE_PROMPT_PATH.read_text(encoding="utf-8")


def build_judge_messages(question: str, evidence: str, answer: str) -> list[dict]:
    """Render the pinned prompt into chat messages."""
    prompt = load_prompt()
    filled = (
        prompt.replace("{question}", question)
        .replace("{evidence}", evidence)
        .replace("{answer}", answer)
    )
    return [{"role": "user", "content": filled}]


def parse_judgment(raw: str) -> tuple[int | None, int | None, str | None, str | None]:
    """Extract (faithfulness, relevance, rationale, parse_error) from raw text."""
    match = _JSON_RE.search(raw or "")
    if match is None:
        return None, None, None, "no JSON object in judge response"
    try:
        payload = json.loads(match.group(0))
    except ValueError as exc:
        return None, None, None, f"invalid JSON in judge response: {exc}"

    def _score(key: str) -> int | None:
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value) if 1 <= int(value) <= 5 else None

    rationale = payload.get("rationale")
    return (
        _score("faithfulness"),
        _score("relevance"),
        rationale if isinstance(rationale, str) else None,
        None,
    )


@dataclass
class LLMJudge:
    """Judge gateway. ``transport(messages) -> str`` is injectable for tests."""

    config: JudgeConfig
    transport: object = None  # Callable[[list[dict]], str]; default = ollama
    prompt_hash: str = field(default="")

    def __post_init__(self) -> None:
        if not self.prompt_hash:
            import hashlib

            self.prompt_hash = hashlib.sha256(load_prompt().encode("utf-8")).hexdigest()[:16]

    def judge(self, question_id: str, question: str, evidence: str, answer: str) -> JudgeResult:
        if not self.config.enabled:
            return JudgeResult(enabled=False, question_id=question_id)
        messages = build_judge_messages(question, evidence, answer)
        raw = self._call(messages)
        faithfulness, relevance, rationale, parse_error = parse_judgment(raw)
        return JudgeResult(
            enabled=True,
            question_id=question_id,
            faithfulness=faithfulness,
            relevance=relevance,
            rationale=rationale,
            raw_judgment=raw,
            judge_provider=self.config.provider,
            judge_model=self.config.model,
            prompt_version=JUDGE_PROMPT_VERSION,
            prompt_hash=self.prompt_hash,
            parse_error=parse_error,
        )

    def _call(self, messages: list[dict]) -> str:
        if self.transport is not None:
            return str(self.transport(messages))
        # Default local transport: Ollama /api/generate (never a remote API).
        import httpx

        response = httpx.post(
            f"{self.config.base_url.rstrip('/')}/api/generate",
            json={
                "model": self.config.model,
                "prompt": messages[-1]["content"],
                "stream": False,
            },
            timeout=120.0,
        )
        response.raise_for_status()
        return str(response.json().get("response", ""))


__all__ = [
    "JUDGE_LABEL",
    "JUDGE_PROMPT_VERSION",
    "JudgeConfig",
    "JudgeResult",
    "LLMJudge",
    "build_judge_messages",
    "load_prompt",
    "parse_judgment",
]
