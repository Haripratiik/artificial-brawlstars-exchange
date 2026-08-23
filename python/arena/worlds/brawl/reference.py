"""Frozen standardization snapshots.

This module carries the project's answer to its hardest data problem.

The collector cannot draw a representative sample of Brawl Stars players. It
seeds from leaderboards and snowballs through opponents, so its composition is
skewed toward whoever it happened to reach, and that composition *drifts* as the
crawl expands. A raw win rate computed from such a sample would move whenever
the crawler's reach moved, and a contract settling on it would be pricing the
crawler as much as the game.

The fix is standardization. Rather than reporting the sample's own average, we
report what the average *would have been* had the strata appeared in fixed,
pre-declared proportions:

    adjusted = sum_s omega_s * p_s     with omega pinned before the window opens

Now composition drift cancels. What remains is the thing the contract meant.

**Nothing in a snapshot is hardcoded.** Weights, priors, and prior strength are
all estimated from data that was knowable at ``as_of`` -- see
:mod:`arena.worlds.brawl.estimation`. A snapshot is immutable once created, but
the *series* of snapshots is re-derived as the game moves, so the settlement
rule tracks the metagame without any individual contract's rule ever changing
under it. A contract pins one snapshot id; the engine refuses to settle it with
any other, and refuses to settle it with a snapshot dated after its window
opened.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from arena.determinism import digest, digest_of
from arena.worlds.brawl.schema import StratumKey

__all__ = ["ReferenceSnapshot", "load_reference", "save_reference"]

_ISO = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True, slots=True)
class ReferenceSnapshot:
    """Immutable standardization weights, hierarchical priors, and shrinkage strength."""

    reference_id: str
    as_of: datetime
    # Weight per stratum, normally that stratum's share of observed play volume.
    # Need not sum to 1: the metric renormalizes over whichever strata are in
    # play and reports the coverage it achieved.
    weights: tuple[tuple[str, float], ...]
    # Neutral win rate per stratum -- the shrinkage target. Estimated from the
    # pooled performance of every brawler in the stratum, then partially pooled
    # toward the mode. This is what lets a high-trophy Showdown cell shrink
    # toward something different than a low-trophy one.
    stratum_priors: tuple[tuple[str, float], ...]
    # Fallback for a stratum absent from the estimation window, and the target
    # the stratum priors were pooled toward. Lands near 0.5 for symmetric team
    # modes and near 0.4 for a top-four-of-ten mode without being told either.
    mode_priors: tuple[tuple[str, float], ...]
    # Prior strength in pseudo-battles, from method of moments on the
    # beta-binomial. A stratum with this many observations is weighted equally
    # between its own rate and its prior.
    prior_strength: float
    # Digest of the DATASET this snapshot was estimated from. Part of the
    # snapshot's content, because "which data produced these weights" is
    # provenance a settlement may need to defend years later.
    source_digest: str | None = None
    # How this snapshot was derived. Part of the digest, so a snapshot fitted
    # differently is a different snapshot even if the numbers coincide.
    estimation: tuple[tuple[str, Any], ...] = field(default=())
    # Digest of the JSON FILE these values were read from. A different fact
    # entirely from source_digest, and deliberately excluded from to_dict and
    # from equality: it describes the container, not the content, so a snapshot
    # loaded from disk must compare equal to the one that was written.
    file_digest: str | None = field(default=None, compare=False, repr=False)

    _weight_index: dict[str, float] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )
    _stratum_prior_index: dict[str, float] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )
    _mode_prior_index: dict[str, float] = field(
        init=False, repr=False, compare=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        if not self.reference_id:
            raise ValueError("reference_id is required")
        if self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        if self.prior_strength < 0:
            raise ValueError("prior_strength cannot be negative")
        if not self.weights:
            raise ValueError("a reference snapshot needs at least one stratum weight")

        seen: set[str] = set()
        for key, weight in self.weights:
            if weight < 0:
                raise ValueError(f"weight for {key} is negative")
            if key in seen:
                raise ValueError(f"duplicate stratum weight for {key}")
            seen.add(key)

        for label, table in (("stratum", self.stratum_priors), ("mode", self.mode_priors)):
            for key, prior in table:
                if not 0.0 <= prior <= 1.0:
                    raise ValueError(
                        f"{label} prior for {key} must lie in [0, 1], got {prior}"
                    )

        object.__setattr__(self, "_weight_index", dict(self.weights))
        object.__setattr__(self, "_stratum_prior_index", dict(self.stratum_priors))
        object.__setattr__(self, "_mode_prior_index", dict(self.mode_priors))

    # -- lookups -----------------------------------------------------------

    def weight_for(self, stratum: StratumKey) -> float:
        """Standardization weight, or 0.0 if the stratum is out of universe.

        A zero weight is meaningful: it says the snapshot deliberately excludes
        this cell, so observing it must not move the metric.
        """
        return self._weight_index.get(stratum.key, 0.0)

    def prior_for(self, stratum: StratumKey) -> float:
        """Shrinkage target for a stratum: its own prior, else its mode's.

        The fallback matters. A map added after the estimation window has no
        stratum prior, but its mode does, and shrinking toward the mode is far
        better than refusing to settle or guessing 0.5.
        """
        prior = self._stratum_prior_index.get(stratum.key)
        if prior is not None:
            return prior
        prior = self._mode_prior_index.get(stratum.mode_id)
        if prior is not None:
            return prior
        raise KeyError(
            f"reference {self.reference_id} has no prior for stratum {stratum.key!r} "
            f"nor for its mode {stratum.mode_id!r}; shrinking toward a guessed neutral "
            "point would silently bias every thin cell in the same direction"
        )

    @property
    def strata(self) -> tuple[StratumKey, ...]:
        """Every stratum the snapshot declares, in canonical order.

        The metric iterates this rather than the observed rows, so a stratum
        with no data still participates -- shrinking fully to its prior instead
        of being dropped. That is what removes the missing-at-random assumption
        from the coverage calculation.
        """
        return tuple(_parse_stratum(key) for key, _weight in sorted(self.weights))

    @property
    def total_weight(self) -> float:
        return sum(weight for _key, weight in self.weights)

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "as_of": self.as_of.astimezone(timezone.utc).strftime(_ISO),
            "prior_strength": self.prior_strength,
            "weights": dict(self.weights),
            "stratum_priors": dict(self.stratum_priors),
            "mode_priors": dict(self.mode_priors),
            "source_digest": self.source_digest,
            "estimation": [list(pair) for pair in self.estimation],
        }

    @property
    def snapshot_digest(self) -> str:
        return digest(self.to_dict())


def _parse_stratum(key: str) -> StratumKey:
    parts = key.split("/")
    if len(parts) != 3:
        raise ValueError(
            f"malformed stratum key {key!r}; expected 'mode/map/trophy_bucket'"
        )
    return StratumKey(parts[0], parts[1], parts[2])


def load_reference(path: str | Path) -> ReferenceSnapshot:
    """Load a frozen snapshot from JSON, recording a digest of the bytes read."""
    payload = Path(path).read_bytes()
    raw = json.loads(payload)
    return ReferenceSnapshot(
        reference_id=raw["reference_id"],
        as_of=datetime.strptime(raw["as_of"], _ISO).replace(tzinfo=timezone.utc),
        weights=tuple(sorted((str(k), float(v)) for k, v in raw["weights"].items())),
        stratum_priors=tuple(
            sorted((str(k), float(v)) for k, v in raw.get("stratum_priors", {}).items())
        ),
        mode_priors=tuple(
            sorted((str(k), float(v)) for k, v in raw.get("mode_priors", {}).items())
        ),
        prior_strength=float(raw["prior_strength"]),
        source_digest=raw.get("source_digest"),
        estimation=tuple((str(k), v) for k, v in raw.get("estimation", [])),
        # The claim "these exact bytes were on disk". Kept alongside, never
        # confused with, the digest of the data the snapshot was fitted on.
        file_digest=digest_of(payload),
    )


def save_reference(snapshot: ReferenceSnapshot, path: str | Path) -> None:
    """Write a snapshot to JSON. Refuses to overwrite an existing snapshot.

    Snapshots are immutable by contract. Overwriting one would silently change
    the settlement rule of every contract that pinned it, so a new estimate
    must be given a new id and a new file.
    """
    location = Path(path)
    if location.exists():
        raise FileExistsError(
            f"{location} already exists. Reference snapshots are immutable -- "
            "contracts pin them by id and settle against them years later. "
            "Give the new estimate a new reference_id instead of editing this one."
        )
    location.parent.mkdir(parents=True, exist_ok=True)
    location.write_text(json.dumps(snapshot.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
