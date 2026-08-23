"""The settlement record.

This is the project's most durable artifact. Prices are opinions and PnL is
bookkeeping, but a settlement record is a claim about what actually happened,
and every experimental result eventually rests on one. It is therefore built to
be checked by someone who does not trust us: it carries the spec digest, every
resolved input, the provenance of every source, and its own digest over all of
that.

Note what is deliberately absent: a wall-clock timestamp. Determinism means the
same inputs produce byte-identical output, and a `computed_at` field would
break that for no benefit. When a settlement was computed belongs in the run
manifest, which is about the experiment; not in the record, which is about the
world.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from arena.determinism import digest
from arena.settlement.oracle import MetricResolution

__all__ = ["SettlementStatus", "SettlementResult"]


class SettlementStatus:
    SETTLED = "SETTLED"
    VOID = "VOID"

    ALL = (SETTLED, VOID)


@dataclass(frozen=True, slots=True)
class SettlementResult:
    """The outcome of settling one contract against one oracle."""

    contract_id: str
    spec_digest: str
    status: str
    # None when the contract voided. Kept as Decimal because the settlement
    # value is a price on a tick grid, not a measurement, and float repr would
    # reintroduce ambiguity the quantization step exists to remove.
    settlement_value: Decimal | None
    underlying_level: float | None
    resolutions: tuple[MetricResolution, ...]
    void_reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in SettlementStatus.ALL:
            raise ValueError(f"unknown settlement status {self.status!r}")
        if self.status == SettlementStatus.SETTLED:
            if self.settlement_value is None:
                raise ValueError("a settled contract must carry a settlement value")
            if self.void_reason is not None:
                raise ValueError("a settled contract cannot carry a void reason")
        else:
            if self.settlement_value is not None:
                raise ValueError("a voided contract must not carry a settlement value")
            if not self.void_reason:
                raise ValueError("a voided contract must record why")

    @property
    def settled(self) -> bool:
        return self.status == SettlementStatus.SETTLED

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "spec_digest": self.spec_digest,
            "status": self.status,
            "settlement_value": (
                None if self.settlement_value is None else str(self.settlement_value)
            ),
            "underlying_level": self.underlying_level,
            "resolutions": [resolution.to_dict() for resolution in self.resolutions],
            "void_reason": self.void_reason,
        }

    @property
    def result_digest(self) -> str:
        """Content address of the whole record, inputs and provenance included."""
        return digest(self.to_dict())
