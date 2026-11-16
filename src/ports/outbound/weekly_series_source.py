# SPDX-License-Identifier: MIT-0
"""The weekly-aggregate source port: the roster and the two partitioned UNLOADs (design §3c).

The plan is in domain terms (pinned publication IDs, selectors, shard size, destinations); no SQL
crosses this port. The adapter composes and fences the statements.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class WeeklySeriesError(Exception):
    """A statement was refused, failed or timed out; `code` is the input-build failure category."""

    def __init__(
        self, code: str, query_id: str | None = None, *, stop_requested: bool | None = None
    ) -> None:
        super().__init__(code)
        self.code = code
        self.query_id = query_id
        self.stop_requested = stop_requested


@dataclass(frozen=True, slots=True)
class WeeklySeriesPlan:
    """One build attempt's pinned publications and its two empty destination prefixes."""

    sales_publication_ids: tuple[str, ...]
    traffic_publication_ids: tuple[str, ...]
    distributor_view: str
    selling_program: str
    shard_max_asins: int
    target_destination: str
    traffic_destination: str

    def __post_init__(self) -> None:
        if not self.sales_publication_ids:
            raise ValueError("a weekly series plan binds at least one sales publication.")
        if self.shard_max_asins < 1:
            raise ValueError("shard_max_asins must be at least 1.")
        for destination in (self.target_destination, self.traffic_destination):
            if not destination.startswith("s3://") or not destination.endswith("/"):
                raise ValueError("an UNLOAD destination is an s3:// prefix ending in /.")


@dataclass(frozen=True, slots=True)
class StatementStat:
    """What one statement cost: its kind, query ID, scanned bytes and durations."""

    kind: str
    query_id: str
    bytes_scanned: int
    engine_millis: int
    total_millis: int


@dataclass(frozen=True, slots=True)
class RosterResult:
    """The sorted roster with the shard the statement assigned each ASIN."""

    canonical_asins: tuple[str, ...]
    shards: tuple[int, ...]
    statement: StatementStat


@dataclass(frozen=True, slots=True)
class UnloadResult:
    """The two UNLOAD statements' costs."""

    statements: tuple[StatementStat, ...]


class WeeklySeriesSourcePort(Protocol):
    """Reads the roster and unloads the weekly aggregates partitioned by shard."""

    def roster(self, plan: WeeklySeriesPlan) -> RosterResult:
        """Return every canonical ASIN with a row in the sales pins, sorted, with its shard."""
        ...

    def unload(self, plan: WeeklySeriesPlan) -> UnloadResult:
        """Unload the target and traffic aggregates; raise `WeeklySeriesError` on any failure."""
        ...
