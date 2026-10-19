# SPDX-License-Identifier: MIT-0
"""Technology-neutral CURRENT source-coverage boundary for presentation windows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from domain.catalog.freshness import common_intervals
from domain.shared_kernel.values import DateRange

if TYPE_CHECKING:
    from domain.catalog.capability import Catalog, Metric

__all__ = [
    "SourceCoverageBounds",
    "SourceCoverageContributor",
    "SourceCoveragePort",
    "SourceCoverageRequest",
    "SourceCoverageRequirement",
    "SourceCoverageResult",
    "metric_coverage_requirement",
]


def _require_nonblank(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a nonblank string.")


_PAIR_MEMBERS = 2


def _require_pairs(value: object, field_name: str) -> tuple[tuple[str, str], ...]:
    if type(value) is not tuple:
        raise ValueError(f"{field_name} must be a tuple of string pairs.")
    raw_pairs = cast(tuple[object, ...], value)
    pairs: list[tuple[str, str]] = []
    for pair in raw_pairs:
        if type(pair) is not tuple:
            raise ValueError(f"{field_name} must contain two-string tuples.")
        raw_pair = cast(tuple[object, ...], pair)
        if len(raw_pair) != _PAIR_MEMBERS:
            raise ValueError(f"{field_name} must contain two-string tuples.")
        first = cast(str, raw_pair[0])
        second = cast(str, raw_pair[1])
        _require_nonblank(first, f"{field_name} key")
        _require_nonblank(second, f"{field_name} value")
        pairs.append((first, second))
    result = tuple(pairs)
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} cannot repeat a pair.")
    return result


def _require_exact_intervals(value: object, field_name: str) -> tuple[DateRange, ...]:
    if type(value) is not tuple:
        raise ValueError(f"{field_name} must be a tuple of DateRange values.")
    raw_intervals = cast(tuple[object, ...], value)
    if any(type(item) is not DateRange for item in raw_intervals):
        raise ValueError(f"{field_name} must be a tuple of DateRange values.")
    intervals = cast(tuple[DateRange, ...], raw_intervals)
    if common_intervals((intervals,)) != intervals:
        raise ValueError(f"{field_name} must be sorted, disjoint, and coalesced.")
    return intervals


@dataclass(frozen=True, slots=True)
class SourceCoverageRequirement:
    """Exact logical sources and selectors that one presentation metric requires.

    `facts` names the canonical facts the metric reads (`Catalog.facts_read`). When given, only
    a source's writers of those facts decide its coverage, so a pack that writes another fact
    from the same source dataset (and has not delivered yet) never empties this metric's
    coverage. Empty keeps every fact writer of the source, the original rule.
    """

    sources: tuple[tuple[str, str], ...]
    selectors: tuple[tuple[str, str], ...] = ()
    facts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.sources) is not tuple or not self.sources:
            raise ValueError("source coverage sources must be a nonempty tuple of string pairs.")
        _require_pairs(self.sources, "source coverage sources")
        selectors = _require_pairs(self.selectors, "source coverage selectors")
        if tuple(sorted(selectors)) != selectors:
            raise ValueError("source coverage selectors must use canonical sorted order.")
        if type(self.facts) is not tuple:
            raise ValueError("source coverage facts must be a tuple of fact names.")
        for fact in cast(tuple[object, ...], self.facts):
            _require_nonblank(fact, "source coverage fact")
        if len(set(self.facts)) != len(self.facts):
            raise ValueError("source coverage facts cannot repeat a fact.")
        if tuple(sorted(self.facts)) != self.facts:
            raise ValueError("source coverage facts must use canonical sorted order.")


def metric_coverage_requirement(
    catalog: Catalog, metric: Metric, selectors: tuple[tuple[str, str], ...] = ()
) -> SourceCoverageRequirement:
    """Return one metric's coverage requirement, scoped to the facts it reads.

    Every metric-derived requirement is built here, so none can fall back to the unscoped rule
    by omission: a metric that names no source or no fact is refused, never left unscoped.
    """
    sources = catalog.sources_for(metric)
    facts = tuple(sorted(catalog.facts_read(metric)))
    if not sources or not facts:
        raise ValueError(f"metric {metric.name} declares no coverage source or fact.")
    return SourceCoverageRequirement(sources, selectors, facts)


@dataclass(frozen=True, slots=True)
class SourceCoverageRequest:
    """Declared source requirements whose CURRENT coverage must be inspected."""

    requirements: tuple[SourceCoverageRequirement, ...]

    def __post_init__(self) -> None:
        if type(self.requirements) is not tuple or not self.requirements:
            raise ValueError("source coverage request needs at least one requirement.")
        if any(type(item) is not SourceCoverageRequirement for item in self.requirements):
            raise ValueError(
                "source coverage requirements must be SourceCoverageRequirement values."
            )
        if len(set(self.requirements)) != len(self.requirements):
            raise ValueError("source coverage request cannot repeat a requirement.")


@dataclass(frozen=True, slots=True)
class SourceCoverageContributor:
    """Exact CURRENT coverage and load context for one required logical source scope."""

    requirement: SourceCoverageRequirement
    source_system: str
    source_dataset: str
    contributor_id: str
    intervals: tuple[DateRange, ...]
    loaded_at: datetime | None

    def __post_init__(self) -> None:
        if type(self.requirement) is not SourceCoverageRequirement:
            raise ValueError("source coverage contributor requires a source coverage requirement.")
        _require_nonblank(self.source_system, "source coverage contributor source system")
        _require_nonblank(self.source_dataset, "source coverage contributor source dataset")
        _require_nonblank(self.contributor_id, "source coverage contributor id")
        _require_exact_intervals(self.intervals, "source coverage contributor intervals")
        if self.loaded_at is not None and (
            type(self.loaded_at) is not datetime or self.loaded_at.tzinfo is None
        ):
            raise ValueError("source coverage contributor loaded_at must be timezone-aware.")


@dataclass(frozen=True, slots=True)
class SourceCoverageBounds:
    """Exact non-bridged common intervals and their outer runtime bounds."""

    intervals: tuple[DateRange, ...]
    earliest_start: date | None
    latest_end: date | None

    def __post_init__(self) -> None:
        intervals = _require_exact_intervals(self.intervals, "source coverage runtime intervals")
        if intervals:
            if self.earliest_start != intervals[0].start or self.latest_end != intervals[-1].end:
                raise ValueError("source coverage runtime bounds must match exact interval bounds.")
        elif self.earliest_start is not None or self.latest_end is not None:
            raise ValueError("empty source coverage cannot carry runtime bounds.")


@dataclass(frozen=True, slots=True)
class SourceCoverageResult:
    """Exact contributor intervals, latest source load, and safe runtime coverage bounds."""

    contributors: tuple[SourceCoverageContributor, ...]
    bounds: SourceCoverageBounds
    latest_loaded_at: datetime | None

    def __post_init__(self) -> None:
        if type(self.contributors) is not tuple or any(
            type(item) is not SourceCoverageContributor for item in self.contributors
        ):
            raise ValueError("source coverage contributors must be a tuple of contributors.")
        keys = tuple(
            (
                item.requirement.sources,
                item.requirement.selectors,
                item.requirement.facts,
                item.source_system,
                item.source_dataset,
                item.contributor_id,
            )
            for item in self.contributors
        )
        if tuple(sorted(keys)) != keys or len(set(keys)) != len(keys):
            raise ValueError("source coverage contributors must be ordered and unique.")
        if type(self.bounds) is not SourceCoverageBounds:
            raise ValueError("source coverage result requires runtime bounds.")
        if self.latest_loaded_at is not None and (
            type(self.latest_loaded_at) is not datetime or self.latest_loaded_at.tzinfo is None
        ):
            raise ValueError("source coverage latest_loaded_at must be timezone-aware.")
        loaded = tuple(item.loaded_at for item in self.contributors if item.loaded_at is not None)
        if self.latest_loaded_at != (max(loaded) if loaded else None):
            raise ValueError(
                "source coverage latest_loaded_at must match contributor load context."
            )


@runtime_checkable
class SourceCoveragePort(Protocol):
    """Return exact CURRENT source coverage without applying page-window policy."""

    def source_coverage(self, request: SourceCoverageRequest) -> SourceCoverageResult:
        """Inspect declared requirements and return exact contributor/runtime coverage."""
        ...
