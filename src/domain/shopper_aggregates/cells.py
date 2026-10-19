# SPDX-License-Identifier: MIT-0
"""Closed enumerations: the cells a delivery must return, and `not_returned` for the rest.

A suppressed cell and an empty one both come back with no row (design §4.5), so a missing cell is
written as `cell_state = not_returned` with null measures (`NullMeaning.NOT_RETURNED`). It is
absent, never zero. An undeclared value means the pack's SQL and its contract disagree, so the
whole delivery is refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from itertools import product

from domain.data_contracts.contract import CELL_STATE_COLUMN, ExpectedAxis, ExpectedCellSet
from domain.shopper_aggregates.cohorts import triangle_cohorts
from domain.shopper_aggregates.findings import (
    ShopperAggregateContractError,
    ShopperAggregateFinding,
)

type CellKey = tuple[tuple[str, str], ...]
"""Column and value pairs naming one cell, slice or cohort, in declaration order."""

FREQUENCY_BUCKET_NULL = "FREQUENCY_BUCKET_NULL"
"""A NULL `frequency_bucket` on an unsuppressed pack 6 or 7 row (design §4.5, review DS3-2).

The bucket `CASE` has no `ELSE`, so a NULL can only mean a shopper with no in-window impression
slipped past the fence: the delivery is a contract violation, named apart from other NULL cells.
"""
NULL_CODES: dict[str, str] = {"frequency_bucket": FREQUENCY_BUCKET_NULL}
"""The finding code a NULL in a declared column carries, where it differs from CELL_VALUE_NULL."""


class CellState(StrEnum):
    """Whether an expected cell came back from AMC (ADR-099 point 1)."""

    RETURNED = "returned"
    NOT_RETURNED = "not_returned"


@dataclass(frozen=True, slots=True)
class EnumerationAxis:
    """One closed column and every value it may take, as the pack's SQL emits them."""

    column: str
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.column.strip():
            raise ValueError("an enumeration axis needs a column.")
        if not self.values or len(set(self.values)) != len(self.values):
            raise ValueError(f"{self.column}: an enumeration axis needs unique values.")
        if any(not value.strip() for value in self.values):
            raise ValueError(f"{self.column}: an enumeration value cannot be blank.")


@dataclass(frozen=True, slots=True)
class ClosedEnumeration:
    """The expected cells of one delivery, grouped into the slices loss is graded over.

    - `cells`: the axes every slice (and every returned cohort) must fill, for example
      `exposure_source_set` (packs 5) or `frequency_bucket` (packs 6 and 7).
    - `slices`: closed axes that split the delivery into graded slices; every value is
      expected (`identity_space` for packs 5 to 7, `acquisition_source_set` for 14 and 15).
    - `open_slices`: slice columns known only from the rows that came back. Pack 7 applies the
      enumeration to each ad product that returned at least one row; a rostered ad product with
      no row at all is the materializer's roster check, not the load's.
    - `cohorts`: axes inside a slice whose combinations may be absent entirely. A cohort with no
      row at any cell is an absent cohort: it is reported and left out of the expected cells.
    - `triangle`: a cohort vintage's cohorts, each with its own follow-up offsets (packs 14-15,
      `domain.shopper_aggregates.cohorts`). Each cohort is a cohort as above, and its cells are
      its offsets by `cells`; a row outside the triangle is undeclared.
    """

    cells: tuple[EnumerationAxis, ...]
    slices: tuple[EnumerationAxis, ...] = ()
    open_slices: tuple[str, ...] = ()
    cohorts: tuple[EnumerationAxis, ...] = ()
    triangle: tuple[tuple[CellKey, tuple[CellKey, ...]], ...] = ()
    triangle_columns: tuple[str, ...] = ()
    """The triangle's cohort and offset columns, when it has one."""

    def __post_init__(self) -> None:
        if not self.cells:
            raise ValueError("a closed enumeration needs at least one cell axis.")
        columns = self.key_columns
        if len(set(columns)) != len(columns):
            raise ValueError("a closed enumeration names a column twice.")
        if any(not column.strip() for column in self.open_slices):
            raise ValueError("an open slice needs a column.")
        if self.triangle and (self.cohorts or len(self.triangle_columns) != 2):  # noqa: PLR2004
            raise ValueError("a triangle names its cohort and offset columns, and no cohort axes.")

    @property
    def key_columns(self) -> tuple[str, ...]:
        """Every column that identifies a cell: slices, open slices, cohorts, then cells."""
        return (
            *(axis.column for axis in self.slices),
            *self.open_slices,
            *(axis.column for axis in self.cohorts),
            *self.triangle_columns,
            *(axis.column for axis in self.cells),
        )

    @property
    def closed_axes(self) -> tuple[EnumerationAxis, ...]:
        """Every axis whose values are declared."""
        return (*self.slices, *self.cohorts, *self.cells)


