"""A multi-instrument venue: books, accounts, collateral, and settlement.

This is the component that makes the project a market rather than a book. It
holds one matching engine per symbol, an account per agent, and the instrument
registry that says what each symbol settles into -- so a trade can finally
become a position, a position can be marked, and an expiry can turn it into
cash.

Responsibilities, and what is deliberately *not* here:

    here          symbols, accounts, collateral checks, marks, settlement
    not here      matching (one MatchingEngine per symbol, untouched)
    not here      latency and scheduling (the kernel's job)
    not here      what a metric means (the world's job)

Keeping matching out is what preserves the C++ port's acceptance test: each
engine remains a pure function of its own command stream, so the port can still
be validated by replaying one symbol's commands through both implementations.

**Risk is checked before an order reaches the book, not after it fills.** An
exchange that discovers insolvency after the trade has printed cannot unprint
it, and a simulation that allows it produces PnL nobody could have earned. Every
instrument here settles inside a known interval, so the check is exact: the
worst case of the resulting position is arithmetic, not an estimate.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from arena.contracts.spec import ContractSpec
from arena.exchange.engine import MatchingEngine
from arena.exchange.events import (
    Acknowledged,
    Cancel,
    Cancelled,
    Command,
    Event,
    Filled,
    Rejected,
    Replace,
    Replaced,
    Submit,
    Traded,
)
from arena.exchange.types import (
    AgentId,
    OrderId,
    Price,
    Quantity,
    RejectReason,
    SequenceNumber,
    Side,
)
from arena.exchange.session import AuctionResult, SessionState, indicative_auction
from arena.market.fees import FREE, FeeSchedule
from arena.market.instrument import Instrument
from arena.portfolio.account import Account
from arena.portfolio.money import Money, from_money, to_money
from arena.settlement.result import SettlementResult, SettlementStatus

__all__ = ["Venue", "SymbolCommand", "InstrumentRegistry"]

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class SymbolCommand:
    """A command addressed to one symbol.

    The engine's commands carry no symbol -- deliberately, since each engine
    serves exactly one book. Routing is the venue's job, so it is expressed as
    an envelope rather than by widening the engine's own message types.
    """

    symbol: str
    command: Command


class InstrumentRegistry:
    """The listed universe. Immutable once a symbol is listed."""

    def __init__(self) -> None:
        self._instruments: dict[str, Instrument] = {}

    def list_instrument(self, instrument: Instrument) -> None:
        if instrument.symbol in self._instruments:
            raise ValueError(
                f"{instrument.symbol} is already listed. A symbol binds to one "
                "contract for its whole life; relisting it would silently change "
                "what open positions settle into."
            )
        self._instruments[instrument.symbol] = instrument

    def get(self, symbol: str) -> Instrument | None:
        return self._instruments.get(symbol)

    def require(self, symbol: str) -> Instrument:
        instrument = self._instruments.get(symbol)
        if instrument is None:
            raise KeyError(f"{symbol} is not listed on this venue")
        return instrument

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._instruments))

    def by_class(self, instrument_class: str) -> tuple[Instrument, ...]:
        return tuple(
            self._instruments[s]
            for s in self.symbols
            if self._instruments[s].instrument_class == instrument_class
        )


# Where fees land. A real account, so the conservation check sees it.
FEE_ACCOUNT_ID = AgentId("venue-treasury")


class Venue:
    """Books, accounts, and settlement across many instruments."""

    def __init__(
        self,
        name: str = "arena",
        starting_cash: Decimal | int = 1_000_000,
        clock: Callable[[], datetime] | None = None,
        sim_clock: Callable[[], int] | None = None,
        fees: FeeSchedule = FREE,
        price_band: float | None = None,
        limit_state_ns: int = 15_000_000_000,
        pause_ns: int = 300_000_000_000,
        reference_window_ns: int = 300_000_000_000,
        min_reference_prints: int = 20,
        balances: dict[AgentId, Decimal | int] | None = None,
    ) -> None:
        self.name = name
        # Supplies wall-clock or simulated calendar time, so expiries can be
        # enforced. Left None when a caller wants to trade a contract outside
        # its window deliberately -- which every unit test does, since a fixture
        # contract's window is a fixed date in the past or future.
        self._clock = clock
        # Elapsed simulated time in nanoseconds, which is a different question
        # from what the calendar says and is answered by a different thing. The
        # calendar decides whether a contract has expired; elapsed time decides
        # how long a symbol has been in a limit state and when its pause is
        # over. Conflating them would ask the kernel what year it is.
        #
        # Settable after construction because the venue is built before the
        # kernel that will drive it.
        self.sim_clock = sim_clock
        self.registry = InstrumentRegistry()
        # Converted once, here. Everything downstream is integer minor units,
        # so the ledger conserves exactly rather than nearly.
        self.starting_cash = to_money(starting_cash)
        # Per-agent opening balances, for participants who should not start with
        # the same capital as everyone else. A market maker needs a balance
        # sized to quote every book at once; a person needs one they can read a
        # profit against. Forty million in an account makes a gain of a hundred
        # invisible, which is a bad way to learn what your trade did.
        self._balances: dict[AgentId, Money] = {
            agent_id: to_money(amount) for agent_id, amount in (balances or {}).items()
        }
        self._engines: dict[str, MatchingEngine] = {}
        self._accounts: dict[AgentId, Account] = {}
        self._phase: dict[str, SessionState] = {}
        # Symbols that have settled. Tracked here rather than only on the
        # accounts, because a symbol nobody held would otherwise settle twice
        # without complaint -- and a settlement firing more than once is a
        # plausible bug in any event-driven system.
        self._settled: set[str] = set()
        # Orders each agent has working, per symbol: order_id -> (side, qty,
        # price in minor units). Collateral is reserved against these, not only
        # against filled positions.
        self._working: dict[tuple[AgentId, str], dict[OrderId, tuple[Side, int, int]]] = {}
        # Last traded price per symbol, in ticks. The mark of last resort when
        # a book has no two-sided quote.
        self._last: dict[str, Price] = {}
        # Distributions already paid, per symbol, per unit and cumulative.
        #
        # A contract that pays as it goes is worth less afterwards by exactly
        # what it paid, so this is subtracted from both ends of its range. That
        # is not bookkeeping tidiness: collateral is computed from those ends,
        # so without it a short would keep reserving against a stream it has
        # already paid out, and would look insolvent for having met its
        # obligations.
        self._distributed: dict[str, Money] = {}
        self._distributions_paid: dict[str, int] = {}
        # Fees move value; they do not destroy it. Whatever traders pay lands in
        # the venue's own account, which is checked for conservation like any
        # other -- so switching fees on cannot quietly break the ledger's central
        # invariant, and venue revenue becomes a number rather than an idea.
        self.fees = fees
        self.fees_collected = Money(0)
        # How far price may travel from the reference before trading is
        # suspended, as a fraction. None disables the breaker entirely, which
        # is the default so existing measurements are unchanged.
        self.price_band = price_band
        # How long price must stay outside the band before trading pauses, and
        # how long the pause lasts, in nanoseconds.
        #
        # Both exist because a breaker without them is a different mechanism.
        # The rule this models (the US limit up-limit down plan) does not halt
        # on a single print outside the band: the symbol enters a *limit state*
        # and only pauses if it is still there fifteen seconds later, because
        # most excursions are one bad order and halting on those turns a fat
        # finger into an outage. The first version here halted immediately on
        # one print, which is the crude version of the same idea and would have
        # made the breaker fire far more often than the thing it is named after.
        self.limit_state_ns = limit_state_ns
        self.pause_ns = pause_ns
        self._reference: dict[str, Price] = {}
        # Prints inside the reference window, oldest first, as (time, price).
        # The reference is their mean rather than the last trade or the open:
        # a single print can be anywhere, and a breaker referenced to one
        # measures the print rather than the market.
        self._recent: dict[str, deque[tuple[int, Price]]] = {}
        # When each symbol entered its current limit state, if it is in one.
        self._limit_since: dict[str, int] = {}
        # How far back the reference mean looks, and how many prints it needs
        # before the band is enforced at all.
        #
        # The second is not a softening of the rule, it is the rule's own
        # premise. A band is a statement about distance from a *reliable*
        # price, and one print is not one. Enforced from the first trade, the
        # breaker measured every symbol's walk away from its opening print --
        # which on a thinly-attended opening auction is exactly the walk it
        # should be making -- and paused 24 of 26 symbols inside a minute.
        self.reference_window_ns = reference_window_ns
        self.min_reference_prints = min_reference_prints
        # When each paused symbol may reopen.
        self._reopen_at: dict[str, int] = {}
        # Every breach, for the record: a halt that leaves no trace is
        # indistinguishable from a market that simply went quiet.
        self.halts: list[dict[str, Any]] = []
        # Who filled whom, per participant, most recent last. A trade event
        # carries order ids rather than agents, so the pairing is resolved while
        # both orders still resolve -- afterwards the ids are just numbers.
        #
        # Kept per agent rather than as one shared window, which was the first
        # attempt and was quietly useless: the bots print thousands of fills a
        # minute, so a person's single trade was evicted from a shared buffer
        # within seconds of making it. The question being answered is "who
        # filled *me*", and that has to survive everyone else's activity.
        self.fills_log: dict[str, list[dict[str, Any]]] = {}

    # -- listing and accounts ---------------------------------------------

    def list_instrument(self, instrument: Instrument) -> None:
        self.registry.list_instrument(instrument)
        self._engines[instrument.symbol] = MatchingEngine(instrument.symbol)
        # Listed instruments open continuous, because that is what every
        # existing caller expects. A venue that wants an opening auction asks
        # for one with `begin_session`.
        self._phase[instrument.symbol] = SessionState.CONTINUOUS

    def engine(self, symbol: str) -> MatchingEngine:
        return self._engines[symbol]

    def account(self, agent_id: AgentId) -> Account:
        account = self._accounts.get(agent_id)
        if account is None:
            account = Account(
                agent_id=str(agent_id),
                starting_cash=self._balances.get(agent_id, self.starting_cash),
            )
            self._accounts[agent_id] = account
        return account

    def open_account(self, agent_id: AgentId, balance: Decimal | int) -> Account:
        """Create an account with its own opening balance.

        Refused once the account exists. An account's ``starting_cash`` is what
        every profit-and-loss figure and the conservation check are measured
        against, so changing it after the fact would rewrite history rather
        than add capital.
        """
        if agent_id in self._accounts:
            raise ValueError(
                f"{agent_id} already has an account; its opening balance is what "
                "its whole PnL is measured against and cannot be restated"
            )
        self._balances[agent_id] = to_money(balance)
        return self.account(agent_id)

    @property
    def accounts(self) -> dict[AgentId, Account]:
        return dict(sorted(self._accounts.items()))

    # -- marking -----------------------------------------------------------

    def mark(self, symbol: str) -> Money:
        """The price open positions are valued at, in minor units.

        Mid when the book is two-sided, otherwise the last trade, otherwise the
        midpoint of the contract's settlement bounds -- and in every case held
        inside whatever side of the touch is standing.

        The final fallback matters more than it looks: a contract that has never
        traded still has to be marked, and marking it at zero would report every
        short as instantly profitable.

        The clamp matters for a subtler reason. A last trade is a fact about the
        past and a resting order is an offer about the present, so when they
        disagree the resting order wins: if someone is bidding 4682 right now,
        the position is worth at least 4682 whatever the tape says, because that
        is a price you can hit. Without it a one-lot print can drag the mark
        away from a touch that a thousand lots are standing at -- which is
        exactly what a stale or unrepresentative print is, and why real venues
        bound settlement prices by the book rather than taking the last price
        as given.
        """
        instrument = self.registry.require(symbol)
        tick = instrument.tick_in_minor
        book = self._engines[symbol].book.snapshot()
        crossed = (
            book.best_bid is not None
            and book.best_ask is not None
            and int(book.best_bid) > int(book.best_ask)
        )
        if not crossed and book.best_bid is not None and book.best_ask is not None:
            # Averaged in minor units rather than in ticks, so a one-tick spread
            # marks at the true midpoint instead of being floored to the bid.
            return Money((int(book.best_bid) * tick + int(book.best_ask) * tick) // 2)

        last = self._last.get(symbol)
        if last is not None:
            reference = int(last) * tick
        else:
            low, high = self.bounds_in_minor(instrument)
            reference = (int(low) + int(high)) // 2
        # Not clamped into a crossed touch. A book in a call phase is crossed
        # on purpose -- orders accumulate without matching -- so "inside the
        # touch" is an empty interval, and forcing a number into it produced a
        # mark *below* the standing bid, which is the opposite of what the
        # clamp exists to prevent.
        if not crossed:
            if book.best_bid is not None:
                reference = max(reference, int(book.best_bid) * tick)
            if book.best_ask is not None:
                reference = min(reference, int(book.best_ask) * tick)
        return Money(reference)

    def bounds_in_minor(self, instrument: Instrument) -> tuple[Money, Money]:
        """The claim's remaining range, after whatever it has already paid.

        The instrument declares the range it had when it was written. The venue
        is the only thing that knows how much of it has since been handed over,
        so the adjustment lives here rather than on the contract -- which must
        stay immutable, because every price ever printed against it was an
        opinion about the terms as written.
        """
        low, high = instrument.bounds_in_minor
        paid = int(self._distributed.get(instrument.symbol, Money(0)))
        if not paid:
            return (low, high)
        return (Money(int(low) - paid), Money(int(high) - paid))

    def marks(self) -> dict[str, Money]:
        return {symbol: self.mark(symbol) for symbol in self.registry.symbols}

    def mark_price(self, symbol: str) -> Decimal:
        """The mark as a human-readable price. Reporting only."""
        return from_money(self.mark(symbol))

    # -- trading -----------------------------------------------------------

    def submit(self, agent_id: AgentId, symbol: str, command: Command) -> list[Event]:
        """Route a command to a symbol's book, after checking it is affordable."""
        instrument = self.registry.get(symbol)
        if instrument is None or symbol not in self._engines:
            return [
                Rejected(SequenceNumber(0), agent_id, RejectReason.UNKNOWN_ORDER)
            ]
        # A contract's own terms say when it stops trading, and until now
        # nothing enforced them: the expiry sat on the instrument as
        # documentation while the book carried on past it. Once the observation
        # window has closed the outcome is determined, so anyone still trading
        # is trading against an answer that already exists.
        if self._clock is not None and self._clock() >= instrument.expiry:
            self._set_phase(symbol, SessionState.CLOSED)

        if not self.session(symbol).accepts_orders and isinstance(
            command, (Submit, Replace)
        ):
            # Cancels stay legal after the close so an agent can tidy up; new
            # risk cannot be taken once the outcome is determined.
            return [Rejected(SequenceNumber(0), agent_id, RejectReason.ALREADY_TERMINAL)]

        # Replace is checked as well as Submit. Guarding only new orders leaves
        # a hole wide enough to drive through: an agent could work ten lots,
        # then replace them with five hundred at a worse price and take on
        # exposure the account was never able to cover. A modification is a
        # request for risk exactly like an order is.
        if isinstance(command, (Submit, Replace)) and not self._affordable(
            agent_id, instrument, command
        ):
            return [
                Rejected(
                    SequenceNumber(0),
                    agent_id,
                    RejectReason.INSUFFICIENT_COLLATERAL,
                    getattr(command, "order_id", None),
                )
            ]

        self._apply_band(symbol)
        events = self._engines[symbol].apply(command)
        self._check_limit_state(symbol)
        self._book_fills(symbol, instrument, events)
        self._track_working(agent_id, symbol, events)
        return events

    def _affordable(
        self, agent_id: AgentId, instrument: Instrument, command: Submit
    ) -> bool:
        """Could the account survive every one of its working orders filling?

        Checking only the position an order would create is not enough, and the
        gap is not academic: a market maker works a bid and an ask at once, each
        individually affordable, and ends up over-committed when both fill. The
        symptom is an account with negative free cash, which is a venue that
        allowed a trade it could not collateralise.

        So the test is over *scenarios*, not over one order. Collateral is
        reserved against the worse of the two directional extremes:

            every working buy fills   ->  position + total working buy quantity
            every working sell fills  ->  position - total working sell quantity

        Only one of those can be the adverse one, so the maximum of the two is
        both sufficient and not over-conservative. Working orders are priced at
        their own limits; a market order is priced at the far end of the
        settlement range, because it can fill anywhere and the only honest
        assumption is the worst price it could get.
        """
        symbol = instrument.symbol
        account = self.account(agent_id)
        bounds = self.bounds_in_minor(instrument)
        working = dict(self._working.get((agent_id, symbol), {}))

        if isinstance(command, Replace):
            existing = working.pop(command.order_id, None)
            if existing is None:
                # Nothing of ours to replace. Let the engine reject it for the
                # right reason rather than pre-empting with a collateral error.
                return True
            side = existing[0]
            quantity = int(command.new_quantity)
            price = (
                instrument.price_in_minor(command.new_price)
                if command.new_price is not None
                else Money(existing[2])
            )
        elif command.price is not None:
            side = command.side
            price = instrument.price_in_minor(command.price)
            quantity = int(command.quantity)
        else:
            side = command.side
            # A market order can only ever trade against resting liquidity, so
            # its exposure is bounded by the book rather than by the contract's
            # settlement range. Reserving against the far end of the range --
            # 10,000 on a win-rate future quoted near 4,700 -- rejects orders
            # that could never have cost anything like that much, and does it
            # for a price the order was structurally incapable of paying.
            quantity, price = self._market_exposure(
                symbol, instrument, side, int(command.quantity)
            )
            if quantity == 0:
                # No liquidity to hit. The order will cancel unfilled and can
                # create no position, so there is nothing to collateralise.
                return True

        position = account.positions.get(symbol)
        current = position.quantity if position else 0

        # `working` already has the order being replaced removed, so a
        # modification is measured as the difference it makes rather than as
        # additional exposure on top of what it supersedes.
        buys = sum(q for s, q, _p in working.values() if s is Side.BUY)
        sells = sum(q for s, q, _p in working.values() if s is Side.SELL)
        if side is Side.BUY:
            buys += quantity
        else:
            sells += quantity

        # Price each scenario at the worst working price on that side, so a
        # cheap order cannot subsidise an expensive one.
        prices = [p for _s, _q, p in working.values()] + [int(price)]
        worst_buy = Money(max(prices))
        worst_sell = Money(min(prices))

        required = max(
            int(account.collateral_required(current + buys, worst_buy, bounds)),
            int(account.collateral_required(current - sells, worst_sell, bounds)),
        )
        released = int(account.collateral.get(symbol, Money(0)))
        return int(account.free_cash) + released >= required

    def _market_exposure(
        self, symbol: str, instrument: Instrument, side: Side, quantity: int
    ) -> tuple[int, Money]:
        """How much a market order could fill, and the worst price it could pay.

        Walks the opposite side of the book. The answer is exact rather than
        conservative because the engine will walk exactly the same levels in
        exactly the same order: a market order cannot reach a price nobody is
        quoting, and cannot fill more than is resting.
        """
        book = self._engines[symbol].book.snapshot(levels=1 << 20)
        levels = book.asks if side is Side.BUY else book.bids

        filled = 0
        worst: Price | None = None
        for price, available in levels:
            filled += min(quantity - filled, int(available))
            worst = price
            if filled >= quantity:
                break

        if filled == 0 or worst is None:
            return 0, Money(0)
        return filled, instrument.price_in_minor(worst)

    def _track_working(
        self, agent_id: AgentId, symbol: str, events: list[Event]
    ) -> None:
        """Maintain the book of orders this agent has working.

        Reserving against working orders is the whole point of the affordability
        scenario above, so this has to stay in step with the engine's view.
        """
        book = self._working.setdefault((agent_id, symbol), {})
        for event in events:
            if isinstance(event, Acknowledged) and event.price is not None:
                book[event.order_id] = (
                    event.side,
                    int(event.quantity),
                    int(self.registry.require(symbol).price_in_minor(event.price)),
                )
            elif isinstance(event, Filled):
                existing = book.get(event.order_id)
                if existing is not None:
                    remaining = int(event.remaining)
                    if remaining <= 0:
                        book.pop(event.order_id, None)
                    else:
                        book[event.order_id] = (existing[0], remaining, existing[2])
            elif isinstance(event, Replaced):
                # The engine keeps the order id across a replace, whether or
                # not queue priority survived, so the entry is updated in place.
                # Leaving the old size and price here would reserve collateral
                # against an order that no longer exists.
                book[event.order_id] = (
                    book.get(event.order_id, (Side.BUY, 0, 0))[0],
                    int(event.quantity),
                    int(self.registry.require(symbol).price_in_minor(event.price)),
                )
            elif isinstance(event, (Cancelled, Rejected)):
                book.pop(getattr(event, "order_id", None), None)

    def _book_fills(
        self,
        symbol: str,
        instrument: Instrument,
        events: list[Event],
        auction: bool = False,
    ) -> None:
        bounds = self.bounds_in_minor(instrument)
        charged = 0
        for event in events:
            if isinstance(event, Filled):
                signed = int(event.quantity) * (1 if event.side is Side.BUY else -1)
                price = instrument.price_in_minor(event.price)
                fee = self.fees.charge(
                    abs(int(price) * int(event.quantity)), event.aggressor, auction
                )
                charged += int(fee)
                self.account(event.agent_id).apply_fill(
                    symbol, signed, price, bounds, fee
                )
            elif isinstance(event, Traded):
                self._last[symbol] = event.price
                self._record_counterparties(symbol, event)
                self._check_price_band(symbol, event.price)
        if charged:
            # The exact counterpart of what the participants paid, so the two
            # sides of every fee cancel to the unit.
            treasury = self.account(FEE_ACCOUNT_ID)
            treasury.cash = Money(int(treasury.cash) + charged)
            self.fees_collected = Money(int(self.fees_collected) + charged)

    # -- lifecycle ---------------------------------------------------------

    def _record_counterparties(self, symbol: str, trade: Traded) -> None:
        """Name both sides of a print, while the order ids still resolve."""
        book = self._engines[symbol].book
        buyer = book.get(trade.buy_order_id)
        seller = book.get(trade.sell_order_id)
        if buyer is None or seller is None:
            return
        entry = {
            "symbol": symbol,
            "quantity": int(trade.quantity),
            "price": int(trade.price),
            "buyer": str(buyer.agent_id),
            "seller": str(seller.agent_id),
            "aggressor": trade.aggressor_side.value,
        }
        for side in (entry["buyer"], entry["seller"]):
            log = self.fills_log.setdefault(side, [])
            log.append(entry)
            if len(log) > 60:
                del log[:-60]

    def counterparties_for(self, agent_id: AgentId, limit: int = 40) -> list[dict[str, Any]]:
        """The other side of this agent's recent fills, most recent first."""
        who = str(agent_id)
        out: list[dict[str, Any]] = []
        for entry in reversed(self.fills_log.get(who, ())):
            mine_is_buy = entry["buyer"] == who
            instrument = self.registry.get(entry["symbol"])
            out.append(
                {
                    "symbol": entry["symbol"],
                    "side": "buy" if mine_is_buy else "sell",
                    "quantity": entry["quantity"],
                    "price": (
                        str(instrument.from_ticks(Price(entry["price"])))
                        if instrument is not None
                        else str(entry["price"])
                    ),
                    "counterparty": entry["seller"] if mine_is_buy else entry["buyer"],
                }
            )
            if len(out) >= limit:
                break
        return out

    # -- sessions ----------------------------------------------------------

    def session(self, symbol: str) -> SessionState:
        return self._phase.get(symbol, SessionState.CONTINUOUS)

    def _set_phase(self, symbol: str, state: SessionState) -> None:
        self._phase[symbol] = state
        # A venue whose mechanism is not a matching engine (the scoring rule)
        # keeps the phase without an engine to push it into.
        engine = self._engines.get(symbol)
        if engine is not None:
            engine.phase = state

    def begin_session(self, symbol: str) -> None:
        """Put a symbol into its opening call phase. Orders rest, nothing trades."""
        self.registry.require(symbol)
        self._set_phase(symbol, SessionState.PRE_OPEN)

    def indicative(self, symbol: str) -> AuctionResult | None:
        """What the auction would clear at right now, changing nothing.

        Published during a call phase as real venues publish indicative prices
        and opening imbalances, so an agent can respond to the auction rather
        than only to its result.
        """
        return indicative_auction(
            self._engines[symbol].book, self._reference.get(symbol)
        )

    def uncross(self, symbol: str) -> AuctionResult | None:
        """Clear the call phase and return to continuous trading."""
        result, _events = self.uncross_events(symbol)
        return result

    def uncross_events(self, symbol: str) -> tuple[AuctionResult | None, list[Event]]:
        """The same, and the events it produced, so they can be delivered.

        An auction fills real orders belonging to real participants, and until
        this existed nobody told them. The ledger moved, the agents did not
        hear, and every one of them then traded on a position it did not have:
        measured after two minutes, **342 of 483** (agent, symbol) pairs had an
        agent's own belief about its position disagreeing with the venue's
        record. A market maker skewing its quotes off an inventory that is not
        its inventory is not managing risk, it is guessing.

        The fills go through exactly the same account path as continuous ones,
        so collateral, fees and conservation cannot diverge between the two --
        an auction that settled through its own accounting would be the ideal
        place for a leak to hide.
        """
        instrument = self.registry.require(symbol)
        engine = self._engines[symbol]
        result = indicative_auction(engine.book, self._reference.get(symbol))
        events = engine.uncross(self._reference.get(symbol))
        self._book_fills(symbol, instrument, events, auction=True)
        # Grouped by owner, because an auction's events belong to everyone who
        # was in it. Skipping this left collateral reserved against orders the
        # uncross had already filled or cancelled.
        by_owner: dict[AgentId, list[Event]] = {}
        for event in events:
            owner = getattr(event, "agent_id", None)
            if owner is not None:
                by_owner.setdefault(owner, []).append(event)
        for owner, owned in by_owner.items():
            self._track_working(owner, symbol, owned)
        if result is not None and result.volume > 0:
            self._reference[symbol] = result.price
            self._recent.setdefault(symbol, deque()).append((self._now(), result.price))
        self._reopen_at.pop(symbol, None)
        self._limit_since.pop(symbol, None)
        self._set_phase(symbol, SessionState.CONTINUOUS)
        return result, events

    def halt(self, symbol: str, reason: str = "manual") -> None:
        """Suspend trading. Orders keep arriving; the reopen is an auction.

        Resuming straight into continuous trading would hand the whole
        dislocation to whichever order arrived first, which is the outcome a
        halt exists to prevent.
        """
        self.registry.require(symbol)
        if self.session(symbol) is SessionState.CLOSED:
            return
        self._set_phase(symbol, SessionState.AUCTION)
        self.halts.append({"symbol": symbol, "reason": reason})

    def _now(self) -> int:
        """Elapsed simulated nanoseconds, or zero when nothing is driving time.

        Zero is a deliberate answer rather than an error: a venue used directly
        in a unit test has no kernel, and a breaker whose clock never advances
        simply never times anything out, which is the right behaviour for a
        venue that is being poked rather than run.
        """
        return int(self.sim_clock()) if self.sim_clock is not None else 0

    def _reference_price(self, symbol: str) -> Price | None:
        """The mean of prints inside the reference window.

        The opening print until the window has filled, which is what a venue
        uses before it has five minutes of trading to average -- and it is the
        auction price, so it is a price size actually transacted at.
        """
        window = self._recent.get(symbol)
        cutoff = self._now() - self.reference_window_ns
        while window and window[0][0] < cutoff:
            window.popleft()
        if window:
            return Price(sum(int(price) for _t, price in window) // len(window))
        return self._quoted_reference(symbol)

    def _quoted_reference(self, symbol: str) -> Price | None:
        """Where the market is, when nothing has traded recently.

        The mid, and only then the last cleared price. Falling straight back to
        the stored one was the obvious thing and it strands the band: a symbol
        that goes quiet keeps a reference from whenever it last printed, the
        market walks away from it, and every unpriced order is then collared
        against a price that no longer exists. Measured, the band on
        `SPIKE_WR_FUT` sat at 6,392 while the book was quoting 4,760 -- a third
        of the way across the contract's range -- and no market order could
        trade at all.

        A quote is weaker evidence than a trade, which is why it is the
        fallback rather than the rule. It is much better evidence than a price
        from a minute ago.
        """
        book = self._engines[symbol].book.snapshot()
        if book.best_bid is not None and book.best_ask is not None:
            if int(book.best_bid) <= int(book.best_ask):
                return Price((int(book.best_bid) + int(book.best_ask)) // 2)
        return self._reference.get(symbol)

    def _check_price_band(self, symbol: str, price: Price) -> None:
        """Record a print in the reference window. Nothing else.

        Prints cannot leave the band any more -- the engine refuses to match
        outside it -- so a print is no longer evidence of anything except where
        the market is. The limit state is judged from the *quote* instead, in
        :meth:`_check_limit_state`, which is what the rule actually says: a
        symbol is in a limit state when the best bid or offer is *at* a band,
        not when a trade has already happened beyond it.
        """
        window = self._recent.setdefault(symbol, deque())
        window.append((self._now(), price))
        self._reference.setdefault(symbol, price)

    def _check_price_band(self, symbol: str, price: Price) -> None:
        """Record a print in the reference window. Nothing else.

        Prints cannot leave the band any more -- the engine refuses to match
        outside it -- so a print is no longer evidence of anything except where
        the market is. The limit state is judged from the *quote* instead, in
        :meth:`_check_limit_state`, which is what the rule actually says: a
        symbol is in a limit state when the best bid or offer is *at* a band,
        not when a trade has already happened beyond it.
        """
        window = self._recent.setdefault(symbol, deque())
        window.append((self._now(), price))
        self._reference.setdefault(symbol, price)

    def _check_limit_state(self, symbol: str) -> None:
        """Enter a limit state while the quote presses against a band, and pause
        if it stays there.

        Three states rather than two, which is the whole point of modelling this
        properly: quoting inside the band, pressing against it, and paused. One
        order that reaches the edge is not an outage -- it is one order -- so
        reaching it starts a clock and only staying there stops the market.
        """
        if self.price_band is None or self.session(symbol) is not SessionState.CONTINUOUS:
            return
        now = self._now()

        # A locked book is a limit state by definition, whatever the reference
        # says. Bid above offer while trading means interest that wants to
        # cross and is not allowed to -- which happens when an order slid to a
        # band edge and the band later moved away from it. Nothing in
        # continuous trading can clear that; an auction can, and clearing it at
        # one price is exactly what an auction is for.
        book = self._engines[symbol].book.snapshot()
        if (
            book.best_bid is not None
            and book.best_ask is not None
            and int(book.best_bid) > int(book.best_ask)
        ):
            since = self._limit_since.get(symbol)
            if since is None:
                self._limit_since[symbol] = now
                self.halts.append(
                    {"symbol": symbol, "reason": "limit_state", "locked": True}
                )
                return
            if now - since < self.limit_state_ns:
                return
            self._limit_since.pop(symbol, None)
            self._reopen_at[symbol] = now + self.pause_ns
            self.halts.append(
                {"symbol": symbol, "reason": "price_band", "locked": True}
            )
            self._set_phase(symbol, SessionState.AUCTION)
            return

        window = self._recent.get(symbol)
        reference = self._reference_price(symbol)
        if reference is None or not window or len(window) < self.min_reference_prints:
            # Pausing a market is disruptive, so it waits for a reference
            # several prints deep. Banding an execution is protective and does
            # not: the worst it does is leave an order unfilled.
            return

        # A fraction of what the contract can be *worth*, not of what it costs.
        #
        # A percentage of price is the rule real equity venues use, and it is
        # meaningless here. A binary trading at fifty cents gets a band of two
        # and a half cents, which any ordinary tick of opinion breaks, so the
        # breaker paused every event contract on the exchange repeatedly while
        # leaving the future -- whose 5% is 233 points, sixteen standard
        # deviations -- untouched. Percentage bands assume a price with no
        # natural scale. Every contract here has one: its settlement range. The
        # same reasoning already governs the maker's inventory skew, and it
        # makes one parameter mean the same thing on a future and on a coin
        # flip.
        low, high = self.registry.require(symbol).tick_bounds
        allowed = abs(int(high) - int(low)) * self.price_band
        book = self._engines[symbol].book.snapshot()
        # Pressing against a band means there is interest that cannot trade: a
        # bid at or above the top of the band, or an offer at or below the
        # bottom of it. Either is the market saying it wants to be somewhere
        # the venue will not let it go.
        price = None
        if book.best_bid is not None and int(book.best_bid) - int(reference) >= allowed:
            price = book.best_bid
        elif book.best_ask is not None and int(reference) - int(book.best_ask) >= allowed:
            price = book.best_ask
        if price is None:
            self._limit_since.pop(symbol, None)
            return

        since = self._limit_since.get(symbol)
        if since is None:
            self._limit_since[symbol] = now
            self.halts.append(
                {
                    "symbol": symbol,
                    "reason": "limit_state",
                    "reference": int(reference),
                    "price": int(price),
                    "band": self.price_band,
                }
            )
            return
        if now - since < self.limit_state_ns:
            return

        self._limit_since.pop(symbol, None)
        self._reopen_at[symbol] = now + self.pause_ns
        self.halts.append(
            {
                "symbol": symbol,
                "reason": "price_band",
                "reference": int(reference),
                "price": int(price),
                "band": self.price_band,
            }
        )
        self._set_phase(symbol, SessionState.AUCTION)

    def _apply_band(self, symbol: str) -> None:
        """Tell the engine where trades may print, before it matches anything.

        Recomputed per command because the reference is a trailing mean and
        moves with the tape. Cheap: it is two integers and a lookup.

        Applied as soon as there is any reference at all, which is a lower bar
        than the one for *halting*, and deliberately. Banding an execution is
        protective: the worst it does is leave an order unfilled. Halting is
        disruptive, so it waits for a reference several prints deep. Holding
        both to the strict bar meant a market order could still walk a thin
        book to the floor for as long as the tape was quiet -- which is exactly
        when a thin book is walkable.
        """
        engine = self._engines.get(symbol)
        if engine is None:
            return
        if self.price_band is None:
            engine.execution_band = None
            return
        reference = self._reference_price(symbol)
        if reference is None:
            engine.execution_band = None
            return
        low, high = self.registry.require(symbol).tick_bounds
        allowed = abs(int(high) - int(low)) * self.price_band
        engine.execution_band = (
            int(max(int(low), int(reference) - allowed)),
            int(min(int(high), int(reference) + allowed)),
        )

    def reopen_due(self) -> tuple[str, ...]:
        """Symbols whose pause has run its course, in canonical order.

        The venue does not reopen them itself. Something has to decide *when*
        time passes, and that is the simulation's business rather than the
        ledger's -- so this reports and the caller uncrosses.
        """
        now = self._now()
        return tuple(
            sorted(
                symbol
                for symbol, at in self._reopen_at.items()
                if now >= at and self.session(symbol) is SessionState.AUCTION
            )
        )

    def close(self, symbol: str) -> None:
        """Stop trading. The outcome is determined; only settlement remains."""
        self.registry.require(symbol)
        self._set_phase(symbol, SessionState.CLOSED)

    @property
    def closed_symbols(self) -> tuple[str, ...]:
        return tuple(
            sorted(s for s, p in self._phase.items() if p is SessionState.CLOSED)
        )

    @property
    def settled_symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._settled))

    def settle(self, symbol: str, result: SettlementResult) -> dict[AgentId, Decimal]:
        """Apply a settlement to every account holding the symbol.

        A VOID releases collateral without realising anything: the world never
        produced the evidence the contract required, so nobody wins and nobody
        loses. Paying out a guess instead would make the guess indistinguishable
        from a measurement the moment it hit a PnL statement.
        """
        instrument = self.registry.require(symbol)
        if result.spec_digest != instrument.spec.spec_digest:
            raise ValueError(
                f"settlement for {symbol} carries digest {result.spec_digest} but the "
                f"listed instrument is {instrument.spec.spec_digest}. The contract that "
                "settled is not the contract that traded."
            )
        if symbol in self._settled:
            raise ValueError(
                f"{symbol} has already settled. Settling twice would pay every "
                "position out twice, and an expiry firing more than once is a "
                "plausible bug rather than an impossible one."
            )
        self._settled.add(symbol)
        self._set_phase(symbol, SessionState.CLOSED)
        # Nothing can be working on a settled contract. Leaving entries behind
        # would keep reserving collateral against orders that can never fill.
        for key in [k for k in self._working if k[1] == symbol]:
            del self._working[key]

        realised: dict[AgentId, Decimal] = {}
        for agent_id in sorted(self._accounts):
            account = self._accounts[agent_id]
            if result.status == SettlementStatus.VOID:
                account.void(symbol)
                realised[agent_id] = Money(0)
            else:
                assert result.settlement_value is not None
                realised[agent_id] = account.settle(
                    symbol, to_money(result.settlement_value)
                )
        return realised

    # -- reporting ---------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        marks = self.marks()
        return {
            "venue": self.name,
            "instruments": [
                self.registry.require(s).to_dict() for s in self.registry.symbols
            ],
            "marks": {s: str(from_money(m)) for s, m in marks.items()},
            "accounts": [
                self._accounts[a].to_dict(marks) for a in sorted(self._accounts)
            ],
            "total_equity": str(
                from_money(
                    Money(sum(int(a.equity(marks)) for a in self._accounts.values()))
                )
            ),
        }

    def distribute(self, symbol: str, per_unit: Money) -> Money:
        """Pay ``per_unit`` to every holder of ``symbol``, charged to every short.

        Returns the gross amount that moved, which is a measurement rather than
        a total: the net is always zero, because every contract outstanding has
        someone long it and someone short it, and the same integer is added on
        one side and subtracted on the other. That is why this cannot break
        conservation even in principle, and the test still checks it.

        The remaining range narrows by exactly what was paid, so collateral
        follows the cash in the same instant rather than a moment later.
        """
        instrument = self.registry.require(symbol)
        if instrument.spec.distribution is None:
            raise ValueError(
                f"{symbol} declares no distribution schedule; paying one would be "
                "a payment its holders never agreed to and its shorts never "
                "collateralised"
            )
        if symbol in self._settled:
            raise ValueError(f"{symbol} has already settled")

        scheduled = len(instrument.spec.distribution.windows)
        already = self._distributions_paid.get(symbol, 0)
        if already >= scheduled:
            raise ValueError(
                f"{symbol} has already paid all {scheduled} of its scheduled "
                f"distributions; a further payment would take the claim below "
                "the range its collateral was computed from"
            )

        self._distributed[symbol] = Money(
            int(self._distributed.get(symbol, Money(0))) + int(per_unit)
        )
        self._distributions_paid[symbol] = already + 1
        bounds = self.bounds_in_minor(instrument)

        moved = 0
        for account in self._accounts.values():
            moved += abs(int(account.distribute(symbol, per_unit, bounds)))
        return Money(moved)

    def conservation_check(self) -> Money:
        """Total equity minus total starting capital, in minor units.

        Must be **exactly** zero: trading moves value between participants, it
        does not create it. A non-zero figure means an accounting leak, and this
        is the single sharpest check available on the whole portfolio layer --
        which is why the ledger runs on integers, so "exactly" can be meant
        literally.

        Fees do not change that. They are a transfer to the venue's own account,
        which is counted here alongside everyone else's, so a schedule with any
        rates at all still nets to zero. If it ever did not, fees would be
        creating or destroying value rather than moving it.
        """
        marks = self.marks()
        equity = sum(int(a.equity(marks)) for a in self._accounts.values())
        started = sum(int(a.starting_cash) for a in self._accounts.values())
        return Money(equity - started)
