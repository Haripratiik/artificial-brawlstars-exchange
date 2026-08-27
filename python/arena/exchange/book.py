"""The limit order book: price-time priority, one instrument.

The data structure is chosen for a reference implementation rather than for
throughput. Correctness has to be obvious by reading, because this engine's job
is to be the oracle the C++ port is validated against -- if the reference is
subtly wrong, the port will be validated into being identically wrong.

    price -> PriceLevel(FIFO deque of orders, running total)
    plus a heap of prices per side, for O(log n) best-price lookup

Levels are removed lazily: emptying a level leaves its price in the heap, and
the price is discarded when it surfaces and turns out to be stale. Eager removal
would need a heap supporting arbitrary deletion, which is more machinery for no
behavioural difference.

**Priority is (price, arrival), and arrival is a monotonically increasing
sequence, never a timestamp.** Two orders arriving in the same microsecond must
still have a defined order, and a wall clock cannot promise that. The sequence
can, and it is also what makes a seeded replay reproduce exactly.
"""

from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass, field

from arena.exchange.session import SENTINEL as _SENTINEL
from arena.exchange.types import (
    AgentId,
    OrderId,
    OrderStatus,
    Price,
    Quantity,
    Side,
)

__all__ = ["Order", "PriceLevel", "OrderBook", "BookSnapshot"]


@dataclass(slots=True)
class Order:
    """A resting or in-flight order. Mutable: quantity falls as it fills."""

    order_id: OrderId
    agent_id: AgentId
    side: Side
    price: Price
    quantity: Quantity
    remaining: Quantity
    # Arrival rank, assigned by the engine. Defines time priority within a level.
    priority: int
    status: OrderStatus = OrderStatus.NEW

    @property
    def filled(self) -> Quantity:
        return Quantity(self.quantity - self.remaining)

    @property
    def is_resting(self) -> bool:
        return not self.status.terminal and self.remaining > 0


