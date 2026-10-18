# SPDX-License-Identifier: MIT-0
"""Folds: streamed reductions that keep a bounded summary of a read, never its rows (ADR-143).

A bake slot is read one of two ways. A **held** answer keeps its rows, up to the bake ceiling,
because the render needs every one. A **reduction** streams its rows through a fold that keeps
only what the render needs: the keys observed, the keys by group, or sums by subset and day.
`answers.fold_answer` turns each row into a figure by the same rules a held answer uses and
hands the figure here; a fold never re-implements how a row becomes a figure.

**Every fold enforces its bound.** Each has a `max_kept` set at construction. Once keeping one
more entry would exceed it, the fold sets `overflowed` and keeps nothing more, though the read
still counts its rows. An overflow is the same `rows_over_ceiling` refusal as a truncated read,
so a fold never holds more than the memory model counts for it, whatever the statement returns.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Final, Protocol

from domain.catalog.capability import MetricKind
from domain.shared_kernel.errors import ErrorCode, InvariantError

if TYPE_CHECKING:  # pragma: no cover
    from domain.measurement.figures import Figure
    from domain.measurement.planning import QueryPlan
    from domain.narrative.shaped_response import Metadata, Units

__all__ = [
    "FOLD_INELIGIBLE",
    "FOLD_UNDECLARED_CELL",
    "AuthorizedDailySums",
    "Fold",
    "FoldResult",
    "KeysByGroup",
    "ObservedKeys",
    "require_foldable",
]

FOLD_INELIGIBLE: Final = ErrorCode("FOLD", 1)
"""A fold was declared over a plan whose rows can't be reduced exactly: a defect."""
FOLD_UNDECLARED_CELL: Final = ErrorCode("FOLD", 2)
"""A figure arrived for a cell the fold didn't declare (a day outside its window): a defect."""


class Fold[T](Protocol):
    """A bounded reduction over a read's figures."""

    @property
    def max_kept(self) -> int:
        """The most entries this fold may keep; the memory model counts exactly this."""
        ...

    @property
    def overflowed(self) -> bool:
        """Whether keeping one more entry would have exceeded `max_kept`."""
        ...

    def add(self, figure: Figure) -> None:
        """Take one figure; keep what the summary needs, within `max_kept`."""
        ...

    def value(self) -> T:
        """Return the summary kept so far."""
        ...


@dataclass(frozen=True, slots=True)
class FoldResult[T]:
    """A fold's summary, and what the read said about itself.

    `metadata` is built by the one builder a held answer's metadata comes from
    (`answers.answer_metadata`), with `row_count = rows_read`, so coverage, freshness, caveats,
    suppression and truncation can't diverge between a held and a folded answer.
    `truncated = rows_read > max_rows or overflowed`.
    """

    value: T
    rows_read: int
    truncated: bool
    units: Units | None
    bytes_scanned: int
    metadata: Metadata


class _Bounded:
    """The shared bound: count what is kept, refuse to keep past `max_kept`."""

    def __init__(self, max_kept: int) -> None:
        if isinstance(max_kept, bool) or max_kept < 1:
            raise ValueError("a fold keeps at least one entry.")
        self._max_kept = max_kept
        self._kept = 0
        self._overflowed = False

    @property
    def max_kept(self) -> int:
        """The most entries this fold keeps."""
        return self._max_kept

    @property
    def overflowed(self) -> bool:
        """Whether an entry past `max_kept` was refused."""
        return self._overflowed

    def _room(self) -> bool:
        """Claim room for one more entry, or mark the overflow and keep nothing."""
        if self._kept >= self._max_kept:
            self._overflowed = True
            return False
        self._kept += 1
        return True


def _value(figure: Figure, column: str) -> str:
    dimensions = dict(figure.dimensions)
    if column not in dimensions:
        raise InvariantError(
            FOLD_INELIGIBLE,
            "A fold reads a column its figures don't carry.",
            context={"column": column},
        )
    return dimensions[column]


