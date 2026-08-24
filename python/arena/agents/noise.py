"""Noise traders.

Not filler, and worth being clear about why. Without uninformed order flow there
is nobody for a market maker to earn a spread from, and no camouflage for an
informed trader to hide behind -- so both the market-making and the
information-asymmetry questions become degenerate. Every classic microstructure
model needs noise for the same reason: Kyle's insider is only profitable because
the market maker cannot tell them apart from the noise.

Two behaviours, because a population of purely random traders is *too* benign.
Real uninformed flow chases trends and clusters, which is what produces the
autocorrelated order flow and occasional runs that a market maker actually has
to survive.
"""

from __future__ import annotations

from arena.agents.base import TradingAgent
from arena.exchange.types import AgentId, Price, Side, TimeInForce
from arena.market.instrument import Instrument
from arena.sim.kernel import SimulationContext
from arena.sim.messages import TradePrint
from arena.sim.time import Duration, millis

__all__ = ["NoiseTrader"]


class NoiseTrader(TradingAgent):
    """Uninformed flow: random direction, with a configurable trend-chasing bias."""

    def __init__(
        self,
        agent_id: AgentId,
        venue_id: AgentId,
        instruments: dict[str, Instrument],
        wake_interval: Duration = millis(900),
        max_size: int = 6,
        aggressive_probability: float = 0.55,
        momentum_bias: float = 0.25,
    ) -> None:
        super().__init__(agent_id, venue_id, instruments, wake_interval)
        self.max_size = max_size
        self.aggressive_probability = aggressive_probability
        self.momentum_bias = momentum_bias
        # Sign of the last observed price change per symbol. The only state
        # these agents keep, and the only thing that makes their flow
        # autocorrelated rather than independent.
        self._drift: dict[str, int] = dict.fromkeys(instruments, 0)
        self._previous: dict[str, int] = {}

    def on_print(self, ctx: SimulationContext, print_: TradePrint) -> None:
        previous = self._previous.get(print_.symbol)
        if previous is not None and int(print_.price) != previous:
            self._drift[print_.symbol] = 1 if int(print_.price) > previous else -1
        self._previous[print_.symbol] = int(print_.price)

    def act(self, ctx: SimulationContext) -> None:
        rng = ctx.rng
        symbol = rng.choice(sorted(self.instruments))
        book = self.books[symbol]
        if book.mid is None:
            # Nothing to anchor to yet. Waiting rather than guessing keeps these
            # agents from inventing the opening price, which is the market
            # maker's job.
            return

        # Trend chasing: bias direction toward the last observed move.
        drift = self._drift.get(symbol, 0)
        threshold = 0.5 - self.momentum_bias * drift
        side = Side.BUY if rng.random() > threshold else Side.SELL
        size = rng.randint(1, self.max_size)

        if rng.random() < self.aggressive_probability:
            self.take(ctx, symbol, side, size)
            return

        # Otherwise rest a passive order a tick or two behind the touch, and let
        # it expire rather than linger -- an uninformed trader who leaves stale
        # orders in the book would become a free option for everyone else.
        instrument = self.instruments[symbol]
        offset = rng.randint(1, 3)
        if side is Side.BUY:
            anchor = book.bid if book.bid is not None else Price(int(book.mid))
            price = Price(int(anchor) - offset)
        else:
            anchor = book.ask if book.ask is not None else Price(int(book.mid))
            price = Price(int(anchor) + offset)
        self.quote(ctx, symbol, side, price, size, TimeInForce.GTC)