@dataclass(slots=True)
class PriceLevel:
    """All orders at one price, in arrival order.

    ``total`` is maintained incrementally rather than summed on demand. Depth is
    queried far more often than it changes -- every market-data update reads it --
    and recomputing it would make snapshot cost proportional to queue length.
    """

    price: Price
    orders: deque[Order] = field(default_factory=deque)
    total: Quantity = Quantity(0)

    def append(self, order: Order) -> None:
        self.orders.append(order)
        self.total = Quantity(self.total + order.remaining)

    def reduce(self, amount: Quantity) -> None:
        self.total = Quantity(self.total - amount)

    def popleft(self) -> Order:
        return self.orders.popleft()

    def peek(self) -> Order:
        return self.orders[0]

    @property
    def empty(self) -> bool:
        return not self.orders

    def prune(self) -> None:
        """Drop terminal or exhausted orders from the front of the queue.

        Cancelled orders are not removed from their level when cancelled -- that
        would be O(n) in the queue. They are tombstoned and skipped here, on the
        way past, which keeps cancellation O(1). Cancel-heavy flow is the norm in
        electronic markets, so this is the operation worth optimising.
        """
        while self.orders and (
            self.orders[0].status.terminal or self.orders[0].remaining <= 0
        ):
            self.orders.popleft()


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """An L2 view: aggregated depth per price, best prices, and the spread."""

    bids: tuple[tuple[Price, Quantity], ...]
    asks: tuple[tuple[Price, Quantity], ...]

    @property
    def best_bid(self) -> Price | None:
        """The best *priced* bid, which is not always the first level.

        Market orders rest at a sentinel price during a call phase so they
        cross every candidate the auction considers. That makes them the top of
        the book by a margin of 2^61, and it makes them not prices: an order
        that names no price cannot be the best price. Reporting one as the
        touch marked books at zero, produced spreads of 2^62, and fed the
        market maker a mid that no instrument could quote around.

        The levels themselves keep them, because the auction has to count that
        interest to know what would trade.
        """
        return _first_priced(self.bids)

    @property
    def best_ask(self) -> Price | None:
        return _first_priced(self.asks)

    @property
    def priced_bids(self) -> tuple[tuple[Price, Quantity], ...]:
        """Bids that name a price, for anything a person will look at.

        The raw levels keep market-on-open interest at its sentinel price
        because the auction has to count it. A ladder on a screen must not
        show it: the API published a bid of 4,611,686,018,427,387,904 and the
        page dutifully rendered it.
        """
        return tuple((p, q) for p, q in self.bids if abs(int(p)) < _SENTINEL)

    @property
    def priced_asks(self) -> tuple[tuple[Price, Quantity], ...]:
        return tuple((p, q) for p, q in self.asks if abs(int(p)) < _SENTINEL)

    @property
    def spread(self) -> int | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return int(self.best_ask) - int(self.best_bid)

    @property
    def mid(self) -> float | None:
        """Midpoint in ticks. Fractional, because a one-tick spread has none."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return (int(self.best_bid) + int(self.best_ask)) / 2.0


def _first_priced(levels: tuple[tuple[Price, Quantity], ...]) -> Price | None:
    """The first level that names a price rather than "any"."""
    for price, _quantity in levels:
        if abs(int(price)) < _SENTINEL:
            return price
    return None


class OrderBook:
    """Price-time priority book for a single instrument."""

    def __init__(self) -> None:
        self._levels: dict[Side, dict[Price, PriceLevel]] = {
            Side.BUY: {},
            Side.SELL: {},
        }
        # Bids are stored negated so both sides use a min-heap and "best" is
        # always heap[0]. Avoids two nearly-identical code paths.
        self._prices: dict[Side, list[int]] = {Side.BUY: [], Side.SELL: []}
        self._orders: dict[OrderId, Order] = {}

    # -- queries -----------------------------------------------------------

    def get(self, order_id: OrderId) -> Order | None:
        return self._orders.get(order_id)

    def best_price(self, side: Side) -> Price | None:
        """Best resting price on ``side``, discarding stale heap entries."""
        heap = self._prices[side]
        levels = self._levels[side]
        while heap:
            price = Price(-heap[0] if side is Side.BUY else heap[0])
            level = levels.get(price)
            if level is not None:
                level.prune()
                if not level.empty:
                    return price
                del levels[price]
            heapq.heappop(heap)
        return None

    def best_level(self, side: Side) -> PriceLevel | None:
        price = self.best_price(side)
        return None if price is None else self._levels[side][price]

    def depth_at(self, side: Side, price: Price) -> Quantity:
        """Live resting quantity at a price.

        Returns the level's maintained total rather than summing the deque, and
        the distinction is a correctness one rather than a performance one.
        Cancellation tombstones an order instead of splicing it out, and
        ``prune`` only clears tombstones from the *front* of the queue -- so a
        cancelled order sitting mid-queue is invisible to matching but was still
        being counted by a naive sum.

        That over-reported depth was not merely cosmetic. ``_fillable`` reads it
        to decide whether a fill-or-kill order can be satisfied, so an inflated
        figure let a FOK order be accepted and then partially fill, which is
        precisely what fill-or-kill exists to prevent.

        The total is exact because every path that removes quantity -- a fill via
        ``consume``, a cancellation via ``remove``, a shrinking replace -- reduces
        it at the same moment.
        """
        level = self._levels[side].get(price)
        if level is None:
            return Quantity(0)
        level.prune()
        return Quantity(max(0, int(level.total)))

    def snapshot(self, levels: int = 5) -> BookSnapshot:
        """Aggregated depth, best first, at most ``levels`` prices per side."""
        return BookSnapshot(
            bids=self._aggregate(Side.BUY, levels),
            asks=self._aggregate(Side.SELL, levels),
        )

    def _aggregate(self, side: Side, limit: int) -> tuple[tuple[Price, Quantity], ...]:
        # Sorted from the live level map rather than the heap, because the heap
        # may hold stale prices and popping them here would mutate on a read.
        prices = sorted(self._levels[side], reverse=side is Side.BUY)
        out: list[tuple[Price, Quantity]] = []
        for price in prices:
            quantity = self.depth_at(side, price)
            # A level can be present but empty until something prunes it, so
            # depth rather than presence decides whether it is shown.
            if quantity > 0:
                out.append((price, quantity))
                if len(out) >= limit:
                    break
        return tuple(out)

    @property
    def resting_orders(self) -> tuple[Order, ...]:
        """Every live order, in deterministic (side, price, priority) order."""
        return tuple(
            sorted(
                (o for o in self._orders.values() if o.is_resting),
                key=lambda o: (o.side.value, int(o.price), o.priority),
            )
        )

    @property
    def total_resting_quantity(self) -> int:
        return sum(o.remaining for o in self._orders.values() if o.is_resting)

    # -- mutation ----------------------------------------------------------

    def add(self, order: Order) -> None:
        """Rest an order at its price, behind everything already there."""
        levels = self._levels[order.side]
        level = levels.get(order.price)
        if level is None:
            level = PriceLevel(price=order.price)
            levels[order.price] = level
            heapq.heappush(
                self._prices[order.side],
                -int(order.price) if order.side is Side.BUY else int(order.price),
            )
        level.append(order)
        self._orders[order.order_id] = order

    def track(self, order: Order) -> None:
        """Record an order without resting it, so it can still be looked up.

        Needed for orders that fully fill or expire on arrival: they never join
        a level, but their id must still resolve for acknowledgements and for a
        subsequent cancel to be rejected as terminal rather than unknown.
        """
        self._orders[order.order_id] = order

    def consume(self, order: Order, quantity: Quantity) -> None:
        """Remove ``quantity`` from a resting order and its level's total."""
        order.remaining = Quantity(order.remaining - quantity)
        level = self._levels[order.side].get(order.price)
        if level is not None:
            level.reduce(quantity)
        if order.remaining <= 0:
            order.status = OrderStatus.FILLED
        else:
            order.status = OrderStatus.PARTIALLY_FILLED

    def remove(self, order: Order) -> None:
        """Tombstone an order. The level skips it on the way past.

        Deliberately not O(queue): cancellation is the most common operation in
        an electronic market, and scanning a deque to splice one out would make
        the common case the expensive one.
        """
        level = self._levels[order.side].get(order.price)
        if level is not None and order.remaining > 0:
            level.reduce(order.remaining)
