"""Unit tests for the yfinance price provider (SPEC §20 get_prices, §25).

The provider itself is exercised WITHOUT network: its lazy ``yfinance`` import
and request path are stubbed via a fake module injected into ``sys.modules``,
so the real code path (normalization, empty-frame handling, error wrapping) is
what runs — never a live download. The agent tests additionally inject a fake
provider object; nothing here or there touches the network.
"""

from __future__ import annotations

import sys
import types
from datetime import date

import pytest

from quarterline.sources.prices.yfinance_provider import (
    PriceProviderUnavailable,
    YfinancePriceProvider,
    default_window,
)


class _FakeFrame:
    def __init__(self, rows) -> None:
        self._rows = rows
        self.empty = not rows

    def iterrows(self):
        yield from self._rows


class _FakeTicker:
    def __init__(self, rows, *, raise_error: bool = False) -> None:
        self._rows = rows
        self._raise = raise_error
        self.calls: list[dict] = []

    def history(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise:
            raise ConnectionError("simulated yfinance outage")
        return _FakeFrame(self._rows)


def _install_fake_yfinance(monkeypatch, rows, *, raise_error: bool = False) -> _FakeTicker:
    ticker = _FakeTicker(rows, raise_error=raise_error)
    module = types.ModuleType("yfinance")
    module.Ticker = lambda symbol: ticker
    monkeypatch.setitem(sys.modules, "yfinance", module)
    return ticker


def test_get_prices_normalizes_rows(monkeypatch) -> None:
    import pandas as pd

    frame_rows = [
        (
            pd.Timestamp("2026-09-01"),
            {"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 1500},
        )
    ]
    ticker = _install_fake_yfinance(monkeypatch, frame_rows)

    rows = YfinancePriceProvider().get_prices("aapl", date(2026, 9, 1), date(2026, 9, 5))

    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "AAPL"  # normalized to upper
    assert row["trade_date"] == "2026-09-01"
    assert row["close"] == 100.5
    assert row["volume"] == 1500
    assert row["source"] == "yfinance"
    assert ticker.calls[0]["start"] == "2026-09-01"
    assert ticker.calls[0]["end"] == "2026-09-06"  # inclusive end via +1 day


def test_empty_frame_raises_unavailable(monkeypatch) -> None:
    _install_fake_yfinance(monkeypatch, [])

    with pytest.raises(PriceProviderUnavailable) as excinfo:
        YfinancePriceProvider().get_prices("NOLISTING", date(2026, 9, 1), date(2026, 9, 5))

    assert "no rows" in str(excinfo.value)


def test_provider_error_wrapped_as_unavailable(monkeypatch) -> None:
    _install_fake_yfinance(monkeypatch, [], raise_error=True)

    with pytest.raises(PriceProviderUnavailable) as excinfo:
        YfinancePriceProvider().get_prices("AAPL", date(2026, 9, 1), date(2026, 9, 5))

    assert "yfinance request failed" in str(excinfo.value)


def test_empty_ticker_rejected_without_network(monkeypatch) -> None:
    ticker = _install_fake_yfinance(monkeypatch, [])

    with pytest.raises(PriceProviderUnavailable):
        YfinancePriceProvider().get_prices("  ", date(2026, 9, 1), date(2026, 9, 5))

    assert ticker.calls == [], "no request attempted for an empty ticker"


def test_default_window_uses_period_end_or_today() -> None:
    start, end = default_window(date(2026, 6, 27))
    assert end == date(2026, 6, 27)
    assert (end - start).days == 30
