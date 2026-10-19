# SPDX-License-Identifier: MIT-0
"""Which query pack version is currently published for each day of a window (pack coverage).

A metric reading a column a later query pack version added is answered only over a window whose
every day is published from that version or later (`domain.measurement.pack_coverage`). This
port reads the per-day version from publication state, never a fact row.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from domain.data_contracts.contract import DataContract
from domain.measurement.pack_coverage import PackDayVersions
from domain.shared_kernel.values import DateRange

__all__ = ["PackCoveragePort", "PackCoverageReadRequest"]


@dataclass(frozen=True, slots=True)
class PackCoverageReadRequest:
    """The contracts with pack-versioned columns, the population filters, and the windows."""

    contracts: tuple[DataContract, ...]
    filters: Mapping[str, str]
    windows: tuple[DateRange, ...]

    def __post_init__(self) -> None:
        if not self.contracts or not self.windows:
            raise ValueError("pack coverage needs at least one contract and one window.")


@runtime_checkable
class PackCoveragePort(Protocol):
    """Reads each day's CURRENT publication's recorded query pack version."""

    def pack_coverage(self, request: PackCoverageReadRequest) -> tuple[PackDayVersions, ...]:
        """Return one per-day version map per contract, over every day of every window."""
        ...
