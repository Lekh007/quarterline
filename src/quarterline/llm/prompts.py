"""Prompt loading, versioning and strict rendering (SPEC §17, §23).

Prompts live in the repository ``prompts/`` directory, one file per version
(``prompts/brief/v1.txt``). Each artifact carries:

- ``name``   — the directory name (``brief`` / ``qa`` / ``memo`` / ``judge``);
- ``version``— the file stem (``v1``);
- ``sha256`` — content hash; it participates in the generation cache key and
  in run events (SPEC §24/§25: cache keys reflect content versions).

:func:`render_prompt` substitutes ``{placeholder}`` tokens strictly: an
unknown placeholder, a missing variable, or an unused variable are errors.
Prompt changes MUST add a new version file (SPEC §23); the validation gate and
cache key change with the hash, not with the file mtime.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

#: Repository ``prompts/`` directory (src/quarterline/llm/prompts.py -> repo root).
PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"

_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


class PromptNotFoundError(FileNotFoundError):
    """The requested prompt name/version does not exist under ``prompts/``."""


class PromptRenderError(ValueError):
    """Strict placeholder substitution failed (missing/unknown/unused var)."""


@dataclass(frozen=True)
class PromptSpec:
    """One versioned, hashed prompt artifact."""

    name: str
    version: str
    path: Path
    content: str

    @property
    def sha256(self) -> str:
        """Content hash (cache key + run-event component; SPEC §24/§25)."""
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    @property
    def prompt_version(self) -> str:
        """``name/version`` label used in cache keys and run events."""
        return f"{self.name}/{self.version}"


def load_prompt(name: str, version: str = "v1") -> PromptSpec:
    """Load ``prompts/<name>/<version>.txt``; raises PromptNotFoundError."""
    path = PROMPTS_DIR / name / f"{version}.txt"
    if not path.is_file():
        raise PromptNotFoundError(f"prompt {name}/{version} not found at {path}")
    return PromptSpec(name=name, version=version, path=path, content=path.read_text("utf-8"))


def render_prompt(spec: PromptSpec, /, **variables: str) -> str:
    """Strict ``{placeholder}`` substitution.

    - a placeholder in the template without a matching variable -> error;
    - a variable that never appears in the template -> error (typos surface);
    - substituted values may contain braces safely (only the template is
      scanned).
    """
    used: set[str] = set()

    def _sub(match: re.Match[str]) -> str:
        var = match.group(1)
        if var not in variables:
            raise PromptRenderError(
                f"prompt {spec.prompt_version} needs variable {var!r} which was not supplied"
            )
        used.add(var)
        return str(variables[var])

    rendered = _PLACEHOLDER_RE.sub(_sub, spec.content)
    unused = sorted(set(variables) - used)
    if unused:
        raise PromptRenderError(
            f"prompt {spec.prompt_version} does not use supplied variable(s): {unused}"
        )
    return rendered


__all__ = [
    "PROMPTS_DIR",
    "PromptNotFoundError",
    "PromptRenderError",
    "PromptSpec",
    "load_prompt",
    "render_prompt",
]
