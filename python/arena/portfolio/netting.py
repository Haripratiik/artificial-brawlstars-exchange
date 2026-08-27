"""What a portfolio can lose, exactly.

Collateral here is charged per contract. An account holding a long future and a
short call on the same Brawler posts against the worst case of each separately,
as though the world could be simultaneously terrible for both -- and it cannot,
because both are functions of the same number. The arbitrageur feels this most:
its whole business is holding offsetting packages, and it pays collateral on
every leg of every one of them.

Netting them is what a clearing house is for. The usual objection is that
portfolio margining means a risk *model*, and a model is an estimate, and an
estimate is exactly what this project's collateral is not. That objection does
not apply here, and the reason is the same one that makes single-contract
collateral exact: every instrument settles as a known function of a bounded
scalar. So the worst case of a portfolio is

    min over level in [0, 1] of  sum_i quantity_i * payoff_i(level)

which is a minimisation of a **piecewise-linear function of one bounded
variable**. Its minimum is attained at an endpoint or at a kink, every kink is
known in advance -- a call's strike, a put's strike, a binary's threshold -- and
there are a handful of them. Evaluating at each is not an approximation of the
answer. It is the answer.

Two consequences worth being explicit about:

* **Only same-underlying positions net.** A future on SPIKE and a future on
  CROW are functions of *different* numbers, and nothing here knows how those
  two move together. Netting them would require a correlation, which would be
  an estimate, and then the whole guarantee is gone. They are collateralised
  separately, and that is not a limitation to be fixed later -- it is the line
  between arithmetic and modelling.
* **Netting can only ever reduce the requirement**, never raise it, because the
  gross figure is the sum of per-contract worst cases and the net is the worst
  case of the sum. Anything else would be an arithmetic error.
"""

from __future__ import annotations

from decimal import Decimal

from arena.contracts.payoff import Binary, Call, Linear, Payoff, Put
from arena.contracts.spec import ContractSpec

__all__ = ["kinks_of", "worst_case", "netting_benefit"]


def kinks_of(payoff: Payoff, bounds: tuple[float, float]) -> list[float]:
    """Levels where this payoff changes slope, inside ``bounds``.

    A linear payoff has none. An option has one, at its strike. A binary's step
    is not a kink but a jump, and its worst point is on the *far* side of the
    threshold -- so both sides are offered as candidates and the caller
    evaluates each.
    """
    low, high = bounds
    found: list[float] = []

    if isinstance(payoff, (Call, Put)):
        if payoff.scale:
            found.append(payoff.strike / payoff.scale)
    elif isinstance(payoff, Binary):
        # A step: the value differs on either side of the threshold and there
        # is no level at which it is between them. Both neighbours are offered
        # because which of them is adverse depends on the sign of the position,
        # and the caller does not have to know which.
        found.extend((payoff.threshold, payoff.threshold * (1 + 1e-12) + 1e-12))
    elif not isinstance(payoff, Linear):
        # An unknown payoff shape could kink anywhere. Rather than guess, sample
        # the interval finely enough that the answer is conservative by a
        # rounding error instead of wrong by an unknown amount.
        found.extend(low + (high - low) * n / 64.0 for n in range(1, 64))

    return [level for level in found if low <= level <= high]


def worst_case(
    holdings: list[tuple[ContractSpec, int, Decimal]],
) -> Decimal:
    """The most this portfolio can lose, over every level the metric can take.

    ``holdings`` is ``(spec, signed quantity, price paid)``. Every spec must be
    written on the same underlying; grouping is the caller's job, because only
    the caller knows what "the same underlying" means for its world.

    Returns a non-negative loss. Zero means the portfolio cannot lose anything
    at any level, which a fully hedged package genuinely cannot.
    """
    if not holdings:
        return Decimal(0)

    bounds = holdings[0][0].underlying.bounds()
    candidates = {float(bounds[0]), float(bounds[1])}
    for spec, _quantity, _price in holdings:
        candidates.update(kinks_of(spec.payoff, bounds))

    worst = Decimal(0)
    for level in sorted(candidates):
        value = Decimal(0)
        for spec, quantity, price in holdings:
            settlement = Decimal(str(spec.claim_value(level)))
            value += Decimal(quantity) * (settlement - price)
        worst = min(worst, value)
    return -worst


def netting_benefit(
    holdings: list[tuple[ContractSpec, int, Decimal]],
) -> tuple[Decimal, Decimal]:
    """``(gross, net)`` collateral for the same holdings, for reporting.

    Gross is what charging each contract separately costs; net is what the
    portfolio can actually lose. The difference is the capital a clearing house
    hands back, and it is worth being able to quote rather than assert.
    """
    gross = Decimal(0)
    for spec, quantity, price in holdings:
        gross += spec.collateral_for(quantity, price)
    return gross, worst_case(holdings)
