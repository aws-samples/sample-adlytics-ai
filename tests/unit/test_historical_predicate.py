# SPDX-License-Identifier: MIT-0
"""V-8: the derived `historical` class is total over the planner's own output.

Every phase-B delivery (and every old daily run phase B replaces) classifies historical, and
every phase-0 and phase-A delivery, and every reserved probe slot, classifies non-historical.
The declared period alone cannot decide: phase-A runs restate days before the cut, and phase-0
seals and product-hierarchy chunks start at the first span day. The two Vendor sales
restatements that fall due on the first phase-A days are planned twice, once as phase-A
cadence and once as the backfill's last report for their day, at one immutable key. They are
phase-A deliveries, so they classify non-historical, and phase B re-landing them puts nothing.
"""

from __future__ import annotations

import functools
import pathlib
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from adapters.jobs.land_fixtures.bundle import (
    DeliveryClass,
    LandedDelivery,
    manifest_facts,
    plan_with_backfill,
)
from domain.operations.run_request import (
    HistoricalPolicy,
    ManifestFacts,
    RetrievalEncoding,
    is_historical,
    is_probe_slot,
    probe_slot_instants,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-086")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
_PHASE_A_RUN_DAYS = 56


@functools.cache
def _plan() -> tuple[tuple[LandedDelivery, ...], HistoricalPolicy]:
    return plan_with_backfill(ROOT)


def _phase(item: LandedDelivery, policy: HistoricalPolicy, phase_a_keys: frozenset[str]) -> str:
    if item.delivery_class in {DeliveryClass.TRAP, DeliveryClass.WHOLE, DeliveryClass.SEAL}:
        return "0"
    if item.delivery_class is DeliveryClass.CADENCE:
        assert item.run_day is not None
        return "old" if item.run_day <= policy.cut else "A"
    return "A" if item.manifest.manifest_key in phase_a_keys else "B"


def test_v8_the_predicate_is_total_over_the_planner_output() -> None:
    planned, policy = _plan()
    phase_a_keys = frozenset(
        item.manifest.manifest_key
        for item in planned
        if item.delivery_class is DeliveryClass.CADENCE
        and item.run_day is not None
        and item.run_day > policy.cut
    )
    wrong: list[str] = []
    counts: Counter[str] = Counter()
    for item in planned:
        phase = _phase(item, policy, phase_a_keys)
        counts[phase] += 1
        expected = phase in {"B", "old"}
        if is_historical(manifest_facts(item.manifest), policy) is not expected:
            wrong.append(f"{phase}: {item.fixture}")
    assert wrong == []
    # The plan's own counts (ADR-086 §14): 2,038 phase-B deliveries, 2 of them phase-A keys, plus
    # the 70 periodic backfill deliveries each of pack 1 (amc-new-to-brand-buyers-platform) and
    # pack 2 (amc-new-to-brand-buyers-channel) adds, one per complete ISO week and calendar month
    # of the corpus that ends on or before the cut, and pack 3's (amc-new-to-brand-buyers-campaign)
    # 13, one per calendar month.
    periodic_backfill = sum(
        1
        for item in planned
        if item.delivery_class is DeliveryClass.BACKFILL
        and item.fixture.startswith(
            (
                "generated/amc-new-to-brand-buyers-platform.",
                "generated/amc-new-to-brand-buyers-channel.",
                "generated/amc-new-to-brand-buyers-campaign.",
            )
        )
    )
    assert periodic_backfill == 153
    # Packs 5-7 (exposure groups, frequency per platform and channel) add their 97 periods that
    # land on or before the cut; packs 8-15 plan none there.
    d2_backfill = sum(
        1
        for item in planned
        if item.delivery_class is DeliveryClass.BACKFILL
        and item.fixture.startswith(
            (
                "generated/amc-exposure-groups.",
                "generated/amc-exposure-frequency-platform.",
                "generated/amc-exposure-frequency-channel.",
            )
        )
    )
    assert d2_backfill == 97
    assert (
        counts["B"]
        + sum(
            1
            for item in planned
            if item.delivery_class is DeliveryClass.BACKFILL
            and item.manifest.manifest_key in phase_a_keys
        )
        == 2_038 + periodic_backfill + d2_backfill
    )
    assert counts["0"] > 0 and counts["A"] > 0 and counts["old"] > 0


def test_v8_the_declared_period_alone_is_not_total() -> None:
    """The control: the design's candidate period rule misclassifies phase 0 and phase A."""
    planned, policy = _plan()
    misread = [
        item
        for item in planned
        if item.delivery_class is not DeliveryClass.BACKFILL
        and not is_historical(manifest_facts(item.manifest), policy)
        and item.manifest.period_end <= policy.cut
    ]
    assert {item.delivery_class for item in misread} >= {DeliveryClass.CADENCE, DeliveryClass.TRAP}


def test_v8_every_phase_b_backfill_slot_is_historical() -> None:
    planned, policy = _plan()
    backfill = [
        item
        for item in planned
        if item.delivery_class is DeliveryClass.BACKFILL and "#backfill#" in item.fixture
    ]
    assert backfill
    assert all(is_historical(manifest_facts(item.manifest), policy) for item in backfill)


def test_v8_every_probe_slot_is_non_historical_for_every_phase_a_dataset() -> None:
    planned, policy = _plan()
    instants = probe_slot_instants(policy)
    assert len(instants) == policy.probe_slot_count * policy.stride_seconds
    phase_a = {
        (item.manifest.source_system, item.manifest.source_dataset): item
        for item in planned
        if item.delivery_class is DeliveryClass.CADENCE
        and item.run_day is not None
        and item.run_day > policy.cut
    }
    for item in phase_a.values():
        for instant in (instants[0], instants[len(instants) // 2], instants[-1]):
            facts = replace(manifest_facts(item.manifest), retrieved_at=instant)
            assert is_probe_slot(instant, policy)
            assert not is_historical(facts, policy)


def test_v8_the_probe_slot_base_sits_above_every_planned_retrieval() -> None:
    """R4-14: above every cadence, seal and backfill offset the planner emits."""
    planned, policy = _plan()
    first_probe = policy.replay_base + timedelta(seconds=policy.probe_first_offset)
    assert all(item.manifest.retrieved_at < first_probe for item in planned)
    backfill_slots = [
        int((item.manifest.retrieved_at - policy.replay_base) // timedelta(seconds=1))
        // policy.stride_seconds
        for item in planned
        if "#backfill#" in item.fixture
    ]
    assert max(backfill_slots) + 1 == policy.probe_slot_base


def _policy() -> HistoricalPolicy:
    return HistoricalPolicy(
        replay_base=datetime(2026, 10, 6, tzinfo=UTC),
        first_day=date(2025, 7, 1),
        cut=date(2026, 8, 11),
        stride_seconds=64,
        seal_slot_base=463,
        backfill_slot_base=527,
        probe_slot_base=530,
        probe_slot_count=64,
        encodings={("amc", "dsp_impressions"): RetrievalEncoding.STRIDED},
    )


def test_v8_outside_the_replay_band_the_declared_period_decides() -> None:
    policy = _policy()
    after_band = policy.replay_base + timedelta(seconds=policy.probe_end_offset)
    old = ManifestFacts("amc", "dsp_impressions", date(2026, 8, 11), after_band)
    new = ManifestFacts("amc", "dsp_impressions", date(2026, 8, 12), after_band)
    assert is_historical(old, policy)
    assert not is_historical(new, policy)
    before_base = ManifestFacts(
        "amc", "dsp_impressions", date(2025, 6, 1), policy.replay_base - timedelta(days=1)
    )
    assert not is_historical(before_base, policy)


def test_v8_a_naive_instant_or_inverted_policy_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(_policy(), replay_base=datetime(2026, 10, 6))  # noqa: DTZ001
    with pytest.raises(ValueError, match="strictly increase"):
        replace(_policy(), backfill_slot_base=600)
    facts = ManifestFacts("amc", "dsp_impressions", date(2026, 8, 12), datetime(2026, 10, 6))  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone-aware"):
        is_historical(facts, _policy())
