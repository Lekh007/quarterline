"""SEC XBRL company facts access (contract C3 helpers)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from quarterline.sources.sec.client import SecClient

COMPANYFACTS_URL_TEMPLATE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"


def normalize_cik(cik: int | str) -> int:
    """Accept ``320193``, ``"0000320193"`` etc. and return the integer CIK."""
    text = str(cik).strip()
    stripped = text.lstrip("0")
    if not stripped.isdigit():
        raise ValueError(f"invalid CIK: {cik!r}")
    return int(stripped)


def cik10(cik: int | str) -> str:
    """Zero-padded ten-digit CIK string."""
    return f"{normalize_cik(cik):010d}"


def companyfacts_url(cik: int | str) -> str:
    return COMPANYFACTS_URL_TEMPLATE.format(cik=normalize_cik(cik))


def fetch_companyfacts(client: SecClient, cik: int | str) -> dict:
    """Fetch (and cache) the official companyfacts payload for ``cik``."""
    return client.get_json(companyfacts_url(cik))


def iter_unit_facts(
    companyfacts: dict[str, Any], taxonomy: str, tag: str
) -> Iterator[dict[str, Any]]:
    """Yield raw unit observations for one (taxonomy, tag) pair.

    Each yielded dict is the raw companyfacts entry (start, end, val, accn, fy,
    fp, form, filed, frame?) enriched with ``unit``, ``taxonomy`` and ``tag``.
    """
    units = (companyfacts.get("facts") or {}).get(taxonomy, {}).get(tag, {}).get("units") or {}
    for unit_name, facts in units.items():
        for fact in facts:
            enriched = dict(fact)
            enriched["unit"] = unit_name
            enriched["taxonomy"] = taxonomy
            enriched["tag"] = tag
            yield enriched
