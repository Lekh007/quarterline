"""yfinance price provider (SPEC §20 ``get_prices``, §25 price degradation).

Informational market context ONLY: price rows never feed scores, labels, or
facts, and a price failure NEVER blocks a facts-only memo (SPEC §20/§25 —
"Price provider unavailable -> omit price context").

The heavy ``yfinance`` dependency is imported LAZILY inside
:meth:`YfinancePriceProvider.get_prices` so the package imports (and the whole
test suite) work without touching the network. Tests inject a fake provider
object exposing the same :meth:`get_prices` signature — no test ever constructs
a live yfinance session.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

#: Number of calendar days of history returned when no explicit window given.
DEFAULT_LOOKBACK_DAYS = 30


class PriceProviderUnavailable(RuntimeError):
    """The price provider could not be reached or returned no data (SPEC §25:

    omit price context; never fabricate rows)."""


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class YfinancePriceProvider:
    """Yahoo Finance daily OHLCV provider (informational context only)."""

    source = "yfinance"

    def get_prices(self, ticker: str, start: date, end: date) -> list[dict[str, object]]:
        """Return daily OHLCV rows for ``ticker`` in ``[start, end]``.

        Rows are plain dicts with ISO date strings and floats/None so they are
        JSON-serializable for the agent tool-result log. Raises
        :class:`PriceProviderUnavailable` on any failure (callers translate
        that into a graceful "price unavailable" tool result — it must never
        raise past the tool boundary with fabricated data).
        """
        ticker = (ticker or "").strip()
        if not ticker:
            raise PriceProviderUnavailable("empty ticker; no price context")
        try:  # lazy import: keeps the dependency off every non-price code path
            import yfinance as yf
        except ImportError as exc:  # pragma: no cover - yfinance is a hard dep
            raise PriceProviderUnavailable(f"yfinance is not importable: {exc}") from exc

        try:
            frame = yf.Ticker(ticker.upper()).history(
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                auto_adjust=False,
            )
        except Exception as exc:
            raise PriceProviderUnavailable(f"yfinance request failed: {exc}") from exc

        if frame is None or getattr(frame, "empty", True):
            raise PriceProviderUnavailable(
                f"yfinance returned no rows for {ticker!r} in "
                f"[{start.isoformat()}, {end.isoformat()}]"
            )

        rows: list[dict[str, object]] = []
        for index, row in frame.iterrows():  # type: ignore[union-attr]
            trade_date = index.date() if hasattr(index, "date") else None
            volume = row.get("Volume")
            rows.append(
                {
                    "ticker": ticker.upper(),
                    "trade_date": trade_date.isoformat() if trade_date else None,
                    "open": _to_float(row.get("Open")),
                    "high": _to_float(row.get("High")),
                    "low": _to_float(row.get("Low")),
                    "close": _to_float(row.get("Close")),
                    "volume": int(volume) if volume is not None else None,
                    "currency": row.get("Currency"),
                    "source": self.source,
                    "fetched_at": datetime.now(UTC).isoformat(),
                }
            )
        return rows


def default_window(period_end: date | None = None) -> tuple[date, date]:
    """``[end - lookback, end]`` for the price window (end = period_end or today)."""
    end = period_end or datetime.now(UTC).date()
    return end - timedelta(days=DEFAULT_LOOKBACK_DAYS), end


__all__ = [
    "DEFAULT_LOOKBACK_DAYS",
    "PriceProviderUnavailable",
    "YfinancePriceProvider",
    "default_window",
]
