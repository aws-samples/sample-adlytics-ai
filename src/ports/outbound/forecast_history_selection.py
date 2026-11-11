# SPDX-License-Identifier: MIT-0
"""The demand-forecast selection port: CURRENT pins and covered days for one forecast set.

Completeness and values come from the same pins, so they can't describe different revisions
(design §3b, R3-01). `SourceCoveragePort` is not used: its result carries no publication ID,
fencing token or digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from domain.shared_kernel.values import DateRange
from ports.outbound.publication_selection import PinnedPublication

MAX_SELECTION_DAYS = 1000
"""The exact-read bound: one `current_for_scope` read attests at most 1,000 partitions."""


@dataclass(frozen=True, slots=True)
class ForecastHistorySelectionRequest:
    """The set's marketplace, its sorted selectors and the window to read (at most 1,000 days)."""

    marketplace_key: str
    selectors: tuple[tuple[str, str], ...]
    window: DateRange

    def __post_init__(self) -> None:
        if not self.marketplace_key.strip():
            raise ValueError("a forecast selection names its marketplace.")
        if list(self.selectors) != sorted(self.selectors):
            raise ValueError("forecast selectors are sorted by name.")
        if self.window.days > MAX_SELECTION_DAYS:
            raise ValueError(f"a forecast selection reads at most {MAX_SELECTION_DAYS} days.")


@dataclass(frozen=True, slots=True)
class PinnedContractHistory:
    """One contract's pins, each pin's bucket, the coalesced covered days and the newest time."""

    contract_id: str
    pins: tuple[PinnedPublication, ...]
    buckets: tuple[DateRange, ...]
    covered: tuple[DateRange, ...]
    newest_published_at: datetime | None

    def __post_init__(self) -> None:
        if len(self.pins) != len(self.buckets):
            raise ValueError("every pin carries its bucket.")


@dataclass(frozen=True, slots=True)
class ForecastHistorySelectionResult:
    """The sales and traffic history of the window and the latest product publication pins."""

    sales: PinnedContractHistory
    traffic: PinnedContractHistory
    products: PinnedContractHistory


class ForecastHistorySelectionPort(Protocol):
    """Selects the CURRENT publications a forecast run reads."""

    def select(self, request: ForecastHistorySelectionRequest) -> ForecastHistorySelectionResult:
        """Return the pins and covered days; a scope with no CURRENT publication adds none."""
        ...
