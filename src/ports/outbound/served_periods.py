# SPDX-License-Identifier: MIT-0
"""Which complete periods a periodic contract has CURRENT publications for (ADR-100).

Chat answers a periodic metric only at a complete period that was measured, and names it
(`domain.conversation.periodic.resolve_chat_period`). That decision needs to know which periods
are published, without query authority and without reading a fact: this port reads the
publication state only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from domain.data_contracts.contract import DataContract
from domain.semantics.calendar_periods import CalendarPeriod

__all__ = ["ServedPeriods", "ServedPeriodsPort", "ServedPeriodsRequest"]


@dataclass(frozen=True, slots=True)
class ServedPeriodsRequest:
    """The periodic contracts one answer reads, and the population its filters select."""

    contracts: tuple[DataContract, ...]
    filters: Mapping[str, str]
    now: datetime

    def __post_init__(self) -> None:
        if not self.contracts:
            raise ValueError("served periods require at least one periodic contract.")
        if self.now.tzinfo is None:
            raise ValueError("served periods instant must be timezone-aware.")


@dataclass(frozen=True, slots=True)
class ServedPeriods:
    """Every (contract, complete period) published for every selected population.

    `cadence_lag_days` is each contract's declared lag after period end, so a caller can say
    when a period that hasn't landed is expected.
    """

    publications: frozenset[tuple[str, CalendarPeriod]]
    cadence_lag_days: Mapping[str, int] = field(default_factory=dict[str, int])
    series: Mapping[tuple[str, CalendarPeriod], tuple[str | None, ...]] = field(
        default_factory=dict[tuple[str, CalendarPeriod], tuple[str | None, ...]]
    )
    """For a contract declaring pack variants (plan D9): which series hold each published period.

    `None` is the base pack, a string a variant id. A period with one series is served from it;
    with two it is mixed and refused (`domain.measurement.periodic.active_variant`). A contract
    declaring no variant is absent, and its periods are the base pack's.
    """

    def series_of(self, contract_id: str, period: CalendarPeriod) -> tuple[str | None, ...]:
        """Return the series holding one published period, the base pack's by default."""
        return self.series.get((contract_id, period), (None,))


@runtime_checkable
class ServedPeriodsPort(Protocol):
    """Reads which complete periods are CURRENT, never a fact row."""

    def served_periods(self, request: ServedPeriodsRequest) -> ServedPeriods:
        """Return the complete periods every selected population has a CURRENT publication for."""
        ...
