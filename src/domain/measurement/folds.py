# SPDX-License-Identifier: MIT-0
"""Folds: streamed reductions that keep a bounded summary of a read, never its rows (ADR-143).

A bake slot is read one of two ways. A **held** answer keeps its rows, up to the bake ceiling,
because the render needs every one. A **reduction** streams its rows through a fold that keeps
only what the render needs: the keys observed, the keys by group, sums by subset and day, or one
compact record per entity (`KeyedColumns`, ADR-152).
`answers.fold_answer` turns each row into a figure by the same rules a held answer uses and
hands the figure here; a fold never re-implements how a row becomes a figure.

**Every fold enforces its bound.** Each has a `max_kept` set at construction. Once keeping one
more entry would exceed it, the fold sets `overflowed` and keeps nothing more, though the read
still counts its rows. An overflow is the same `rows_over_ceiling` refusal as a truncated read,
so a fold never holds more than the memory model counts for it, whatever the statement returns.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
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
    "FOLD_REWRITTEN_CELL",
    "FOLD_UNDECLARED_CELL",
    "AuthorizedDailySums",
    "ColumnView",
    "Fold",
    "FoldResult",
    "KeyedColumn",
    "KeyedColumns",
    "KeysByGroup",
    "ObservedKeys",
    "require_foldable",
]

FOLD_INELIGIBLE: Final = ErrorCode("FOLD", 1)
"""A fold was declared over a plan whose rows can't be reduced exactly: a defect."""
FOLD_UNDECLARED_CELL: Final = ErrorCode("FOLD", 2)
"""A figure arrived for a cell the fold didn't declare (a day outside its window): a defect."""
FOLD_REWRITTEN_CELL: Final = ErrorCode("FOLD", 3)
"""A second figure arrived for one key's cell: the read groups by its key, so a defect."""


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
    """The set of key tuples with at least one defined figure, at most `max_kept` of them.

    `among`, when given, names the only keys asked about: any other key is ignored, so a fold
    bounded by `len(among)` can't overflow however many other keys the read returns.
    """

    def __init__(
        self,
        columns: tuple[str, ...],
        max_kept: int,
        among: frozenset[tuple[str, ...]] | None = None,
    ) -> None:
        super().__init__(max_kept)
        if not columns:
            raise ValueError("observed keys read at least one column.")
        self._columns = columns
        self._among = among
        self._keys: set[tuple[str, ...]] = set()

    def add(self, figure: Figure) -> None:
        """Keep the figure's key tuple, once, within the bound, if it is one asked about."""
        key = tuple(_value(figure, column) for column in self._columns)
        if self._among is not None and key not in self._among:
            return
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


class KeyedColumns(_Bounded):
    """One compact record per entity: `key → one value per declared column` (ADR-152).

    The store a page that renders every entity keeps instead of one held answer per read: each
    read is one column, folded by its `KeyedColumn`, so an entity costs one key and one list of
    values however many reads describe it. `among` names the only keys asked about (the
    authorized population), at most `max_kept` of them: every record is declared before the
    first figure, so the store never overflows, and a figure for any other key is never kept.
    Each cell is written at most once.

    Its reads fold on concurrent workers, each into its own column. Declaring every record up
    front means no worker inserts a key, so two workers never race on one record.
    """

    def __init__(  # noqa: PLR0913, PLR0917 -- the key, the columns, the bound and the keys asked
        self,
        key_columns: tuple[str, ...],
        columns: tuple[str, ...],
        max_kept: int,
        among: frozenset[tuple[str, ...]],
    ) -> None:
        super().__init__(max_kept)
        if not key_columns or not columns or len(set(columns)) != len(columns):
            raise ValueError("keyed columns read at least one key column and distinct columns.")
        if len(among) > max_kept:
            raise ValueError("keyed columns are asked about more keys than they may keep.")
        self.key_columns = key_columns
        self._index = {name: position for position, name in enumerate(columns)}
        self._records: dict[tuple[str, ...], list[Decimal | int | None]] = {
            key: self._empty_record() for key in among
        }
        self._negative: set[str] = set()

    def column(self, name: str) -> KeyedColumn:
        """Return the fold that writes column `name` of every kept key."""
        if name not in self._index:
            raise ValueError("keyed columns don't declare that column.")
        return KeyedColumn(self, name)

    def put(self, name: str, figure: Figure) -> None:
        """Write one figure into column `name` of its key's record, if its key is asked about."""
        key = tuple(_value(figure, column) for column in self.key_columns)
        value = figure.value
        if value < 0:
            self._negative.add(name)
        record = self._records.get(key)
        if record is None:
            return
        position = self._index[name]
        if record[position] is not None:
            raise InvariantError(
                FOLD_REWRITTEN_CELL,
                "A keyed column received a second figure for one key.",
                context={"problem": "the read groups by its key"},
            )
        record[position] = value

    def _empty_record(self) -> list[Decimal | int | None]:
        return [None] * len(self._index)

    def cell(self, name: str, key: tuple[str, ...]) -> Decimal | int | None:
        """Return one key's value in column `name`, or None when the read returned none."""
        record = self._records.get(key)
        return None if record is None else record[self._index[name]]

    def keys_in(self, name: str) -> Iterator[tuple[str, ...]]:
        """Iterate the keys column `name` holds a value for."""
        position = self._index[name]
        return (key for key, record in self._records.items() if record[position] is not None)

    def negative_in(self, name: str) -> bool:
        """Whether column `name`'s read returned a negative figure, for any key."""
        return name in self._negative


