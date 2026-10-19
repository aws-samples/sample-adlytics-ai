# SPDX-License-Identifier: MIT-0
"""The planner's own output, split into the phases the PB-1 and PB-2 readiness tests compare.

Phase A is every cadence delivery whose run day lies after the cut (the newest 56 run days);
phase B is every `backfill` delivery. Nothing here lands anything: phase B never starts in this
plan, and these tests read only the planned bytes.
"""

from __future__ import annotations

import collections
import csv
import functools
import io
import json
import pathlib
from datetime import date
from typing import cast

from adapters.jobs.fixture_generation.shapes import WINDOW_COLUMNS
from adapters.jobs.land_fixtures.bundle import (
    AMC_DAILY_DATE_COLUMNS,
    AMC_PERIODIC_FIXTURES,
    DeliveryClass,
    LandedDelivery,
    plan_with_backfill,
)
from adapters.jobs.land_fixtures.bundle import (  # the planner's own cell rule
    _written_cells as written_cells,  # pyright: ignore[reportPrivateUsage]
)
from domain.operations.run_request import HistoricalPolicy

ROOT = pathlib.Path(__file__).resolve().parents[2]

type Cell = tuple[str, date]


@functools.cache
def plan() -> tuple[tuple[LandedDelivery, ...], HistoricalPolicy]:
    """Return the full plan, phase-B backfill included, and its replay band."""
    return plan_with_backfill(ROOT)


def phase_a() -> tuple[LandedDelivery, ...]:
    """Return the cadence deliveries phase A replays: run day after the cut."""
    planned, policy = plan()
    return tuple(
        item
        for item in planned
        if item.delivery_class is DeliveryClass.CADENCE
        and item.run_day is not None
        and item.run_day > policy.cut
    )


def phase_b() -> tuple[LandedDelivery, ...]:
    """Every phase-B backfill delivery."""
    planned, _ = plan()
    return tuple(item for item in planned if item.delivery_class is DeliveryClass.BACKFILL)


def restating(delivery: LandedDelivery) -> bool:
    """Return whether a delivery can restate a day: it isn't a periodic corpus (ADR-100).

    A periodic delivery never restates: its phase-B copy is the same immutable delivery
    (`bundle._backfill`), and a week and a month of one corpus share days but not a grain, so
    their (table, date) cells meeting is no restatement.
    """
    return family(delivery) not in AMC_PERIODIC_FIXTURES


@functools.cache
def overlaps() -> dict[Cell, tuple[LandedDelivery, ...]]:
    """Every (table, date) a phase-B delivery and a different phase-A delivery both write."""
    by_cell: dict[Cell, list[LandedDelivery]] = collections.defaultdict(list)
    for item in filter(restating, phase_a()):
        for cell in written_cells(item):
            by_cell[cell].append(item)
    found: dict[Cell, tuple[LandedDelivery, ...]] = {}
    for backfill in filter(restating, phase_b()):
        for cell in written_cells(backfill):
            daily = [
                item
                for item in by_cell.get(cell, [])
                if item.manifest.manifest_key != backfill.manifest.manifest_key
            ]
            if daily:
                found[cell] = (*found.get(cell, ()), backfill, *daily)
    return {
        cell: tuple({item.manifest.manifest_key: item for item in items}.values())
        for cell, items in found.items()
    }


def rows(delivery: LandedDelivery, day: date) -> tuple[str, ...]:
    """Return the delivery's rows for one day, canonically ordered (AMC CSV or Vendor JSON).

    An AMC row's `time_window_start` and `time_window_end` are its own delivery's window
    (`bundle._windowed`), checked against the registration at finalize and mapped to no contract
    field, so they are left out: the comparison is of the cell's data, not of which delivery
    carried it.
    """
    family = delivery.fixture.split("#", 1)[0]
    column = AMC_DAILY_DATE_COLUMNS.get(family)
    found: list[str] = []
    for item in delivery.data:
        text = item.body.decode("utf-8")
        if column is not None:
            found.extend(
                json.dumps(
                    {key: value for key, value in row.items() if key not in WINDOW_COLUMNS},
                    sort_keys=True,
                )
                for row in csv.DictReader(io.StringIO(text))
                if row.get(column) == day.isoformat()
            )
            continue
        document = cast("dict[str, object]", json.loads(text))
        for value in document.values():
            if isinstance(value, list):
                found.extend(
                    json.dumps(row, sort_keys=True)
                    for row in cast("list[object]", value)
                    if isinstance(row, dict)
                    and str(cast("dict[str, object]", row).get("startDate", ""))[:10]
                    == day.isoformat()
                )
    return tuple(sorted(found))


def family(delivery: LandedDelivery) -> str:
    """Return the fixture family a planned delivery comes from."""
    return delivery.fixture.split("#", 1)[0]
