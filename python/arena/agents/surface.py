"""A market maker that quotes a whole option chain from one distribution.

The defect this exists to fix was measured on the live market and is a
riskless arbitrage sitting in the book: `SPIKE_C4700` marked at 72.7 while
`SPIKE_C4600` marked at 59.1. A call struck higher cannot be worth more than
one struck lower -- the lower strike pays whatever the higher one pays and
sometimes more -- so anyone could buy the 4,600 and sell the 4,700 and never
lose. Put-call parity was out by 35 ticks at the same time.

There was one cause. The plain maker anchors each book on its own trade prints
and opens it at the middle of its own settlement range, so every strike was
priced by a process that had never heard of the strike next to it. Nothing
related them, so nothing kept them related.

The fix is the one real desks use: quote the whole ladder off a **single
distribution** of where the underlying will settle, and the ladder is
arbitrage-free by construction. A set of call prices at one maturity is free of
static arbitrage exactly when it is decreasing and convex in strike with slope
in [-1, 0] (Davis and Hobson 2007; Carr and Madan 2005), and every one of those
conditions is automatic for prices of the form ``E[(F - K)+]`` under any fixed
law: monotone because the payoff is, convex because a maximum of affine
functions is, slope ``-P(F > K)`` which lives in [-1, 0] by definition. Parity
holds because the put is *defined* here as ``C - (E[F] - K)``.

Two things it deliberately does not do, because they would make it price the
answer rather than a belief:

  * The distribution is centred on the **market's** view of the underlying --
    the maker's own anchor for the future, which is a slow average of where
    that future has actually traded -- and never on the settlement value. If
    the future is mispriced, the whole chain is consistently mispriced, which
    is right: consistency is the property being fixed, not correctness.
  * Its width is **estimated from the tape**, not configured. A constant
    would have been a number chosen to make option prices look plausible, and
    it would have frozen the one quantity an option market is actually about.
    Instead the maker keeps an exponentially-weighted variance of how far
    prints in the underlying land from its own anchor -- the same estimator a
    risk desk runs -- and converts it into a Beta concentration by matching
    moments: for a rate ``m`` scaled by ``S``, a standard deviation of ``s`` in
    price implies ``kappa = m(1-m)S^2/s^2 - 1``.

    So the width follows the market. When the underlying is quiet, options
    price near intrinsic; when it moves, they carry time value. That is a
    belief the maker can still be wrong about -- realised dispersion is not
    settlement uncertainty, and the difference is the adverse selection an
    options market maker actually faces -- but it is wrong in a way that
    responds to evidence rather than in a way that was typed in.

Inventory is skewed in the underlying, not per strike, and that is not a
detail. The plain maker skews each book by a fraction of *that contract's*
settlement range, which is sensible for a future worth half its range and
absurd for an option worth one percent of it: a full inventory in
`SPIKE_C4600` would move its quote by 540, against a fair value near 70. Here
the chain's net delta shifts the underlying anchor and the whole ladder
reprices from the shifted anchor, so inventory control happens in the units the
risk is actually denominated in -- and the ladder stays consistent while it
happens.
"""

from __future__ import annotations

from scipy.special import betainc

from arena.agents.market_maker import MarketMaker
from arena.agents.base import TradePrint
from arena.contracts.payoff import Call, Linear, Put
from arena.contracts.spec import ObservationWindow
from arena.contracts.underlying import Underlying
from arena.determinism import canonical_json
from arena.exchange.types import AgentId
from arena.market.instrument import Instrument
from arena.sim.kernel import SimulationContext
from arena.sim.time import Duration, millis

__all__ = ["SurfaceMarketMaker", "ChainMember", "derive_chains", "option_value", "call_delta"]


def _key(underlying: Underlying, window: ObservationWindow) -> str:
    return canonical_json(
        {"underlying": underlying.to_dict(), "window": window.to_dict()}
    )


class ChainMember:
    """One option, and the future whose price it is quoted off."""

    __slots__ = ("underlying_symbol", "strike", "scale", "is_call")

    def __init__(self, underlying_symbol: str, strike: float, scale: float, is_call: bool):
        self.underlying_symbol = underlying_symbol
        self.strike = strike
        self.scale = scale
        self.is_call = is_call