class ColumnView(Mapping[tuple[str, ...], Decimal | int]):
    """One column of a `KeyedColumns` store, read as `key → value`; nothing is copied."""

    def __init__(self, store: KeyedColumns, name: str) -> None:
        self._store = store
        self._name = name

    def __getitem__(self, key: tuple[str, ...]) -> Decimal | int:
        value = self._store.cell(self._name, key)
        if value is None:
            raise KeyError(key)
        return value

    def __iter__(self) -> Iterator[tuple[str, ...]]:
        return self._store.keys_in(self._name)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    @property
    def negative(self) -> bool:
        """Whether any figure the read returned, for any key, was negative."""
        return self._store.negative_in(self._name)


class KeyedColumn:
    """The fold of one read into its column of a shared `KeyedColumns` store.

    It keeps each row's figure as `figure_from_row` made it and never combines rows, so a ratio
    or joined read folds exactly (`require_foldable(per_row=True)`). It is its own reduction
    factory: the store, not the fold, holds the state.
    """

    def __init__(self, store: KeyedColumns, name: str) -> None:
        self._store = store
        self.name = name

    def __call__(self) -> KeyedColumn:
        """Return this column's fold (the request's `reduction` factory)."""
        return self

    @property
    def max_kept(self) -> int:
        """The store's bound: the keys it may keep, shared by every column."""
        return self._store.max_kept

    @property
    def overflowed(self) -> bool:
        """Whether the store refused a key past its bound."""
        return self._store.overflowed

    @property
    def store(self) -> KeyedColumns:
        """The store this column writes, which the memory model counts once."""
        return self._store

    def add(self, figure: Figure) -> None:
        """Write the figure into this column of its key's record, if its key is asked about."""
        self._store.put(self.name, figure)

    def value(self) -> ColumnView:
        """Return this column as a read-only `key → value` view."""
        return ColumnView(self._store, self.name)


class AuthorizedDailySums:
    """`subset → day → sum`, adding only the identities each subset authorizes.

    Its cells are declared before the read (`len(subsets) x len(days)`), so it can't overflow:
    a figure for an undeclared day is an `InvariantError`, because the read is fenced to the
    window. The subsets are held by reference, never copied: they are structures the render
    already holds, which the memory model counts once.
    """

    def __init__(  # noqa: PLR0913 -- the subsets, the days and the columns they key
        self,
        subsets: Mapping[str, frozenset[tuple[str, ...]]],
        days: tuple[date, ...],
        *,
        identity: tuple[str, ...],
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
        identity = tuple(_value(figure, column) for column in self._identity)
        for name, members in self.subsets.items():
            if identity in members:
                self._sums[name][day] += Decimal(figure.value)

    def value(self) -> dict[str, dict[date, Decimal]]:
        """Return each subset's daily sums, days in order."""
        return {name: dict(sorted(days.items())) for name, days in self._sums.items()}


def require_foldable(plan: QueryPlan, *, per_row: bool = False) -> None:
    """Refuse a fold over a plan whose rows can't be reduced exactly (closed eligibility).

    A simple metric, one measure, no join, and either no roster or an empty key-read roster.
    Ratios and graded answers stay held. A `per_row` fold (`KeyedColumn`) keeps each row's figure
    and combines none, so it admits ratios and joins; only a graded roster, which can withdraw
    every figure after the read, stays held. Checked when the batch is prepared, so an
    ineligible fold is a defect a unit test catches, never a runtime surprise.
    """
    problem = None
    if plan.roster is not None and plan.roster.entries:
        problem = "a graded roster read"
    elif per_row:
        pass
    elif plan.metric.kind is not MetricKind.SIMPLE:
        problem = "not a simple metric"
    elif len(plan.measures) != 1:
        problem = "not one measure"
    elif plan.join is not None:
        problem = "a joined read"
    if problem is not None:
        raise InvariantError(
            FOLD_INELIGIBLE,
            "A fold was declared over a read whose rows can't be reduced exactly.",
            context={"problem": problem, "metric": plan.metric.name},
        )
