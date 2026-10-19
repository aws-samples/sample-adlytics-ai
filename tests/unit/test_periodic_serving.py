# SPDX-License-Identifier: MIT-0
"""Periodic serving on a synthetic periodic fact with a 16-day lag (ADR-100; design §4.7, S0.2).

No dictionary metric is periodic yet (S2.1 adds the first), so these tests give the real catalog
synthetic periodic metrics in memory: a pack-5-like `synthetic_shoppers_reached`, a pack-6-like
`synthetic_frequency_shoppers`, a D-6 ratio over the real daily `ad_spend`, and the ARP cohort
ratio. Every refusal asserts its exact signal and the layer that raised it, beside a positive
control.
"""

from __future__ import annotations

import pathlib
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from periodic_support import (
    AUGUST,
    GRAINS,
    SEPTEMBER,
    US,
    WEEK_38,
    periodic_metric,
    periodic_shape,
    synthetic_catalog,
)
from publication_support import bound_plan

from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.athena.composition import compose
from adapters.outbound.metric_dictionary import load_catalog
from domain.catalog.capability import (
    GRAIN_NOT_SUPPORTED_CODE,
    Catalog,
    CatalogError,
    CrossDeliveryException,
)
from domain.measurement.periodic import (
    CrossDeliveryCheck,
    CrossDeliveryOperation,
    DeliveryOperand,
    arp_ratio_withheld_reason,
    check_cross_delivery,
    require_daily_coverage,
    select_served_period,
)
from domain.measurement.planning import FactShape, FactShapes, PlanRequest, plan
from domain.semantics.calendar_periods import (
    CalendarPeriod,
    CalendarPeriodError,
    grain_list_text,
    is_complete_period,
    latest_complete_period,
    period_containing,
)
from domain.semantics.time import FactTimeClass, TimeGrain
from domain.shared_kernel.errors import DomainError
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-100", "ADR-048")]

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return synthetic_catalog(load_catalog(ROOT))


def _shape(fact: str) -> FactShape:
    return periodic_shape(fact)


def _filters(grain: str = "month") -> dict[str, str]:
    return {"time_grain": grain, "grain_set": "marketplace", "marketplace_key": US}


def _refused(call: object) -> DomainError:
    with pytest.raises(DomainError) as refused:
        call()  # type: ignore[operator]
    return refused.value


# --- Calendar periods: the one home of the arithmetic --------------------------------------------


def test_complete_periods_are_iso_weeks_calendar_months_and_quarters_in_utc() -> None:
    assert is_complete_period(TimeGrain.WEEK, date(2026, 9, 14), date(2026, 9, 20))
    assert not is_complete_period(TimeGrain.WEEK, date(2026, 9, 15), date(2026, 9, 21))
    assert is_complete_period(TimeGrain.MONTH, date(2026, 2, 1), date(2026, 2, 28))
    assert not is_complete_period(TimeGrain.MONTH, date(2026, 9, 1), date(2026, 9, 29))
    assert is_complete_period(TimeGrain.QUARTER, date(2026, 10, 1), date(2026, 12, 31))
    assert not is_complete_period(TimeGrain.QUARTER, date(2026, 8, 1), date(2026, 10, 31))
    with pytest.raises(CalendarPeriodError, match="isn't one complete week"):
        CalendarPeriod(date(2026, 9, 15), date(2026, 9, 21), TimeGrain.WEEK)
    with pytest.raises(CalendarPeriodError, match="isn't a calendar grain"):
        period_containing(TimeGrain.DAY, date(2026, 9, 1))


def test_labels_name_the_period_and_are_built_on_the_server() -> None:
    assert WEEK_38.label == "Week of 14\u201320 Sep 2026"
    assert period_containing(TimeGrain.WEEK, date(2026, 10, 1)).label == (
        "Week of 28 Sep\u20134 Oct 2026"
    )
    assert period_containing(TimeGrain.WEEK, date(2026, 1, 1)).label == (
        "Week of 29 Dec 2025\u20134 Jan 2026"
    )
    assert SEPTEMBER.label == "September 2026"
    assert period_containing(TimeGrain.QUARTER, date(2026, 8, 9)).label == "Q3 2026 (Jul\u2013Sep)"
    assert SEPTEMBER.previous() == AUGUST
    assert latest_complete_period(TimeGrain.MONTH, date(2026, 9, 30)) == SEPTEMBER
    assert latest_complete_period(TimeGrain.MONTH, date(2026, 10, 5)) == SEPTEMBER
    assert grain_list_text((TimeGrain.QUARTER, TimeGrain.MONTH)) == "months and quarters"
    assert grain_list_text(GRAINS) == "weeks, months and quarters"


# --- Planner: exact period selection (D-1, D-7) ---------------------------------------------------


