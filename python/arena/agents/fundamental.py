"""A fundamental trader: forms a view of settlement, trades the gap.

This is the agent that makes prices mean something. A market of makers and noise
traders has liquidity and volatility but no anchor -- its price is whatever the
last flow happened to push it to. A fundamental agent supplies the force that
drags price toward the value the contract will actually settle at, which is what
turns "does the market aggregate information?" into a question with an answer.

**Its information is deliberately imperfect and deliberately parameterised.**
The agent is told the true settlement value, then given a *noisy* view of it:

    estimate = truth + noise,   noise scaled by `precision`

That is the knob every information experiment turns. A population of agents with
different precisions is a population with heterogeneous information, and the
market's job is to aggregate them. Later phases replace the noise term with
something better motivated -- an agent that has observed ``n`` battles has a
posterior whose width follows from ``n`` rather than from a free parameter -- but
the interface is the same, and the trading logic does not change.

It trades on **edge relative to its own uncertainty**, not on raw distance from
price. An agent with a vague view should not bet heavily on a small gap, and one
with a sharp view should. Sizing by conviction is what stops a noisy agent from
dominating the book simply by being wrong loudly.
"""

from __future__ import annotations

from arena.agents.base import TradingAgent
from arena.exchange.types import AgentId, Price, Side, TimeInForce
from arena.market.instrument import Instrument
from arena.sim.kernel import SimulationContext
from arena.sim.time import Duration, millis

__all__ = ["FundamentalTrader"]


class FundamentalTrader(TradingAgent):
    """Trades the gap between price and its own estimate of settlement."""

    def __init__(
        self,
        agent_id: AgentId,
        venue_id: AgentId,
        instruments: dict[str, Instrument],
        truth: dict[str, float],
        wake_interval: Duration = millis(1_500),
        precision: float = 1.0,
        max_position: int = 150,
        base_size: int = 8,
        patience: float = 0.5,
    ) -> None:
        super().__init__(agent_id, venue_id, instruments, wake_interval)
        self.truth = truth
        self.precision = max(0.01, precision)
        self.max_position = max_position
        self.base_size = base_size
        self.patience = patience
        self._estimate: dict[str, float] = {}
        self._noise_scale: dict[str, float] = {}

    def _view(self, ctx: SimulationContext, symbol: str) -> tuple[float, float]:
        """This agent's estimate of settlement, and its own uncertainty.

        Drawn once per symbol and then held. An agent that redrew its view every
        wakeup would behave like a noise trader with extra steps -- its
        "information" would average out to nothing and it would exert no
        directional pull on price at all.
        """
        if symbol not in self._estimate:
            instrument = self.instruments[symbol]
            low, high = instrument.tick_bounds
            span = max(1.0, (int(high) - int(low)))
            # Uncertainty as a fraction of the contract's whole range, so the
            # same precision means the same thing on a future and on a binary.
            scale = span * 0.02 / self.precision
            truth = self.truth.get(symbol)
            if truth is None:
                self._estimate[symbol] = float("nan")
                self._noise_scale[symbol] = scale
            else:
                self._estimate[symbol] = truth + ctx.rng.gauss(0.0, scale)
                self._noise_scale[symbol] = scale
        return self._estimate[symbol], self._noise_scale[symbol]

    def act(self, ctx: SimulationContext) -> None:
        for symbol in sorted(self.instruments):
            self._trade(ctx, symbol)

    def _trade(self, ctx: SimulationContext, symbol: str) -> None:
        book = self.books[symbol]
        if book.mid is None:
            return
        estimate, uncertainty = self._view(ctx, symbol)
        if estimate != estimate:  # NaN: no view on this symbol
            return

        edge = estimate - book.mid
        # Trade only when the gap is large relative to what this agent actually
        # knows. The threshold scales with uncertainty, so a vague agent needs a
        # bigger discrepancy before it acts.
        if abs(edge) < uncertainty * self.patience:
            self.cancel_all(ctx, symbol)
            return

        side = Side.BUY if edge > 0 else Side.SELL
        inventory = self.position.get(symbol, 0)
        if (side is Side.BUY and inventory >= self.max_position) or (
            side is Side.SELL and inventory <= -self.max_position
        ):
            return

        # Size by conviction: how many uncertainties away the price is.
        conviction = min(4.0, abs(edge) / max(1e-9, uncertainty))
        size = max(1, int(self.base_size * conviction))
        headroom = self.max_position - abs(inventory)
        size = max(1, min(size, headroom))

        self.cancel_all(ctx, symbol)

        # Cross when the touch is already inside the estimate; otherwise post at
        # the touch and wait. Always crossing would pay the spread away on every
        # trade and turn a correct view into a losing strategy.
        if side is Side.BUY and book.ask is not None and int(book.ask) < estimate:
            self.take(ctx, symbol, side, size)
        elif side is Side.SELL and book.bid is not None and int(book.bid) > estimate:
            self.take(ctx, symbol, side, size)
        else:
            anchor = book.bid if side is Side.BUY else book.ask
            if anchor is None:
                return
            self.quote(ctx, symbol, side, Price(int(anchor)), size, TimeInForce.GTC)
