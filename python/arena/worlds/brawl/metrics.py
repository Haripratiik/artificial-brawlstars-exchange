"""Canonical metric definitions.

These are the quantities contracts settle on, defined here once, in code,
rather than described in prose and reimplemented per experiment. If a
definition changes it changes as a new reference snapshot id, and every
contract pinned to the old one keeps settling the old way.

Three metrics, computed over :class:`AggregateRow` filtered to the contract's
window and universe:

    raw_win_rate        wins / battles. Honest, unstandardized, sample-composition
                        dependent. Kept because "standardization changed the
                        answer by X" is itself a result worth reporting.

    adjusted_win_rate   the settlement metric. Shrunk per stratum, then
                        standardized onto the snapshot's weights.

    use_rate            appearances / total slots, standardized the same way.

The adjusted rate composes two corrections that solve different problems:

    shrinkage        fixes *within-stratum* noise. A cell with nine battles and
                     six wins is not evidence of a 67% win rate. Pulling it
                     toward its stratum's neutral point by a prior worth
                     ``prior_strength`` pseudo-battles keeps thin cells from
                     dominating a weighted average through sheer variance.

    standardization  fixes *between-stratum* composition. Reweighting onto the
                     snapshot's pinned proportions removes the crawler's
                     changing reach from the number, which is the entire reason
                     the metric is trustworthy despite a non-representative
                     sample.

Order matters: shrink first (each stratum's estimate is improved on its own
evidence), then weight (composition is imposed from outside).

**On missing strata.** The default is to iterate the *snapshot's* strata rather
than the observed ones, so a stratum with no data still participates and
shrinks fully to its prior. This is deliberate. Dropping missing strata and
renormalizing over the survivors implicitly assumes the missing ones behave
like the observed ones -- but strata go missing precisely because they are
unpopular, high-trophy, or rare map/mode pairs, which is close to the opposite
of missing-at-random. Partial pooling is the standard poststratification answer
to empty cells, and full shrinkage to the prior is exactly that. Coverage is
still reported, and still gates: it now measures how much of the metric rests
on real evidence rather than on the prior.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from arena.determinism import stable_sum
from arena.worlds.brawl.reference import ReferenceSnapshot
from arena.worlds.brawl.schema import AggregateRow, StratumKey

__all__ = [
    "MetricOutcome",
    "MissingStrata",
    "InsufficientEvidence",
    "raw_win_rate",
    "adjusted_win_rate",
    "use_rate",
    "METRICS",
]


class MissingStrata:
    """How to treat a stratum the snapshot declares but the data does not cover."""

    # Include it at zero evidence, so it shrinks entirely to its prior. Makes no
    # missing-at-random assumption. The default.
    IMPUTE_FROM_PRIOR = "IMPUTE_FROM_PRIOR"
    # Exclude it and renormalize over the survivors. Assumes missing strata
    # resemble observed ones. Kept for comparison, not recommended.
    DROP_AND_RENORMALIZE = "DROP_AND_RENORMALIZE"

    ALL = (IMPUTE_FROM_PRIOR, DROP_AND_RENORMALIZE)


class InsufficientEvidence(Exception):
    """The rows supplied cannot support the metric to the standard requested."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason} ({detail})" if detail else reason)


@dataclass(frozen=True, slots=True)
class MetricOutcome:
    """A computed metric plus the evidence trail behind it."""

    value: float
    sample_size: int
    diagnostics: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _Cell:
    """A stratum's totals, after summing every sub-window row that fell in it."""

    stratum: StratumKey
    battles: int
    wins: int
    slots: int


def _collapse(rows: Iterable[AggregateRow]) -> dict[StratumKey, _Cell]:
    """Sum rows into one cell per stratum.

    Rows arrive per sub-window; a contract settles over the whole window.
    Summing counts and *then* taking a ratio is not the same as averaging the
    sub-window ratios, and the former is correct: every battle should carry
    equal weight regardless of which week it landed in.
    """
    totals: dict[StratumKey, tuple[int, int, int]] = {}
    for row in rows:
        battles, wins, slots = totals.get(row.stratum, (0, 0, 0))
        totals[row.stratum] = (
            battles + row.brawler_battles,
            wins + row.brawler_wins,
            slots + row.stratum_slots,
        )
    return {
        stratum: _Cell(stratum, values[0], values[1], values[2])
        for stratum, values in totals.items()
    }


