"""
The bridge between the two halves of the economy: what a real registered
business EARNS, and what its stock is worth.

Registered businesses (`/eco-admin company create`) and stock market listings
(`/eco-admin business create`) are still separate records. This module links
them — explicitly via `/eco-admin business link`, or implicitly when the two
share a name — and turns revenue into price movement in two places:

* `apply_revenue_payment()` — the instant reaction when a business is paid.
* `tick_move()` — the real pricing, run once per market tick in cogs/stocks.py.

The valuation model, in one paragraph: every tick a business is expected to earn
config.STOCK_REVENUE_EXPECTED_YIELD of its own MARKET CAP. Hit that and the
price holds; beat it and the price rises; miss it and the price falls. Because
expectation is a share of the market cap, a stock that has run up needs more
revenue just to stand still — so a price can only stay high while the business
keeps earning, and a business that stops trading always bleeds back down. That
self-correction is what stops the link from being a one-way money printer.

Owner deposits are NOT revenue (see cogs/companies.py), so paying your own money
into your business can never move its stock price.

This module also owns two pieces of market plumbing that every price writer
shares:

* `business_lock()` — one asyncio.Lock per listing. ANY code that reads a
  listing's price / available shares / holdings and then writes them (buy,
  sell, the market tick, revenue events, admin adjustments) holds this lock
  for the whole read-compute-write, so two of them can never interleave.
* `check_circuit_breaker()` — halts trading on a listing for the rest of the
  tick window once its price has travelled too far from where the window
  opened, whatever combination of trades and revenue events got it there.
"""

import asyncio
import time
from contextlib import nullcontext
from typing import Dict, Optional, Tuple

import config

# One lock per business id, created on first use. Module-level on purpose:
# cogs/stocks.py, cogs/companies.py and cogs/admin.py all move prices, and a
# lock only protects anything if every one of them takes the SAME lock object.
_BUSINESS_LOCKS: Dict[int, asyncio.Lock] = {}


def business_lock(business_id: int) -> asyncio.Lock:
    """The lock guarding one listing's price, float and holdings."""
    return _BUSINESS_LOCKS.setdefault(int(business_id), asyncio.Lock())


def market_cap(price: float, total_shares: int) -> float:
    return float(price) * int(total_shares)


def expected_revenue(cap: float) -> float:
    """What a business of this size is expected to earn in one tick."""
    return max(0.0, cap * float(config.STOCK_REVENUE_EXPECTED_YIELD))


def tick_move(revenue_delta: int, cap: float) -> float:
    """The fractional price change justified by one period's revenue.

    0.05 means +5%. Returns 0.0 when the model has nothing to say (a business
    with no market cap, or the feature turned off).
    """
    if not config.STOCK_REVENUE_LINK_ENABLED or cap <= 0:
        return 0.0

    expected = expected_revenue(cap)
    if expected <= 0:
        # No expectation to beat: any revenue at all is good news, but keep it
        # bounded by the same cap as everything else.
        return config.STOCK_REVENUE_MAX_GAIN if revenue_delta > 0 else 0.0

    performance = (revenue_delta - expected) / expected
    move = float(config.STOCK_REVENUE_IMPACT) * performance
    return max(-float(config.STOCK_REVENUE_MAX_DROP), min(float(config.STOCK_REVENUE_MAX_GAIN), move))


def instant_move(amount: int, cap: float, budget_used: float = 0.0) -> float:
    """The small immediate nudge a single payment gives a stock.

    Capped twice: per payment (STOCK_REVENUE_INSTANT_MAX) and against what is
    left of the listing's per-window budget (STOCK_REVENUE_INSTANT_PERIOD_MAX
    minus `budget_used`, the fractional move already spent this window). The
    second cap is what stops ten quick payments from stacking into a 20% move.
    """
    if not (config.STOCK_REVENUE_LINK_ENABLED and config.STOCK_REVENUE_INSTANT_ENABLED):
        return 0.0
    if cap <= 0 or not amount:
        return 0.0

    move = float(config.STOCK_REVENUE_INSTANT_FACTOR) * (amount / cap)
    limit = float(config.STOCK_REVENUE_INSTANT_MAX)
    move = max(-limit, min(limit, move))

    period_max = float(config.STOCK_REVENUE_INSTANT_PERIOD_MAX)
    if period_max > 0:
        remaining = max(0.0, period_max - abs(float(budget_used)))
        move = max(-remaining, min(remaining, move))
    return move


def describe(name: str, old_price: float, new_price: float, move: float) -> str:
    """One line suitable for appending to a command response."""
    arrow = "📈" if move >= 0 else "📉"
    return (
        f"{arrow} **{name}** stock {move * 100:+.2f}% "
        f"(${old_price:,.2f} → ${new_price:,.2f}) on the revenue."
    )


def window_move(price: float, window_open_price: float) -> float:
    """How far (fractionally) the price sits from where the tick window opened."""
    if not window_open_price or window_open_price <= 0:
        return 0.0
    return (float(price) - float(window_open_price)) / float(window_open_price)


