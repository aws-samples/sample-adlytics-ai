# SPDX-License-Identifier: MIT-0
"""Describe exact coverage available from active Semantic V2 publications."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from domain.shared_kernel.provenance import FreshnessStatus, MeasurementBasis
from domain.shared_kernel.values import DateRange

__all__ = ["Coverage", "common_intervals", "latest_complete_window"]


@dataclass(frozen=True, slots=True)
class Coverage:
    """What one fact actually covers right now."""

    fact_name: str
    window: DateRange | None
    freshness: FreshnessStatus
    measurement_basis: MeasurementBasis
    valid_grains: tuple[str, ...]
    loaded_at: datetime | None = None
    failure_reason: str | None = None
    intervals: tuple[DateRange, ...] = ()
    """Exact normalized coverage intervals; disjoint intervals never become one envelope."""

    holes: tuple[DateRange, ...] = ()
    """Exact requested intervals unavailable from CURRENT publications."""

    def __post_init__(self) -> None:
        if not self.valid_grains:
            raise ValueError(f"{self.fact_name}: at least one valid grain is required.")
        if self.freshness is FreshnessStatus.NEVER_LOADED and self.window is not None:
            raise ValueError(
                f"{self.fact_name}: a fact that has never loaded cannot carry a window, since "
                "no load established one."
            )
        if len(set(self.intervals)) != len(self.intervals):
            raise ValueError(f"{self.fact_name}: exact coverage repeats an interval.")
        if tuple(sorted(self.intervals, key=lambda item: (item.start, item.end))) != self.intervals:
            raise ValueError(f"{self.fact_name}: exact coverage intervals must be sorted.")
        if len(set(self.holes)) != len(self.holes):
            raise ValueError(f"{self.fact_name}: exact coverage repeats a hole.")
        if tuple(sorted(self.holes, key=lambda item: (item.start, item.end))) != self.holes:
            raise ValueError(f"{self.fact_name}: exact coverage holes must be sorted.")

    def covers(self, requested: DateRange) -> bool:
        """Say whether one requested window is fully covered by this fact."""
        return self.covers_all((requested,))

    def covers_all(self, requested: Sequence[DateRange]) -> bool:
        """Require every requested interval without bridging publication gaps."""
        if not requested:
            return False
        available = self.intervals or (() if self.window is None else (self.window,))
        return all(
            any(interval.covers(item) for interval in available)
            and not any(hole.start <= item.end and item.start <= hole.end for hole in self.holes)
            for item in requested
        )


def common_intervals(groups: Sequence[Sequence[DateRange]]) -> tuple[DateRange, ...]:
    """Intersect exact interval sets without turning gaps into a min/max envelope."""
    if not groups:
        return ()
    current = tuple(groups[0])
    for following in groups[1:]:
        intersections = tuple(
            DateRange(max(left.start, right.start), min(left.end, right.end))
            for left in current
            for right in following
            if max(left.start, right.start) <= min(left.end, right.end)
        )
        if not intersections:
            return ()
        ordered = sorted(set(intersections), key=lambda item: (item.start, item.end))
        merged: list[DateRange] = [ordered[0]]
        for item in ordered[1:]:
            previous = merged[-1]
            if item.start <= previous.end + timedelta(days=1):
                merged[-1] = DateRange(previous.start, max(previous.end, item.end))
            else:
                merged.append(item)
        current = tuple(merged)
    return current


def latest_complete_window(
    groups: Sequence[Sequence[DateRange]],
    days: int,
) -> DateRange | None:
    """Return the latest exact contiguous window common to every coverage group."""
    if days < 1:
        raise ValueError("latest complete window requires a positive day count.")
    intervals = common_intervals(groups)
    eligible = tuple(interval for interval in intervals if interval.days >= days)
    if not eligible:
        return None
    latest = max(eligible, key=lambda interval: (interval.end, interval.start))
    return DateRange(latest.end - timedelta(days=days - 1), latest.end)
