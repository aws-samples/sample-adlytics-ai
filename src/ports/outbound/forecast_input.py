# SPDX-License-Identifier: MIT-0
"""The job's staged-input port: one shard's manifest and its weekly aggregates (design §4e).

Adapted from the source engine's input port. The adapter validates each file's physical schema
against its `kind` model before returning a row, so a renamed column never reaches the domain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from domain.forecasting.boundary import (
    InputManifestV1,
    WeeklyTargetAggregateRowV1,
    WeeklyTrafficAggregateRowV1,
)


class ForecastInputError(Exception):
    """The staged input is refused; `code` is the job's failure category."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ShardInput:
    """One shard's validated rows."""

    manifest: InputManifestV1
    targets: tuple[WeeklyTargetAggregateRowV1, ...]
    traffic: tuple[WeeklyTrafficAggregateRowV1, ...]


class ForecastInputPort(Protocol):
    """Reads one shard's staged input."""

    def read_shard(self, shard: int) -> ShardInput:
        """Return the shard's rows; raise `ForecastInputError` on any schema or shard mismatch."""
        ...
