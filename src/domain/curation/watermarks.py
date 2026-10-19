# SPDX-License-Identifier: MIT-0
"""Stable source partition identity used by Semantic V2 ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

__all__ = ["PartitionKey", "WatermarkError"]


class WatermarkError(Exception):
    """A source partition identity is invalid."""


@dataclass(frozen=True, slots=True)
class PartitionKey:
    """The source, contract, lens, marketplace, and period one delivery describes."""

    source_system: str
    contract_id: str
    report_options_hash: str
    marketplace_key: str
    period_start: date
    period_end: date

    def __post_init__(self) -> None:
        blank = [
            name
            for name in ("source_system", "contract_id", "report_options_hash", "marketplace_key")
            if not str(getattr(self, name)).strip()
        ]
        if blank:
            raise WatermarkError(f"partition key is missing: {', '.join(blank)}.")
        if self.period_end < self.period_start:
            raise WatermarkError(
                f"{self.contract_id}: period ends {self.period_end} before it starts "
                f"{self.period_start}."
            )

    @property
    def identity(self) -> str:
        """Return the stable storage identity, ordered general to specific."""
        return "|".join(
            (
                self.source_system,
                self.contract_id,
                self.report_options_hash,
                self.marketplace_key,
                self.period_start.isoformat(),
                self.period_end.isoformat(),
            )
        )