def test_a_periodic_metric_plans_at_one_exact_period_and_composes_an_exact_predicate(
    catalog: Catalog,
) -> None:
    request = PlanRequest(
        "synthetic_shoppers_reached", ("marketplace",), SEPTEMBER.window, _filters()
    )
    planned = plan(catalog, request, _shape("fct_syn_exposure"))
    assert planned.period == SEPTEMBER
    assert planned.periodic is True
    assert dict(planned.filters) == _filters()
    composed = compose(bound_plan(planned, datetime(2026, 10, 20, tzinfo=UTC)), max_rows=10)
    assert '"period_start" = CAST(? AS DATE) AND "period_end" = CAST(? AS DATE)' in composed.sql
    assert "BETWEEN" not in composed.sql
    assert composed.parameters[:2] == ("2026-09-01", "2026-09-30")


def test_a_rolling_window_is_refused_with_the_nearest_complete_period(catalog: Catalog) -> None:
    """ADR-100 point 3: 'in the last 7 days' refuses shopper counts."""
    rolling = PlanRequest(
        "synthetic_shoppers_reached",
        ("marketplace",),
        DateRange(date(2026, 9, 16), date(2026, 9, 22)),
        _filters("week"),
    )
    error = _refused(lambda: plan(catalog, rolling, _shape("fct_syn_exposure")))
    assert error.code == GRAIN_NOT_SUPPORTED_CODE
    assert error.context["reason"] == "period_not_complete"
    assert error.context["nearest_period_start"] == "2026-09-14"
    assert error.context["nearest_period_end"] == "2026-09-20"
    assert error.context["nearest_period_label"] == "Week of 14\u201320 Sep 2026"
    assert "distinct shoppers can't be added up across days" in error.description
    # Positive control: the complete week plans.
    week = replace(rolling, window=WEEK_38.window)
    assert plan(catalog, week, _shape("fct_syn_exposure")).period == WEEK_38


def test_the_period_grain_is_an_exact_selector(catalog: Catalog) -> None:
    """D-7: no default sum across grains; an undeclared grain is refused."""
    unpinned = PlanRequest(
        "synthetic_shoppers_reached",
        ("marketplace",),
        SEPTEMBER.window,
        {"grain_set": "marketplace", "marketplace_key": US},
    )
    error = _refused(lambda: plan(catalog, unpinned, _shape("fct_syn_exposure")))
    # Refused by the dictionary's required selector before the period rule runs.
    assert error.context["reason"] == "requires_source_option"
    weekly_month = PlanRequest(
        "synthetic_shoppers_reached", ("marketplace",), SEPTEMBER.window, _filters("week")
    )
    error = _refused(lambda: plan(catalog, weekly_month, _shape("fct_syn_exposure")))
    assert error.context["reason"] == "period_not_complete"


def test_a_served_period_is_selected_and_an_unserved_one_names_the_nearest() -> None:
    served = frozenset({AUGUST, WEEK_38})
    assert select_served_period("synthetic_shoppers_reached", AUGUST, served) == AUGUST
    error = _refused(lambda: select_served_period("synthetic_shoppers_reached", SEPTEMBER, served))
    assert error.code == GRAIN_NOT_SUPPORTED_CODE
    assert error.context["reason"] == "period_not_served"
    assert error.context["nearest_period_label"] == "August 2026"
    assert error.description == (
        "synthetic_shoppers_reached hasn't been measured for September 2026. "
        "The nearest measured month is August 2026."
    )


# --- D-6: a daily-additive numerator over a periodic denominator ---------------------------------


def test_d6_spend_per_shopper_sums_exactly_the_period_days(catalog: Catalog) -> None:
    contracts = [contract for _, contract in load_contracts(ROOT)]
    shapes = FactShapes(
        numerator=FactShape.for_fact("fct_ad_exposure", contracts),
        denominator=_shape("fct_syn_exposure"),
    )
    request = PlanRequest(
        "synthetic_cost_per_shopper", ("marketplace",), SEPTEMBER.window, _filters()
    )
    planned = plan(catalog, request, shapes)
    assert planned.period == SEPTEMBER
    assert planned.periodic is False
    assert planned.join is not None
    assert planned.join.periodic is True
    assert "time_grain" not in planned.filters
    assert dict(planned.join.filters)["time_grain"] == "month"
    composed = compose(bound_plan(planned, datetime(2026, 10, 20, tzinfo=UTC)), max_rows=10)
    numerator_sql, denominator_sql = composed.sql.split("FULL JOIN")
    assert '"date_key" BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)' in numerator_sql
    assert '"period_start" = CAST(? AS DATE)' in denominator_sql
    # A rolling window refuses the D-6 ratio too, from the planner.
    rolling = replace(request, window=DateRange(date(2026, 9, 1), date(2026, 9, 29)))
    error = _refused(lambda: plan(catalog, rolling, shapes))
    assert error.context["reason"] == "period_not_complete"


def test_d6_refuses_a_period_with_a_day_missing_its_publication() -> None:
    covered = (
        DateRange(date(2026, 9, 1), date(2026, 9, 14)),
        DateRange(date(2026, 9, 16), date(2026, 9, 30)),
    )
    error = _refused(
        lambda: require_daily_coverage("synthetic_cost_per_shopper", SEPTEMBER, covered)
    )
    assert error.context["reason"] == "daily_coverage_incomplete"
    assert "2026-09-15 has no current publication" in error.description
    # Positive control: full coverage passes.
    require_daily_coverage(
        "synthetic_cost_per_shopper", SEPTEMBER, (DateRange(date(2026, 8, 30), date(2026, 10, 2)),)
    )


