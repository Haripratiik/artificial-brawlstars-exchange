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
from arena.worlds.brawl.modes import baseline_gap, mechanical_baseline
from arena.worlds.brawl.reference import ReferenceSnapshot
from arena.worlds.brawl.schema import AggregateRow, StratumKey

__all__ = [
    "EstimationReport",
    "estimate_weights",
    "estimate_priors",
    "estimate_prior_strength",
    "mechanical_gaps",
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


def _pooled(rows: Sequence[AggregateRow]) -> tuple[dict[str, float], dict[str, int]]:
    """Observed pooled score and battle count, per mode, under half-draw scoring."""
    scored: dict[str, float] = {}
    battles: dict[str, int] = {}
    for row in rows:
        scored[row.mode_id] = (
            scored.get(row.mode_id, 0.0) + row.brawler_wins + 0.5 * row.brawler_draws
        )
        battles[row.mode_id] = battles.get(row.mode_id, 0) + row.brawler_battles
    return (
        {mode: scored[mode] / n for mode, n in battles.items() if n > 0},
        battles,
    )


def mechanical_gaps(rows: Sequence[AggregateRow]) -> dict[str, float]:
    """Deviation of each mode's observed pooled rate from the value its rules force.

    A free, exact correctness check on the whole aggregation pipeline. Because a
    battlelog names every participant, the pooled rate over *all* brawlers is
    arithmetic, not an estimate: 0.500 for 3v3, 0.450 for Showdown. A material
    gap means participants are being dropped from battles, battles are being
    double counted, or draws are being scored as losses.

    Only meaningful over a corpus that covers essentially every brawler. A
    deliberately partial fixture will show a large gap, and should.
    """
    observed, _battles = _pooled(rows)
    gaps: dict[str, float] = {}
    for mode, rate in sorted(observed.items()):
        gap = baseline_gap(mode, rate)
        if gap is not None:
            gaps[mode] = gap
    return gaps


def estimate_priors(
    rows: Sequence[AggregateRow],
    pooling_strength: float = DEFAULT_PRIOR_POOLING,
    *,
    use_mechanical: bool = True,
) -> tuple[dict[str, float], dict[str, float]]:
    """Hierarchical neutral win rates: per stratum, pooled toward per mode.

    **Mode baselines are taken from the rules, not from the data, wherever the
    rules pin them.** A 3v3 battle puts three of six brawlers on the winning
    side, so the population win rate is exactly 0.500; Showdown awards wins to
    ranks 1-4 and a draw to 5th, so it is exactly 0.450. An exact constant
    beats any estimate of the same quantity, and using it means a thin or
    skewed estimation window cannot drag the shrinkage target around.

    ``use_mechanical=False`` falls back to the observed pooled rate. Useful for
    checking the estimator against the constant, and necessary for any mode
    whose rules are not characterized in :mod:`arena.worlds.brawl.modes`.

    The stratum-level rate then captures real deviation -- a map where the team
    that scores first usually holds -- and is pooled back toward its mode so a
    thin stratum cannot invent a baseline from a handful of battles.

    Returns ``(stratum_priors, mode_priors)``.
    """
    stratum_scored: dict[str, float] = {}
    stratum_battles: dict[str, int] = {}
    stratum_mode: dict[str, str] = {}

    for row in rows:
        key = row.stratum.key
        stratum_scored[key] = (
            stratum_scored.get(key, 0.0) + row.brawler_wins + 0.5 * row.brawler_draws
        )
        stratum_battles[key] = stratum_battles.get(key, 0) + row.brawler_battles
        stratum_mode[key] = row.mode_id

    observed, _battles = _pooled(rows)
    if not observed:
        raise ValueError("cannot estimate priors: no battles observed in the window")

    mode_priors: dict[str, float] = {}
    for mode, rate in sorted(observed.items()):
        forced = mechanical_baseline(mode) if use_mechanical else None
        mode_priors[mode] = rate if forced is None else forced

    stratum_priors: dict[str, float] = {}
    for key in sorted(stratum_battles):
        n = stratum_battles[key]
        mode_prior = mode_priors.get(stratum_mode[key])
        if mode_prior is None:
            continue
        if n <= 0:
            stratum_priors[key] = mode_prior
            continue
        rate = stratum_scored[key] / n
        stratum_priors[key] = (n * rate + pooling_strength * mode_prior) / (
            n + pooling_strength
        )

    return stratum_priors, mode_priors


def estimate_prior_strength(
    rows: Sequence[AggregateRow],
    stratum_priors: dict[str, float],
    mode_priors: dict[str, float],
    min_cell_battles: int = DEFAULT_MIN_CELL_BATTLES,
    design_effect: float = 1.0,
) -> tuple[float, dict[str, float]]:
    """Method-of-moments kappa for the beta-binomial. See the module docstring.

    ``design_effect`` corrects for the fact that battles are not independent
    draws. The crawler fetches a player's last 25 battles at once, so those
    share a pilot and therefore a skill level; matchmaking further correlates
    opponents. Clustering inflates the true sampling variance above the
    binomial formula by a factor DEFF = 1 + (m - 1) * ICC.

    The direction of the resulting bias is worth stating precisely, because it
    is not obvious. Understating the binomial component makes the *residual*
    look like real between-cell variation, which inflates the between term,
    which makes kappa come out **too small** -- so the metric shrinks thin
    cells too little and is noisier than it reports. Dividing each cell's
    battle count by DEFF restores the correct decomposition.

    The default of 1.0 asserts independence, which is wrong but honest: DEFF
    cannot be estimated without real clustered data. The collector already
    records which player's log surfaced each battle, so it is estimable as soon
    as a corpus exists.

    Returns ``(kappa, diagnostics)``.
    """
    if design_effect < 1.0:
        raise ValueError(
            f"design_effect must be at least 1.0 (independence), got {design_effect}"
        )

    # Collapse to one cell per (brawler, stratum): kappa describes how much a
    # brawler's true rate in a stratum deviates from that stratum's baseline,
    # so sub-windows must be pooled first or the same deviation is counted
    # once per week and kappa comes out far too small.
    cells: dict[tuple[str, str], tuple[int, float]] = {}
    for row in rows:
        key = (row.brawler_id, row.stratum.key)
        battles, scored = cells.get(key, (0, 0.0))
        cells[key] = (
            battles + row.brawler_battles,
            scored + row.brawler_wins + 0.5 * row.brawler_draws,
        )

    second_moment = 0.0
    sum_a = 0.0
    sum_a_over_n = 0.0
    used = 0

    for (_brawler, stratum_key), (battles, scored) in sorted(cells.items()):
        if battles < min_cell_battles:
            continue
        prior = stratum_priors.get(stratum_key)
        if prior is None:
            mode = stratum_key.split("/", 1)[0]
            prior = mode_priors.get(mode)
        if prior is None:
            continue

        observed = scored / battles
        effective = battles / design_effect
        amplitude = prior * (1.0 - prior)
        second_moment += (observed - prior) ** 2
        sum_a += amplitude
        sum_a_over_n += amplitude / effective
        used += 1

    diagnostics = {
        "cells_used": float(used),
        "second_moment": second_moment,
        "binomial_component": sum_a_over_n,
        "between_component": max(second_moment - sum_a_over_n, 0.0),
        "design_effect": design_effect,
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
    design_effect: float = 1.0,
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
        rows, stratum_priors, mode_priors, min_cell_battles, design_effect
    )
    gaps = mechanical_gaps(rows)

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
            ("design_effect", design_effect),
            ("rows_used", float(len(rows))),
            ("cells_used", diagnostics["cells_used"]),
            ("between_component", diagnostics["between_component"]),
            # Recorded, not acted on: a large gap means the corpus does not
            # cover every brawler (expected on a fixture) or the aggregation is
            # defective (never expected). Either way the snapshot should carry
            # the evidence rather than quietly discard it.
            # A mapping, not a list of pairs: JSON round-trips an object back to
            # a dict, but turns a list of tuples into a list of lists, which
            # would make a reloaded snapshot compare unequal to the one written.
            ("mechanical_gaps", dict(sorted(gaps.items()))),
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
    train: dict[tuple[str, str], tuple[int, float]] = {}
    test: dict[tuple[str, str], tuple[int, float]] = {}

    for row in rows:
        bucket = train if row.window_end <= split_at else test
        key = (row.brawler_id, row.stratum.key)
        battles, scored = bucket.get(key, (0, 0.0))
        bucket[key] = (
            battles + row.brawler_battles,
            scored + row.brawler_wins + 0.5 * row.brawler_draws,
        )

    shared = sorted(set(train) & set(test))
    results: list[tuple[float, float]] = []

    for kappa in candidates:
        errors: list[float] = []
        weights: list[float] = []
        for key in shared:
            train_battles, train_scored = train[key]
            test_battles, test_scored = test[key]
            if train_battles < min_cell_battles or test_battles < min_cell_battles:
                continue
            prior = stratum_priors.get(key[1]) or mode_priors.get(key[1].split("/", 1)[0])
            if prior is None:
                continue
            predicted = (train_scored + kappa * prior) / (train_battles + kappa)
            realized = test_scored / test_battles
            errors.append(test_battles * (predicted - realized) ** 2)
            weights.append(float(test_battles))
        if not weights:
            continue
        results.append((kappa, stable_sum(errors) / stable_sum(weights)))

    return results
