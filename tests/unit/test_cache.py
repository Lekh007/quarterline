"""Unit tests for the raw disk cache (SPEC §25)."""

from __future__ import annotations

import hashlib

import pytest

from quarterline.ingest.cache import cache_get, cache_put


@pytest.fixture
def storage_dir(offline_env):
    return offline_env


def test_put_get_roundtrip(storage_dir) -> None:
    url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    content = b'{"cik": 320193, "entityName": "Apple Inc."}'
    entry = cache_put(
        url,
        content,
        etag='"abc123"',
        last_modified="Wed, 09 Sep 2026 00:00:00 GMT",
        content_type="application/json",
    )

    assert entry.content_hash == hashlib.sha256(content).hexdigest()
    assert entry.etag == '"abc123"'
    assert entry.last_modified == "Wed, 09 Sep 2026 00:00:00 GMT"
    assert entry.content_type == "application/json"
    assert entry.fetched_at  # ISO timestamp recorded

    loaded = cache_get(url)
    assert loaded is not None
    assert loaded.content == content
    assert loaded.content_hash == entry.content_hash
    assert loaded.etag == '"abc123"'
    assert loaded.last_modified == entry.last_modified
    assert loaded.metadata["url"] == url
    assert str(loaded.path).startswith(str(storage_dir))


def test_get_miss_returns_none(storage_dir) -> None:
    assert cache_get("https://data.sec.gov/not-cached.json") is None


def test_corrupt_body_treated_as_miss(storage_dir) -> None:
    url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json"
    cache_put(url, b"original-bytes", etag='"v1"')
    # Simulate on-disk corruption: overwrite the body behind the cache's back.
    loaded = cache_get(url)
    assert loaded is not None
    loaded.path.write_bytes(b"tampered-bytes")
    assert cache_get(url) is None


def test_url_keyed_by_sha256_not_path(storage_dir) -> None:
    cache_put("https://example.gov/a", b"A")
    cache_put("https://example.gov/b", b"B")
    a, b = cache_get("https://example.gov/a"), cache_get("https://example.gov/b")
    assert a is not None and b is not None
    assert (a.content, b.content) == (b"A", b"B")