class ObservedKeys(_Bounded):
    """The set of key tuples with at least one defined figure, at most `max_kept` of them."""

    def __init__(self, columns: tuple[str, ...], max_kept: int) -> None:
        super().__init__(max_kept)
        if not columns:
            raise ValueError("observed keys read at least one column.")
        self._columns = columns
        self._keys: set[tuple[str, ...]] = set()

    def add(self, figure: Figure) -> None:
        """Keep the figure's key tuple, once, within the bound."""
        key = tuple(_value(figure, column) for column in self._columns)
        if key in self._keys or not self._room():
            return
        self._keys.add(key)

    def value(self) -> tuple[tuple[str, ...], ...]:
        """Return the observed key tuples, sorted."""
        return tuple(sorted(self._keys))


class KeysByGroup(_Bounded):
    """`group → sorted keys` with a defined figure; `max_kept` bounds the keys over all groups."""

    def __init__(self, group: str, key: str, max_kept: int) -> None:
        super().__init__(max_kept)
        self._group = group
        self._key = key
        self._groups: dict[str, set[str]] = {}

    def add(self, figure: Figure) -> None:
        """Keep the figure's key under its group, once, within the bound over all groups."""
        group, key = _value(figure, self._group), _value(figure, self._key)
        members = self._groups.get(group)
        if members is not None and key in members:
            return
        if not self._room():
            return
        self._groups.setdefault(group, set()).add(key)

    def value(self) -> dict[str, tuple[str, ...]]:
        """Return each group's sorted keys, groups sorted."""
        return {group: tuple(sorted(keys)) for group, keys in sorted(self._groups.items())}


class AuthorizedDailySums:
    """`subset → day → sum`, adding only the identities each subset authorizes.

    Its cells are declared before the read (`len(subsets) x len(days)`), so it can't overflow:
    a figure for an undeclared day is an `InvariantError`, because the read is fenced to the
    window. The subsets are held by reference, never copied: they are structures the render
    already holds, which the memory model counts once.
    """

    def __init__(  # noqa: PLR0913 -- the subsets, the days and the two columns they key
        self,
        subsets: Mapping[str, frozenset[str]],
        days: tuple[date, ...],
        *,
        identity: str,
        day_column: str,
    ) -> None:
        if not subsets or not days:
            raise ValueError("authorized daily sums declare at least one subset and one day.")
        self.subsets = subsets
        self._days = frozenset(days)
        self._identity = identity
        self._day_column = day_column
        self._sums: dict[str, dict[date, Decimal]] = {
            name: dict.fromkeys(days, Decimal(0)) for name in subsets
        }

    @property
    def max_kept(self) -> int:
        """The declared cells: subsets times days."""
        return len(self._sums) * len(self._days)

    @property
    def overflowed(self) -> bool:
        """Never: every kept cell is declared before the first figure."""
        return False

    def add(self, figure: Figure) -> None:
        """Add the figure to every subset authorizing its identity, on its declared day."""
        day = date.fromisoformat(_value(figure, self._day_column))
        if day not in self._days:
            raise InvariantError(
                FOLD_UNDECLARED_CELL,
                "A daily figure arrived for a day the fold didn't declare.",
                context={"problem": "the read is fenced to the declared window"},
            )
        identity = _value(figure, self._identity)
        for name, members in self.subsets.items():
            if identity in members:
                self._sums[name][day] += Decimal(figure.value)

    def value(self) -> dict[str, dict[date, Decimal]]:
        """Return each subset's daily sums, days in order."""
        return {name: dict(sorted(days.items())) for name, days in self._sums.items()}


def require_foldable(plan: QueryPlan) -> None:
    """Refuse a fold over a plan whose rows can't be reduced exactly (closed eligibility).

    A simple metric, one measure, no join, and either no roster or an empty key-read roster.
    Ratios and graded answers stay held. Checked when the batch is prepared, so an ineligible
    fold is a defect a unit test catches, never a runtime surprise.
    """
    problem = None
    if plan.metric.kind is not MetricKind.SIMPLE:
        problem = "not a simple metric"
    elif len(plan.measures) != 1:
        problem = "not one measure"
    elif plan.join is not None:
        problem = "a joined read"
    elif plan.roster is not None and plan.roster.entries:
        problem = "a graded roster read"
    if problem is not None:
        raise InvariantError(
            FOLD_INELIGIBLE,
            "A fold was declared over a read whose rows can't be reduced exactly.",
            context={"problem": problem, "metric": plan.metric.name},
        )
