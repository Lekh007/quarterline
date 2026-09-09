"""yfinance price provider — STUB ONLY (activated by wave F7).

Wave F2 (documents) owns this file solely to pin the module layout; the
implementation is deliberately missing. Do not call it before wave F7 wires
price ingestion/cache (SPEC §25 price cache, agent ``get_prices`` tool).
"""

from __future__ import annotations

from datetime import date


class YfinancePriceProvider:
    """Stub price provider; wave F7 (agent workflow) activates it."""

    source = "yfinance"

    def get_prices(self, ticker: str, start: date, end: date) -> list[dict[str, object]]:
        """Return daily OHLCV rows for ``ticker`` in [start, end].

        Raises NotImplementedError in this wave: price ingestion, its cache,
        and the ``prices`` table population are activated by wave F7.
        """
        raise NotImplementedError(
            "YfinancePriceProvider is a stub; wave F7 (agent workflow) activates it."
        )
