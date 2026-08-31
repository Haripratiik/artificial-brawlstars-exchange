"""Market-making strategies, in order of how much they assume.

``FixedSpread``       a constant half-spread skewed by inventory. The baseline,
                      and roughly what the makers in this repository already do.
``AvellanedaStoikov`` inventory-optimal quoting: the spread is independent of
                      inventory and the *centre* moves instead.
``GueantLehalleFT``   the closed-form solution with a hard inventory bound and
                      an additive term for measured adverse selection.
``GlostenMilgrom``    quotes conditioned on the order that just arrived, which
                      is the only one of the four in which being filled is news.
"""

from arena.strategies.making.avellaneda import AvellanedaStoikov
from arena.strategies.making.fixed import FixedSpread
from arena.strategies.making.glosten import GlostenMilgrom
from arena.strategies.making.gueant import GueantLehalleFT

__all__ = ["AvellanedaStoikov", "FixedSpread", "GlostenMilgrom", "GueantLehalleFT"]
