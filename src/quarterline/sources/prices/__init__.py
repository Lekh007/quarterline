"""Price providers (SPEC §9.9 ``prices`` table, PRICE_PROVIDER setting).

Stub wave: the yfinance provider lands as a stub only; wave F7 (agent
workflow / price context) activates it. No price ingestion happens before
then.
"""

from quarterline.sources.prices.yfinance_provider import YfinancePriceProvider

__all__ = ["YfinancePriceProvider"]
