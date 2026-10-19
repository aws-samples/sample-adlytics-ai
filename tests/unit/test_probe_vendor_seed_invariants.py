# SPDX-License-Identifier: MIT-0
"""Pure invariants of the Vendor seed probe over committed-fixture rows (design §2.5)."""

from __future__ import annotations

import functools
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import probe_vendor_seed_invariants as probe  # noqa: E402

from adapters.jobs.fixture_generation.catalog import load_synthetic_entity_catalog  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-086")]


@functools.cache
def _expected() -> probe.VendorExpectations:
    return probe.expected_vendor(load_synthetic_entity_catalog(_ROOT))


def _key(index: int = 100) -> tuple[str, probe.date]:
    return sorted(_expected().final)[index]


def test_the_committed_final_values_pass_with_full_denominators() -> None:
    expected = _expected()
    assert len(expected.final) == len(expected.asins) * expected.span.days
    for report in probe.reports(expected.final, expected):
        assert not report.failed, report.line()


def test_cogs_at_revenue_is_one_named_violation() -> None:
    expected = _expected()
    key = _key()
    item = expected.final[key]
    report = probe.margin_report(
        {**expected.final, key: replace(item, cogs=item.revenue)}, expected
    )
    assert len(report.violations) == 1 and report.violations[0].startswith(f"{key[0]} {key[1]}")


def test_zero_glance_views_is_one_named_violation() -> None:
    expected = _expected()
    key = _key()
    seeded = {**expected.final, key: replace(expected.final[key], glance_views=Decimal(0))}
    report = probe.views_report(seeded, expected)
    assert len(report.violations) == 1 and report.violations[0].startswith(f"{key[0]} {key[1]}")


def test_window_returns_above_shipped_are_named() -> None:
    expected = _expected()
    key = _key()
    item = expected.final[key]
    seeded = {**expected.final, key: replace(item, returns=item.units * 20)}
    report = probe.returns_window_report(seeded, expected)
    assert report.violations
    assert all(entry.startswith((key[0], "all")) for entry in report.violations)


def test_served_revenue_per_unit_out_of_band_is_one_named_violation() -> None:
    expected = _expected()
    key = _key()
    item = expected.final[key]
    seeded = {**expected.final, key: replace(item, revenue=item.units * 100)}
    report = probe.served_rpu_report(seeded, expected)
    assert len(report.violations) == 1 and report.violations[0].startswith(f"{key[0]} {key[1]}")


def test_a_first_report_day_that_differs_from_final_is_named_under_final() -> None:
    expected = _expected()
    assert expected.retail.unrestated_returns == "final"
    last = expected.span.end
    asin = expected.asins[0]
    item = expected.final[(asin, last)]
    seeded = {**expected.final, (asin, last): replace(item, returns=item.returns + 1)}
    report = probe.first_report_report(seeded, expected)
    assert report.violations == (f"{asin} {last}: first report differs from the final report",)
    missing = {key: value for key, value in expected.final.items() if key != (asin, last)}
    assert (
        f"{asin} {last}: first-report day not served"
        in probe.first_report_report(missing, expected).violations
    )


def test_an_empty_read_fails_on_its_denominator() -> None:
    expected = _expected()
    assert all(report.failed for report in probe.reports({}, expected))
