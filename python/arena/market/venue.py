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

from dataclasses import dataclass, field
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


class Venue:
    """Books, accounts, and settlement across many instruments."""

    def __init__(self, name: str = "arena", starting_cash: Decimal | int = 1_000_000):
        self.name = name
        self.registry = InstrumentRegistry()
        # Converted once, here. Everything downstream is integer minor units,
        # so the ledger conserves exactly rather than nearly.
        self.starting_cash = to_money(starting_cash)
        self._engines: dict[str, MatchingEngine] = {}
        self._accounts: dict[AgentId, Account] = {}
        self._closed: set[str] = set()
        # Orders each agent has working, per symbol: order_id -> (side, qty,
        # price in minor units). Collateral is reserved against these, not only
        # against filled positions.
        self._working: dict[tuple[AgentId, str], dict[OrderId, tuple[Side, int, int]]] = {}
        # Last traded price per symbol, in ticks. The mark of last resort when
        # a book has no two-sided quote.
        self._last: dict[str, Price] = {}

    # -- listing and accounts ---------------------------------------------

    def list_instrument(self, instrument: Instrument) -> None:
        self.registry.list_instrument(instrument)
        self._engines[instrument.symbol] = MatchingEngine(instrument.symbol)

    def engine(self, symbol: str) -> MatchingEngine:
        return self._engines[symbol]

    def account(self, agent_id: AgentId) -> Account:
        account = self._accounts.get(agent_id)
        if account is None:
            account = Account(agent_id=str(agent_id), starting_cash=self.starting_cash)
            self._accounts[agent_id] = account
        return account

    @property
    def accounts(self) -> dict[AgentId, Account]:
        return dict(sorted(self._accounts.items()))

    # -- marking -----------------------------------------------------------

    def mark(self, symbol: str) -> Money:
        """The price open positions are valued at, in minor units.

        Mid when the book is two-sided, otherwise the last trade, otherwise the
        midpoint of the contract's settlement bounds. The final fallback matters
        more than it looks: a contract that has never traded still has to be
        marked, and marking it at zero would report every short as instantly
        profitable.
        """
        instrument = self.registry.require(symbol)
        tick = instrument.tick_in_minor
        book = self._engines[symbol].book.snapshot()
        if book.best_bid is not None and book.best_ask is not None:
            # Averaged in minor units rather than in ticks, so a one-tick spread
            # marks at the true midpoint instead of being floored to the bid.
            return Money((int(book.best_bid) * tick + int(book.best_ask) * tick) // 2)
        last = self._last.get(symbol)
        if last is not None:
            return Money(int(last) * tick)
        low, high = instrument.bounds_in_minor
        return Money((int(low) + int(high)) // 2)

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
        if symbol in self._closed and isinstance(command, (Submit, Replace)):
            # Cancels stay legal after the close so an agent can tidy up; new
            # risk cannot be taken once the outcome is determined.
            return [Rejected(SequenceNumber(0), agent_id, RejectReason.ALREADY_TERMINAL)]

        if isinstance(command, Submit) and not self._affordable(
            agent_id, instrument, command
        ):
            return [
                Rejected(SequenceNumber(0), agent_id, RejectReason.INSUFFICIENT_COLLATERAL)
            ]

        events = self._engines[symbol].apply(command)
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
        bounds = instrument.bounds_in_minor

        if command.price is not None:
            price = instrument.price_in_minor(command.price)
            quantity = int(command.quantity)
        else:
            # A market order can only ever trade against resting liquidity, so
            # its exposure is bounded by the book rather than by the contract's
            # settlement range. Reserving against the far end of the range --
            # 10,000 on a win-rate future quoted near 4,700 -- rejects orders
            # that could never have cost anything like that much, and does it
            # for a price the order was structurally incapable of paying.
            quantity, price = self._market_exposure(
                symbol, instrument, command.side, int(command.quantity)
            )
            if quantity == 0:
                # No liquidity to hit. The order will cancel unfilled and can
                # create no position, so there is nothing to collateralise.
                return True

        position = account.positions.get(symbol)
        current = position.quantity if position else 0

        working = self._working.get((agent_id, symbol), {})
        buys = sum(q for side, q, _p in working.values() if side is Side.BUY)
        sells = sum(q for side, q, _p in working.values() if side is Side.SELL)
        if command.side is Side.BUY:
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
            elif isinstance(event, (Cancelled, Rejected)):
                book.pop(getattr(event, "order_id", None), None)

    def _book_fills(
        self, symbol: str, instrument: Instrument, events: list[Event]
    ) -> None:
        bounds = instrument.bounds_in_minor
        for event in events:
            if isinstance(event, Filled):
                signed = int(event.quantity) * (1 if event.side is Side.BUY else -1)
                self.account(event.agent_id).apply_fill(
                    symbol, signed, instrument.price_in_minor(event.price), bounds
                )
            elif isinstance(event, Traded):
                self._last[symbol] = event.price

    # -- lifecycle ---------------------------------------------------------

    def close(self, symbol: str) -> None:
        """Stop trading. The outcome is determined; only settlement remains."""
        self.registry.require(symbol)
        self._closed.add(symbol)

    @property
    def closed_symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._closed))

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
        self._closed.add(symbol)

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

    def conservation_check(self) -> Money:
        """Total equity minus total starting capital, in minor units.

        Must be **exactly** zero in a closed market with no fees: trading moves
        value between participants, it does not create it. A non-zero figure
        means an accounting leak, and this is the single sharpest check
        available on the whole portfolio layer -- which is why the ledger runs on
        integers, so "exactly" can be meant literally.
        """
        marks = self.marks()
        equity = sum(int(a.equity(marks)) for a in self._accounts.values())
        started = sum(int(a.starting_cash) for a in self._accounts.values())
        return Money(equity - started)
