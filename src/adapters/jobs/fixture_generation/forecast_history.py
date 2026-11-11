# SPDX-License-Identifier: MIT-0
"""The synthetic catalogue's weekly Vendor history for the STAGED demand-forecast input (K-4).

The fixture landing refuses Vendor deliveries older than `last_day` - 56 days
(`FULL_HISTORY_NOT_ENABLED`), so dev can't hold the catalogue's 104 weeks. This module rolls the
same day values the landed fixtures carry (`ShapeValues.product_day`, final reports) up to the
input builder's weekly aggregate grain, from `shape.forecast.vendor_history_start` to the input
end. It leaves out the declared `vendor_day_unlanded` day (uncovered for every ASIN) and every
product-day a forecasting shape declares absent. Nothing here is a customer value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from adapters.jobs.fixture_generation.catalog import SyntheticEntityCatalog
from adapters.jobs.fixture_generation.values import ProductDayValues

CANONICAL_PREFIX = "amazon_asin:"
_DAYS_PER_WEEK = 7


@dataclass(frozen=True, slots=True)
class CatalogueWeek:
    """One product-week at the input builder's aggregate grain; only weeks with a row exist."""

    canonical_asin: str
    week_start: date
    units_shipped: Decimal
    units_ordered: Decimal
    revenue_shipped: Decimal
    cogs_shipped: Decimal
    days_with_row: int
    glance_views: int


@dataclass(frozen=True, slots=True)
class CatalogueHistory:
    """Every catalogue product's weeks, its title and the covered day intervals."""

    canonical_asins: tuple[str, ...]
    """Sorted, as the roster orders them."""
    titles: dict[str, str]
    weeks: dict[str, tuple[CatalogueWeek, ...]]
    coverage: tuple[tuple[date, date], ...]


@dataclass(frozen=True, slots=True)
class CatalogueHistoryRequest:
    """The catalogue, the input end (a Saturday) and the number of weeks ending on it."""

    catalog: SyntheticEntityCatalog
    input_end: date
    weeks: int = 104

    def __post_init__(self) -> None:
        if self.input_end.weekday() != 5:  # noqa: PLR2004 -- Saturday ends a Vendor week
            raise ValueError("the input end must be a Saturday.")
        if self.input_end > self.catalog.shape.fixture_span.last_day:
            raise ValueError("the input end must not pass fixture_span.last_day.")
        if self.weeks < 1:
            raise ValueError("at least one week is required.")


def canonical_asin(asin: str) -> str:
    """Return the canonical product key the input builder writes for a catalogue ASIN."""
    return f"{CANONICAL_PREFIX}{asin.lower()}"


def _week(asin: str, start: date, days: list[ProductDayValues]) -> CatalogueWeek:
    return CatalogueWeek(
        canonical_asin=canonical_asin(asin),
        week_start=start,
        units_shipped=Decimal(sum(item.units_shipped for item in days)),
        units_ordered=Decimal(sum(item.units_ordered for item in days)),
        revenue_shipped=sum((item.shipped_revenue for item in days), Decimal(0)),
        cogs_shipped=sum((item.shipped_cogs for item in days), Decimal(0)),
        days_with_row=len(days),
        glance_views=sum(item.glance_views for item in days),
    )


def catalogue_history(request: CatalogueHistoryRequest) -> CatalogueHistory:
    """Roll the catalogue's Vendor day values up to weeks (Sunday to Saturday)."""
    catalog = request.catalog
    forecast = catalog.shape.forecast
    first_week = request.input_end - timedelta(days=_DAYS_PER_WEEK * request.weeks - 1)
    starts = tuple(first_week + timedelta(weeks=index) for index in range(request.weeks))
    first_day = max(first_week, forecast.vendor_history_start)
    weeks: dict[str, tuple[CatalogueWeek, ...]] = {}
    for product in catalog.products:
        found: list[CatalogueWeek] = []
        for start in starts:
            days = [
                catalog.values.product_day(product.asin, product.scenario, day)
                for day in (start + timedelta(days=offset) for offset in range(_DAYS_PER_WEEK))
                if day >= first_day
                and day != forecast.vendor_day_unlanded
                and catalog.values.vendor_present(product.asin, day)
            ]
            if days:
                found.append(_week(product.asin, start, days))
        weeks[canonical_asin(product.asin)] = tuple(found)
    unlanded = forecast.vendor_day_unlanded
    coverage = tuple(
        (low, high)
        for low, high in (
            (first_day, unlanded - timedelta(days=1)),
            (max(first_day, unlanded + timedelta(days=1)), request.input_end),
        )
        if low <= high
    )
    return CatalogueHistory(
        canonical_asins=tuple(sorted(weeks)),
        titles={canonical_asin(item.asin): item.title for item in catalog.products},
        weeks=weeks,
        coverage=coverage,
    )
