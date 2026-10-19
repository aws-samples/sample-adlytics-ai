# SPDX-License-Identifier: MIT-0
"""The probe residue constraint (design, "Probe inputs", residue; V-24, R3-11, R4-14).

A probe copy stays in the raw zone as the newest retrieval of the copied delivery's rows. That
is harmless only if no other delivery in the planner's output, phase-B backfill included,
writes any (table, date) of the copy with a retrieval newer than the original. Because the
probe slots sit above every planned retrieval, the test asserts that strong form directly.
"""

from __future__ import annotations

import functools
import pathlib
import sys

import pytest

from adapters.jobs.land_fixtures.bundle import (
    DeliveryClass,
    LandedDelivery,
    plan_with_backfill,
    restating_deliveries,
)
from domain.operations.run_request import HistoricalPolicy

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))
from validation import probe_ingestion_traceability_gate as gate  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-086")]


@functools.cache
def _plan() -> tuple[tuple[LandedDelivery, ...], HistoricalPolicy]:
    return plan_with_backfill(_ROOT)


def _named(name: str) -> LandedDelivery:
    planned, _ = _plan()
    return next(
        item
        for item in planned
        if item.fixture == name and item.delivery_class is DeliveryClass.CADENCE
    )


def test_probe_delivery_unrestated_for_the_runner_fixed_delivery() -> None:
    planned, policy = _plan()
    original = _named(gate.FIXED_PROBE_DELIVERY)
    assert original.run_day is not None and original.run_day > policy.cut
    assert restating_deliveries(planned, original) == ()


def test_probe_delivery_unrestated_fails_for_a_delivery_a_later_cadence_run_restates() -> None:
    planned, _ = _plan()
    restated = _named("generated/amc-dsp-impressions.csv#daily-run=2026-10-05#chunk=2026-10-03")
    later = restating_deliveries(planned, restated)
    assert later
    assert {item.delivery_class for item in later} == {DeliveryClass.CADENCE}


def test_probe_delivery_unrestated_fails_for_a_delivery_a_phase_b_backfill_overlaps() -> None:
    planned, policy = _plan()
    near_cut = next(
        item
        for item in planned
        if item.fixture.startswith("generated/amc-dsp-impressions.csv#daily-run=")
        and item.delivery_class is DeliveryClass.CADENCE
        and item.run_day is not None
        and item.run_day > policy.cut
        and item.manifest.period_start <= policy.cut
    )
    overlapping = restating_deliveries(planned, near_cut)
    assert any(item.delivery_class is DeliveryClass.BACKFILL for item in overlapping)


def test_probe_delivery_unrestated_fails_for_a_campaign_dimension_the_seal_restates() -> None:
    planned, _ = _plan()
    dimension = _named("generated/amc-campaign-dsp.csv#daily-run=2026-10-06#chunk=2026-10-04")
    assert any(
        item.delivery_class is DeliveryClass.SEAL
        for item in restating_deliveries(planned, dimension)
    )
