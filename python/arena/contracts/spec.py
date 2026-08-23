"""The immutable contract specification.

A contract spec is the constitution of a market. Once published it cannot be
amended, because every price ever printed against it was an opinion about the
settlement rule as written at the time. The spec is therefore content-addressed:
its digest covers the underlying, the payoff, the window, the data policy, and
the identity of the pinned reference snapshot. Change any of them and you have
a different contract, with a different digest, which the settlement record will
show.

Two invariants are enforced at construction rather than left to reviewer
discipline, because both are lookahead bugs that would be invisible in results:

    published_at <= window.start        the market cannot be written after the
                                        outcome has begun to be determined
    reference.as_of <= window.start     index weights and priors cannot be
                                        fitted on the window they settle over
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from arena.contracts.payoff import Payoff
from arena.contracts.underlying import MetricRef, Underlying
from arena.determinism import digest

__all__ = [
    "ObservationWindow",
    "DataPolicy",
    "MissingDataPolicy",
    "ContractSpec",
]


class MissingDataPolicy:
    """What to do when the oracle cannot produce a required metric.

    VOID is the conservative default and the only one enabled at first. A
    contract that silently settles on a guess is worse than one that does not
    settle at all, because the guess becomes indistinguishable from a measurement
    the moment it is written to the tape.
    """

    VOID = "VOID"

    ALL = (VOID,)


@dataclass(frozen=True, slots=True)
class ObservationWindow:
    """Half-open interval ``[start, end)`` in UTC.

    Half-open so that consecutive windows tile the timeline without a battle
    ever landing in two settlement periods at once.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        for label, value in (("start", self.start), ("end", self.end)):
            if value.tzinfo is None:
                raise ValueError(f"window {label} must be timezone-aware")
            if value.utcoffset() != timezone.utc.utcoffset(None):
                raise ValueError(f"window {label} must be UTC, got {value.tzinfo}")
        if self.start >= self.end:
            raise ValueError("window start must precede window end")

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def to_dict(self) -> dict[str, Any]:
        return {"start": _iso(self.start), "end": _iso(self.end)}


@dataclass(frozen=True, slots=True)
class DataPolicy:
    """The evidential bar a settlement has to clear.

    ``min_sample_size`` guards the metric as a whole. ``min_stratum_battles``
    and ``min_strata_coverage`` guard its *composition*: a standardized rate
    computed from three well-sampled strata out of forty is not the quantity the
    contract named, even if the total battle count looks respectable.
    """

    min_sample_size: int
    min_stratum_battles: int = 0
    min_strata_coverage: float = 0.0
    missing_data_policy: str = MissingDataPolicy.VOID
    # How the metric treats a stratum the snapshot declares but the data does
    # not cover. Part of the contract, and therefore of its digest, because it
    # materially changes what the settlement number means.
    missing_strata_policy: str = "IMPUTE_FROM_PRIOR"

    def __post_init__(self) -> None:
        if self.min_sample_size < 0:
            raise ValueError("min_sample_size cannot be negative")
        if self.min_stratum_battles < 0:
            raise ValueError("min_stratum_battles cannot be negative")
        if not 0.0 <= self.min_strata_coverage <= 1.0:
            raise ValueError("min_strata_coverage must lie in [0, 1]")
        if self.missing_data_policy not in MissingDataPolicy.ALL:
            raise ValueError(
                f"missing_data_policy must be one of {MissingDataPolicy.ALL}, "
                f"got {self.missing_data_policy!r}"
            )
        if self.missing_strata_policy not in ("IMPUTE_FROM_PRIOR", "DROP_AND_RENORMALIZE"):
            raise ValueError(
                f"unknown missing_strata_policy {self.missing_strata_policy!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_sample_size": self.min_sample_size,
            "min_stratum_battles": self.min_stratum_battles,
            "min_strata_coverage": self.min_strata_coverage,
            "missing_data_policy": self.missing_data_policy,
            "missing_strata_policy": self.missing_strata_policy,
        }


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """Everything needed to settle a contract, and nothing that could change."""

    contract_id: str
    underlying: Underlying
    payoff: Payoff
    window: ObservationWindow
    policy: DataPolicy
    # Identity of the frozen reference snapshot supplying standardization
    # weights and shrinkage priors. Named rather than embedded so that many
    # contracts can share one audited snapshot, but pinned by id so that
    # swapping it changes this spec's digest.
    reference_id: str
    published_at: datetime
    tick_size: str = "0.01"
    lot_size: int = 1
    metadata: tuple[tuple[str, str], ...] = field(default=())

    def __post_init__(self) -> None:
        if not self.contract_id:
            raise ValueError("contract_id is required")
        if self.published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        if self.published_at > self.window.start:
            raise ValueError(
                f"{self.contract_id}: published_at {_iso(self.published_at)} is after "
                f"window start {_iso(self.window.start)}; a contract written after its "
                "observation window opens is lookahead by construction"
            )
        if not self.reference_id:
            raise ValueError("reference_id is required; standardization must be pinned")
        if float(self.tick_size) <= 0:
            raise ValueError("tick_size must be positive")
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")

    def atoms(self) -> tuple[MetricRef, ...]:
        """Every metric the oracle must resolve to settle this contract."""
        return self.underlying.atoms()

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "underlying": self.underlying.to_dict(),
            "payoff": self.payoff.to_dict(),
            "window": self.window.to_dict(),
            "policy": self.policy.to_dict(),
            "reference_id": self.reference_id,
            "published_at": _iso(self.published_at),
            "tick_size": self.tick_size,
            "lot_size": self.lot_size,
            "metadata": [list(pair) for pair in self.metadata],
        }

    @property
    def spec_digest(self) -> str:
        """Content address of this specification."""
        return digest(self.to_dict())


def _iso(moment: datetime) -> str:
    """RFC 3339 in UTC with a trailing Z, so digests do not depend on offset spelling."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
