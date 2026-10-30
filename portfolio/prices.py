from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from portfolio.models import PriceUnavailableError, Ticker


@dataclass(frozen=True)
class StaticPriceSource:
    """In-memory price feed for tests and examples."""

    prices: Mapping[Ticker, Decimal]

    def current_price(self, ticker: Ticker) -> Decimal:
        try:
            return self.prices[ticker]
        except KeyError:
            # Translated so callers handle one error type regardless of the feed.
            raise PriceUnavailableError(f"No price for {ticker}") from None
