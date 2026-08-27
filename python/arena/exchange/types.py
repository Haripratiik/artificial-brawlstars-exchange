"""Core exchange value types.

Two decisions here shape everything above them.

**Prices are integers, always.** A price is a count of ticks, never a float and
never a Decimal. Real exchanges work this way for the same reason we do: integer
comparison is exact, so price-time priority is unambiguous, and a book can never
develop two "different" prices that happen to be equal to fifteen decimal places.
Conversion to and from a contract's tick grid happens once, at the boundary, in
:mod:`arena.contracts`. Inside the engine there are only ticks.

**Quantities are integers too**, counted in lots. Same reasoning, and it makes
the conservation invariant -- quantity is neither created nor destroyed by
matching -- checkable exactly rather than approximately.

Both choices also make the eventual C++ port a transcription rather than a
redesign: `int64_t` on both sides, identical arithmetic, identical results. That
matters because the port will be validated by feeding both engines the same order
stream and demanding identical tapes.
"""

from __future__ import annotations

from enum import Enum
from typing import NewType

__all__ = [
    "OrderId",
    "Price",
    "Quantity",
    "SequenceNumber",
    "AgentId",
    "Side",
    "OrderType",
    "TimeInForce",
    "SelfTradePrevention",
    "OrderStatus",
    "RejectReason",
]

# Distinct aliases rather than bare ints, so a price cannot be passed where a
# quantity belongs without a type checker noticing.
OrderId = NewType("OrderId", int)
Price = NewType("Price", int)
Quantity = NewType("Quantity", int)
SequenceNumber = NewType("SequenceNumber", int)
AgentId = NewType("AgentId", str)


class Side(Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY

    def crosses(self, resting: Price, incoming: Price) -> bool:
        """Whether an incoming order at ``incoming`` can trade with ``resting``.

        A buy crosses a resting ask at or below its limit; a sell crosses a
        resting bid at or above it.
        """
        return incoming >= resting if self is Side.BUY else incoming <= resting


class OrderType(Enum):
    LIMIT = "limit"
    # A market order is a limit order with no price bound, and is modelled as
    # exactly that rather than as a separate matching path. It carries no price
    # and is always immediate-or-cancel: resting an unpriced order would leave
    # the book with a level that matches anything.
    MARKET = "market"
    # Dormant until the market trades at or through a trigger, then a market
    # order. The classic risk tool and the classic accelerant: a stop sells
    # into a fall, which pushes the price further down, which triggers more
    # stops. Nothing here prevents that cascade, and it should not -- being able
    # to *measure* one is most of the reason to model stops at all.
    STOP = "stop"
    # The same trigger, becoming a limit order rather than a market one. It
    # protects against the fill an unpriced stop can get in a fast market, at
    # the cost of possibly not filling at all -- which is the trade every stop
    # user actually faces.
    STOP_LIMIT = "stop_limit"


class TimeInForce(Enum):
    # Rests until filled or cancelled. The default.
    GTC = "gtc"
    # Take what is available immediately, cancel the rest. Never rests.
    IOC = "ioc"
    # All or nothing, immediately. Fills completely or does not trade at all.
    FOK = "fok"
    # Rests like GTC, but is rejected outright rather than crossing. Exists
    # because of maker-taker pricing: a maker that accidentally crosses pays the
    # taker fee instead of earning the rebate, which can turn a profitable quote
    # into a losing one. This is the order type that makes that impossible.
    POST_ONLY = "post_only"


class SelfTradePrevention(Enum):
    """What to do when an agent's order would match its own resting order.

    Without this, a market maker quoting both sides crosses with itself every
    time it requotes: its cancels are still in flight while the new quote
    arrives, so the new bid trades against its own stale offer. The position
    nets to zero and the PnL nets to zero, which is exactly why it is dangerous
    -- nothing looks wrong. What it destroys is the tape: volume, prices and
    every impact measurement derived from them are then largely fictional.

    Real venues offer the same policies under the name self-match prevention.

    CANCEL_OLDEST is the default because it is right for the case that actually
    occurs here: the resting order is a stale quote the agent has already tried
    to cancel, so removing it and letting the incoming order continue against
    everyone else is what the agent intended.
    """

    # Remove the resting order and carry on matching against other agents.
    CANCEL_OLDEST = "cancel_oldest"
    # Cancel the incoming order's remainder and stop.
    CANCEL_NEWEST = "cancel_newest"
    # Remove both sides.
    CANCEL_BOTH = "cancel_both"
    # Permit the match. Only for tests that need to observe the raw behaviour.
    ALLOW = "allow"


class OrderStatus(Enum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"

    @property
    def terminal(self) -> bool:
        """Whether the order is finished and can never trade again."""
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)


class RejectReason(Enum):
    UNKNOWN_ORDER = "unknown_order"
    ALREADY_TERMINAL = "already_terminal"
    INVALID_QUANTITY = "invalid_quantity"
    INVALID_PRICE = "invalid_price"
    MARKET_ORDER_MUST_BE_IOC = "market_order_must_be_ioc"
    LIMIT_ORDER_REQUIRES_PRICE = "limit_order_requires_price"
    FOK_NOT_FILLABLE = "fok_not_fillable"
    POST_ONLY_WOULD_CROSS = "post_only_would_cross"
    # Immediate-or-cancel and fill-or-kill are instructions about *now*, and
    # during a call phase there is no now.
    NOT_ACCEPTED_IN_AUCTION = "not_accepted_in_auction"
    NOT_ORDER_OWNER = "not_order_owner"
    # Raised by the venue, not the engine: the account could not cover the
    # worst case of the position the order would create. Checked before the
    # order reaches a book, because an exchange cannot unprint a trade it
    # should not have allowed.
    INSUFFICIENT_COLLATERAL = "insufficient_collateral"
    # Priced beyond where a trade may print. The rule this models prevents
    # executions outside its bands rather than only pausing after one, and a
    # limit order left resting outside them would lock the book against itself:
    # a bid above the band and an ask below it, neither allowed to trade,
    # crossed and stuck until something halted.
    OUTSIDE_PRICE_BAND = "outside_price_band"
    # A stop with no trigger, a trigger on an order that has none, or a trigger
    # already reached: all of them are an instruction that cannot be followed.
    INVALID_STOP_PRICE = "invalid_stop_price"
