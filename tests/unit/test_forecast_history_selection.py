# SPDX-License-Identifier: MIT-0
"""The demand-forecast selection's pins and covered days.

If they disagreed, a week would be scored complete on values from another revision, or an
uncovered day would be forecast as zero.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import cast

import pytest
from forecast_publication_support import (
    SALES_FILTERS,
    Declarations,
    MemoryCurrent,
    declarations,
    publication,
)

from adapters.outbound.publication_selection import ForecastHistorySelector, publication_digest
from domain.shared_kernel.values import DateRange
from ports.outbound.forecast_history_selection import ForecastHistorySelectionRequest
from ports.outbound.semantic_state import CurrentPublicationInspectionPort

pytestmark = pytest.mark.unit

WINDOW = DateRange(date(2026, 1, 1), date(2026, 3, 31))
SELECTORS = (("distributor_view", "MANUFACTURING"), ("selling_program", "RETAIL"))


@pytest.fixture(scope="module")
def known() -> Declarations:
    return declarations()


def _selector(known: Declarations, current: MemoryCurrent) -> ForecastHistorySelector:
    return ForecastHistorySelector(
        cast("CurrentPublicationInspectionPort", current),
        known.packs,
        known.contracts,
        known.bridge,
    )


def _request(marketplace: str = "amazon_us") -> ForecastHistorySelectionRequest:
    return ForecastHistorySelectionRequest(marketplace, SELECTORS, WINDOW)


def test_pins_and_covered_days_agree_and_an_unpublished_day_is_uncovered(
    known: Declarations,
) -> None:
    current = MemoryCurrent()
    sales = known.scope("vendor-sales-by-asin", SALES_FILTERS)
    current.load_days(sales, date(2026, 1, 1), date(2026, 1, 9))
    current.load_days(sales, date(2026, 1, 11), date(2026, 1, 14))
    result = _selector(known, current).select(_request())
    assert result.sales.covered == (
        DateRange(date(2026, 1, 1), date(2026, 1, 9)),
        DateRange(date(2026, 1, 11), date(2026, 1, 14)),
    )
    assert len(result.sales.pins) == 13
    assert {bucket.start for bucket in result.sales.buckets} == {
        date(2026, 1, 1) + timedelta(days=n) for n in range(14) if n != 9
    }
    assert result.traffic.pins == ()


def test_a_restated_day_yields_the_new_publication_and_its_digest(known: Declarations) -> None:
    current = MemoryCurrent()
    sales = known.scope("vendor-sales-by-asin", SALES_FILTERS)
    current.load_days(sales, date(2026, 1, 1), date(2026, 1, 3))
    restated = publication(sales, date(2026, 1, 2), revision="b")
    current.put(restated)
    pins = _selector(known, current).select(_request()).sales.pins
    second = next(pin for pin in pins if pin.publication_id == restated.publication_id.value)
    assert second.digest == publication_digest(restated)
    assert len(pins) == 3


def test_the_marketplace_filter_excludes_another_population(known: Declarations) -> None:
    current = MemoryCurrent()
    with pytest.raises(ValueError, match="outside"):
        _selector(known, current).select(_request("not_a_marketplace"))


def test_days_outside_the_window_are_not_pinned(known: Declarations) -> None:
    current = MemoryCurrent()
    sales = known.scope("vendor-sales-by-asin", SALES_FILTERS)
    current.load_days(sales, date(2025, 12, 30), date(2026, 1, 2))
    current.load_days(sales, date(2026, 3, 31), date(2026, 4, 2))
    covered = _selector(known, current).select(_request()).sales.covered
    assert covered == (
        DateRange(date(2026, 1, 1), date(2026, 1, 2)),
        DateRange(date(2026, 3, 31), date(2026, 3, 31)),
    )
