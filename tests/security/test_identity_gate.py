"""Security tests: the EDGAR identity gate must block live SEC access (SPEC §2.4.1, §7)."""

from __future__ import annotations

import httpx
import pytest

from quarterline.config import Settings
from quarterline.sources.sec.client import IdentityError, SecClient
from quarterline.sources.sec.companyfacts import fetch_companyfacts

URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"


def _gated_client(tmp_path, monkeypatch, identity: str) -> tuple[SecClient, list]:
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    settings = Settings(_env_file=None, edgar_identity=identity)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    client = SecClient(settings, transport=httpx.MockTransport(handler))
    return client, requests


PLACEHOLDER = "Quarterline your-real-contact@email.example"


@pytest.mark.parametrize("identity", [PLACEHOLDER, "", "   "])
def test_invalid_identity_blocks_get_json(tmp_path, monkeypatch, identity: str) -> None:
    client, requests = _gated_client(tmp_path, monkeypatch, identity)
    try:
        with pytest.raises(IdentityError):
            client.get_json(URL)
        with pytest.raises(IdentityError):
            client.download(URL)
    finally:
        client.close()
    assert requests == []  # nothing ever left the machine


def test_placeholder_identity_blocks_companyfacts_helper(tmp_path, monkeypatch) -> None:
    client, requests = _gated_client(tmp_path, monkeypatch, PLACEHOLDER)
    try:
        with pytest.raises(IdentityError):
            fetch_companyfacts(client, "0000320193")
    finally:
        client.close()
    assert requests == []


def test_valid_identity_allowed_through(tmp_path, monkeypatch) -> None:
    client, requests = _gated_client(
        tmp_path, monkeypatch, "Quarterline research contact@research.example.org"
    )
    try:
        assert client.get_json(URL) == {}
    finally:
        client.close()
    assert len(requests) == 1
    assert requests[0].headers["user-agent"].startswith("Quarterline research")
