# SPDX-License-Identifier: MIT-0
"""Pure invariants of the AMC lake probe, over committed-fixture rows (design §2.5, R-11).

Each invariant runs once over the committed fixture's own values, which must pass with a full
denominator, and once with exactly one seeded violation, which must fail naming that row.
"""

from __future__ import annotations

import functools
import sys
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import probe_amc_current_invariants as probe  # noqa: E402

from adapters.jobs.fixture_generation import catalog as catalog_module  # noqa: E402
from adapters.jobs.fixture_generation.catalog import load_synthetic_entity_catalog  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-086")]


@functools.cache
def _expected() -> probe.CampaignExpectations:
    return probe.expected_campaigns(load_synthetic_entity_catalog(_ROOT))


def _days(
    expected: probe.CampaignExpectations,
) -> tuple[dict[probe.Pair, frozenset[date]], dict[probe.Pair, frozenset[date]]]:
    return probe.days_by_pair(expected.exposure), probe.days_by_pair(expected.conversion)


def test_the_probe_reads_its_span_and_gap_from_the_catalog() -> None:
    catalog = load_synthetic_entity_catalog(_ROOT)
    span = catalog.shape.fixture_span
    assert probe.catalog_span(catalog) == probe.DateRange(span.first_day, span.last_day)
    campaign, day = probe.catalog_coverage_gap(catalog)
    assert campaign == "campaign:northstar-sponsored-30"
    assert day == catalog.shape.event_anchor - timedelta(days=4)
    expected = _expected()
    assert expected.span.days == span.span_days
    assert expected.coverage_gap[1] == day
    assert not hasattr(probe, "_DAYS")
    assert not hasattr(catalog_module, "COVERAGE_GAP_DAY_INDEX")
    assert (
        "COVERAGE_GAP_DAY_INDEX"
        not in (_ROOT / "validation/probe_amc_current_invariants.py").read_text()
    )


def test_the_committed_fixture_passes_every_invariant_with_a_full_denominator() -> None:
    expected = _expected()
    exposure_days, conversion_days = _days(expected)
    reports = (
        probe.coverage_report("exposure", exposure_days, expected.exposure_days),
        probe.coverage_report("conversion", conversion_days, expected.conversion_days),
        probe.absence_report(exposure_days, conversion_days, expected),
        probe.reach_report(expected.exposure),
        probe.funnel_report(expected.exposure, expected.conversion),
        probe.window_band_report(expected.exposure, expected.conversion, expected),
        probe.continuity_report(
            expected.exposure,
            expected.conversion,
            expected,
            (expected.span.end - timedelta(days=56), expected.span.end - timedelta(days=55)),
        ),
    )
    for report in reports:
        assert not report.failed, report.line()
    # 59 campaigns x 462 days of exposure, less the one gap day.
    assert reports[0].evaluated == len(expected.all_pairs) * expected.span.days - 1
    assert reports[5].evaluated > len(expected.all_pairs) * 400


def test_coverage_names_the_campaign_with_a_missing_day() -> None:
    expected = _expected()
    exposure_days, _ = _days(expected)
    pair = sorted(expected.all_pairs)[0]
    seeded = {**exposure_days, pair: exposure_days[pair] - {expected.span.end}}
    report = probe.coverage_report("exposure", seeded, expected.exposure_days)
    assert report.violations == (f"{pair[0]}: missing=1 extra=0",)


def test_each_row_absence_is_detected_and_each_control_is_required() -> None:
    expected = _expected()
    exposure_days, conversion_days = _days(expected)
    launch, first_delivery = expected.launch
    early = {
        **conversion_days,
        launch: conversion_days[launch] | {first_delivery - timedelta(days=1)},
    }
    assert probe.absence_report(exposure_days, early, expected).violations == (
        f"{launch[0]}: conversion rows before its first delivery",
    )
    pause, first, _last = expected.pause
    paused = {**conversion_days, pause: conversion_days[pause] | {first}}
    assert probe.absence_report(exposure_days, paused, expected).violations == (
        f"{pause[0]}: conversion rows on its pause days",
    )
    gap, gap_day = expected.coverage_gap
    filled = {**exposure_days, gap: exposure_days[gap] | {gap_day}}
    assert probe.absence_report(filled, conversion_days, expected).violations == (
        f"{gap[0]}: exposure rows on its coverage-gap day",
    )
    # Positive control: a pause campaign missing its day-after row fails the control.
    emptied = {**conversion_days, pause: conversion_days[pause] - {_last + timedelta(days=1)}}
    assert probe.absence_report(exposure_days, emptied, expected).violations == (
        f"{pause[0]}: no conversion row on {_last + timedelta(days=1)} (control)",
    )


def test_reach_above_impressions_is_one_named_violation() -> None:
    expected = _expected()
    key = next(
        key for key, item in sorted(expected.exposure.items()) if item.reach_users is not None
    )
    item = expected.exposure[key]
    seeded = {**expected.exposure, key: replace(item, reach_users=item.impressions + 1)}
    report = probe.reach_report(seeded)
    assert len(report.violations) == 1 and report.violations[0].startswith(f"{key[0][0]} {key[1]}")


