"""A market that runs in wall-clock time, with a human as one of the agents.

Everything else in the project runs the kernel as fast as it will go, which is
right for experiments and useless for watching. This drives the same kernel in
slices synchronised to a real clock, so a browser can see the book move and a
person can put an order into it.

Two properties are preserved deliberately, because losing them would make the
live view a different system from the one under test:

**The human is an agent.** Their orders travel through the kernel, with a
latency, and reach the venue the same way an algorithm's do. They get no
privileged read of the book, no instant fills, and no exemption from the
collateral check. Watching a market you are exempt from teaches nothing.

**The engine is untouched.** Stepping is a property of the *kernel* -- see
``Kernel.start``/``advance``/``finish`` -- not a special mode. The same seed,
replayed headless, produces the same tape.

The one thing that genuinely differs is the clock: real time advances whether or
not the queue has work, so an idle market still moves forward. A batch run would
simply jump to the next event.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from arena.agents.base import TradingAgent
from arena.exchange.events import Cancel, Submit
from arena.exchange.types import (
    AgentId,
    OrderType,
    Price,
    Quantity,
    Side,
    TimeInForce,
)
from arena.market.instrument import Instrument
from arena.market.venue import SymbolCommand, Venue
from arena.market.venue_agent import VenueAgent
from arena.portfolio.money import from_money
from arena.sim.kernel import Kernel, SimulationContext
from arena.sim.messages import Feed, PrivateEvent, Subscribe, TopOfBook, TradePrint
from arena.sim.time import Duration, Timestamp, millis, seconds

__all__ = ["LiveMarket", "HumanAgent"]

VENUE_ID = AgentId("venue")
HUMAN_ID = AgentId("you")


class HumanAgent(TradingAgent):
    """The person at the browser, as an ordinary participant.

    Orders arrive here from the UI and are forwarded on the next wakeup rather
    than injected directly, so a human's action enters the event queue exactly
    like an agent's. It also means a human cannot act faster than their wakeup
    cadence, which is the honest analogue of a person's reaction time.
    """

    def __init__(
        self, venue_id: AgentId, instruments: dict[str, Instrument]
    ) -> None:
        super().__init__(HUMAN_ID, venue_id, instruments, millis(50))
        self._outbox: list[SymbolCommand] = []
        self.log: list[dict[str, Any]] = []

    def enqueue(self, command: SymbolCommand) -> None:
        self._outbox.append(command)

    def act(self, ctx: SimulationContext) -> None:
        pending, self._outbox = self._outbox, []
        for envelope in pending:
            ctx.send(self.venue_id, envelope)

    def on_private(self, ctx: SimulationContext, event: Any) -> None:
        self.log.append({"t": int(ctx.now), **event.to_dict()})
        if len(self.log) > 200:
            del self.log[:-200]


@dataclass
class LiveMarket:
    """Owns the kernel, the venue, the agents, and the wall-clock loop."""

    venue: Venue
    kernel: Kernel
    venue_agent: VenueAgent
    human: HumanAgent
    agents: list[TradingAgent] = field(default_factory=list)
    speed: float = 1.0
    _wall_start: float = 0.0
    _running: bool = False

    def start(self) -> None:
        self.kernel.start()
        self._wall_start = time.monotonic()
        self._running = True

    def step(self) -> int:
        """Advance simulated time to match the wall clock. Returns events run."""
        if not self._running:
            return 0
        elapsed = (time.monotonic() - self._wall_start) * self.speed
        target = Timestamp(int(elapsed * 1_000_000_000))
        # A cap per slice, so a burst of activity cannot stall the event loop
        # that is serving the browser. Anything left simply runs next slice.
        return self.kernel.advance(until=target, max_events=20_000)

    # -- human actions -----------------------------------------------------

    def submit(
        self, symbol: str, side: str, quantity: int, price: Decimal | None
    ) -> dict[str, Any]:
        instrument = self.venue.registry.get(symbol)
        if instrument is None:
            return {"ok": False, "error": f"unknown symbol {symbol}"}
        if quantity <= 0:
            return {"ok": False, "error": "quantity must be positive"}

        try:
            ticks = None if price is None else instrument.to_ticks(price)
        except ValueError as bad_price:
            return {"ok": False, "error": str(bad_price)}

        command = Submit(
            HUMAN_ID,
            Side.BUY if side.lower() == "buy" else Side.SELL,
            Quantity(quantity),
            ticks,
            OrderType.LIMIT if ticks is not None else OrderType.MARKET,
            TimeInForce.GTC if ticks is not None else TimeInForce.IOC,
        )
        self.human.enqueue(SymbolCommand(symbol, command))
        return {"ok": True}

    def cancel(self, order_id: int) -> dict[str, Any]:
        symbol = self.human.live_orders.get(order_id)
        if symbol is None:
            return {"ok": False, "error": "no such live order"}
        self.human.enqueue(SymbolCommand(symbol, Cancel(HUMAN_ID, order_id)))
        return {"ok": True}

    def cancel_all(self) -> dict[str, Any]:
        """Pull every working order. Distinct from flatten, which closes risk."""
        for order_id, symbol in list(self.human.live_orders.items()):
            self.human.enqueue(SymbolCommand(symbol, Cancel(HUMAN_ID, order_id)))
        return {"ok": True}

    def flatten(self) -> dict[str, Any]:
        """Close every position at market. The panic button."""
        account = self.venue.account(HUMAN_ID)
        for symbol, position in sorted(account.positions.items()):
            if position.quantity == 0:
                continue
            side = Side.SELL if position.quantity > 0 else Side.BUY
            self.human.enqueue(
                SymbolCommand(
                    symbol,
                    Submit(
                        HUMAN_ID,
                        side,
                        Quantity(abs(position.quantity)),
                        None,
                        OrderType.MARKET,
                        TimeInForce.IOC,
                    ),
                )
            )
        return {"ok": True}

    # -- reporting ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Everything the UI needs, in one message."""
        marks = self.venue.marks()
        account = self.venue.account(HUMAN_ID)
        symbols = self.venue.registry.symbols

        books = {}
        for symbol in symbols:
            instrument = self.venue.registry.require(symbol)
            snap = self.venue.engine(symbol).book.snapshot(8)
            books[symbol] = {
                "bids": [[str(instrument.from_ticks(p)), int(q)] for p, q in snap.bids],
                "asks": [[str(instrument.from_ticks(p)), int(q)] for p, q in snap.asks],
                "mark": str(from_money(marks[symbol])),
                "spread": (
                    str(instrument.from_ticks(Price(snap.spread)))
                    if snap.spread is not None
                    else None
                ),
                "class": instrument.instrument_class,
                "tick": str(instrument.tick_size),
                "bounds": [str(b) for b in instrument.settlement_bounds],
                "trades": self.venue.engine(symbol).tape.__len__(),
            }

        recent = []
        for stamp, message in self.venue_agent.public_log[-60:]:
            if isinstance(message, TradePrint):
                instrument = self.venue.registry.require(message.symbol)
                recent.append(
                    {
                        "t": int(stamp),
                        "symbol": message.symbol,
                        "price": str(instrument.from_ticks(message.price)),
                        "quantity": int(message.quantity),
                        "side": message.aggressor_side.value,
                    }
                )

        positions = []
        for symbol in symbols:
            position = account.positions.get(symbol)
            if position is None or (position.quantity == 0 and position.volume == 0):
                continue
            positions.append(
                {
                    "symbol": symbol,
                    "quantity": position.quantity,
                    "average_price": str(round(position.average_price, 4)),
                    "unrealized": str(from_money(position.unrealized_pnl(marks[symbol]))),
                    "realized": str(from_money(position.realized_pnl)),
                }
            )

        return {
            "clock": int(self.kernel.now),
            "events": self.kernel.processed,
            "books": books,
            "tape": recent[::-1],
            "account": {
                "cash": str(from_money(account.cash)),
                "free_cash": str(from_money(account.free_cash)),
                "collateral": str(from_money(account.posted_collateral)),
                "equity": str(from_money(account.equity(marks))),
                "pnl": str(
                    from_money(account.equity(marks)) - from_money(account.starting_cash)
                ),
                "positions": positions,
            },
            "orders": [
                {"order_id": oid, "symbol": sym}
                for oid, sym in sorted(self.human.live_orders.items())
            ],
            "log": self.human.log[-12:][::-1],
            "conservation": str(self.venue.conservation_check()),
        }