@dataclass(frozen=True, slots=True)
class EnumerationRequest:
    """The rows that survived suppression in one delivery, and the measures a gap nulls."""

    enumeration: ClosedEnumeration
    rows: tuple[Mapping[str, object], ...]
    measures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SliceCells:
    """Expected and missing cells in one graded slice, for the loss ratio (§4.4)."""

    key: CellKey
    expected: int
    missing: int
    absent_cohorts: tuple[CellKey, ...] = ()

    def __post_init__(self) -> None:
        if self.expected < 0 or not 0 <= self.missing <= self.expected:
            raise ValueError("a slice's missing cells must fall within its expected cells.")


@dataclass(frozen=True, slots=True)
class EnumerationOutcome:
    """Every returned row and every `not_returned` gap, plus per-slice cell counts."""

    rows: tuple[dict[str, object], ...]
    slices: tuple[SliceCells, ...]

    @property
    def not_returned(self) -> tuple[dict[str, object], ...]:
        """The rows written for expected cells that didn't come back."""
        return tuple(
            row for row in self.rows if row[CELL_STATE_COLUMN] == CellState.NOT_RETURNED.value
        )


def _text(value: object) -> str | None:
    if value is None:
        return None
    rendered = str(value)
    return rendered if rendered.strip() else None


def _value_findings(request: EnumerationRequest) -> list[ShopperAggregateFinding]:
    enumeration = request.enumeration
    findings: list[ShopperAggregateFinding] = []
    for index, row in enumerate(request.rows):
        for column in enumeration.open_slices:
            if _text(row.get(column)) is None:
                findings.append(
                    ShopperAggregateFinding(
                        "CELL_VALUE_NULL", f"row {index}: {column} is NULL on an unsuppressed row."
                    )
                )
        for axis in enumeration.closed_axes:
            value = _text(row.get(axis.column))
            if value is None:
                findings.append(
                    ShopperAggregateFinding(
                        NULL_CODES.get(axis.column, "CELL_VALUE_NULL"),
                        f"row {index}: {axis.column} is NULL on an unsuppressed row.",
                    )
                )
            elif value not in axis.values:
                findings.append(
                    ShopperAggregateFinding(
                        "CELL_VALUE_UNDECLARED",
                        f"row {index}: {axis.column} = {value!r} is not one of "
                        f"{', '.join(axis.values)}.",
                    )
                )
        findings.extend(_triangle_findings(enumeration, index, row))
        # A returned cell's distinct count is an integer, 0 when no shopper qualifies; a NULL
        # would contradict its `returned` state, because NULL means `not_returned` here.
        findings.extend(
            ShopperAggregateFinding(
                "CELL_VALUE_NULL", f"row {index}: {measure} is NULL on an unsuppressed row."
            )
            for measure in request.measures
            if _text(row.get(measure)) is None
        )
    return findings


def _triangle_findings(
    enumeration: ClosedEnumeration, index: int, row: Mapping[str, object]
) -> list[ShopperAggregateFinding]:
    """Refuse a row whose cohort and offset aren't a cell of the vintage's triangle."""
    if not enumeration.triangle:
        return []
    cohort_column, offset_column = enumeration.triangle_columns
    cohort, offset = _text(row.get(cohort_column)), _text(row.get(offset_column))
    if cohort is None or offset is None:
        return [
            ShopperAggregateFinding(
                "CELL_VALUE_NULL",
                f"row {index}: {cohort_column} or {offset_column} is NULL on an unsuppressed row.",
            )
        ]
    offsets = dict(enumeration.triangle).get(((cohort_column, cohort),))
    if offsets is None or ((offset_column, offset),) not in offsets:
        return [
            ShopperAggregateFinding(
                "CELL_VALUE_UNDECLARED",
                f"row {index}: {cohort_column} = {cohort!r}, {offset_column} = {offset!r} is not a "
                "cell of this vintage's cohort triangle.",
            )
        ]
    return []


def _combinations(axes: tuple[EnumerationAxis, ...]) -> tuple[CellKey, ...]:
    return tuple(
        tuple(zip((axis.column for axis in axes), values, strict=True))
        for values in product(*(axis.values for axis in axes))
    )


