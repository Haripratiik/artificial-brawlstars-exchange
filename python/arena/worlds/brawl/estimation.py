"""Deriving a reference snapshot from data instead of inventing one.

A settlement rule has to be frozen, or prices printed against it meant nothing.
But a *hardcoded* rule is worse than frozen -- it is arbitrary, and it stops
describing the game the moment the game moves. The resolution is that snapshots
are immutable individually and re-derived as a series: `ref-2026S09` is
estimated from data strictly before the windows it settles, `ref-2026S10` from
later data, and neither is ever edited.

Everything a snapshot contains is estimated here.

**Weights** are play volume. A stratum's standardization weight is its share of
observed brawler slots:

    omega_s = slots_s / sum_j slots_j

so "adjusted win rate" means "the rate this brawler would post if the game were
played in the proportions it was actually played in during the estimation
window". That is a statement anyone can check, unlike a hand-chosen constant.

**Priors** are hierarchical. Each stratum's neutral win rate is estimated from
the pooled performance of every brawler in it, then partially pooled toward its
mode's rate so a thin stratum borrows strength rather than inventing its own
baseline. This is what makes a high-trophy Showdown cell shrink toward
something different than a low-trophy one.

**Prior strength** comes from method of moments, the standard empirical-Bayes
estimator for a beta-binomial. Modelling cell c as

    theta_c ~ Beta(mean m_c, strength kappa),   x_c ~ Binomial(n_c, theta_c)

and writing A_c = m_c(1 - m_c), the second moment about the prior mean is

    E[(p_hat_c - m_c)^2] = (A_c / (kappa + 1)) * (1 + kappa / n_c)

Summing over cells and solving for kappa gives a closed form:

    kappa = (sum A_c - S) / (S - sum A_c/n_c),   S = sum (p_hat_c - m_c)^2

It behaves correctly at the boundary: if the observed spread of cell rates is
fully explained by binomial noise, the denominator collapses to zero and kappa
diverges, meaning "trust none of this variation". No grid search, no held-out
windows required -- though `sweep_prior_strength` provides an out-of-sample
check that the closed form lands near the predictive optimum.

Every estimator here reads only rows that were *knowable* at ``as_of``. That is
the whole point: a snapshot fitted on the window it settles would leak the
future into its own contract.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from arena.determinism import stable_sum
from arena.worlds.brawl.reference import ReferenceSnapshot
from arena.worlds.brawl.schema import AggregateRow, StratumKey

__all__ = [
    "EstimationReport",
    "estimate_weights",
    "estimate_priors",
    "estimate_prior_strength",
    "build_snapshot",
    "sweep_prior_strength",
]

# Bounds on kappa. The lower bound stops a noisy estimation window from
# producing a snapshot that trusts nine-battle cells; the upper bound stops a
# degenerate one from shrinking every cell onto the prior and making the metric
# constant. Both are reported when they bind, because a clamped kappa means the
# estimation window was not informative and someone should look.
KAPPA_MIN = 1.0
KAPPA_MAX = 100_000.0

# Cells thinner than this are excluded from the kappa fit. Method of moments is
# unweighted, so a swarm of tiny cells would let pure binomial noise dominate
# the second moment and bias kappa upward.
DEFAULT_MIN_CELL_BATTLES = 30

# Strength of the pooling of a stratum prior toward its mode prior, in battles.
DEFAULT_PRIOR_POOLING = 5_000.0

# What "play volume" means when weighting strata. See estimate_weights.
WEIGHT_BASIS_SLOTS = "slots"
WEIGHT_BASIS_BATTLES = "battles"
WEIGHT_BASES = (WEIGHT_BASIS_SLOTS, WEIGHT_BASIS_BATTLES)


@dataclass(frozen=True, slots=True)
class EstimationReport:
    """What the estimator saw and decided. Attached to the snapshot as provenance."""

    rows_used: int
    strata: int
    cells_used: int
    kappa: float
    kappa_clamped: bool
    second_moment: float
    binomial_component: float
    between_component: float
    total_slots: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_used": self.rows_used,
            "strata": self.strata,
            "cells_used": self.cells_used,
            "kappa": self.kappa,
            "kappa_clamped": self.kappa_clamped,
            "second_moment": self.second_moment,
            "binomial_component": self.binomial_component,
            "between_component": self.between_component,
            "total_slots": self.total_slots,
        }


def _visible_rows(
    rows: Iterable[AggregateRow], as_of: datetime, lookback: timedelta
) -> list[AggregateRow]:
    """Rows both knowable at ``as_of`` and inside the estimation lookback.

    Two filters, and both are load-bearing:

    ``observed_at <= as_of`` -- we may only fit on data that had actually been
    collected by the snapshot date. Using a row collected later would let the
    snapshot depend on information that did not exist when it claims to.

    ``window_end <= as_of`` -- and only on periods that had finished. A window
    still in progress is a partial count.
    """
    floor = as_of - lookback
    return [
        row
        for row in rows
        if row.observed_at <= as_of and row.window_end <= as_of and row.window_start >= floor
    ]


def estimate_weights(
    rows: Sequence[AggregateRow], basis: str = WEIGHT_BASIS_SLOTS
) -> dict[str, float]:
    """Standardization weights proportional to observed play volume.

    **The basis is a real modelling choice, not a detail.** A Showdown battle
    offers ten brawler slots; a 3v3 offers six. So the two bases answer
    different questions:

        slots    "if you played this brawler at a randomly chosen slot in the
                 game as it was actually played, how often would you win?"
        battles  "...in a randomly chosen battle?"

    Slots is the default because a win rate is measured per appearance, and a
    brawler occupies exactly one slot. But it does weight Showdown roughly
    ten-sixths as heavily per battle as a team mode, and Showdown's baseline
    win rate is far lower, so the choice visibly moves the settlement value.
    Whichever is chosen is recorded in the snapshot's provenance.

    Note the deduplication. ``stratum_slots`` describes the stratum, not the
    brawler, so it repeats identically on every brawler's row for that stratum
    and sub-window. Summing it naively would multiply each stratum's weight by
    however many brawlers happened to be observed in it -- making the weights a
    function of crawl coverage, the exact thing standardization exists to
    remove.
    """
    if basis not in WEIGHT_BASES:
        raise ValueError(f"weight basis must be one of {WEIGHT_BASES}, got {basis!r}")

    volume_by_cell: dict[tuple[str, datetime, datetime], int] = {}
    for row in rows:
        key = (row.stratum.key, row.window_start, row.window_end)
        volume_by_cell[key] = (
            row.stratum_slots if basis == WEIGHT_BASIS_SLOTS else row.stratum_battles
        )

    totals: dict[str, int] = {}
    for (stratum_key, _start, _end), volume in volume_by_cell.items():
        totals[stratum_key] = totals.get(stratum_key, 0) + volume

    grand_total = sum(totals.values())
    if grand_total <= 0:
        raise ValueError(f"cannot estimate weights: no {basis} observed in the window")
    return {key: total / grand_total for key, total in sorted(totals.items())}


def estimate_priors(
    rows: Sequence[AggregateRow], pooling_strength: float = DEFAULT_PRIOR_POOLING
) -> tuple[dict[str, float], dict[str, float]]:
    """Hierarchical neutral win rates: per stratum, pooled toward per mode.

    The mode-level rate is what a symmetric game mechanically pins -- in a 3v3
    exactly half of all appearances are on the winning side, so it lands near
    0.5 without being told to, and a mode scored as "top four of ten" lands
    near 0.4 without being told that either. Estimating rather than asserting
    means the metric self-calibrates to whatever "win" means in each mode.

    The stratum-level rate then captures real deviation -- a map where the
    first team to score usually holds -- and is pooled back toward its mode so
    a thin stratum cannot invent a baseline from a handful of battles.

    Returns ``(stratum_priors, mode_priors)``.
    """
    mode_wins: dict[str, int] = {}
    mode_battles: dict[str, int] = {}
    stratum_wins: dict[str, int] = {}
    stratum_battles: dict[str, int] = {}
    stratum_mode: dict[str, str] = {}

    for row in rows:
        key = row.stratum.key
        stratum_wins[key] = stratum_wins.get(key, 0) + row.brawler_wins
        stratum_battles[key] = stratum_battles.get(key, 0) + row.brawler_battles
        stratum_mode[key] = row.mode_id
        mode_wins[row.mode_id] = mode_wins.get(row.mode_id, 0) + row.brawler_wins
        mode_battles[row.mode_id] = mode_battles.get(row.mode_id, 0) + row.brawler_battles

    mode_priors = {
        mode: mode_wins[mode] / battles
        for mode, battles in sorted(mode_battles.items())
        if battles > 0
    }
    if not mode_priors:
        raise ValueError("cannot estimate priors: no battles observed in the window")

    stratum_priors: dict[str, float] = {}
    for key in sorted(stratum_battles):
        n = stratum_battles[key]
        mode_prior = mode_priors.get(stratum_mode[key])
        if mode_prior is None:
            continue
        if n <= 0:
            stratum_priors[key] = mode_prior
            continue
        observed = stratum_wins[key] / n
        stratum_priors[key] = (n * observed + pooling_strength * mode_prior) / (
            n + pooling_strength
        )

    return stratum_priors, mode_priors


def estimate_prior_strength(
    rows: Sequence[AggregateRow],
    stratum_priors: dict[str, float],
    mode_priors: dict[str, float],
    min_cell_battles: int = DEFAULT_MIN_CELL_BATTLES,
) -> tuple[float, dict[str, float]]:
    """Method-of-moments kappa for the beta-binomial. See the module docstring.

    Returns ``(kappa, diagnostics)``.
    """
    # Collapse to one cell per (brawler, stratum): kappa describes how much a
    # brawler's true rate in a stratum deviates from that stratum's baseline,
    # so sub-windows must be pooled first or the same deviation is counted
    # once per week and kappa comes out far too small.
    cells: dict[tuple[str, str], tuple[int, int]] = {}
    for row in rows:
        key = (row.brawler_id, row.stratum.key)
        battles, wins = cells.get(key, (0, 0))
        cells[key] = (battles + row.brawler_battles, wins + row.brawler_wins)

    second_moment = 0.0
    sum_a = 0.0
    sum_a_over_n = 0.0
    used = 0

    for (_brawler, stratum_key), (battles, wins) in sorted(cells.items()):
        if battles < min_cell_battles:
            continue
        prior = stratum_priors.get(stratum_key)
        if prior is None:
            mode = stratum_key.split("/", 1)[0]
            prior = mode_priors.get(mode)
        if prior is None:
            continue

        observed = wins / battles
        amplitude = prior * (1.0 - prior)
        second_moment += (observed - prior) ** 2
        sum_a += amplitude
        sum_a_over_n += amplitude / battles
        used += 1

    diagnostics = {
        "cells_used": float(used),
        "second_moment": second_moment,
        "binomial_component": sum_a_over_n,
        "between_component": max(second_moment - sum_a_over_n, 0.0),
    }

    if used < 2:
        # Not enough cells to separate real variation from noise. Shrink hard
        # rather than pretend the estimate means something.
        return KAPPA_MAX, diagnostics

    denominator = second_moment - sum_a_over_n
    if denominator <= 0.0:
        # Every bit of observed spread is consistent with binomial noise.
        return KAPPA_MAX, diagnostics

    kappa = (sum_a - second_moment) / denominator
    return max(KAPPA_MIN, min(KAPPA_MAX, kappa)), diagnostics


def build_snapshot(
    dataset,
    *,
    reference_id: str,
    as_of: datetime,
    lookback: timedelta,
    min_cell_battles: int = DEFAULT_MIN_CELL_BATTLES,
    prior_pooling: float = DEFAULT_PRIOR_POOLING,
    weight_basis: str = WEIGHT_BASIS_SLOTS,
) -> tuple[ReferenceSnapshot, EstimationReport]:
    """Derive a complete, immutable snapshot from data available at ``as_of``."""
    rows = _visible_rows(dataset, as_of, lookback)
    if not rows:
        raise ValueError(
            f"no rows visible at {as_of.isoformat()} within {lookback.days} days; "
            "cannot estimate a reference snapshot"
        )

    weights = estimate_weights(rows, weight_basis)
    stratum_priors, mode_priors = estimate_priors(rows, prior_pooling)
    kappa, diagnostics = estimate_prior_strength(
        rows, stratum_priors, mode_priors, min_cell_battles
    )

    snapshot = ReferenceSnapshot(
        reference_id=reference_id,
        as_of=as_of,
        weights=tuple(sorted(weights.items())),
        stratum_priors=tuple(sorted(stratum_priors.items())),
        mode_priors=tuple(sorted(mode_priors.items())),
        prior_strength=kappa,
        source_digest=getattr(dataset, "source_digest", None),
        estimation=(
            ("method", "moments/beta-binomial"),
            ("as_of", as_of.strftime("%Y-%m-%dT%H:%M:%SZ")),
            ("weight_basis", weight_basis),
            ("lookback_days", float(lookback.days)),
            ("min_cell_battles", float(min_cell_battles)),
            ("prior_pooling", prior_pooling),
            ("rows_used", float(len(rows))),
            ("cells_used", diagnostics["cells_used"]),
            ("between_component", diagnostics["between_component"]),
        ),
    )

    report = EstimationReport(
        rows_used=len(rows),
        strata=len(weights),
        cells_used=int(diagnostics["cells_used"]),
        kappa=kappa,
        kappa_clamped=kappa in (KAPPA_MIN, KAPPA_MAX),
        second_moment=diagnostics["second_moment"],
        binomial_component=diagnostics["binomial_component"],
        between_component=diagnostics["between_component"],
        total_slots=sum({(r.stratum.key, r.window_start): r.stratum_slots for r in rows}.values()),
    )
    return snapshot, report


def sweep_prior_strength(
    rows: Sequence[AggregateRow],
    stratum_priors: dict[str, float],
    mode_priors: dict[str, float],
    candidates: Sequence[float],
    *,
    split_at: datetime,
    min_cell_battles: int = DEFAULT_MIN_CELL_BATTLES,
) -> list[tuple[float, float]]:
    """Out-of-sample check on kappa: fit on data before ``split_at``, score after.

    Method of moments is a modelling assumption -- it presumes the cell rates
    really are beta-distributed around their stratum prior. This sweep tests
    that assumption where it matters: predictive accuracy on a period the
    estimate never saw. If the closed form's kappa sits near the minimum of
    this curve, the assumption is doing its job; if it sits far away, the beta
    prior is misspecified and the number should not be trusted.

    Errors are weighted by the *out-of-sample* battle count, because an
    unweighted mean would be dominated by thin cells whose realized rate is
    mostly noise and therefore unpredictable by construction.

    Returns ``[(kappa, weighted_mse), ...]``.
    """
    train: dict[tuple[str, str], tuple[int, int]] = {}
    test: dict[tuple[str, str], tuple[int, int]] = {}

    for row in rows:
        bucket = train if row.window_end <= split_at else test
        key = (row.brawler_id, row.stratum.key)
        battles, wins = bucket.get(key, (0, 0))
        bucket[key] = (battles + row.brawler_battles, wins + row.brawler_wins)

    shared = sorted(set(train) & set(test))
    results: list[tuple[float, float]] = []

    for kappa in candidates:
        errors: list[float] = []
        weights: list[float] = []
        for key in shared:
            train_battles, train_wins = train[key]
            test_battles, test_wins = test[key]
            if train_battles < min_cell_battles or test_battles < min_cell_battles:
                continue
            prior = stratum_priors.get(key[1]) or mode_priors.get(key[1].split("/", 1)[0])
            if prior is None:
                continue
            predicted = (train_wins + kappa * prior) / (train_battles + kappa)
            realized = test_wins / test_battles
            errors.append(test_battles * (predicted - realized) ** 2)
            weights.append(float(test_battles))
        if not weights:
            continue
        results.append((kappa, stable_sum(errors) / stable_sum(weights)))

    return results
