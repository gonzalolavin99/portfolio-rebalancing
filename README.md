# Portfolio rebalancing

A `Portfolio` holds stocks and a target `Allocation` (e.g. 40% META, 60% AAPL).
`Portfolio.rebalance(prices)` returns the buy and sell orders needed to reach that
allocation, given the latest prices.

## Running the tests

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # on macOS/Linux: .venv/bin/python
.venv/Scripts/python -m pytest
```

## Usage

```python
from decimal import Decimal

from portfolio.models import Allocation, Holding, Portfolio, Stock
from portfolio.prices import StaticPriceSource

portfolio = Portfolio(
    holdings=(
        Holding(Stock("META"), Decimal("10")),
        Holding(Stock("AAPL"), Decimal("5")),
        Holding(Stock("TSLA"), Decimal("0.3")),
    ),
    allocation=Allocation({"META": Decimal("0.4"), "AAPL": Decimal("0.6")}),
    cash=Decimal("100"),
)
prices = StaticPriceSource({"META": Decimal("100"), "AAPL": Decimal("200"), "TSLA": Decimal("250")})

for order in portfolio.rebalance(prices):
    print(order.side.value, order.ticker, order.quantity, order.amount)
```

```
SELL META 1.300000 130.00
SELL TSLA 0.3 75.00
BUY AAPL 1.525000 305.00
```

The total value is $2,175 (stocks plus cash). META is $130 over its 40% target,
TSLA is not in the allocation so it is sold entirely, and the proceeds plus the
cash buy the $305 AAPL is missing.

## Assumptions

- **Weights are fractions** (`0.4` means 40%) and must sum to 1 within a 0.0001
  tolerance. They are a share of total value: holdings plus cash.
- **Fractional shares** are allowed, to 6 decimals.
- **No fees or taxes.**
- **Optional cash** (default 0) is uninvested money, such as a deposit. The
  rebalance invests it; no other money enters or leaves.
- All numbers are `Decimal`. Floats are rejected with an explicit error, because
  they can't represent money exactly.

## How it works

1. Take the union of held and allocated tickers. A holding outside the allocation
   is weight 0; an allocated stock not held is quantity 0. One formula, no special
   cases.
2. Fetch each price once.
3. For each ticker, `delta = weight × total − quantity × price`. Negative is a
   sell, positive a buy.
4. Drop orders below `min_order_amount` (default $1), except full liquidations.
5. Round.
6. If the buys now exceed sells plus cash, trim them, largest first.
7. Return sells first (they fund the buys), then buys, each sorted by ticker.

## Design decisions

- **Prices are separate from the allocation.** The allocation is intent; prices
  are market data from a `PriceSource` passed to `rebalance`. `Stock.current_price`
  receives the source instead of storing it, so one rebalance can't mix two feeds.
- **Price snapshot.** Each price is fetched once per rebalance, so every number in
  the calculation sees the same market even if the feed moves mid-calculation.
- **`rebalance` is pure.** It returns orders and never modifies the portfolio.
  That makes it easy to test and lets callers preview orders before executing them.
- **Rounding works against us.** Quantities round down (never sell more than held
  nor buy more than funded); sell amounts round down and buy amounts round up. If
  the reported amounts balance, the real ones do too. The cash check runs *after*
  rounding, so it verifies final amounts:
  `sum(buys) <= sum(sells) + cash`.
- **Full liquidations skip the threshold.** Otherwise a $0.50 position in a
  removed stock would survive every future rebalance. They also sell the exact
  quantity held, unrounded, so no residue is left.
- **Duplicates are rejected, not merged.** Two holdings of the same ticker likely
  mean an upstream bug; summing them silently would hide it.

## Out of scope

- Fees and taxes.
- Whole shares only (turns the problem into an optimization with leftover cash).
- Per-asset tolerance bands (only rebalance assets that drift beyond X%).

## AI usage

This solution was designed and written with Claude (Claude Code). Conversation:
_[link to the conversation]_