def raw_win_rate(
    rows: Sequence[AggregateRow],
    reference: ReferenceSnapshot,
    *,
    min_stratum_battles: int = 0,
    min_coverage: float = 0.0,
    missing_strata: str = MissingStrata.IMPUTE_FROM_PRIOR,
) -> MetricOutcome:
    """Unstandardized wins / battles across every row supplied.

    Extra arguments are accepted but unused, so this can be swapped for the
    adjusted metric in an experiment without changing any call site. That
    symmetry is the point: raw-versus-adjusted is itself a comparison worth
    producing.
    """
    cells = _collapse(rows)
    battles = sum(cell.battles for cell in cells.values())
    if battles == 0:
        raise InsufficientEvidence("no battles observed")
    wins = sum(cell.wins for cell in cells.values())
    return MetricOutcome(
        value=wins / battles,
        sample_size=battles,
        diagnostics=(
            ("metric", "raw_win_rate"),
            ("strata_observed", len(cells)),
            ("standardized", False),
        ),
    )


def adjusted_win_rate(
    rows: Sequence[AggregateRow],
    reference: ReferenceSnapshot,
    *,
    min_stratum_battles: int = 0,
    min_coverage: float = 0.0,
    missing_strata: str = MissingStrata.IMPUTE_FROM_PRIOR,
) -> MetricOutcome:
    """Shrunk, standardized win rate. The settlement metric.

    Every stratum the snapshot declares is walked. One with enough evidence
    contributes its shrunk rate; one without contributes its prior (under
    IMPUTE_FROM_PRIOR) or nothing at all (under DROP_AND_RENORMALIZE).

    ``coverage`` is the share of reference weight backed by real evidence. It
    is a diagnostic under either policy and a gate under both: a metric that is
    mostly prior is not a measurement, however well-formed it looks.
    """
    if missing_strata not in MissingStrata.ALL:
        raise ValueError(f"missing_strata must be one of {MissingStrata.ALL}")

    observed = _collapse(rows)
    if not observed:
        raise InsufficientEvidence("no rows in window")

    contributing: list[tuple[str, float, float]] = []
    covered_weight = 0.0
    battles_used = 0
    evidenced = 0
    imputed = 0

    for stratum in reference.strata:
        weight = reference.weight_for(stratum)
        if weight <= 0.0:
            continue
        cell = observed.get(stratum)
        prior = reference.prior_for(stratum)
        strength = reference.prior_strength

        if cell is not None and cell.battles >= min_stratum_battles and cell.battles > 0:
            # Shrink toward the stratum's neutral point. With strength 0 this is
            # the plain rate; as strength grows a thin cell is pulled to the
            # prior and stops contributing spurious signal.
            value = (cell.wins + strength * prior) / (cell.battles + strength)
            covered_weight += weight
            battles_used += cell.battles
            evidenced += 1
        elif missing_strata == MissingStrata.IMPUTE_FROM_PRIOR:
            # Zero evidence shrinks completely to the prior. Same formula, n=0.
            value = prior
            imputed += 1
        else:
            continue

        contributing.append((stratum.key, weight, value))

    if not contributing:
        raise InsufficientEvidence(
            "no stratum could be evaluated",
            f"{len(observed)} strata observed, none carrying reference weight",
        )

    coverage = covered_weight / reference.total_weight
    if coverage < min_coverage:
        raise InsufficientEvidence(
            "insufficient strata coverage",
            f"{coverage:.4f} of reference weight backed by evidence, "
            f"{min_coverage:.4f} required",
        )

    numerator = stable_sum(weight * value for _key, weight, value in sorted(contributing))
    denominator = stable_sum(weight for _key, weight, _value in sorted(contributing))

    # The reported sample size counts ONLY battles that carried evidence.
    # Counting imputed or out-of-universe strata here would let a contract clear
    # its min_sample_size bar on data the metric never used.
    return MetricOutcome(
        value=numerator / denominator,
        sample_size=battles_used,
        diagnostics=(
            ("metric", "adjusted_win_rate"),
            ("reference_id", reference.reference_id),
            ("missing_strata_policy", missing_strata),
            ("strata_evidenced", evidenced),
            ("strata_imputed", imputed),
            ("coverage", coverage),
            ("battles_used", battles_used),
            ("battles_observed", sum(cell.battles for cell in observed.values())),
            ("prior_strength", reference.prior_strength),
            ("standardized", True),
        ),
    )


