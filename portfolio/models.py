"""Portfolio rebalancing.

Design notes:
- An Allocation is intent (ticker -> weight); prices are market data supplied by a
  PriceSource at rebalance time. Keeping them apart lets the same allocation be
  evaluated against any feed: live, historical, or fixed in tests.
- Money and quantities are Decimal end to end. Floats can't represent 0.1 exactly,
  and rounding drift in money is a bug, not noise.
- Every ticker in holdings ∪ allocation goes through the same `target - current`
  formula: an unallocated holding is weight 0, an allocated stock not held is
  quantity 0. No special cases for "new" or "removed" stocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from enum import Enum
from types import MappingProxyType
from typing import ClassVar, Mapping, Protocol

Ticker = str

QUANTITY_STEP = Decimal("0.000001")
MONEY_STEP = Decimal("0.01")


# Invalid input fails loudly; nothing is silently "fixed".
class InvalidAllocationError(ValueError): ...


class InvalidPortfolioError(ValueError): ...


class PriceUnavailableError(LookupError): ...


def _require_decimal(name: str, value: object) -> None:
    # Without this, a float fails later with an obscure Decimal + float TypeError,
    # or worse, gets converted and carries its binary error into money math.
    if not isinstance(value, Decimal):
        raise TypeError(
            f"{name} must be Decimal, got {type(value).__name__}: floats can't "
            f"represent money exactly, use Decimal('0.1') instead"
        )


class PriceSource(Protocol):
    def current_price(self, ticker: Ticker) -> Decimal: ...


@dataclass(frozen=True)
class Stock:
    ticker: Ticker

    def current_price(self, prices: PriceSource) -> Decimal:
        # The source is received instead of stored, so a single rebalance can
        # never mix prices coming from two different feeds.
        price = prices.current_price(self.ticker)
        _require_decimal(f"Price of {self.ticker}", price)
        if price <= 0:
            raise PriceUnavailableError(f"Invalid price for {self.ticker}: {price}")
        return price


@dataclass(frozen=True)
class Allocation:
    """Target distribution as fractions of total value (0.40 means 40%).

    Being a mapping, duplicate tickers are impossible by construction.
    """

    weights: Mapping[Ticker, Decimal]
    # Lets callers express thirds (0.3333 * 3) without hitting exactly 1.
    TOLERANCE: ClassVar[Decimal] = Decimal("0.0001")

    def __post_init__(self) -> None:
        # Frozen only blocks reassignment; copy so the caller can't mutate it later.
        object.__setattr__(self, "weights", MappingProxyType(dict(self.weights)))
        if not self.weights:
            raise InvalidAllocationError("Allocation cannot be empty")
        for ticker, weight in self.weights.items():
            _require_decimal(f"Weight of {ticker}", weight)
        if any(w < 0 for w in self.weights.values()):
            raise InvalidAllocationError("Weights cannot be negative")
        total = sum(self.weights.values(), Decimal(0))
        if abs(total - 1) > self.TOLERANCE:
            raise InvalidAllocationError(f"Weights must sum to 1, got {total}")

    def weight_of(self, ticker: Ticker) -> Decimal:
        return self.weights.get(ticker, Decimal(0))


@dataclass(frozen=True)
class Holding:
    stock: Stock
    quantity: Decimal

    def __post_init__(self) -> None:
        _require_decimal(f"Quantity of {self.stock.ticker}", self.quantity)


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Order:
    ticker: Ticker
    side: Side
    quantity: Decimal
    amount: Decimal


@dataclass(frozen=True)
class Portfolio:
    holdings: tuple[Holding, ...]
    allocation: Allocation
    # Uninvested cash (e.g. a new deposit); the rebalance puts it to work.
    cash: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        _require_decimal("Cash", self.cash)
        object.__setattr__(self, "holdings", tuple(self.holdings))
        tickers = [h.stock.ticker for h in self.holdings]
        if len(tickers) != len(set(tickers)):
            # Rejected rather than merged: duplicates usually signal an upstream
            # bug, and summing them silently would hide it.
            raise InvalidPortfolioError("Duplicate holdings")
        if any(h.quantity < 0 for h in self.holdings):
            raise InvalidPortfolioError("Quantities cannot be negative")
        if self.cash < 0:
            raise InvalidPortfolioError("Cash cannot be negative")

    def rebalance(
        self, prices: PriceSource, min_order_amount: Decimal = Decimal(1)
    ) -> list[Order]:
        """Orders needed to reach the allocation, sells first.

        Pure: the portfolio is not modified. Guarantees that the final buy
        amounts never exceed sell amounts plus cash.
        """
        held = {h.stock.ticker: h.quantity for h in self.holdings}
        # Sorted so the output is deterministic.
        universe = sorted(held.keys() | self.allocation.weights.keys())
        # One price call per ticker: every number in this rebalance sees the
        # same prices even if the feed moves mid-calculation.
        snapshot = {t: Stock(t).current_price(prices) for t in universe}
        total = self.cash + sum((held.get(t, 0) * snapshot[t] for t in universe), Decimal(0))

        sells: list[Order] = []
        buys: list[Order] = []
        for ticker in universe:
            price = snapshot[ticker]
            current = held.get(ticker, Decimal(0))
            weight = self.allocation.weight_of(ticker)

            if weight == 0:
                if current > 0:
                    # Full liquidations skip the threshold (a dust position would
                    # otherwise survive every future rebalance) and sell the exact
                    # quantity so no residue is left behind.
                    sells.append(Order(ticker, Side.SELL, current, _money_down(current * price)))
                continue

            delta = weight * total - current * price
            if abs(delta) < min_order_amount:
                continue
            quantity = _quantity_down(abs(delta) / price)
            if quantity == 0:
                continue
            if delta < 0:
                sells.append(Order(ticker, Side.SELL, quantity, _money_down(quantity * price)))
            else:
                buys.append(Order(ticker, Side.BUY, quantity, _money_up(quantity * price)))

        available = self.cash + sum((o.amount for o in sells), Decimal(0))
        return sells + _fit_buys_to_cash(buys, available, snapshot)


def _fit_buys_to_cash(
    buys: list[Order], available: Decimal, prices: Mapping[Ticker, Decimal]
) -> list[Order]:
    """Trim buys, largest first, until they are funded by sells plus cash.

    Runs after rounding so it checks the final amounts. The shortfall comes from
    sells dropped by the threshold and from rounding, so it is small; taking it
    from the largest buys distorts the weights the least.
    """
    # Rounded up to cents so each trimmed budget stays on the cent grid, which
    # makes the trimmed amount (rounded up) never exceed it.
    shortfall = _money_up(sum((o.amount for o in buys), Decimal(0)) - available)
    fitted = []
    for order in sorted(buys, key=lambda o: o.amount, reverse=True):
        if shortfall > 0:
            budget = order.amount - shortfall
            price = prices[order.ticker]
            quantity = _quantity_down(budget / price) if budget > 0 else Decimal(0)
            amount = _money_up(quantity * price)
            shortfall -= order.amount - amount
            order = Order(order.ticker, Side.BUY, quantity, amount)
        if order.quantity > 0:
            fitted.append(order)
    return sorted(fitted, key=lambda o: o.ticker)


# Quantities round down: never sell more than held nor buy more than funded.
def _quantity_down(value: Decimal) -> Decimal:
    return value.quantize(QUANTITY_STEP, rounding=ROUND_DOWN)


# Amounts round against us (sells down, buys up), so if the reported amounts
# balance, the real ones do too.
def _money_down(value: Decimal) -> Decimal:
    return value.quantize(MONEY_STEP, rounding=ROUND_DOWN)


def _money_up(value: Decimal) -> Decimal:
    return value.quantize(MONEY_STEP, rounding=ROUND_UP)
