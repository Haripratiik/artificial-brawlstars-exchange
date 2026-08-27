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


def _distribution(instrument) -> dict[str, object] | None:
    """The payment stream, for a contract that has one.

    Only shares do. What a trader needs is how many payments are left and what
    each is written on, because that -- not the settlement -- is what they are
    buying.
    """
    schedule = instrument.spec.distribution
    if schedule is None:
        return None
    return {
        "periods": len(schedule.windows),
        "payoff": schedule.payoff.to_dict(),
        "first": schedule.windows[0].end.strftime("%Y-%m-%d"),
        "last": schedule.windows[-1].end.strftime("%Y-%m-%d"),
    }


def _indicative_price(venue, instrument, symbol: str) -> str | None:
    """Where an auction in progress would clear, or ``None`` if none is."""
    from arena.exchange.session import SessionState

    if venue.session(symbol) is SessionState.CONTINUOUS:
        return None
    result = venue.indicative(symbol)
    if result is None or result.volume <= 0:
        return None
    return str(instrument.from_ticks(result.price))


class HumanAgent(TradingAgent):
    """A person at a browser, as an ordinary participant.

    Orders arrive here from the UI and are forwarded on the next wakeup rather
    than injected directly, so a human's action enters the event queue exactly
    like an agent's. It also means a human cannot act faster than their wakeup
    cadence, which is the honest analogue of a person's reaction time.

    One of these per person, not one per exchange. Sharing a single agent meant
    two browser tabs were one trader: the same account, the same blotter, the
    same working orders, and each tab able to cancel the other's.
    """

    def __init__(
        self,
        venue_id: AgentId,
        instruments: dict[str, Instrument],
        agent_id: AgentId = HUMAN_ID,
        display_name: str = "You",
    ) -> None:
        super().__init__(agent_id, venue_id, instruments, millis(50))
        self.display_name = display_name
        self._outbox: list[SymbolCommand] = []
        self.log: list[dict[str, Any]] = []

    def enqueue(self, command: SymbolCommand) -> None:
        self._outbox.append(command)

    def act(self, ctx: SimulationContext) -> None:
        pending, self._outbox = self._outbox, []
        for envelope in pending:
            ctx.send(self.venue_id, envelope)

    def _on_private(self, ctx: SimulationContext, event: Any, symbol: str) -> None:
        """Record the event for the blotter, then let the base book it.

        Overridden at the underscore level rather than at ``on_private`` because
        only this one carries the symbol. Without it a blotter can say a fill
        happened but not in what -- and the price would be a raw tick count,
        which for a contract on a 0.25 grid is four times the number a person
        expects to read.
        """
        super()._on_private(ctx, event, symbol)
        entry: dict[str, Any] = {"t": int(ctx.now), "symbol": symbol, **event.to_dict()}
        instrument = self.instruments.get(symbol)
        ticks = entry.get("price")
        if ticks is not None and instrument is not None:
            entry["price"] = str(instrument.from_ticks(int(ticks)))
        self.log.append(entry)
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
    # Everyone trading here, by account id. The first entry is the account a
    # visitor gets before signing in, which keeps every existing caller -- and
    # every test -- working unchanged.
    traders: dict[AgentId, HumanAgent] = field(default_factory=dict)
    latency: Any = None
    seat_cash: int = 0
    _wall_last: float = 0.0
    _sim_seconds: float = 0.0
    _running: bool = False

    def __post_init__(self) -> None:
        self.traders.setdefault(self.human.agent_id, self.human)

    # -- people ------------------------------------------------------------

    def seat(self, name: str) -> AgentId:
        """Give a person their own account, agent and blotter. Returns the id.

        Joining a running market rather than reserving a fixed number of seats
        up front. A pool would have been simpler and would have made "the
        exchange is full" a thing that could happen to someone, which is not a
        property of an exchange -- it is a property of a workaround.

        The id is derived from the display name only for readability; the
        server hands out an opaque one. Two people with the same name get two
        accounts, because a name is not an identity.
        """
        index = len(self.traders)
        agent_id = AgentId(f"you-{index}")
        while agent_id in self.traders:
            index += 1
            agent_id = AgentId(f"you-{index}")

        self.venue.open_account(agent_id, self.seat_cash or self.venue.starting_cash)
        agent = HumanAgent(
            self.venue_agent.agent_id,
            dict(self.human.instruments),
            agent_id=agent_id,
            display_name=name,
        )
        if self.latency is not None:
            # The same distance from the exchange as anyone else at a browser.
            self.latency.per_agent[agent_id] = self.latency.per_agent.get(
                self.human.agent_id, millis(20)
            )
        self.traders[agent_id] = agent
        self.agents.append(agent)
        self.kernel.join(agent)
        return agent_id

    def trader(self, agent_id: AgentId | None) -> HumanAgent:
        """The agent for an account id, falling back to the shared one."""
        if agent_id is None:
            return self.human
        return self.traders.get(agent_id, self.human)

    def start(self) -> None:
        self.kernel.start()
        self._wall_last = time.monotonic()
        self._sim_seconds = 0.0
        self._running = True

    def step(self) -> int:
        """Advance simulated time to match the wall clock. Returns events run.

        Simulated time is *accumulated* from each slice rather than recomputed
        from the session start, so that changing speed applies from the moment
        it changes. Recomputing rescaled the whole history instead: raising the
        speed made the clock leap forward past events already scheduled, and
        lowering it would have moved the clock **backwards**, which the kernel
        rightly refuses to do.
        """
        if not self._running:
            return 0
        now = time.monotonic()
        # Clamped so a stalled event loop, a suspended laptop, or a debugger
        # breakpoint does not hand the kernel an hour of catch-up to run in one
        # slice and freeze the browser it is meant to be serving.
        delta = min(1.0, max(0.0, now - self._wall_last))
        self._wall_last = now
        self._sim_seconds += delta * self.speed
        target = Timestamp(int(self._sim_seconds * 1_000_000_000))
        # A cap per slice, so a burst of activity cannot stall the event loop
        # that is serving the browser. Anything left simply runs next slice.
        return self.kernel.advance(until=target, max_events=20_000)

    # -- human actions -----------------------------------------------------

    def submit(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: Decimal | None,
        tif: str = "",
        trader: AgentId | None = None,
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

        # A market order can only ever be immediate; a limit order defaults to
        # resting. Anything else the caller asks for is honoured, so the browser
        # can reach post-only and fill-or-kill rather than only the two defaults.
        if ticks is None:
            duration = TimeInForce.IOC
        else:
            try:
                duration = TimeInForce(tif.lower()) if tif else TimeInForce.GTC
            except ValueError:
                return {"ok": False, "error": f"unknown time in force {tif!r}"}

        who = self.trader(trader)
        command = Submit(
            who.agent_id,
            Side.BUY if side.lower() == "buy" else Side.SELL,
            Quantity(quantity),
            ticks,
            OrderType.LIMIT if ticks is not None else OrderType.MARKET,
            duration,
        )
        who.enqueue(SymbolCommand(symbol, command))
        return {"ok": True}

    def cancel(
        self, order_id: int, trader: AgentId | None = None, symbol: str | None = None
    ) -> dict[str, Any]:
        """Pull one working order.

        The symbol is part of the address, not decoration. Order ids come from
        the matching engine and there is one per book, so id 5 exists on every
        contract at once -- cancelling by id alone means cancelling whichever
        of them happened to be found first. The client sends both because the
        blotter it is reading already shows both; without one, the lookup falls
        back to a search and refuses if it is ambiguous.
        """
        who = self.trader(trader)
        if symbol is None:
            matches = [key for key in who.live_orders if key[1] == order_id]
            if len(matches) != 1:
                return {"ok": False, "error": "no such live order"}
            symbol = matches[0][0]
        if (symbol, order_id) not in who.live_orders:
            # Someone else's order, or none. Reported the same way either way:
            # confirming that an id exists but belongs to another account tells
            # a stranger something about that account.
            return {"ok": False, "error": "no such live order"}
        who.enqueue(SymbolCommand(symbol, Cancel(who.agent_id, order_id)))
        return {"ok": True}

    def cancel_all(self, trader: AgentId | None = None) -> dict[str, Any]:
        """Pull every working order. Distinct from flatten, which closes risk."""
        who = self.trader(trader)
        for (symbol, order_id), _ in list(who.live_orders.items()):
            who.enqueue(SymbolCommand(symbol, Cancel(who.agent_id, order_id)))
        return {"ok": True}

    def flatten(self, trader: AgentId | None = None) -> dict[str, Any]:
        """Close every position at market. The panic button."""
        who = self.trader(trader)
        account = self.venue.account(who.agent_id)
        for symbol, position in sorted(account.positions.items()):
            if position.quantity == 0:
                continue
            side = Side.SELL if position.quantity > 0 else Side.BUY
            who.enqueue(
                SymbolCommand(
                    symbol,
                    Submit(
                        who.agent_id,
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

    def snapshot(self, trader: AgentId | None = None) -> dict[str, Any]:
        """Everything the UI needs, in one message, for one person.

        The books, the tape and the clock are the same for everybody; the
        account, the blotter, the working orders and the counterparties are
        not. Before there was one of each, so two browsers were one trader --
        they shared a balance, and either could cancel the other's orders.
        """
        who = self.trader(trader)
        marks = self.venue.marks()
        account = self.venue.account(who.agent_id)
        symbols = self.venue.registry.symbols

        books = {}
        for symbol in symbols:
            instrument = self.venue.registry.require(symbol)
            snap = self.venue.engine(symbol).book.snapshot(8)
            books[symbol] = {
                # Priced levels only. Market-on-open interest rests at a
                # sentinel so it crosses every candidate the auction weighs;
                # putting it on a screen published a bid of
                # 4,611,686,018,427,387,904 and a spread to match.
                "bids": [
                    [str(instrument.from_ticks(p)), int(q)] for p, q in snap.priced_bids
                ],
                "asks": [
                    [str(instrument.from_ticks(p)), int(q)] for p, q in snap.priced_asks
                ],
                "mark": str(from_money(marks[symbol])),
                # A crossed book has no spread, and during a call phase it is
                # *meant* to be crossed: orders accumulate without matching
                # until the uncross. Reporting the arithmetic difference
                # printed "-10,000.00" on the screen of anyone who looked
                # before the open, which is not a spread and not a number
                # anyone should have to interpret.
                "spread": (
                    str(instrument.from_ticks(Price(snap.spread)))
                    if snap.spread is not None and snap.spread >= 0
                    else None
                ),
                "session": self.venue.session(symbol).value,
                # What the call would clear at, published during it exactly as
                # real venues publish an indicative price -- so an agent, or a
                # person, can respond to the auction rather than only to its
                # result.
                "indicative": _indicative_price(self.venue, instrument, symbol),
                "class": instrument.instrument_class,
                "tick": str(instrument.tick_size),
                # What the claim can be worth, not only what it settles at.
                # A share settles at nothing because it has paid everything
                # out, so the settlement range would say a share is worth
                # zero to zero -- true at the last instant and useless
                # before it.
                "bounds": [str(b) for b in instrument.value_bounds],
                "trades": len(self.venue.engine(symbol).tape),
                # What the contract actually is, so a trader can see the terms
                # rather than only the price. A market where you cannot read the
                # contract is a casino with extra steps.
                "contract": {
                    "id": instrument.spec.contract_id,
                    "payoff": instrument.spec.payoff.to_dict(),
                    "underlying": instrument.spec.underlying.to_dict(),
                    "expiry": instrument.expiry.strftime("%Y-%m-%d"),
                    "digest": instrument.spec.spec_digest[7:19],
                    "distribution": _distribution(instrument),
                },
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
                for (sym, oid), _ in sorted(who.live_orders.items())
            ],
            "log": who.log[-12:][::-1],
            "you": {"id": str(who.agent_id), "name": who.display_name},
            "traders": [
                {"id": str(a.agent_id), "name": a.display_name}
                for a in self.traders.values()
            ],
            # Who actually took the other side of your orders. A simulated
            # exchange should be able to answer that with names.
            "counterparties": self.venue.counterparties_for(who.agent_id, limit=25),
            "conservation": str(self.venue.conservation_check()),
        }
