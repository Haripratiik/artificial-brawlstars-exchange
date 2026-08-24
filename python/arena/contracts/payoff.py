"""How an underlying level becomes a settlement value.

Payoffs are deliberately boring: a linear map for futures and spreads, a step
function for event contracts. Anything more exotic -- options above all -- is a
function of a *traded future*, not of the raw measured metric, so it belongs to
a later milestone and a different module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

__all__ = ["Payoff", "Linear", "Binary"]

_COMPARISONS = {
    ">": lambda level, threshold: level > threshold,
    ">=": lambda level, threshold: level >= threshold,
    "<": lambda level, threshold: level < threshold,
    "<=": lambda level, threshold: level <= threshold,
}


class Payoff(ABC):
    @abstractmethod
    def apply(self, level: float) -> float:
        """Map an underlying level to a settlement value, before tick rounding."""

    @abstractmethod
    def bounds(self, level_bounds: tuple[float, float]) -> tuple[float, float]:
        """The range of settlement values, given the underlying's range.

        This is what makes collateral exact. Every instrument here settles
        inside a known interval, so a position's worst case is arithmetic
        rather than a value-at-risk estimate -- which is not true of an
        ordinary future on an unbounded price.
        """

    @abstractmethod
    def to_dict(self) -> dict[str, Any]:
        """Canonical form, which feeds the contract spec digest."""


@dataclass(frozen=True, slots=True)
class Linear(Payoff):
    """``scale * level + offset``.

    The scale exists to move a rate in the 0-1 range onto a price grid with
    useful tick resolution. A win rate of 0.5537 at scale 10000 settles at
    5537, so one tick is a quotable amount rather than a rounding artifact.
    """

    scale: float
    offset: float = 0.0

    def apply(self, level: float) -> float:
        return self.scale * level + self.offset

    def bounds(self, level_bounds: tuple[float, float]) -> tuple[float, float]:
        # A negative scale is a legitimate inverse contract, and it flips the
        # interval. Assuming the low bound maps to the low settlement would
        # silently invert the collateral requirement on such a contract.
        ends = (self.apply(level_bounds[0]), self.apply(level_bounds[1]))
        return (min(ends), max(ends))

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "linear", "scale": self.scale, "offset": self.offset}


@dataclass(frozen=True, slots=True)
class Binary(Payoff):
    """``payout`` if the comparison holds at expiry, otherwise zero.

    The price of such a contract is often read as a probability. Whether that
    reading survives contact with data is one of the questions this project
    exists to test, so nothing here enforces a 0-1 price range or treats the
    price as a probability. That interpretation has to be earned empirically.
    """

    comparison: str
    threshold: float
    payout: float = 1.0

    def __post_init__(self) -> None:
        if self.comparison not in _COMPARISONS:
            raise ValueError(
                f"comparison must be one of {sorted(_COMPARISONS)}, got {self.comparison!r}"
            )

    def apply(self, level: float) -> float:
        holds = _COMPARISONS[self.comparison](level, self.threshold)
        return self.payout if holds else 0.0

    def bounds(self, level_bounds: tuple[float, float]) -> tuple[float, float]:
        """Always zero to payout, regardless of the underlying's range.

        A binary discards everything about the level except which side of the
        threshold it fell on, so its settlement range does not depend on how
        wide the underlying's range is. Even a threshold outside the
        underlying's range keeps this interval: the contract is then certain to
        settle at one end, but it is still *defined* over both.
        """
        return (min(0.0, self.payout), max(0.0, self.payout))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "binary",
            "comparison": self.comparison,
            "threshold": self.threshold,
            "payout": self.payout,
        }
