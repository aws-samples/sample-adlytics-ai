# SPDX-License-Identifier: MIT-0
"""PB-2: older entities load, so phase B's older rows resolve (S4).

Phase B lands older facts behind phase A. Their campaigns and products must exist in the
dimensions, including campaigns and products whose last activity ended before phase A's window:

- every campaign a phase-B fact row names is in a campaign-dimension delivery the plan lands
  (phase-0 seals, phase-A cadence or phase-B backfill), and phase B itself carries campaign
  dimension deliveries for the days it covers;
- every product a phase-B row names is in the planned product hierarchy (phase 0).
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from typing import cast

import pytest
from phase_b_support import family, phase_b, plan

from adapters.jobs.land_fixtures.bundle import CAMPAIGN_DIMENSION_FIXTURES, LandedDelivery

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-086")]

_CAMPAIGN = "campaign_id_string"
_ASIN_COLUMNS = ("asin", "tracked_asin")


def _csv_values(
    columns: tuple[str, ...], deliveries: Iterable[LandedDelivery]
) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for item in deliveries:
        if not item.fixture.split("#", 1)[0].endswith(".csv"):
            continue
        for obj in item.data:
            for row in csv.DictReader(io.StringIO(obj.body.decode("utf-8"))):
                for column in columns:
                    value = row.get(column)
                    if value:
                        found.setdefault(column, set()).add(value)
    return found


def _phase_b_campaign_last_days() -> dict[str, str]:
    last: dict[str, str] = {}
    for item in phase_b():
        if family(item) in CAMPAIGN_DIMENSION_FIXTURES or not family(item).endswith(".csv"):
            continue
        for obj in item.data:
            reader = csv.DictReader(io.StringIO(obj.body.decode("utf-8")))
            date_column = (reader.fieldnames or [""])[0]
            for row in reader:
                campaign = row.get(_CAMPAIGN)
                if campaign:
                    last[campaign] = max(last.get(campaign, ""), row[date_column])
    return last


def test_pb2_phase_b_carries_campaign_dimension_deliveries_for_its_days() -> None:
    families = {family(item) for item in phase_b()}
    assert set(CAMPAIGN_DIMENSION_FIXTURES) <= families


def _first_observed(deliveries: Iterable[LandedDelivery]) -> dict[str, str]:
    """Each campaign's earliest `source_observed_on` across campaign-dimension deliveries."""
    first: dict[str, str] = {}
    for item in deliveries:
        for obj in item.data:
            for row in csv.DictReader(io.StringIO(obj.body.decode("utf-8"))):
                campaign, day = row.get(_CAMPAIGN), row.get("source_observed_on")
                if campaign and day:
                    first[campaign] = min(first.get(campaign, day), day)
    return first


def test_pb2_every_phase_b_campaign_has_a_dimension_version_on_or_before_its_fact_days() -> None:
    last = _phase_b_campaign_last_days()
    assert last, "phase-B facts name campaigns"
    backfilled = _first_observed(
        [item for item in phase_b() if family(item) in CAMPAIGN_DIMENSION_FIXTURES]
    )
    # Phase B's own dimension deliveries resolve every campaign its facts name, so a campaign
    # whose activity ends before phase A still resolves without any phase-A delivery.
    assert set(last) <= set(backfilled)
    assert all(backfilled[campaign] <= day for campaign, day in last.items())
    planned, _ = plan()
    every = _first_observed(
        [item for item in planned if family(item) in CAMPAIGN_DIMENSION_FIXTURES]
    )
    assert set(backfilled) <= set(every)


def test_pb2_every_phase_b_product_is_in_the_planned_hierarchy() -> None:  # noqa: PLR0912
    planned, _ = plan()
    hierarchy: set[str] = set()
    for item in planned:
        if "vendor-product-hierarchy" not in item.fixture:
            continue
        for obj in item.data:
            for line in obj.body.decode("utf-8").splitlines():
                if line.strip():
                    hierarchy.add(str(json.loads(line)["asin"]))
    named: set[str] = set()
    for values in _csv_values(_ASIN_COLUMNS, phase_b()).values():
        named |= values
    for item in phase_b():
        if family(item).endswith(".json"):
            for obj in item.data:
                document = cast("dict[str, object]", json.loads(obj.body.decode("utf-8")))
                for value in document.values():
                    if not isinstance(value, list):
                        continue
                    for row in cast("list[object]", value):
                        if isinstance(row, dict) and "asin" in row:
                            named.add(str(cast("dict[str, object]", row)["asin"]))
    assert named, "phase-B rows name products"
    assert named <= hierarchy
