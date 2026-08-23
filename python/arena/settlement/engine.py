"""Deterministic settlement.

The whole engine is one function. That is the point: settlement should be small
enough to read in one sitting and verify by inspection, because every research
claim this project makes eventually reduces to trusting it.

Order of operations matters and is fixed:

    1. check the oracle is the one the contract pinned
    2. resolve every atom, in canonical order
    3. apply the evidential bar (sample size), per atom
    4. evaluate the underlying algebra
    5. apply the payoff
    6. quantize onto the tick grid

Steps 3 and 6 are the ones people skip. Skipping 3 means settling on evidence
the contract said was insufficient; skipping 6 means printing a closing value
the exchange cannot represent.
"""

from __future__ import annotations

from arena.contracts.spec import ContractSpec
from arena.determinism import quantize_to_tick
from arena.settlement.oracle import MetricResolution, MetricUnavailable, Oracle
from arena.settlement.result import SettlementResult, SettlementStatus

__all__ = ["settle", "ReferenceMismatch", "ReferenceLookahead"]


class ReferenceMismatch(Exception):
    """The oracle's standardization snapshot is not the one the contract pinned.

    This is a hard error rather than a void. A void says "the world did not
    supply enough evidence"; this says "you wired the experiment up wrong", and
    silently voiding would hide a configuration bug behind a plausible-looking
    market outcome.
    """


class ReferenceLookahead(Exception):
    """The standardization snapshot post-dates the window it is settling.

    The most dangerous failure this engine can have, because it does not look
    like a failure. A snapshot fitted on data from inside its own observation
    window has seen the outcome, so its weights and priors encode the answer --
    and the settlement it produces will be *better* than an honest one, which
    is precisely why nothing about the result would invite suspicion.
    """


def settle(spec: ContractSpec, oracle: Oracle) -> SettlementResult:
    """Settle ``spec`` against ``oracle``, deterministically.

    Returns a VOID result when the evidence does not meet the contract's stated
    bar. Raises only when the *setup* is wrong, never when the world is.
    """
    if oracle.reference_id != spec.reference_id:
        raise ReferenceMismatch(
            f"{spec.contract_id} is pinned to reference {spec.reference_id!r} but the "
            f"oracle is configured with {oracle.reference_id!r}"
        )

    if oracle.reference_as_of > spec.window.start:
        raise ReferenceLookahead(
            f"{spec.contract_id} opens its observation window at "
            f"{spec.window.start.isoformat()}, but reference {spec.reference_id!r} was "
            f"estimated as of {oracle.reference_as_of.isoformat()} -- after the window "
            "had already begun. Its weights and priors may encode the outcome. "
            "Estimate a new snapshot dated on or before the window start."
        )

    resolutions: list[MetricResolution] = []
    # spec.atoms() is sorted, so resolution order -- and therefore the order of
    # resolutions in the record and its digest -- does not depend on how the
    # underlying happened to be nested.
    for ref in spec.atoms():
        try:
            resolution = oracle.resolve(ref, spec.window)
        except MetricUnavailable as unavailable:
            return _void(spec, resolutions, unavailable.args[0])

        if resolution.sample_size < spec.policy.min_sample_size:
            return _void(
                spec,
                resolutions,
                f"{ref.key}: sample size {resolution.sample_size} below required "
                f"{spec.policy.min_sample_size}",
            )
        resolutions.append(resolution)

    values = {resolution.ref: resolution.value for resolution in resolutions}
    level = spec.underlying.evaluate(values)
    settlement_value = quantize_to_tick(spec.payoff.apply(level), spec.tick_size)

    return SettlementResult(
        contract_id=spec.contract_id,
        spec_digest=spec.spec_digest,
        status=SettlementStatus.SETTLED,
        settlement_value=settlement_value,
        underlying_level=level,
        resolutions=tuple(resolutions),
    )


def _void(
    spec: ContractSpec,
    resolutions: list[MetricResolution],
    reason: str,
) -> SettlementResult:
    """Build a void record that still carries whatever evidence was gathered.

    The partial resolutions are kept on purpose. "Voided because Crow's sample
    was thin" is a far more useful record than "voided", especially when the
    same contract template is about to be reused for the next window.
    """
    return SettlementResult(
        contract_id=spec.contract_id,
        spec_digest=spec.spec_digest,
        status=SettlementStatus.VOID,
        settlement_value=None,
        underlying_level=None,
        resolutions=tuple(resolutions),
        void_reason=reason,
    )