def test_purchases_above_clicks_is_one_named_violation() -> None:
    expected = _expected()
    key = sorted(expected.conversion)[0]
    clicks = expected.exposure[key].clicks
    seeded = {**expected.conversion, key: replace(expected.conversion[key], purchases=clicks + 1)}
    report = probe.funnel_report(expected.exposure, seeded)
    assert len(report.violations) == 1 and report.violations[0].startswith(f"{key[0][0]} {key[1]}")


def test_a_window_out_of_its_roas_band_is_named() -> None:
    expected = _expected()
    pair = sorted(expected.all_pairs - expected.zero_conversion_pairs)[0]
    day = expected.span.start + timedelta(days=200)
    seeded = {
        **expected.conversion,
        (pair, day): replace(expected.conversion[(pair, day)], revenue=Decimal(10) ** 9),
    }
    report = probe.window_band_report(expected.exposure, seeded, expected)
    assert report.violations
    assert all(
        item.startswith(f"{pair[0]} 7D ending") and "ROAS" in item for item in report.violations
    )
    assert len(report.violations) == 7  # every window that holds the seeded day


def test_continuity_names_the_changed_campaign_day_and_a_short_denominator_fails() -> None:
    expected = _expected()
    day = expected.span.end - timedelta(days=56)
    pair = sorted(expected.all_pairs)[0]
    item = expected.exposure[(pair, day)]
    seeded = {**expected.exposure, (pair, day): replace(item, clicks=item.clicks + 1)}
    report = probe.continuity_report(seeded, expected.conversion, expected, (day,))
    assert report.violations == (f"{pair[0]} {day}: served measures differ from the fixture",)
    empty = probe.reach_report({})
    assert empty.failed and empty.evaluated == 0


def _versions(expected: probe.CampaignExpectations) -> list[probe.DimensionVersion]:
    """Build a clean served history: every campaign from `d_c`, the rename versioned."""
    versions: list[probe.DimensionVersion] = []
    rename = expected.rename
    for key, item in expected.dimension.items():
        if rename is not None and key == rename.campaign_key:
            versions.append(
                probe.DimensionVersion(key, rename.old_name, item.first_observed, rename.boundary)
            )
            versions.append(probe.DimensionVersion(key, rename.new_name, rename.boundary, None))
        else:
            versions.append(probe.DimensionVersion(key, item.name, item.first_observed, None))
    return versions


def test_dimension_history_passes_a_clean_history_and_names_a_late_base() -> None:
    expected = _expected()
    assert expected.rename is not None and expected.rename.boundary == date(2026, 8, 12)
    clean = probe.dimension_history_report(_versions(expected), expected.dimension, expected.rename)
    assert not clean.failed, clean.line()
    assert clean.evaluated == len(expected.dimension) == 59  # the unnamed campaign is excluded
    late = [
        replace(version, valid_from=date(2026, 8, 10)) if index == 0 else version
        for index, version in enumerate(
            sorted(_versions(expected), key=lambda item: item.campaign_key)
        )
    ]
    report = probe.dimension_history_report(late, expected.dimension, expected.rename)
    key = sorted(_versions(expected), key=lambda item: item.campaign_key)[0].campaign_key
    assert report.violations and all(item.startswith(key) for item in report.violations)
    # Live positive control in unit form: a rename flattened into one version fails.
    rename = expected.rename
    flattened = [
        version
        for version in _versions(expected)
        if not (version.campaign_key == rename.campaign_key and version.name == rename.old_name)
    ]
    flattened = [
        replace(version, valid_from=expected.span.start)
        if version.campaign_key == rename.campaign_key
        else version
        for version in flattened
    ]
    report = probe.dimension_history_report(flattened, expected.dimension, expected.rename)
    assert (
        f"{rename.campaign_key}: the {rename.boundary} rename is not versioned" in report.violations
    )


def test_the_product_check_requires_every_visible_product_current() -> None:
    asins = [product.asin for product in load_synthetic_entity_catalog(_ROOT).products]
    rows = [(asin, True, date(2026, 8, 28)) for asin in asins]
    report, earliest = probe.product_report(rows, asins)
    assert not report.failed and earliest == date(2026, 8, 28)
    report, _ = probe.product_report([(asins[0], False, date(2026, 8, 28)), *rows[1:]], asins)
    assert report.violations == (f"{asins[0]}: not current",)


def test_continuity_days_always_include_the_phase_boundary() -> None:
    catalog = load_synthetic_entity_catalog(_ROOT)
    request = probe.ProbeRequest(
        "explicit",
        "dev",
        "us-east-1",
        "000000000000",
        "Adlytics-DataV2-dev",
        "amazon_us",
        probe.catalog_span(catalog),
        continuity_days=(date(2026, 9, 9),),
    )
    end = catalog.shape.fixture_span.last_day
    assert probe.continuity_days(request) == tuple(
        sorted({end - timedelta(days=56), end - timedelta(days=55), date(2026, 9, 9)})
    )
