"""Disk cache for raw source downloads (SPEC §25 raw cache).

Layout: ``{STORAGE_DIR}/raw/{sha256(url)}.body`` plus ``{sha256(url)}.json``
metadata sidecar holding url, fetched_at, etag, last_modified, content_type and
the sha256 ``content_hash`` of the body.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from quarterline.config import Settings, get_settings


@dataclass(frozen=True)
class CachedEntry:
    url: str
    content: bytes
    content_hash: str
    etag: str | None
    last_modified: str | None
    content_type: str | None
    fetched_at: str
    path: Path
    metadata: dict[str, object]


def _raw_dir(settings: Settings) -> Path:
    return Path(settings.storage_dir) / "raw"


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _meta_path(url: str, settings: Settings) -> Path:
    return _raw_dir(settings) / f"{_cache_key(url)}.json"


def _body_path(url: str, settings: Settings) -> Path:
    return _raw_dir(settings) / f"{_cache_key(url)}.body"


def cache_put(
    url: str,
    content: bytes,
    etag: str | None = None,
    last_modified: str | None = None,
    content_type: str | None = None,
    settings: Settings | None = None,
) -> CachedEntry:
    """Store a response body plus its HTTP validators on disk."""
    settings = settings or get_settings()
    directory = _raw_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)

    content_hash = hashlib.sha256(content).hexdigest()
    fetched_at = datetime.now(UTC).isoformat()
    body = _body_path(url, settings)
    body.write_bytes(content)

    metadata: dict[str, object] = {
        "url": url,
        "fetched_at": fetched_at,
        "etag": etag,
        "last_modified": last_modified,
        "content_type": content_type,
        "content_hash": content_hash,
        "body_file": body.name,
    }
    meta = _meta_path(url, settings)
    meta.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    return CachedEntry(
        url=url,
        content=content,
        content_hash=content_hash,
        etag=etag,
        last_modified=last_modified,
        content_type=content_type,
        fetched_at=fetched_at,
        path=body,
        metadata=metadata,
    )


def cache_get(url: str, settings: Settings | None = None) -> CachedEntry | None:
    """Return the cached entry for ``url`` or ``None`` on miss/corruption.

    A body whose stored ``content_hash`` no longer matches is treated as a miss
    (never return silently corrupted content).
    """
    settings = settings or get_settings()
    meta = _meta_path(url, settings)
    body = _body_path(url, settings)
    if not meta.is_file() or not body.is_file():
        return None
    try:
        metadata = json.loads(meta.read_text(encoding="utf-8"))
        content = body.read_bytes()
    except (OSError, json.JSONDecodeError):
        return None
    content_hash = str(metadata.get("content_hash") or "")
    if hashlib.sha256(content).hexdigest() != content_hash:
        return None
    return CachedEntry(
        url=url,
        content=content,
        content_hash=content_hash,
        etag=metadata.get("etag"),  # type: ignore[arg-type]
        last_modified=metadata.get("last_modified"),  # type: ignore[arg-type]
        content_type=metadata.get("content_type"),  # type: ignore[arg-type]
        fetched_at=str(metadata.get("fetched_at") or ""),
        path=body,
        metadata=metadata,
    )
