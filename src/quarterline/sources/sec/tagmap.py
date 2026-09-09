"""Ordered US-GAAP tag fallbacks (contract C4, SPEC §10.1).

Priority is list order: the first tag present with a compatible unit, scope and
economic period wins. Priority is applied only *after* compatibility filtering
(normalization wave), never before.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

#: Repository root when the package is used from its source checkout.
_REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_TAGMAP_PATH = _REPO_ROOT / "data" / "tagmap_us.yml"


@lru_cache(maxsize=4)
def _parse_tagmap(path: str) -> dict[str, tuple[str, ...]]:
    tagmap_path = Path(path)
    with tagmap_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise TypeError(f"tag map must be a mapping of concept -> tag list: {tagmap_path}")
    parsed: dict[str, tuple[str, ...]] = {}
    for concept, tags in raw.items():
        if not isinstance(tags, list) or not all(isinstance(tag, str) and tag for tag in tags):
            raise TypeError(f"tag list for concept {concept!r} must be non-empty strings")
        parsed[str(concept)] = tuple(tags)
    return parsed


def load_tagmap(path: str | None = None) -> dict[str, list[str]]:
    """Load ``data/tagmap_us.yml`` as ``{concept: [tags in priority order]}``.

    Returns a fresh copy on every call so callers cannot mutate the cached map.
    """
    tagmap_path = str(Path(path)) if path else str(DEFAULT_TAGMAP_PATH)
    return {concept: list(tags) for concept, tags in _parse_tagmap(tagmap_path).items()}
