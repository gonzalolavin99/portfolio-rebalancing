from collections import Counter
from decimal import Decimal as D

import pytest

from portfolio.models import (
    Allocation,
    Holding,
    InvalidAllocationError,
    InvalidPortfolioError,
    Order,
    Portfolio,
    PriceUnavailableError,
    Side,
    Stock,
)
from portfolio.prices import StaticPriceSource


def holding(ticker, quantity):
    return Holding(Stock(ticker), D(quantity))


def allocation(**weights):
    return Allocation({t: D(w) for t, w in weights.items()})


def prices(**values):
    return StaticPriceSource({t: D(v) for t, v in values.items()})


def assert_funded(portfolio, orders):
    spent = sum(o.amount for o in orders if o.side is Side.BUY)
    received = sum(o.amount for o in orders if o.side is Side.SELL)
    assert spent <= received + portfolio.cash


def test_sells_overweight_and_buys_underweight_with_sells_first():
    portfolio = Portfolio(
        (holding("META", "10"), holding("AAPL", "5")), allocation(META="0.4", AAPL="0.6")
    )

    orders = portfolio.rebalance(prices(META="100", AAPL="200"))

    assert orders == [
        Order("META", Side.SELL, D("2"), D("200")),
        Order("AAPL", Side.BUY, D("1"), D("200")),
    ]


def test_balanced_portfolio_needs_no_orders():
    portfolio = Portfolio(
        (holding("META", "4"), holding("AAPL", "3")), allocation(META="0.4", AAPL="0.6")
    )

    assert portfolio.rebalance(prices(META="100", AAPL="200")) == []


def test_empty_portfolio_needs_no_orders():
    portfolio = Portfolio((), allocation(META="1"))

    assert portfolio.rebalance(prices(META="100")) == []


def test_cash_is_invested_buying_allocated_stocks_from_zero():
    portfolio = Portfolio((), allocation(META="0.4", AAPL="0.6"), cash=D("1000"))

    orders = portfolio.rebalance(prices(META="100", AAPL="200"))

    assert orders == [
        Order("AAPL", Side.BUY, D("3"), D("600")),
        Order("META", Side.BUY, D("4"), D("400")),
    ]


def test_unallocated_stock_is_sold_entirely_with_exact_quantity():
    portfolio = Portfolio(
        (holding("TSLA", "1.23456789"), holding("META", "1")), allocation(META="1")
    )

    orders = portfolio.rebalance(prices(TSLA="10", META="100"))

    # The rounded-up buy (12.35) exceeded the rounded-down sell (12.34): trimmed.
    assert orders == [
        Order("TSLA", Side.SELL, D("1.23456789"), D("12.34")),
        Order("META", Side.BUY, D("0.1234"), D("12.34")),
    ]
    assert_funded(portfolio, orders)


def test_dust_liquidation_ignores_threshold():
    portfolio = Portfolio((holding("TSLA", "0.05"), holding("META", "1")), allocation(META="1"))

    orders = portfolio.rebalance(prices(TSLA="10", META="100"))

    assert orders == [Order("TSLA", Side.SELL, D("0.05"), D("0.50"))]


def test_dropped_sells_never_leave_buys_unfunded():
    # A and B are $0.90 over target (below threshold, dropped); C needs $1.80
    # that would only come from those sells.
    portfolio = Portfolio(
        (holding("A", "1"), holding("B", "1"), holding("C", "1")),
        allocation(A="0.25", B="0.25", C="0.5"),
    )

    orders = portfolio.rebalance(prices(A="100.9", B="100.9", C="198.2"))

    assert orders == []


def test_shortfall_larger_than_one_buy_trims_several():
    # All prices are 1 so quantities equal amounts. A, B and C are $0.95 over
    # target (dropped by the threshold), D funds $2.00, but X and Y need $4.85:
    # the $2.85 shortfall eliminates X ($2.50) and trims the remaining $0.35 off Y.
    portfolio = Portfolio(
        (
            holding("A", "100.95"),
            holding("B", "100.95"),
            holding("C", "100.95"),
            holding("D", "102"),
            holding("X", "297.5"),
            holding("Y", "297.65"),
        ),
        allocation(A="0.1", B="0.1", C="0.1", D="0.1", X="0.3", Y="0.3"),
    )

    orders = portfolio.rebalance(prices(A="1", B="1", C="1", D="1", X="1", Y="1"))

    assert orders == [
        Order("D", Side.SELL, D("2"), D("2.00")),
        Order("Y", Side.BUY, D("2"), D("2.00")),
    ]
    assert_funded(portfolio, orders)


def test_rounding_keeps_buys_funded():
    portfolio = Portfolio((), allocation(A="0.5", B="0.5"), cash=D("100"))

    orders = portfolio.rebalance(prices(A="3", B="7"))

    assert [o.ticker for o in orders] == ["A", "B"]
    assert_funded(portfolio, orders)


def test_prices_are_fetched_once_per_ticker():
    class CountingPriceSource:
        def __init__(self, values):
            self.values = values
            self.calls = Counter()

        def current_price(self, ticker):
            self.calls[ticker] += 1
            return self.values[ticker]

    source = CountingPriceSource({"META": D("100"), "AAPL": D("200"), "TSLA": D("50")})
    portfolio = Portfolio(
        (holding("META", "10"), holding("TSLA", "2")), allocation(META="0.4", AAPL="0.6")
    )

    portfolio.rebalance(source)

    assert source.calls == Counter(META=1, AAPL=1, TSLA=1)


@pytest.mark.parametrize(
    "weights",
    [{}, {"META": D("0.9")}, {"META": D("1.2"), "AAPL": D("-0.2")}],
    ids=["empty", "sum-not-one", "negative"],
)
def test_invalid_allocation_is_rejected(weights):
    with pytest.raises(InvalidAllocationError):
        Allocation(weights)


@pytest.mark.parametrize(
    "holdings, cash",
    [
        ((holding("META", "1"), holding("META", "2")), D(0)),
        ((holding("META", "-1"),), D(0)),
        ((), D("-1")),
    ],
    ids=["duplicate", "negative-quantity", "negative-cash"],
)
def test_invalid_portfolio_is_rejected(holdings, cash):
    with pytest.raises(InvalidPortfolioError):
        Portfolio(holdings, allocation(META="1"), cash=cash)


@pytest.mark.parametrize(
    "build",
    [
        lambda: Allocation({"META": 0.4, "AAPL": 0.6}),
        lambda: Holding(Stock("META"), 1.5),
        lambda: Portfolio((), allocation(META="1"), cash=100.0),
    ],
    ids=["weight", "quantity", "cash"],
)
def test_floats_are_rejected(build):
    with pytest.raises(TypeError, match="floats can't represent money exactly"):
        build()


@pytest.mark.parametrize(
    "source, error",
    [
        (prices(), PriceUnavailableError),
        (prices(META="0"), PriceUnavailableError),
        (StaticPriceSource({"META": 100.0}), TypeError),
    ],
    ids=["missing", "zero", "float"],
)
def test_invalid_price_is_rejected(source, error):
    portfolio = Portfolio((holding("META", "1"),), allocation(META="1"))

    with pytest.raises(error):
        portfolio.rebalance(source)
