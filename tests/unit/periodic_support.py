# SPDX-License-Identifier: MIT-0
"""A synthetic periodic fact for periodic-serving tests (ADR-100; design S0.2).

No dictionary metric is periodic yet, so tests give the real catalog synthetic periodic metrics
in memory: a pack-5-like `synthetic_shoppers_reached`, a pack-6-like
`synthetic_frequency_shoppers`, a D-6 ratio over the real daily `ad_spend`, and the ARP cohort
ratio with and without its declared D-8 (b) exception. Packs 5 to 7 land 16 days after period
end, so `LAG_DAYS` is 16.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from domain.catalog.capability import (
    Catalog,
    CrossDeliveryException,
    InventoryCategory,
    Metric,
    MetricCompatibilityMode,
    MetricKind,
)
from domain.measurement.planning import FactShape
from domain.narrative.shaped_response import UnitKind
from domain.semantics.calendar_periods import CalendarPeriod
from domain.semantics.compatibility import AggregationKind, AxisBehavior, SemanticAxis
from domain.semantics.identity import PlatformKey
from domain.semantics.time import FactTimeClass, TimeGrain
from domain.shared_kernel.provenance import MeasurementBasis
from domain.shopper_aggregates import ShopperPolicyRequest, shopper_aggregate_policy

LAG_DAYS = 16
US = "ATVPDKIKX0DER"
SEPTEMBER = CalendarPeriod(date(2026, 9, 1), date(2026, 9, 30), TimeGrain.MONTH)
AUGUST = CalendarPeriod(date(2026, 8, 1), date(2026, 8, 31), TimeGrain.MONTH)
WEEK_38 = CalendarPeriod(date(2026, 9, 14), date(2026, 9, 20), TimeGrain.WEEK)
GRAINS = (TimeGrain.WEEK, TimeGrain.MONTH, TimeGrain.QUARTER)
SELECTOR = (("time_grain", ("month", "quarter", "week")),)
AXES = frozenset(
    {SemanticAxis.TIME, SemanticAxis.MARKETPLACE, SemanticAxis.ECOSYSTEM, SemanticAxis.PLATFORM}
)
PERIODIC_GRAIN = ("period_start", "period_end", "time_grain", "grain_set", "marketplace_key")


def periodic_metric(name: str, dataset: str, fact: str) -> Metric:
    policy = shopper_aggregate_policy(ShopperPolicyRequest(AggregationKind.EXACT_DISTINCT, AXES))
    return Metric(
        name=name,
        label=name.replace("_", " ").capitalize(),
        kind=MetricKind.SIMPLE,
        unit_kind=UnitKind.COUNT,
        measurement_basis=(MeasurementBasis.AD_ATTRIBUTED,),
        valid_grains=("marketplace",),
        time_class=FactTimeClass.PERIODIC,
        time_grain=TimeGrain.WEEK,
        definition=f"sum({fact}.shoppers)",
        source_system="amc",
        source_datasets=(dataset,),
        non_additive=True,
        required_selectors=SELECTOR,
        aggregation_policy=policy,
        ecosystem_aggregation=policy.ecosystem_aggregation,
        source_platforms=frozenset({PlatformKey("amazon_ads")}),
        period_grains=GRAINS,
    )


def ratio_metric(
    name: str, spec: tuple[str, str, UnitKind], exception: CrossDeliveryException | None = None
) -> Metric:
    numerator, denominator, unit = spec
    return Metric(
        name=name,
        label=name.replace("_", " ").capitalize(),
        kind=MetricKind.RATIO,
        unit_kind=unit,
        measurement_basis=(MeasurementBasis.AD_ATTRIBUTED,),
        valid_grains=("marketplace",),
        compatibility_mode=MetricCompatibilityMode.QUALIFIED_RATIO,
        numerator=numerator,
        denominator=denominator,
        ecosystem_aggregation=AxisBehavior.EXACT_ONLY,
        cross_delivery=exception,
        required_selectors=SELECTOR,
    )


def synthetic_catalog(real_catalog: Catalog) -> Catalog:
    """Return the real catalog plus the synthetic periodic metrics, in their own category."""
    reach = periodic_metric(
        "synthetic_shoppers_reached", "synthetic_exposure_groups", "fct_syn_exposure"
    )
    frequency = periodic_metric(
        "synthetic_frequency_shoppers", "synthetic_frequency_buckets", "fct_syn_frequency"
    )
    repeat = periodic_metric("synthetic_arp_repeat_buyers", "synthetic_arp_cohorts", "fct_syn_arp")
    cohort = periodic_metric("synthetic_cohort_buyers", "synthetic_cohorts", "fct_syn_cohort")
    spend = real_catalog.metric("ad_spend")
    assert spend is not None
    ratios = (
        ratio_metric("synthetic_cost_per_shopper", ("ad_spend", reach.name, UnitKind.CURRENCY)),
        ratio_metric("synthetic_frequency_share", (frequency.name, reach.name, UnitKind.RATIO)),
        ratio_metric(
            "synthetic_arp_rate",
            (repeat.name, cohort.name, UnitKind.RATIO),
            CrossDeliveryException.ARP_COHORT_RATIO,
        ),
        ratio_metric("synthetic_unguarded_rate", (repeat.name, cohort.name, UnitKind.RATIO)),
    )
    # The dictionary's own population and calendar, as a loaded entry would carry them.
    synthetic = tuple(
        replace(
            metric,
            population=spend.population,
            business_calendar=spend.business_calendar,
            profile_version=spend.profile_version,
        )
        for metric in (reach, frequency, repeat, cohort, *ratios)
    )
    last = max(category.display_order for category in real_catalog.inventory_categories)
    return Catalog(
        metrics=(*real_catalog.metrics, *synthetic),
        deferred=real_catalog.deferred,
        inventory_categories=(
            *real_catalog.inventory_categories,
            InventoryCategory(
                "synthetic_periodic",
                "Synthetic periodic",
                last + 1,
                tuple(m.name for m in synthetic),
            ),
        ),
    )


def periodic_shape(fact: str) -> FactShape:
    """Describe a synthetic periodic fact: one row set per period, grain set and marketplace."""
    return FactShape(
        name=fact,
        grain_columns=PERIODIC_GRAIN,
        columns=(*PERIODIC_GRAIN, "shoppers"),
        source_timezone="UTC",
    )
