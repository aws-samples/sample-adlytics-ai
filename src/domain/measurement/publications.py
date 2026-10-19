# SPDX-License-Identifier: MIT-0
"""Bind logical measurement plans to immutable successful Semantic V2 publications."""

from __future__ import annotations

from dataclasses import dataclass

from domain.measurement.planning import QueryPlan
from domain.semantics.publication import SuccessfulPublication

__all__ = ["BoundQueryPlan"]


@dataclass(frozen=True, slots=True)
class BoundQueryPlan:
    """One logical plan with exact immutable publications selected independently per fact side."""

    logical: QueryPlan
    primary: tuple[SuccessfulPublication, ...]
    primary_filters: tuple[tuple[str, str], ...]
    joined: tuple[SuccessfulPublication, ...] = ()
    joined_filters: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.primary:
            raise ValueError("bound query plan requires primary CURRENT publications.")
        if len({item.publication_id for item in self.primary}) != len(self.primary):
            raise ValueError("bound query plan repeats a primary publication.")
        if any(item.partition.scope.dataset.value != self.logical.fact for item in self.primary):
            raise ValueError("primary publication belongs to another fact.")
        self._validate_filters(self.primary_filters, tuple(self.logical.filters), "primary")
        if self.logical.join is None and (self.joined or self.joined_filters):
            raise ValueError("single-fact plan cannot carry joined publications or filters.")
        if self.logical.join is not None:
            if not self.joined:
                raise ValueError("drill-across plan requires denominator CURRENT publications.")
            self._validate_filters(
                self.joined_filters,
                tuple(self.logical.join.filters),
                "denominator",
            )
            if len({item.publication_id for item in self.joined}) != len(self.joined):
                raise ValueError("bound query plan repeats a denominator publication.")
            if any(
                item.partition.scope.dataset.value != self.logical.join.fact for item in self.joined
            ):
                raise ValueError("denominator publication belongs to another fact.")

    @staticmethod
    def _validate_filters(
        filters: tuple[tuple[str, str], ...],
        logical_keys: tuple[str, ...],
        label: str,
    ) -> None:
        if tuple(sorted(filters)) != filters:
            raise ValueError(f"{label} execution filters must be sorted.")
        keys = tuple(key for key, _value in filters)
        if len(set(keys)) != len(keys):
            raise ValueError(f"{label} execution filters repeat a column.")
        if set(keys) != set(logical_keys):
            raise ValueError(f"{label} execution filters must preserve logical filter columns.")

    @property
    def primary_ids(self) -> tuple[str, ...]:
        """Sorted immutable ids admitted to the primary fact scan."""
        return tuple(sorted(item.publication_id.value for item in self.primary))

    @property
    def joined_ids(self) -> tuple[str, ...]:
        """Sorted immutable ids admitted to the denominator fact scan."""
        return tuple(sorted(item.publication_id.value for item in self.joined))

    @property
    def all_publications(self) -> tuple[SuccessfulPublication, ...]:
        """Every selected success, deduplicated by immutable publication id."""
        found: dict[str, SuccessfulPublication] = {}
        for item in (*self.primary, *self.joined):
            found[item.publication_id.value] = item
        return tuple(found[key] for key in sorted(found))
