"""Buy-side strategies: the ones that think the price on the screen is wrong.

``KellyBayesian``  sizes a belief against a price. On this venue the stake and
                   the collateral are the same number, which is Kelly's own
                   setup rather than an analogy.
``StaticArbitrage`` trades relations that must hold at settlement whatever the
                   outcome, so a loss is an execution failure and not a wrong view.
"""

from arena.strategies.taking.arbitrage import StaticArbitrage
from arena.strategies.taking.kelly import KellyBayesian

__all__ = ["KellyBayesian", "StaticArbitrage"]