async def check_circuit_breaker(
    db, guild_id: int, business_id: int, new_price: float, window_open_price: float,
    already_halted: bool, cause: str, actor_id=None, tx=None,
) -> bool:
    """Halts the listing if `new_price` has moved further from the window's
    opening price than config.STOCK_CIRCUIT_BREAKER_PCT allows. Call this
    after every price write made under the business lock. Returns True if the
    breaker is (now) tripped. Logs a stock event the first time it trips."""
    threshold = float(config.STOCK_CIRCUIT_BREAKER_PCT or 0)
    if threshold <= 0:
        return False
    if already_halted:
        return True
    move = window_move(new_price, window_open_price)
    if abs(move) <= threshold:
        return False
    await db.set_business_halted(business_id, True, tx=tx)
    await db.log_stock_event(
        guild_id, business_id, move * 100,
        f"⛔ Circuit breaker: {move * 100:+.1f}% this period ({cause}) — trading halted until the next tick",
        actor_id, tx=tx,
    )
    return True


class InsufficientCash(Exception):
    """apply_revenue_payment(charge_payer=True): the payer can't afford it.
    Nothing was changed."""


async def apply_revenue_payment(
    db,
    guild_id: int,
    company_id: int,
    amount: int,
    reason: str,
    actor_id: Optional[int] = None,
    credit_revenue: bool = False,
    charge_payer: bool = False,
) -> Optional[Tuple[str, float, float, float]]:
    """Moves the stock of the listing tied to `company_id`, if there is one.

    With `credit_revenue=True` it also books the payment into the company
    (add_company_revenue), and with `charge_payer=True` it first takes the
    cash from `actor_id` (raising InsufficientCash if they can't cover it) —
    all inside the SAME transaction, so the charge, the revenue credit and the
    price move can never land without each other.

    Returns (listing name, old price, new price, fractional move) so the
    caller can tell the player what their payment did to the market, or None
    when nothing moved (no listing, feature disabled, budget for this window
    already spent, or a move too small to be worth reporting).

    Everything happens under the listing's business_lock() and in one database
    transaction, with the price and the window budget re-read INSIDE the lock:
    a payment that lands while a /stock buy is settling waits for it, then
    prices off the price that buy produced, not the one it started from.

    The tick in cogs/stocks.py prices the same revenue again from the period
    baseline, which is intentional: this is the market reacting to the news, the
    tick is the market settling on what the news was actually worth.
    """
    instant_enabled = config.STOCK_REVENUE_LINK_ENABLED and config.STOCK_REVENUE_INSTANT_ENABLED

    # Peek (unlocked) to find which listing, if any, we need to lock. Fine to
    # do outside the lock: it only decides WHICH lock to take, and the state
    # itself is re-read inside.
    listing = await db.get_listing_for_company(company_id, guild_id) if config.STOCK_REVENUE_LINK_ENABLED else None
    business_id = listing[0] if listing else None
    lock = business_lock(business_id) if business_id is not None else nullcontext()

    async with lock:
        async with db.transaction() as tx:
            if charge_payer:
                if actor_id is None or not await db.try_spend_cash(actor_id, guild_id, amount, tx=tx):
                    raise InsufficientCash()
            if credit_revenue:
                await db.add_company_revenue(company_id, guild_id, amount, reason, actor_id, tx=tx)
            if business_id is None or not instant_enabled:
                return None

            state = await db.get_business_trade_state(business_id, tx=tx)
            if not state or state[7]:  # gone, or delisted meanwhile
                return None
            (_id, name, _owner, price, _prev, total_shares, _avail, _delisted,
             version, window_open, budget_used, halted, _last_event) = state

            move = instant_move(amount, market_cap(price, total_shares), budget_used)
            if abs(move) < 0.0001:
                return None

            # Charge the move against this window's budget (and stamp the
            # listing for the insider lock-out). With the budget disabled the
            # stamp still happens.
            now = int(time.time())
            period_max = float(config.STOCK_REVENUE_INSTANT_PERIOD_MAX or 0)
            if not await db.spend_window_revenue_move(
                business_id, move, period_max if period_max > 0 else None, now, tx=tx
            ):
                return None  # budget for this window is spent

            new_price = await db.set_business_price(
                business_id, price * (1 + move), config.STOCK_MIN_PRICE,
                expected_version=version, tx=tx,
            )
            if new_price is None:
                # Cannot happen while we hold the lock and the transaction, but
                # if it ever does, refuse rather than overwrite someone's price.
                raise RuntimeError(f"price_version conflict on business {business_id}")

            if abs(move) >= float(config.STOCK_REVENUE_EVENT_MIN_CHANGE):
                await db.log_stock_event(guild_id, business_id, move * 100, f"Revenue: {reason}", actor_id, tx=tx)

            await check_circuit_breaker(
                db, guild_id, business_id, new_price, window_open, bool(halted),
                "revenue", actor_id, tx=tx,
            )
    return name, float(price), float(new_price), move
