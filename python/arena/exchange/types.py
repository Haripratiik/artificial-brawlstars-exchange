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


class TimeInForce(Enum):
    # Rests until filled or cancelled. The default.
    GTC = "gtc"
    # Take what is available immediately, cancel the rest. Never rests.
    IOC = "ioc"
    # All or nothing, immediately. Fills completely or does not trade at all.
    FOK = "fok"


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
    NOT_ORDER_OWNER = "not_order_owner"
    # Raised by the venue, not the engine: the account could not cover the
    # worst case of the position the order would create. Checked before the
    # order reaches a book, because an exchange cannot unprint a trade it
    # should not have allowed.
    INSUFFICIENT_COLLATERAL = "insufficient_collateral"