def use_rate(
    rows: Sequence[AggregateRow],
    reference: ReferenceSnapshot,
    *,
    min_stratum_battles: int = 0,
    min_coverage: float = 0.0,
    missing_strata: str = MissingStrata.IMPUTE_FROM_PRIOR,
) -> MetricOutcome:
    """Share of brawler slots this brawler occupied, standardized.

    Denominated in slots rather than battles: a 3v3 battle offers six slots, so
    a brawler picked by both teams every game would otherwise show a use rate
    above one.

    Two deliberate asymmetries with the win rate.

    **No shrinkage.** A use rate of zero in a well-sampled stratum is a real
    observation -- nobody picked it -- rather than the noise artifact a zero win
    rate would be. Shrinking it would destroy signal.

    **Missing strata are always dropped and renormalized**, whatever the policy
    argument says. Imputing zero would assert the brawler is never picked
    there, which is false; and unlike a win rate there is no neutral point to
    shrink toward, since "how often is this brawler chosen here" has no
    mechanically-pinned baseline. Renormalizing does assume missing strata
    resemble observed ones -- it is the weaker assumption available, and it is
    recorded in the diagnostics rather than hidden.
    """
    observed = _collapse(rows)
    if not observed:
        raise InsufficientEvidence("no rows in window")

    contributing: list[tuple[str, float, float]] = []
    covered_weight = 0.0
    slots_used = 0
    dropped = 0

    for stratum in reference.strata:
        weight = reference.weight_for(stratum)
        if weight <= 0.0:
            continue
        cell = observed.get(stratum)
        if cell is None or cell.slots <= 0 or cell.slots < min_stratum_battles:
            dropped += 1
            continue
        contributing.append((stratum.key, weight, cell.battles / cell.slots))
        covered_weight += weight
        slots_used += cell.slots

    if not contributing:
        raise InsufficientEvidence(
            "no stratum cleared the evidential bar",
            f"{dropped} strata too thin or unobserved",
        )

    coverage = covered_weight / reference.total_weight
    if coverage < min_coverage:
        raise InsufficientEvidence(
            "insufficient strata coverage",
            f"{coverage:.4f} of reference weight backed by evidence, "
            f"{min_coverage:.4f} required",
        )

    numerator = stable_sum(weight * value for _key, weight, value in sorted(contributing))
    denominator = stable_sum(weight for _key, weight, _value in sorted(contributing))

    return MetricOutcome(
        value=numerator / denominator,
        sample_size=slots_used,
        diagnostics=(
            ("metric", "use_rate"),
            ("reference_id", reference.reference_id),
            ("missing_strata_policy", MissingStrata.DROP_AND_RENORMALIZE),
            ("strata_evidenced", len(contributing)),
            ("strata_dropped", dropped),
            ("coverage", coverage),
            ("slots_used", slots_used),
            ("slots_observed", sum(cell.slots for cell in observed.values())),
            ("standardized", True),
        ),
    )


# The registry the oracle dispatches on. A contract naming a metric outside this
# mapping fails at resolution rather than silently measuring something adjacent.
METRICS = {
    "raw_win_rate": raw_win_rate,
    "adjusted_win_rate": adjusted_win_rate,
    "use_rate": use_rate,
}