def derive_chains(instruments: dict[str, Instrument]) -> dict[str, ChainMember]:
    """Match every listed option to the listed future it is written on.

    Read out of the contracts rather than configured, so listing a new strike
    makes it quotable with no code change -- and an option whose underlying
    future is not listed is simply left to the plain maker, because there is
    nothing to anchor it to.
    """
    futures: dict[str, tuple[str, float]] = {}
    for symbol, instrument in sorted(instruments.items()):
        payoff = instrument.spec.payoff
        if instrument.spec.distribution is not None:
            continue
        if isinstance(payoff, Linear) and payoff.offset == 0.0:
            futures[_key(instrument.spec.underlying, instrument.spec.window)] = (
                symbol,
                payoff.scale,
            )

    chain: dict[str, ChainMember] = {}
    for symbol, instrument in sorted(instruments.items()):
        payoff = instrument.spec.payoff
        if not isinstance(payoff, (Call, Put)):
            continue
        found = futures.get(_key(instrument.spec.underlying, instrument.spec.window))
        if found is None or found[1] != payoff.scale:
            continue
        chain[symbol] = ChainMember(
            underlying_symbol=found[0],
            strike=payoff.strike,
            scale=payoff.scale,
            is_call=isinstance(payoff, Call),
        )
    return chain


def call_delta(forward: float, strike: float, scale: float, concentration: float) -> float:
    """P(the option finishes in the money), which is also dC/dF.

    Zero or one outside the metric's range: a call struck above the largest
    value the metric can take can never pay, and there is no distribution to
    consult about it.
    """
    mean = forward / scale
    if not 0.0 < mean < 1.0:
        return 1.0 if forward > strike else 0.0
    threshold = strike / scale
    if threshold <= 0.0:
        return 1.0
    if threshold >= 1.0:
        return 0.0
    a, b = concentration * mean, concentration * (1.0 - mean)
    return float(1.0 - betainc(a, b, threshold))


def option_value(
    forward: float, strike: float, scale: float, concentration: float, is_call: bool
) -> float:
    """``E[(F - K)+]`` for a call, and its parity partner for a put.

    The underlying is a proportion in [0, 1] scaled onto a price grid, so the
    law is a Beta with the given mean and concentration. The truncated mean has
    a closed form: for ``L ~ Beta(a, b)`` and ``k`` in (0, 1),

        E[(L - k)+] = m * (1 - I_k(a + 1, b)) - k * (1 - I_k(a, b))

    where ``I`` is the regularized incomplete beta and ``m = a / (a + b)``.
    It follows from ``L * f_{a,b}(L) = m * f_{a+1,b}(L)``, so no quadrature and
    no simulation is needed -- which matters because this runs on every requote
    of every strike.

    The put is defined by parity rather than integrated separately. Computing
    it independently would leave the two agreeing only up to floating point,
    and the whole purpose of this maker is that they agree exactly.
    """
    mean = forward / scale
    threshold = strike / scale
    if not 0.0 < mean < 1.0 or threshold >= 1.0 or threshold <= 0.0:
        # Degenerate: no distribution to speak of, so fall back on intrinsic
        # value, which is the correct limit and never negative.
        call = max(0.0, forward - strike)
    else:
        a, b = concentration * mean, concentration * (1.0 - mean)
        above_mass = 1.0 - float(betainc(a, b, threshold))
        above_mean = mean * (1.0 - float(betainc(a + 1.0, b, threshold)))
        call = scale * (above_mean - threshold * above_mass)
        call = max(0.0, call)
    if is_call:
        return call
    return call - (forward - strike)


