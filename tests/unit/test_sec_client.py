"""Unit tests for SecClient pacing, retries, identity gate, and caching.

All HTTP traffic is served by ``httpx.MockTransport`` — no network is touched.
"""

from __future__ import annotations

import time

import httpx
import pytest

from quarterline.config import Settings
from quarterline.sources.sec.client import IdentityError, SecClient, SecRequestError

VALID_IDENTITY = "Quarterline research test-contact@quarterline.local"


def make_settings(monkeypatch, tmp_path, **overrides) -> Settings:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    return Settings(_env_file=None, edgar_identity=VALID_IDENTITY, **overrides)


class Handler:
    """MockTransport handler recording request-start times and serving canned responses."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = list(responses)
        self.starts: list[float] = []
        self.requests: list[httpx.Request] = []

    def as_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.starts.append(time.monotonic())
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected extra request")
        return self.responses.pop(0)


URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
URL2 = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json"


def test_pacing_enforces_min_interval(monkeypatch, tmp_path) -> None:
    settings = make_settings(monkeypatch, tmp_path, sec_min_request_interval_ms=200)
    handler = Handler([httpx.Response(200, json={"ok": 1}), httpx.Response(200, json={"ok": 2})])
    client = SecClient(settings, transport=handler.as_transport())
    client.backoff_base = 0.0
    try:
        client.get_json(URL)
        client.get_json(URL2)  # distinct URLs: both must go over the wire, paced
    finally:
        client.close()

    assert len(handler.starts) == 2
    gap = handler.starts[1] - handler.starts[0]
    assert gap >= 0.2, f"request starts {gap=:.4f}s apart, expected >= 0.2s"


def test_retry_on_429_then_success(monkeypatch, tmp_path) -> None:
    settings = make_settings(monkeypatch, tmp_path, sec_min_request_interval_ms=200)
    handler = Handler(
        [
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(503),
            httpx.Response(200, json={"result": "ok"}),
        ]
    )
    client = SecClient(settings, transport=handler.as_transport())
    client.backoff_base = 0.0
    try:
        assert client.get_json(URL) == {"result": "ok"}
    finally:
        client.close()
    assert len(handler.requests) == 3


def test_retries_exhausted_raises(monkeypatch, tmp_path) -> None:
    settings = make_settings(
        monkeypatch, tmp_path, sec_min_request_interval_ms=200, sec_max_retries=1
    )
    handler = Handler([httpx.Response(429), httpx.Response(429)])
    client = SecClient(settings, transport=handler.as_transport())
    client.backoff_base = 0.0
    try:
        with pytest.raises(SecRequestError) as excinfo:
            client.get_json(URL)
        assert excinfo.value.status_code == 429
    finally:
        client.close()
    assert len(handler.requests) == 2  # max_retries + 1 attempts


def test_placeholder_identity_refuses_before_any_request(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    settings = Settings(_env_file=None)  # default placeholder identity
    handler = Handler([])
    client = SecClient(settings, transport=handler.as_transport())
    try:
        with pytest.raises(IdentityError):
            client.get_json(URL)
        with pytest.raises(IdentityError):
            client.get_text(URL)
    finally:
        client.close()
    assert handler.starts == []  # no request ever left the client


def test_cache_hit_on_second_identical_get(monkeypatch, tmp_path) -> None:
    settings = make_settings(monkeypatch, tmp_path, sec_min_request_interval_ms=200)
    handler = Handler(
        [
            httpx.Response(200, content=b'{"a": 1}', headers={"ETag": '"v1"'}),
            httpx.Response(304),
        ]
    )
    client = SecClient(settings, transport=handler.as_transport())
    client.backoff_base = 0.0
    try:
        first = client.download(URL)
        second = client.download(URL)
    finally:
        client.close()

    assert first.cache_hit is False
    assert first.etag == '"v1"'
    assert second.cache_hit is True
    assert second.content == b'{"a": 1}'
    assert second.etag == '"v1"'
    assert len(handler.requests) == 2  # initial GET + conditional revalidation
    assert handler.requests[1].headers["if-none-match"] == '"v1"'


def test_unreachable_sec_falls_back_to_dated_cache(monkeypatch, tmp_path) -> None:
    settings = make_settings(
        monkeypatch, tmp_path, sec_min_request_interval_ms=200, sec_max_retries=0
    )
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, content=b"cached-body", headers={"ETag": '"v1"'})
        raise httpx.ConnectError("network down", request=request)

    client = SecClient(settings, transport=httpx.MockTransport(handler))
    client.backoff_base = 0.0
    try:
        first = client.download(URL)
        second = client.download(URL)  # live request fails -> dated cache is used
    finally:
        client.close()

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.content == b"cached-body"
    assert len(calls) == 2  # initial GET + the failed live attempt
