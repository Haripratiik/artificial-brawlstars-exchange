"""An agent's account: cash, positions, collateral, and settlement.

The collateral model is **full collateralisation**, in the sense event-contract
venues use it: the venue holds the entire contingent payout, nobody is naked
short, and an account can lose everything it committed but can never owe more
than it has. Margin and leverage arrive later as a deliberate experiment rather
than as the default.

What makes it exact here, and unusual, is that **every instrument settles inside
a known interval**. A win-rate future scaled by ten thousand settles somewhere in
[0, 10000], so a short at 5100 loses at most 4900 per lot. That worst case is
arithmetic, not a value-at-risk estimate; an ordinary future on an unbounded
price cannot say the same and needs a volatility model to guess at it. So
solvency can be enforced exactly:

    an order is admissible if the worst case of the position it would create is
    still covered by free cash

All amounts are integer minor units -- see :mod:`arena.portfolio.money` -- so the
ledger conserves value exactly rather than nearly.

The conservatism worth knowing about: an agent holding a long and a short in
economically related contracts posts collateral on both, because nothing here
yet understands that they hedge. Portfolio netting is a later phase, and is one
of the things the margin experiments are *about*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from arena.portfolio.money import Money, from_money
from arena.portfolio.position import Position

__all__ = ["Account", "InsufficientCollateral"]


class InsufficientCollateral(Exception):
    """The account cannot cover the worst case of a proposed position."""


@dataclass(slots=True)
class Account:
    """One agent's book. Cash, positions, and collateral held against them."""

    agent_id: str
    starting_cash: Money
    cash: Money = Money(0)
    positions: dict[str, Position] = field(default_factory=dict)
    # Collateral posted per symbol: held, not spendable, released on close.
    collateral: dict[str, Money] = field(default_factory=dict)
    settled: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if int(self.cash) == 0:
            self.cash = Money(int(self.starting_cash))

    # -- views -------------------------------------------------------------

    def position(self, symbol: str) -> Position:
        position = self.positions.get(symbol)
        if position is None:
            position = Position(symbol=symbol)
            self.positions[symbol] = position
        return position

    @property
    def posted_collateral(self) -> Money:
        return Money(sum(int(v) for v in self.collateral.values()))

    @property
    def free_cash(self) -> Money:
        """Cash not currently backing a position."""
        return Money(int(self.cash) - int(self.posted_collateral))

    def unrealized_pnl(self, marks: dict[str, Money]) -> Money:
        total = 0
        for symbol in sorted(self.positions):
            mark = marks.get(symbol)
            if mark is not None:
                total += int(self.positions[symbol].unrealized_pnl(mark))
        return Money(total)

    @property
    def realized_pnl(self) -> Money:
        return Money(sum(int(p.realized_pnl) for p in self.positions.values()))

    def equity(self, marks: dict[str, Money]) -> Money:
        """Total account value: cash plus mark-to-market on open positions.

        ``cash`` already contains realised PnL, because realising is what moves
        cash. Adding ``realized_pnl`` here too would double-count it, and make a
        strategy look twice as profitable as it was.
        """
        return Money(int(self.cash) + int(self.unrealized_pnl(marks)))

    # -- trading -----------------------------------------------------------

    @staticmethod
    def collateral_required(
        quantity: int, price: Money, bounds: tuple[Money, Money]
    ) -> Money:
        """Worst-case loss on a position of ``quantity`` lots opened at ``price``."""
        low, high = int(bounds[0]), int(bounds[1])
        if quantity > 0:
            return Money(quantity * (int(price) - low))
        if quantity < 0:
            return Money(-quantity * (high - int(price)))
        return Money(0)

    def can_afford(
        self, symbol: str, quantity: int, price: Money, bounds: tuple[Money, Money]
    ) -> bool:
        """Whether the account could cover the position this fill would produce.

        Evaluated on the *resulting* position rather than the incremental trade,
        so a trade that reduces exposure stays admissible even with no free
        cash. It must: otherwise an agent becomes unable to close a losing
        position at precisely the moment it needs to.
        """
        position = self.positions.get(symbol)
        current = position.quantity if position else 0
        resulting = current + quantity
        if resulting == 0:
            return True

        required = int(self.collateral_required(resulting, price, bounds))
        released = int(self.collateral.get(symbol, Money(0)))
        return int(self.free_cash) + released >= required

    def apply_fill(
        self,
        symbol: str,
        quantity: int,
        price: Money,
        bounds: tuple[Money, Money],
        fee: Money = Money(0),
    ) -> None:
        """Book an execution, moving cash and re-posting collateral."""
        position = self.position(symbol)
        record = position.apply_fill(quantity, price, fee)

        # Realised PnL and fees are the only things that move cash before
        # settlement. The notional does not: a futures position is a
        # collateralised commitment, not a purchase, so debiting
        # quantity * price would be spot accounting and would report a trader as
        # broke the instant they opened a large position.
        self.cash = Money(int(self.cash) + int(record.realized) - int(fee))

        if position.quantity == 0:
            self.collateral.pop(symbol, None)
        else:
            # Priced at the position's own average, which is what it would lose
            # from. Derived from the exact basis, so it tracks reality even
            # after a proportional close left a sub-unit remainder behind.
            average = Money(int(position.cost_basis) // position.quantity)
            self.collateral[symbol] = self.collateral_required(
                position.quantity, average, bounds
            )

    def distribute(
        self, symbol: str, per_unit: Money, bounds: tuple[Money, Money]
    ) -> Money:
        """Pay (or charge) a distribution on this symbol, and re-post collateral.

        Longs receive, shorts pay, and the two are the same line of arithmetic
        because a short position is a negative quantity. Nothing is realised:
        the holder gets cash and the contract is worth exactly that much less,
        so equity does not move. That is the correct accounting and it is also
        the thing most likely to be got wrong -- booking a dividend as profit
        would report a holder as making money for holding.

        ``bounds`` are the claim's range *after* this payment, which is what
        makes the collateral work out. Paying ``d`` per unit lowers both ends of
        what is left by ``d``, so a short's requirement falls by exactly the
        cash it just paid and a long's rises by exactly the cash it just
        received. Neither can be made insolvent by a payment it was always
        going to make.
        """
        position = self.positions.get(symbol)
        if position is None or position.quantity == 0:
            return Money(0)

        amount = Money(position.quantity * int(per_unit))
        self.cash = Money(int(self.cash) + int(amount))
        average = Money(int(position.cost_basis) // position.quantity)
        self.collateral[symbol] = self.collateral_required(
            position.quantity, average, bounds
        )
        return amount

    # -- settlement --------------------------------------------------------

    def settle(self, symbol: str, settlement_value: Money) -> Money:
        """Settle an expired contract: realise against the final value, free collateral.

        Idempotent by symbol -- settling twice would pay a position out twice,
        and an expiry firing more than once is a plausible bug in any
        event-driven system.
        """
        if symbol in self.settled:
            raise ValueError(f"{symbol} has already settled for {self.agent_id}")
        self.settled.add(symbol)

        position = self.positions.get(symbol)
        if position is None or position.quantity == 0:
            self.collateral.pop(symbol, None)
            return Money(0)

        realized = position.unrealized_pnl(settlement_value)
        position.realized_pnl = Money(int(position.realized_pnl) + int(realized))
        position.quantity = 0
        position.cost_basis = Money(0)
        self.cash = Money(int(self.cash) + int(realized))
        self.collateral.pop(symbol, None)
        return realized

    def void(self, symbol: str) -> None:
        """Release a voided contract's collateral without realising anything.

        A void means the world never produced the evidence the contract needed.
        Nobody wins or loses; collateral comes back and the position ceases to
        exist. Paying out a guess instead would make the guess indistinguishable
        from a measurement the moment it reached a PnL statement.
        """
        self.settled.add(symbol)
        position = self.positions.get(symbol)
        if position is not None:
            position.quantity = 0
            position.cost_basis = Money(0)
        self.collateral.pop(symbol, None)

    def to_dict(self, marks: dict[str, Money] | None = None) -> dict[str, Any]:
        marks = marks or {}
        return {
            "agent_id": self.agent_id,
            "cash": str(from_money(self.cash)),
            "starting_cash": str(from_money(self.starting_cash)),
            "posted_collateral": str(from_money(self.posted_collateral)),
            "free_cash": str(from_money(self.free_cash)),
            "realized_pnl": str(from_money(self.realized_pnl)),
            "unrealized_pnl": str(from_money(self.unrealized_pnl(marks))),
            "equity": str(from_money(self.equity(marks))),
            "positions": [
                self.positions[s].to_dict(marks.get(s))
                for s in sorted(self.positions)
                if self.positions[s].quantity != 0 or self.positions[s].volume > 0
            ],
        }