def _key(row: Mapping[str, object], columns: tuple[str, ...]) -> CellKey:
    return tuple((column, str(row[column])) for column in columns)


def _index_rows(request: EnumerationRequest) -> dict[CellKey, Mapping[str, object]]:
    columns = request.enumeration.key_columns
    present: dict[CellKey, Mapping[str, object]] = {}
    duplicates: list[ShopperAggregateFinding] = []
    for index, row in enumerate(request.rows):
        key = _key(row, columns)
        if key in present:
            rendered = ", ".join(f"{column}={value}" for column, value in key)
            duplicates.append(
                ShopperAggregateFinding("CELL_DUPLICATE", f"row {index} repeats cell {rendered}.")
            )
        present[key] = row
    if duplicates:
        raise ShopperAggregateContractError(tuple(duplicates))
    return present


def fill_closed_enumeration(request: EnumerationRequest) -> EnumerationOutcome:
    """Validate a delivery's surviving rows and write a `not_returned` row for every gap.

    Raises `ShopperAggregateContractError` for a NULL or undeclared value in a declared column, a
    NULL measure on a returned row, or a repeated cell: the delivery is rejected whole. The
    `not_returned` rows carry the cell's key columns, `cell_state` and a null for each measure;
    the load adds the delivery's own columns.
    """
    findings = _value_findings(request)
    if findings:
        raise ShopperAggregateContractError(tuple(findings))
    enumeration = request.enumeration
    present = _index_rows(request)
    open_values = sorted(
        {_key(row, enumeration.open_slices) for row in request.rows}
        if enumeration.open_slices
        else {()}
    )
    cell_keys = _combinations(enumeration.cells)
    if enumeration.triangle:
        cohort_cells = {
            cohort: tuple((*offset, *cell) for offset in offsets for cell in cell_keys)
            for cohort, offsets in enumeration.triangle
        }
    else:
        cohort_cells = dict.fromkeys(
            _combinations(enumeration.cohorts) if enumeration.cohorts else ((),), cell_keys
        )
    cohort_keys = tuple(cohort_cells)
    may_be_absent = bool(enumeration.cohorts or enumeration.triangle)
    rows: list[dict[str, object]] = []
    slices: list[SliceCells] = []
    for closed in _combinations(enumeration.slices) if enumeration.slices else ((),):
        for opened in open_values:
            slice_key = (*closed, *opened)
            returned = [
                cohort
                for cohort in cohort_keys
                if not may_be_absent
                or any((*slice_key, *cohort, *cell) in present for cell in cohort_cells[cohort])
            ]
            absent = tuple(cohort for cohort in cohort_keys if cohort not in returned)
            missing = 0
            for cohort in returned:
                for cell in cohort_cells[cohort]:
                    key = (*slice_key, *cohort, *cell)
                    row = present.get(key)
                    if row is not None:
                        rows.append({**row, CELL_STATE_COLUMN: CellState.RETURNED.value})
                        continue
                    missing += 1
                    rows.append(
                        {
                            **dict(key),
                            CELL_STATE_COLUMN: CellState.NOT_RETURNED.value,
                            **dict.fromkeys(request.measures),
                        }
                    )
            slices.append(
                SliceCells(
                    key=slice_key,
                    expected=sum(len(cohort_cells[cohort]) for cohort in returned),
                    missing=missing,
                    absent_cohorts=absent if may_be_absent else (),
                )
            )
    return EnumerationOutcome(rows=tuple(rows), slices=tuple(slices))


@dataclass(frozen=True, slots=True)
class DeclaredCellsRequest:
    """A delivery's surviving rows and the expected-cell sets its contract declares (plan D8).

    `measures` are the distinct counts a returned row must carry when a set names none; `nulls`
    every other column a `not_returned` row writes NULL (the additive measures and currencies).
    """

    sets: tuple[ExpectedCellSet, ...]
    rows: tuple[Mapping[str, object], ...]
    delivery_marketplace: str
    measures: tuple[str, ...]
    nulls: tuple[str, ...] = ()
    period: tuple[date, date] | None = None
    """The delivery's inclusive period, which a cohort triangle's cells are enumerated from."""


def _axis(axis: ExpectedAxis, marketplace: str) -> EnumerationAxis:
    return EnumerationAxis(axis.column, (marketplace,) if axis.from_delivery else axis.values)


