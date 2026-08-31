"""Strategies you can write, run and measure without real money or real data.

A strategy reads a :class:`~arena.strategies.base.MarketView` and returns
intents. It never touches the venue, so it cannot see another participant's
position or the settlement level, and a result it produces is one you are
entitled to believe. :class:`~arena.agents.strategy_agent.StrategyAgent` is the
only thing that turns intents into orders.

The library here is the baseline set, not a recommendation: each one is a named
model from the literature so that a strategy you write has something honest to
be compared against.
"""

from arena.strategies.base import (
    MakerStrategy,
    MarketView,
    Quote,
    SymbolView,
    Take,
    TakerStrategy,
    TwoSided,
    snap,
)
from arena.strategies.firm import Firm

__all__ = [
    "Firm",
    "MakerStrategy",
    "MarketView",
    "Quote",
    "SymbolView",
    "Take",
    "TakerStrategy",
    "TwoSided",
    "snap",
]