class SurfaceMarketMaker(MarketMaker):
    """The plain maker, with its option books priced off one distribution."""

    def __init__(
        self,
        agent_id: AgentId,
        venue_id: AgentId,
        instruments: dict[str, Instrument],
        wake_interval: Duration = millis(300),
        vol_weight: float = 0.02,
        min_prints: int = 25,
        skew_sigmas: float = 1.0,
        delta_limit: float | None = None,
        **kwargs,
    ) -> None:
        super().__init__(agent_id, venue_id, instruments, wake_interval, **kwargs)
        self.chain = derive_chains(instruments)
        # How quickly the variance estimate forgets. Slower than the anchor's
        # own weight on purpose: a mean can be tracked from a dozen prints, a
        # variance cannot, and a jumpy width would move every strike at once.
        self.vol_weight = vol_weight
        # Below this many prints the estimate is noise, and the chain is left
        # to the plain maker rather than quoted off a number that is not one.
        self.min_prints = min_prints
        # How far a full inventory shades the forward, in standard deviations
        # of the underlying's own realised dispersion.
        #
        # Not a fraction of the settlement range, which is what the plain maker
        # uses and what this class first copied. A range-sized skew is sensible
        # for a future worth half its range and ruinous for an option worth one
        # percent of it: a net delta of 66 lots shifted the forward by 165 and
        # every call in the chain went to zero while the puts tripled. In
        # dispersion units the same inventory shades the forward by about one
        # standard deviation, which is a statement a trader would recognise.
        self.skew_sigmas = skew_sigmas
        # Net delta, in underlying-equivalent lots, at which that skew is
        # reached. Defaults to a full position in the underlying itself.
        self.delta_limit = float(delta_limit or self.position_limit)
        self._variance: dict[str, float] = {}
        self._prints: dict[str, int] = {}

    # -- what the tape says the width is -----------------------------------

    def on_print(self, ctx: SimulationContext, print_: TradePrint) -> None:
        """Track dispersion around the anchor, then let the anchor move.

        Order matters. Measuring the deviation *before* the anchor absorbs this
        print is what makes it a deviation; afterwards the anchor has already
        moved toward the print and the estimate shrinks toward zero on exactly
        the days it should be growing.
        """
        anchor = self._anchor.get(print_.symbol)
        if anchor is not None:
            deviation = float(int(print_.price)) - anchor
            current = self._variance.get(print_.symbol, deviation * deviation)
            self._variance[print_.symbol] = current + self.vol_weight * (
                deviation * deviation - current
            )
            self._prints[print_.symbol] = self._prints.get(print_.symbol, 0) + 1
        super().on_print(ctx, print_)

    def dispersion_for(self, symbol: str) -> float | None:
        """Realised standard deviation of prints in ``symbol``, in price units.

        ``None`` until there is enough tape to say anything, which is the
        honest answer at the open.
        """
        if self._prints.get(symbol, 0) < self.min_prints:
            return None
        variance = self._variance.get(symbol)
        if not variance or variance <= 0.0:
            return None
        return variance**0.5 * float(self.instruments[symbol].tick_size)

    def concentration_for(self, symbol: str, forward: float, scale: float) -> float | None:
        """Beta concentration implied by that dispersion."""
        sigma = self.dispersion_for(symbol)
        if sigma is None:
            return None
        level = forward / scale
        if not 0.0 < level < 1.0 or sigma <= 0.0:
            return None
        # Moment matching: Var[S*L] = S^2 * m(1-m)/(kappa+1).
        concentration = level * (1.0 - level) * scale * scale / (sigma * sigma) - 1.0
        # A concentration at or below zero is not a distribution. It means the
        # tape is wider than a rate on [0, 1] can be, which is a broken market
        # rather than a very uncertain one.
        return concentration if concentration > 1.0 else None

    # -- the surface -------------------------------------------------------

    def _underlying_anchor(self, symbol: str) -> float | None:
        """Where the underlying is trading now, in price units.

        The **live mid** of the future's book, which is a different choice from
        the one the plain maker makes for its own quotes and deliberately so.
        That maker anchors on prints because it is both sides of its own mid,
        so quoting around the mid would be quoting around itself and the price
        could never move. An option is not in that loop: this maker's option
        quotes do not touch the future's book, so the future's mid is an
        outside price as far as the chain is concerned.

        It also matters that it is fast. Anchoring the chain on the slow print
        average was measured and was clearly wrong: the underlying converges
        from its opening reference to fair value within seconds, and a chain
        priced off a stale forward showed put-call parity out by 715 while
        every quote in it was internally exact. The maker was consistent with a
        price that no longer existed.

        Falls back to prints, then to the opening reference, so a book that has
        not opened still gets a chain rather than nothing.
        """
        book = self.books[symbol]
        ticks = book.mid if book.mid is not None and book.updated_at else None
        if ticks is None:
            ticks = self._anchor.get(symbol, self.reference.get(symbol))
        if ticks is None:
            return None
        return float(ticks) * float(self.instruments[symbol].tick_size)

    def _net_delta(self, underlying_symbol: str, forward: float) -> float:
        """Delta of everything held on this underlying, in future-equivalents."""
        total = float(self.position.get(underlying_symbol, 0))
        for symbol, member in self.chain.items():
            if member.underlying_symbol != underlying_symbol:
                continue
            held = self.position.get(symbol, 0)
            if not held:
                continue
            concentration = self.concentration_for(
                underlying_symbol, forward, member.scale
            )
            if concentration is None:
                continue
            delta = call_delta(forward, member.strike, member.scale, concentration)
            total += held * (delta if member.is_call else delta - 1.0)
        return total

    def _requote(self, ctx: SimulationContext, symbol: str) -> None:
        member = self.chain.get(symbol)
        if member is None:
            super()._requote(ctx, symbol)
            return

        forward = self._underlying_anchor(member.underlying_symbol)
        if forward is None:
            super()._requote(ctx, symbol)
            return
        concentration = self.concentration_for(
            member.underlying_symbol, forward, member.scale
        )

        # Inventory moves the underlying, and the whole ladder follows. Skewing
        # each strike on its own would be the defect this class exists to fix,
        # only arriving through a different door.
        sigma = self.dispersion_for(member.underlying_symbol)
        if sigma is None:
            shifted = forward
        else:
            pull = self._net_delta(member.underlying_symbol, forward) / self.delta_limit
            shifted = forward - max(-1.0, min(1.0, pull)) * self.skew_sigmas * sigma

        instrument = self.instruments[symbol]
        if concentration is None:
            # Not enough tape to have a view on width, so quote intrinsic: the
            # zero-volatility limit, and the one answer that needs no estimate.
            # Falling back to the plain maker here was worse than doing nothing
            # -- it anchored each strike at the middle of its own settlement
            # range, so `SPIKE_C4700` opened near 2,650 against a fair value
            # under 20 and put-call parity was out by a thousand. Intrinsic is
            # wrong about time value and right about everything else, including
            # being monotone and convex across strikes.
            fair = max(0.0, shifted - member.strike)
            if not member.is_call:
                fair = max(0.0, member.strike - shifted)
        else:
            fair = option_value(
                shifted, member.strike, member.scale, concentration, member.is_call
            )
        self._quote_around(ctx, symbol, fair / float(instrument.tick_size))

    def _quote_around(self, ctx: SimulationContext, symbol: str, fair_ticks: float) -> None:
        """Post a two-sided quote around a fair value already in ticks.

        Split out of the plain maker's requote so the two share the widening
        rule and the position-limit handling. What differs between them is only
        where the middle comes from.
        """
        from arena.exchange.types import Price, Side, TimeInForce

        inventory = self.position.get(symbol, 0)
        self.cancel_all(ctx, symbol)

        low, high = self.instruments[symbol].tick_bounds
        pressure = abs(inventory) / max(1, self.position_limit)
        half = self.half_spread * (1.0 + 2.0 * pressure)

        # Clamped into the contract's own range. A quote outside it is one the
        # venue would refuse to collateralise, and an option's fair value can
        # sit within a hair of zero.
        centre = min(max(fair_ticks, float(int(low)) + half), float(int(high)) - half)
        bid = Price(int(centre - half))
        ask = Price(int(centre + half) + 1)

        if inventory < self.position_limit:
            size = min(self.quote_size, self.position_limit - inventory)
            self.quote(ctx, symbol, Side.BUY, bid, size, TimeInForce.GTC)
        if -inventory < self.position_limit:
            size = min(self.quote_size, self.position_limit + inventory)
            self.quote(ctx, symbol, Side.SELL, ask, size, TimeInForce.GTC)
