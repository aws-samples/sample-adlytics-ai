# SPDX-License-Identifier: MIT-0
"""Coverage semantics derived from active Semantic V2 CURRENT publications."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from domain.catalog.freshness import Coverage, common_intervals, latest_complete_window
from domain.shared_kernel.provenance import FreshnessStatus, MeasurementBasis
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-036")]


def _coverage(**overrides: object) -> Coverage:
    fields: dict[str, object] = {
        "fact_name": "fct_retail_sales",
        "window": DateRange(date(2026, 8, 10), date(2026, 8, 16)),
        "freshness": FreshnessStatus.FRESH,
        "measurement_basis": MeasurementBasis.RETAIL_ACTUAL,
        "valid_grains": ("date_key", "amazon_asin"),
        "loaded_at": datetime(2026, 8, 17, tzinfo=UTC),
    }
    fields.update(overrides)
    return Coverage(**fields)  # type: ignore[arg-type]


class TestCoverage:
    def test_a_requested_subrange_is_covered(self) -> None:
        assert _coverage().covers(DateRange(date(2026, 8, 11), date(2026, 8, 15)))

    def test_partial_overlap_is_not_coverage(self) -> None:
        assert not _coverage().covers(DateRange(date(2026, 8, 14), date(2026, 8, 20)))

    def test_exact_intervals_do_not_bridge_a_gap(self) -> None:
        coverage = _coverage(
            window=None,
            intervals=(
                DateRange(date(2026, 8, 1), date(2026, 8, 5)),
                DateRange(date(2026, 8, 10), date(2026, 8, 15)),
            ),
        )
        assert coverage.covers(DateRange(date(2026, 8, 2), date(2026, 8, 4)))
        assert not coverage.covers(DateRange(date(2026, 8, 4), date(2026, 8, 11)))

    def test_comparison_periods_are_checked_independently(self) -> None:
        coverage = _coverage(
            window=None,
            intervals=(
                DateRange(date(2026, 8, 1), date(2026, 8, 5)),
                DateRange(date(2026, 8, 10), date(2026, 8, 15)),
            ),
        )
        assert coverage.covers_all(
            (
                DateRange(date(2026, 8, 2), date(2026, 8, 4)),
                DateRange(date(2026, 8, 11), date(2026, 8, 14)),
            )
        )

    def test_a_declared_hole_refuses_the_requested_period(self) -> None:
        coverage = _coverage(holes=(DateRange(date(2026, 8, 12), date(2026, 8, 13)),))
        assert not coverage.covers(DateRange(date(2026, 8, 11), date(2026, 8, 14)))

    def test_never_loaded_cannot_claim_a_window(self) -> None:
        with pytest.raises(ValueError, match="cannot carry a window"):
            _coverage(freshness=FreshnessStatus.NEVER_LOADED)

    def test_intervals_must_be_sorted_and_unique(self) -> None:
        interval = DateRange(date(2026, 8, 1), date(2026, 8, 2))
        with pytest.raises(ValueError, match="repeats an interval"):
            _coverage(window=None, intervals=(interval, interval))


class TestCommonIntervals:
    def test_intersection_keeps_only_periods_every_contributor_covers(self) -> None:
        assert common_intervals(
            (
                (DateRange(date(2026, 8, 1), date(2026, 8, 10)),),
                (DateRange(date(2026, 8, 5), date(2026, 8, 15)),),
            )
        ) == (DateRange(date(2026, 8, 5), date(2026, 8, 10)),)

    def test_disjoint_intersections_are_empty(self) -> None:
        assert (
            common_intervals(
                (
                    (DateRange(date(2026, 8, 1), date(2026, 8, 2)),),
                    (DateRange(date(2026, 8, 5), date(2026, 8, 6)),),
                )
            )
            == ()
        )

    def test_adjacent_intersections_are_normalized(self) -> None:
        assert common_intervals(
            (
                (
                    DateRange(date(2026, 8, 1), date(2026, 8, 3)),
                    DateRange(date(2026, 8, 4), date(2026, 8, 6)),
                ),
                (DateRange(date(2026, 8, 1), date(2026, 8, 6)),),
            )
        ) == (DateRange(date(2026, 8, 1), date(2026, 8, 6)),)


class TestLatestCompleteWindow:
    def test_selects_the_latest_seven_days_of_common_contiguous_coverage(self) -> None:
        assert latest_complete_window(
            (
                (DateRange(date(2026, 8, 1), date(2026, 8, 20)),),
                (DateRange(date(2026, 8, 8), date(2026, 8, 14)),),
            ),
            7,
        ) == DateRange(date(2026, 8, 8), date(2026, 8, 14))

    def test_chooses_the_later_qualifying_interval_without_bridging_gaps(self) -> None:
        assert latest_complete_window(
            (
                (
                    DateRange(date(2026, 8, 1), date(2026, 8, 8)),
                    DateRange(date(2026, 8, 12), date(2026, 8, 25)),
                ),
            ),
            7,
        ) == DateRange(date(2026, 8, 19), date(2026, 8, 25))

    def test_refuses_when_no_common_interval_is_long_enough(self) -> None:
        assert (
            latest_complete_window(
                (
                    (DateRange(date(2026, 8, 1), date(2026, 8, 6)),),
                    (DateRange(date(2026, 8, 1), date(2026, 8, 10)),),
                ),
                7,
            )
            is None
        )
