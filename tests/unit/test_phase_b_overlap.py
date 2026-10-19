# SPDX-License-Identifier: MIT-0
"""PB-1 (a): every overlap between phase-B windows and phase-A restatement windows (S4).

Each daily run restates back `restatement_window_days - 1` days (`bundle.py`, `_source_runs`), so
phase-A runs near the cut write days phase B also writes. The test lists every such (table,
date) cell from the planner's own output with its expected winner, the newest `retrieved`, and
proves the resolution the code applies cannot change what is current:

- the backfill retrieval slots sit above every cadence retrieval, so in every overlapping cell
  the backfill is strictly the newest retrieval;
- ADR-047's publication rule (`semantic_sidecar.py`, `_split_stale_stages`) orders candidates by
  source revision (the delivery's watermark), skipping a strictly older one and republishing an
  equal one. In some cells the newest retrieval carries an older watermark than a phase-A
  delivery, so that phase-A publication stays current; the test proves its rows equal the
  newest retrieval's rows in every such cell, and that every candidate tied at the top
  watermark carries the same rows, so arrival order never changes the current rows.
"""

from __future__ import annotations

import pytest
from phase_b_support import family, overlaps, phase_b, plan, restating, rows

from adapters.jobs.land_fixtures.bundle import (
    AMC_DAILY_DATE_COLUMNS,
    AMC_PERIODIC_FIXTURES,
    DeliveryClass,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-086")]


def test_pb1a_phase_b_overlaps_daily_restatement_windows_and_every_overlap_is_listed() -> None:
    cells = overlaps()
    assert cells, "phase B overlaps phase-A restatement windows near the cut"
    for (table, day), items in cells.items():
        assert any(item.delivery_class is DeliveryClass.BACKFILL for item in items)
        assert any(item.delivery_class is DeliveryClass.CADENCE for item in items)
        assert table and day <= max(item.manifest.period_end for item in items)
    # Every overlap is an AMC daily family; a Vendor backfill reuses its day's own last report.
    assert {family(item) for items in cells.values() for item in items} <= set(
        AMC_DAILY_DATE_COLUMNS
    )
    assert len([item for item in phase_b() if restating(item)]) == 2038


def test_pb1a_a_periodic_backfill_is_its_own_cadence_delivery_so_it_never_restates() -> None:
    """ADR-100: a complete week or month lands once; phase B carries that same delivery."""
    planned, policy = plan()
    periodic = [item for item in phase_b() if not restating(item)]
    # Every periodic family with a period landing on or before the cut; packs 8-15 have none.
    landable_periodic = set(AMC_PERIODIC_FIXTURES) & {
        family(item)
        for item in planned
        if item.delivery_class is DeliveryClass.CADENCE
        and item.run_day is not None
        and item.run_day <= policy.cut
    }
    assert {family(item) for item in periodic} == landable_periodic
    assert len(periodic) == 250  # 153 NTB periods and 97 from packs 5-7
    cadence = {
        item.manifest.manifest_key
        for item in planned
        if item.delivery_class is DeliveryClass.CADENCE
    }
    assert all(item.manifest.manifest_key in cadence for item in periodic)
    # Paired: a daily backfill is a new delivery, never its cadence twin.
    daily = [item for item in phase_b() if family(item) in AMC_DAILY_DATE_COLUMNS]
    assert daily
    assert not any(item.manifest.manifest_key in cadence for item in daily)


def test_pb1a_the_expected_winner_of_every_overlap_is_the_backfill_as_newest_retrieved() -> None:
    for items in overlaps().values():
        newest = max(items, key=lambda item: item.manifest.retrieved_at)
        assert newest.delivery_class is DeliveryClass.BACKFILL
        assert (
            sum(item.manifest.retrieved_at == newest.manifest.retrieved_at for item in items) == 1
        )


def test_pb1a_rows_are_equal_on_every_overlap_so_the_resolution_rule_keeps_the_newest_rows() -> (
    None
):
    disagreements = 0
    for (_table, day), items in overlaps().items():
        newest = max(items, key=lambda item: item.manifest.retrieved_at)
        watermarks = [item.manifest.source_watermark for item in items]
        assert all(mark is not None for mark in watermarks)
        top = max(mark for mark in watermarks if mark is not None)
        tied = [item for item in items if item.manifest.source_watermark == top]
        tied_rows = {rows(item, day) for item in tied}
        assert len(tied_rows) == 1, "arrival order among equal revisions would change the rows"
        assert rows(newest, day) in tied_rows
        assert rows(newest, day), "an overlapping cell carries rows"
        if newest not in tied:
            disagreements += 1
    # The watermark rule picks a different publication than the newest retrieval in a bounded
    # number of cells; each is row-equal above. Recorded in the S4 evidence.
    assert 0 < disagreements < len(overlaps())
