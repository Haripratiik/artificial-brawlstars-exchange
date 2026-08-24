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
        truth_level: dict[str, float],
        wake_interval: Duration = millis(1_500),
        precision: float = 1.0,
        metric_sigma: float = 0.02,
        draws: int = 128,
        max_position: int = 150,
        base_size: int = 8,
        patience: float = 0.5,
    ) -> None:
        super().__init__(agent_id, venue_id, instruments, wake_interval)
        self.truth_level = truth_level
        self.precision = max(0.01, precision)
        self.metric_sigma = metric_sigma
        self.draws = max(8, draws)
        self.max_position = max_position
        self.base_size = base_size
        self.patience = patience
        self._estimate: dict[str, float] = {}
        self._noise_scale: dict[str, float] = {}

    def _view(self, ctx: SimulationContext, symbol: str) -> tuple[float, float]:
        """This agent's estimate of settlement, and its own uncertainty.

        **The noise is on the metric, not on the settlement value.** A
        fundamental analyst forms a view on Spike's win rate; what that implies
        for a future, an option, or an event contract then follows from the
        contract's own terms. Perturbing the settlement value directly would be
        modelling an analyst who somehow has an opinion about an option premium
        without having one about the underlying.

        The difference is not cosmetic. Scaling uncertainty to a *contract's*
        range gives an option -- whose value is a small fraction of its range --
        a noise term larger than the entire quantity being estimated, so the
        agent's view is dominated by noise and the option collapses to its
        floor. Perturbing the metric instead makes uncertainty propagate through
        the payoff, which also gives the agent the right sensitivity for free:
        it reacts to a rate change in proportion to the contract's delta.

        Drawn once per symbol and then held. An agent redrawing its view every
        wakeup would be a noise trader with extra steps -- its "information"
        would average to nothing and exert no directional pull on price.
        """
        if symbol not in self._estimate:
            instrument = self.instruments[symbol]
            level = self.truth_level.get(symbol)
            if level is None:
                self._estimate[symbol] = float("nan")
                self._noise_scale[symbol] = 1.0
                return self._estimate[symbol], self._noise_scale[symbol]

            # Uncertainty in metric units -- percentage points of win rate.
            # A sharper agent has a tighter posterior about the same quantity.
            sigma = self.metric_sigma / self.precision
            payoff = instrument.spec.payoff
            centre = level + ctx.rng.gauss(0.0, sigma)

            # **E[payoff(level)], not payoff(E[level]).** For a linear future
            # the two coincide, so the distinction is invisible until an option
            # appears -- and then it is the whole of the option's time value. A
            # put struck just above where the metric will land is worth
            # something precisely because the metric might land lower; a point
            # estimate says it is worth its intrinsic value and nothing more,
            # which prices every out-of-the-money option at zero.
            #
            # Averaged over draws rather than integrated, because that works
            # for a kinked payoff, a step payoff and a linear one without any
            # of them being special-cased -- and because a closed form would
            # need a volatility model this agent has no business owning.
            draws = [centre + ctx.rng.gauss(0.0, sigma) for _ in range(self.draws)]
            values = [payoff.apply(d) for d in draws]
            value = sum(values) / len(values)

            # Dispersion of the payoff under the agent's own uncertainty. This
            # is what conviction is measured against, so a contract whose value
            # is insensitive to the metric produces small trades even when the
            # price looks far away.
            variance = sum((v - value) ** 2 for v in values) / len(values)
            tick = float(instrument.tick_size)
            self._estimate[symbol] = value / tick
            # Never zero: a binary far from its threshold has no dispersion at
            # all, and an agent with zero uncertainty would trade infinitely
            # hard on any deviation.
            self._noise_scale[symbol] = max(1.0, (variance**0.5) / tick)
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