# --- D-8: one population per figure ---------------------------------------------------------------


def test_d8_refuses_pack_6_shoppers_over_pack_5_shoppers_in_the_planner(catalog: Catalog) -> None:
    shapes = FactShapes(
        numerator=_shape("fct_syn_frequency"), denominator=_shape("fct_syn_exposure")
    )
    request = PlanRequest(
        "synthetic_frequency_share", ("marketplace",), SEPTEMBER.window, _filters()
    )
    error = _refused(lambda: plan(catalog, request, shapes))
    assert error.code == GRAIN_NOT_SUPPORTED_CODE
    assert error.context["reason"] == "cross_delivery"
    assert "would divide figures from two deliveries" in error.description
    # Positive control (D-8 c): the D-6 ratio over the same period plans (above). (b), the ARP
    # cohort ratio, is the declared exception: D-8 admits its divide across the two deliveries...
    check_cross_delivery(
        CrossDeliveryCheck(
            "synthetic_arp_rate",
            CrossDeliveryOperation.DIVIDE,
            _operand("pack-15", SEPTEMBER),
            _operand("pack-14", SEPTEMBER),
            CrossDeliveryException.ARP_COHORT_RATIO,
        )
    )
    # ...and the Retention bake computes it under the cohort-size check, so the planner refuses
    # it with a typed reason offering both operands, where an undeclared ratio is a D-8 refusal.
    arp = FactShapes(numerator=_shape("fct_syn_arp"), denominator=_shape("fct_syn_cohort"))
    error = _refused(lambda: plan(catalog, replace(request, metric_name="synthetic_arp_rate"), arp))
    assert error.context["reason"] == "not_yet_computable"
    assert error.description.endswith(
        "Available instead: synthetic_arp_repeat_buyers, synthetic_cohort_buyers."
    )
    error = _refused(
        lambda: plan(catalog, replace(request, metric_name="synthetic_unguarded_rate"), arp)
    )
    assert error.context["reason"] == "cross_delivery"


def test_d8_b_withholds_the_arp_rate_when_the_cohort_sizes_disagree() -> None:
    assert arp_ratio_withheld_reason(12, 40, 40) is None
    assert arp_ratio_withheld_reason(12, 40, 39) == "cohort sizes changed between runs"
    assert arp_ratio_withheld_reason(41, 40, 40) == "cohort sizes changed between runs"


def _operand(contract: str, period: CalendarPeriod, scheme: str | None = None) -> DeliveryOperand:
    return DeliveryOperand(contract, "marketplace", period, scheme)


@pytest.mark.parametrize(
    "operation",
    [CrossDeliveryOperation.SUM, CrossDeliveryOperation.SUBTRACT, CrossDeliveryOperation.DIVIDE],
)
def test_d8_refuses_every_cross_contract_operation(operation: CrossDeliveryOperation) -> None:
    check = CrossDeliveryCheck(
        "figure", operation, _operand("pack-6", SEPTEMBER), _operand("pack-5", SEPTEMBER)
    )
    error = _refused(lambda: check_cross_delivery(check))
    assert error.context["reason"] == "cross_delivery"
    assert f"would {operation.value} figures from two deliveries" in error.description
    # Positive control: the same operation inside one delivery.
    check_cross_delivery(replace(check, right=_operand("pack-6", SEPTEMBER)))


def test_d8_a_allows_a_prior_calendar_period_change_and_refuses_a_bucket_scheme_change() -> None:
    allowed = CrossDeliveryCheck(
        "frequency",
        CrossDeliveryOperation.COMPARE,
        _operand("pack-6", SEPTEMBER, "fine"),
        _operand("pack-6", AUGUST, "fine"),
    )
    check_cross_delivery(allowed)
    changed = replace(allowed, right=_operand("pack-6", AUGUST, "coarse"))
    error = _refused(lambda: check_cross_delivery(changed))
    assert error.context["reason"] == "bucket_scheme_changed"
    assert error.description == "bucket scheme changed in September 2026"
    across = replace(allowed, right=_operand("pack-5", AUGUST, "fine"))
    assert _refused(lambda: check_cross_delivery(across)).context["reason"] == "cross_delivery"


def test_a_periodic_metric_declares_its_grains_and_the_selector() -> None:
    metric = periodic_metric("synthetic_shoppers_reached", "synthetic_exposure_groups", "fct_x")
    with pytest.raises(CatalogError, match="requires the time_grain selector"):
        replace(metric, required_selectors=())
    with pytest.raises(CatalogError, match="declares period_grains from week, month and quarter"):
        replace(metric, period_grains=(TimeGrain.MONTH, TimeGrain.WEEK))
    with pytest.raises(CatalogError, match="only a periodic simple metric declares period_grains"):
        replace(metric, time_class=FactTimeClass.DAILY_ADDITIVE)
