"""Mechanical facts about Brawl Stars modes, and what they pin.

Most of a metagame is empirical. A few things are not: they follow from the
rules of the game and hold exactly, in every window, forever. Those are worth
separating out, because a quantity that *must* take a particular value is both
a better prior than anything we could estimate and a free correctness check on
the pipeline that produces it.

The key structural fact is that a battlelog names **every** participant, not
just the player whose log it is. So for any battle we observe, we observe the
full outcome distribution across its slots:

    3v3 modes        6 slots: exactly 3 win, 3 lose  (or all 6 draw)
    Solo Showdown   10 slots: ranks 1-4 win, rank 5 draws, 6-10 lose
    Duo Showdown    10 slots in 5 teams: top 2 teams win, 3rd draws, rest lose

Pool those over all brawlers and the win rate is not an estimate. It is
arithmetic:

    3v3         (3 wins + 0 draws)  / 6  = 0.500
    Showdown    (4 wins + 1 draw/2) / 10 = 0.450

The half-draw convention is what makes this hold *regardless of how often
draws happen*. Scoring a draw as a loss instead gives 3v3 a pooled rate of
(1-d)/2, which drifts with the draw rate -- so a brawler's measured
performance would move when the meta got more defensive, even if the brawler
did not change. That is not a property a settlement metric may have.

These constants are therefore used two ways:

  * as the shrinkage target a mode's strata are pooled toward, in place of an
    estimate that can only be noisier than the exact answer;
  * as a validation: recompute the pooled rate from real data and compare. A
    material gap means missing participants, double-counted battles, or
    mishandled draws -- and nothing else in this project catches those.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ModeMechanics",
    "MECHANICS",
    "mechanical_baseline",
    "baseline_gap",
    "score",
]


@dataclass(frozen=True, slots=True)
class ModeMechanics:
    """The rules of a mode, insofar as they constrain aggregate statistics."""

    slots: int
    winning_slots: int
    drawing_slots: int

    def __post_init__(self) -> None:
        if self.winning_slots + self.drawing_slots > self.slots:
            raise ValueError("winning and drawing slots exceed the slots available")

    @property
    def baseline(self) -> float:
        """Pooled win rate over all brawlers, under the half-draw convention.

        Exact, and invariant to how often draws actually occur.
        """
        return (self.winning_slots + 0.5 * self.drawing_slots) / self.slots


# Keyed by the mode identifiers the API reports. Team modes share one shape;
# they differ in objective, not in how outcomes are distributed over slots.
_TEAM_3V3 = ModeMechanics(slots=6, winning_slots=3, drawing_slots=0)
_SOLO_SHOWDOWN = ModeMechanics(slots=10, winning_slots=4, drawing_slots=1)
# Five teams of two. The top two teams -- four players -- win; the third team
# draws. Same slot count and same baseline as solo, by construction.
_DUO_SHOWDOWN = ModeMechanics(slots=10, winning_slots=4, drawing_slots=2)

MECHANICS: dict[str, ModeMechanics] = {
    "gemGrab": _TEAM_3V3,
    "brawlBall": _TEAM_3V3,
    "heist": _TEAM_3V3,
    "bounty": _TEAM_3V3,
    "hotZone": _TEAM_3V3,
    "knockout": _TEAM_3V3,
    "basketBrawl": _TEAM_3V3,
    "volleyBrawl": _TEAM_3V3,
    "brawlBall5v5": _TEAM_3V3,
    "soloShowdown": _SOLO_SHOWDOWN,
    "duoShowdown": _DUO_SHOWDOWN,
}


def mechanical_baseline(mode_id: str) -> float | None:
    """The pooled win rate a mode's rules force, or None if the mode is unknown.

    None rather than a guess: a mode we have not characterized should fall back
    to an estimated prior, not to an invented constant that would look
    authoritative.
    """
    mechanics = MECHANICS.get(mode_id)
    return None if mechanics is None else mechanics.baseline


def score(wins: int, draws: int, battles: int) -> float:
    """Win rate under the half-draw convention: ``(wins + draws/2) / battles``.

    The convention is not a preference. It is the only scoring that makes a
    mode's pooled rate independent of its draw rate, which is what allows the
    mechanical baselines above to be exact and therefore to function as a
    check.
    """
    if battles <= 0:
        raise ValueError("cannot score zero battles")
    return (wins + 0.5 * draws) / battles


def baseline_gap(mode_id: str, observed: float) -> float | None:
    """Signed deviation of an observed pooled rate from the mechanical one.

    Non-trivial only when computed over *all* brawlers. A single brawler is
    supposed to deviate -- that is the signal. It is the population aggregate
    that must land on the constant, and a gap there indicates a pipeline
    defect: participants dropped from a battle, battles counted twice, or
    draws scored as losses.
    """
    baseline = mechanical_baseline(mode_id)
    return None if baseline is None else observed - baseline