def _triangle(
    cell_set: ExpectedCellSet, period: tuple[date, date] | None
) -> tuple[tuple[CellKey, tuple[CellKey, ...]], ...]:
    triangle = cell_set.cohorts
    if triangle is None:
        return ()
    if period is None:
        raise ValueError("a cohort triangle's cells need the delivery's period.")
    return tuple(
        (
            ((triangle.column, cohort.value),),
            tuple(((triangle.offset, str(offset)),) for offset in cohort.offsets),
        )
        for cohort in triangle_cohorts(triangle, period)
    )


def _enumeration(
    cell_set: ExpectedCellSet, marketplace: str, period: tuple[date, date] | None = None
) -> ClosedEnumeration:
    triangle = cell_set.cohorts
    return ClosedEnumeration(
        cells=tuple(_axis(axis, marketplace) for axis in cell_set.cells),
        slices=tuple(_axis(axis, marketplace) for axis in cell_set.slices),
        open_slices=cell_set.open_slices,
        triangle=_triangle(cell_set, period),
        triangle_columns=() if triangle is None else (triangle.column, triangle.offset),
    )


def _accepts(enumeration: ClosedEnumeration, row: Mapping[str, object]) -> bool:
    return all(
        _text(row.get(axis.column)) in axis.values for axis in enumeration.closed_axes
    ) and not _triangle_findings(enumeration, 0, row)


def _routed(
    request: DeclaredCellsRequest, enumerations: tuple[ClosedEnumeration, ...]
) -> tuple[list[Mapping[str, object]], ...]:
    """Give each row to the one declared set whose closed values it carries.

    With one set every row goes to it, so an undeclared value is that set's own finding. With
    several (a variant's exposure and outcome branches), a row no set accepts is refused here.
    """
    routed: tuple[list[Mapping[str, object]], ...] = tuple([] for _ in enumerations)
    if len(enumerations) == 1:
        routed[0].extend(request.rows)
        return routed
    unmatched: list[ShopperAggregateFinding] = []
    for index, row in enumerate(request.rows):
        target = next(
            (position for position, item in enumerate(enumerations) if _accepts(item, row)), None
        )
        if target is None:
            rendered = ", ".join(
                f"{column}={row.get(column)!r}"
                for column in dict.fromkeys(c for e in enumerations for c in e.key_columns)
            )
            unmatched.append(
                ShopperAggregateFinding(
                    "CELL_VALUE_UNDECLARED",
                    f"row {index}: {rendered} matches no declared cell set.",
                )
            )
            continue
        routed[target].append(row)
    if unmatched:
        raise ShopperAggregateContractError(tuple(unmatched))
    return routed


def fill_declared_cells(request: DeclaredCellsRequest) -> EnumerationOutcome:
    """Fill every declared set's cells and merge their slices into one graded outcome.

    Each set is one closed enumeration (`fill_closed_enumeration`); a gap row also writes NULL in
    each of `nulls` it doesn't already carry. Slices that two sets share (an `identity_space`
    value both of a variant's branches fill) are merged, so loss is graded once per slice.
    """
    enumerations = tuple(
        _enumeration(item, request.delivery_marketplace, request.period) for item in request.sets
    )
    routed = _routed(request, enumerations)
    findings: list[ShopperAggregateFinding] = []
    outcomes: list[EnumerationOutcome] = []
    for cell_set, enumeration, rows in zip(request.sets, enumerations, routed, strict=True):
        try:
            outcomes.append(
                fill_closed_enumeration(
                    EnumerationRequest(
                        enumeration=enumeration,
                        rows=tuple(rows),
                        measures=cell_set.measures or request.measures,
                    )
                )
            )
        except ShopperAggregateContractError as error:
            findings.extend(error.findings)
    if findings:
        raise ShopperAggregateContractError(tuple(findings))
    filled: list[dict[str, object]] = []
    for outcome in outcomes:
        for row in outcome.rows:
            if row[CELL_STATE_COLUMN] == CellState.NOT_RETURNED.value:
                filled.append({**dict.fromkeys(request.nulls), **row})
            else:
                filled.append(row)
    merged: dict[CellKey, SliceCells] = {}
    for outcome in outcomes:
        for item in outcome.slices:
            known = merged.get(item.key)
            merged[item.key] = (
                item
                if known is None
                else SliceCells(
                    key=item.key,
                    expected=known.expected + item.expected,
                    missing=known.missing + item.missing,
                    absent_cohorts=(*known.absent_cohorts, *item.absent_cohorts),
                )
            )
    return EnumerationOutcome(rows=tuple(filled), slices=tuple(merged.values()))
