"""Controlled SEC EDGAR HTTP access (contract C3, SPEC §2.4).

Guarantees:
- refuses live requests when ``EDGAR_IDENTITY`` is missing or a placeholder;
- thread-safe minimum spacing between request starts (default 200 ms,
  ``time.monotonic`` based, applied to every request including retries);
- retry with exponential backoff on 429/503 and connection errors, up to
  ``SEC_MAX_RETRIES``;
- every response goes through the ingest disk cache, revalidating with
  ``If-None-Match`` / ``If-Modified-Since`` when validators are present;
- if SEC is unreachable but a cached copy exists, the clearly dated cache is
  used (SPEC §25 failure table).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Self

import httpx

from quarterline.config import Settings
from quarterline.ingest.cache import cache_get, cache_put

RETRYABLE_STATUS_CODES = (429, 503)


class IdentityError(RuntimeError):
    """Raised when live SEC access is attempted with an invalid EDGAR identity."""


class SecRequestError(RuntimeError):
    """Raised when a SEC request ultimately fails (after retries)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SecDownloadResult:
    """Result of ``SecClient.download`` (PLAN C3 calls this shape CachedResponse)."""

    content: bytes
    source_url: str
    etag: str | None = None
    last_modified: str | None = None
    cache_hit: bool = False


#: Alias so later waves can refer to the C3 name.
CachedResponse = SecDownloadResult


class SecClient:
    """Rate-limited, cached, identity-gated SEC client."""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self.interval_seconds = settings.sec_min_request_interval_ms / 1000.0
        self.max_retries = settings.sec_max_retries
        # Base for exponential backoff between retries. Tests may set this to 0.
        self.backoff_base = 0.5

        self._pacing_lock = threading.Lock()
        self._last_request_start = float("-inf")

        headers = {
            "User-Agent": settings.edgar_identity,
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        }
        self._client = httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(30.0),
            transport=transport,
            follow_redirects=True,
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    # -- pacing and identity -------------------------------------------------

    def _wait_for_slot(self) -> None:
        """Reserve the next request start at least ``interval`` after the previous one.

        Sleeps in a loop until the target start time is actually reached, so OS
        sleep granularity cannot undershoot the mandated minimum spacing.
        """
        with self._pacing_lock:
            while True:
                now = time.monotonic()
                delay = (self._last_request_start + self.interval_seconds) - now
                if delay <= 0:
                    break
                time.sleep(delay)
            self._last_request_start = time.monotonic()

    def _ensure_identity(self) -> None:
        if not self.settings.edgar_identity_is_valid():
            raise IdentityError(
                "EDGAR_IDENTITY is missing or still a placeholder; refusing live SEC "
                "requests. Set a real contact identity in .env (SPEC §7)."
            )

    # -- core request path ---------------------------------------------------

    def _backoff_seconds(self, attempt: int) -> float:
        return self.backoff_base * (2**attempt)

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After", "")
        if retry_after.strip().isdigit():
            return float(retry_after.strip())
        return self._backoff_seconds(attempt)

    def _request(self, url: str, extra_headers: dict[str, str] | None = None) -> httpx.Response:
        """Perform one paced, retried GET; return a 2xx/304 response or raise."""
        self._ensure_identity()
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            self._wait_for_slot()
            try:
                response = self._client.get(url, headers=extra_headers)
            except httpx.TransportError as exc:
                if attempt == attempts - 1:
                    raise SecRequestError(f"connection error for {url}: {exc}") from exc
                time.sleep(self._backoff_seconds(attempt))
                continue
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < attempts - 1:
                time.sleep(self._retry_delay(response, attempt))
                continue
            if response.status_code >= 400:
                raise SecRequestError(
                    f"SEC request failed: HTTP {response.status_code} for {url}",
                    status_code=response.status_code,
                )
            return response
        raise SecRequestError(f"SEC request exhausted retries for {url}")

    # -- public API ----------------------------------------------------------

    def download(self, url: str) -> SecDownloadResult:
        """Download ``url`` through the disk cache with validator revalidation."""
        entry = cache_get(url)

        revalidate_headers: dict[str, str] = {}
        if entry is not None:
            if entry.etag:
                revalidate_headers["If-None-Match"] = entry.etag
            elif entry.last_modified:
                revalidate_headers["If-Modified-Since"] = entry.last_modified
            else:
                # Cached copy has no validators: serve it without spending a request.
                return SecDownloadResult(
                    content=entry.content,
                    source_url=url,
                    etag=None,
                    last_modified=None,
                    cache_hit=True,
                )

        try:
            response = self._request(url, extra_headers=revalidate_headers or None)
        except (SecRequestError, httpx.TransportError):
            if entry is not None:
                # SEC temporarily unavailable: use the clearly dated cache (SPEC §25).
                return SecDownloadResult(
                    content=entry.content,
                    source_url=url,
                    etag=entry.etag,
                    last_modified=entry.last_modified,
                    cache_hit=True,
                )
            raise

        if response.status_code == 304 and entry is not None:
            return SecDownloadResult(
                content=entry.content,
                source_url=url,
                etag=entry.etag,
                last_modified=entry.last_modified,
                cache_hit=True,
            )

        content = response.content
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        content_type = response.headers.get("Content-Type")
        cache_put(url, content, etag=etag, last_modified=last_modified, content_type=content_type)
        return SecDownloadResult(
            content=content,
            source_url=url,
            etag=etag,
            last_modified=last_modified,
            cache_hit=False,
        )

    def get_json(self, url: str) -> dict:
        """GET and JSON-decode a SEC response."""
        return json.loads(self.download(url).content)

    def get_text(self, url: str) -> str:
        """GET and decode a SEC response body as text."""
        content = self.download(url).content
        for encoding in ("utf-8-sig", "cp1252"):
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        return content.decode("utf-8", errors="replace")
