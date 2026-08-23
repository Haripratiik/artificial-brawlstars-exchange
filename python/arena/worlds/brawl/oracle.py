"""The Brawl world's settlement oracle.

Binds three things that must agree for a settlement to mean anything: a
canonical dataset, a frozen reference snapshot, and the metric registry. It
implements the generic :class:`~arena.settlement.oracle.Oracle` protocol, so
the settlement engine can settle a Brawl contract without importing anything
from this package.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from arena.contracts.spec import DataPolicy, ObservationWindow
from arena.contracts.underlying import MetricRef
from arena.settlement.oracle import MetricResolution, MetricUnavailable, SourceRef
from arena.worlds.brawl.dataset import CanonicalDataset
from arena.worlds.brawl.metrics import METRICS, InsufficientEvidence
from arena.worlds.brawl.reference import ReferenceSnapshot

__all__ = ["BrawlOracle"]


class BrawlOracle:
    """Resolves Brawl metrics from a canonical dataset under a pinned snapshot."""

    def __init__(
        self,
        dataset: CanonicalDataset,
        reference: ReferenceSnapshot,
        policy: DataPolicy,
    ) -> None:
        self._dataset = dataset
        self._reference = reference
        # The oracle holds the policy because the evidential bar is applied
        # *inside* the metric -- coverage and per-stratum thresholds shape the
        # computation itself, they are not a check that can be applied to a
        # finished number. The engine still enforces min_sample_size afterwards,
        # which is the one bar that genuinely is a post-hoc check.
        self._policy = policy

    @property
    def reference_id(self) -> str:
        return self._reference.reference_id

    @property
    def reference_as_of(self) -> datetime:
        return self._reference.as_of

    @property
    def reference(self) -> ReferenceSnapshot:
        return self._reference

    def resolve(
        self,
        ref: MetricRef,
        window: ObservationWindow,
        policy_overrides: Mapping[str, Any] | None = None,
    ) -> MetricResolution:
        try:
            metric = METRICS[ref.metric]
        except KeyError:
            raise MetricUnavailable(
                ref,
                "unknown metric",
                f"{ref.metric!r} is not one of {sorted(METRICS)}",
            ) from None

        rows = self._dataset.select(ref, window)
        if not rows:
            raise MetricUnavailable(
                ref,
                "no observations in window",
                f"{window.start.date()} to {window.end.date()}",
            )

        overrides = policy_overrides or {}
        try:
            outcome = metric(
                rows,
                self._reference,
                min_stratum_battles=int(
                    overrides.get("min_stratum_battles", self._policy.min_stratum_battles)
                ),
                min_coverage=float(
                    overrides.get("min_strata_coverage", self._policy.min_strata_coverage)
                ),
                missing_strata=str(
                    overrides.get("missing_strata_policy", self._policy.missing_strata_policy)
                ),
            )
        except InsufficientEvidence as thin:
            raise MetricUnavailable(ref, thin.reason, thin.detail) from None

        return MetricResolution(
            ref=ref,
            value=outcome.value,
            sample_size=outcome.sample_size,
            sources=(
                SourceRef(
                    source_id=self._dataset.source_id,
                    digest=self._dataset.source_digest,
                    rows=len(rows),
                ),
                SourceRef(
                    source_id=f"reference:{self._reference.reference_id}",
                    digest=self._reference.snapshot_digest,
                    rows=len(self._reference.weights),
                ),
            ),
            diagnostics=outcome.diagnostics,
        )
