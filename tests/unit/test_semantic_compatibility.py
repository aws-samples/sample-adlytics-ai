# SPDX-License-Identifier: MIT-0
"""Typed metric compatibility and axis-aware aggregation prerequisites for Semantic V2.

These are component tests for ADR-048. Full SA-09/SA-10 additionally compile declarations into the
planner and prove refusal before query execution; those cross-boundary tests arrive with the planner
migration rather than being claimed here.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from domain.semantics.compatibility import (
    AggregationKind,
    AggregationOperation,
    AggregationPolicy,
    AxisBehavior,
    AxisPolicy,
    CompatibilityField,
    CompatibilitySignature,
    CompositionOperation,
    CompositionRule,
    RatioOfAggregatesPolicy,
    RatioOfSumsPolicy,
    SemanticAxis,
    SemanticCompatibilityError,
    validate_composition,
)
from domain.semantics.identity import CommerceScopeKey
from domain.semantics.time import BusinessCalendarKey, FactTimeClass, TimeGrain
from domain.shared_kernel.provenance import MeasurementBasis

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-048")]

TIME_PRODUCT = frozenset({SemanticAxis.TIME, SemanticAxis.PRODUCT})


def _axis(
    aggregation: AxisBehavior,
    capabilities: tuple[bool, bool, bool] = (True, True, True),
) -> AxisPolicy:
    groupable, filterable, alignable = capabilities
    return AxisPolicy(
        aggregation=aggregation,
        groupable=groupable,
        filterable=filterable,
        alignable=alignable,
    )


def _signature() -> CompatibilitySignature:
    return CompatibilitySignature(
        business_process="retail_demand",
        metric_semantics="revenue_shipped",
        measurement_basis=MeasurementBasis.RETAIL_ACTUAL,
        measurement_method="vendor_central_retail_analytics",
        population=CommerceScopeKey("amazon_us"),
        source_timezone="PST",
        business_calendar=BusinessCalendarKey("amazon_vendor_sunday", week_starts_on=6),
        time_class=FactTimeClass.DAILY_ADDITIVE,
        time_grain=TimeGrain.DAY,
        currency="USD",
        profile_version=1,
    )


def test_additive_measure_can_sum_only_across_axes_declared_additive() -> None:
    policy = AggregationPolicy(
        kind=AggregationKind.SUM,
        available_axes=TIME_PRODUCT,
        axes={
            SemanticAxis.TIME: _axis(AxisBehavior.ADDITIVE),
            SemanticAxis.PRODUCT: _axis(AxisBehavior.ADDITIVE),
        },
    )
    policy.require_rollup(TIME_PRODUCT, operation=AggregationOperation.SUM)


def test_axis_declaration_must_cover_every_available_axis() -> None:
    with pytest.raises(SemanticCompatibilityError, match="missing product"):
        AggregationPolicy(
            kind=AggregationKind.SUM,
            available_axes=TIME_PRODUCT,
            axes={SemanticAxis.TIME: _axis(AxisBehavior.ADDITIVE)},
        )


def test_axis_capabilities_require_real_booleans() -> None:
    with pytest.raises(TypeError, match="groupable must be a boolean"):
        AxisPolicy(
            aggregation=AxisBehavior.ADDITIVE,
            groupable=cast("Any", "false"),
            filterable=True,
            alignable=True,
        )
    with pytest.raises(TypeError, match="filterable must be a boolean"):
        AxisPolicy(
            aggregation=AxisBehavior.ADDITIVE,
            groupable=True,
            filterable=cast("Any", 1),
            alignable=True,
        )


def test_axis_group_filter_and_alignment_capabilities_are_independent() -> None:
    policy = AggregationPolicy(
        kind=AggregationKind.SUM,
        available_axes=frozenset({SemanticAxis.SOURCE_OPTION}),
        axes={
            SemanticAxis.SOURCE_OPTION: _axis(
                AxisBehavior.EXACT_ONLY,
                (False, True, False),
            )
        },
    )
    policy.require_filterable(frozenset({SemanticAxis.SOURCE_OPTION}))
    with pytest.raises(SemanticCompatibilityError, match="not groupable"):
        policy.require_groupable(frozenset({SemanticAxis.SOURCE_OPTION}))
    with pytest.raises(SemanticCompatibilityError, match="not alignable"):
        policy.require_alignable(frozenset({SemanticAxis.SOURCE_OPTION}))


def test_reporting_lens_cannot_be_summed_away() -> None:
    policy = AggregationPolicy(
        kind=AggregationKind.SUM,
        available_axes=frozenset({SemanticAxis.SOURCE_OPTION}),
        axes={SemanticAxis.SOURCE_OPTION: _axis(AxisBehavior.EXACT_ONLY)},
    )
    with pytest.raises(SemanticCompatibilityError, match="source_option is exact_only"):
        policy.require_rollup(
            frozenset({SemanticAxis.SOURCE_OPTION}), operation=AggregationOperation.SUM
        )


def test_exact_distinct_refuses_time_sum() -> None:
    policy = AggregationPolicy(
        kind=AggregationKind.EXACT_DISTINCT,
        available_axes=frozenset({SemanticAxis.TIME}),
        axes={SemanticAxis.TIME: _axis(AxisBehavior.EXACT_ONLY)},
    )
    with pytest.raises(SemanticCompatibilityError, match="cannot be rolled up with sum"):
        policy.require_rollup(frozenset({SemanticAxis.TIME}), operation=AggregationOperation.SUM)


def test_nonaggregatable_metric_cannot_claim_an_additive_axis() -> None:
    with pytest.raises(SemanticCompatibilityError, match="incompatible axis algebra"):
        AggregationPolicy(
            kind=AggregationKind.NON_AGGREGATABLE,
            available_axes=frozenset({SemanticAxis.TIME}),
            axes={SemanticAxis.TIME: _axis(AxisBehavior.ADDITIVE)},
        )


def test_ratio_value_cannot_be_given_a_direct_rollup_policy() -> None:
    with pytest.raises(SemanticCompatibilityError, match="needs RatioOfSumsPolicy"):
        AggregationPolicy(
            kind=AggregationKind.RATIO_OF_SUMS,
            available_axes=frozenset({SemanticAxis.TIME}),
            axes={SemanticAxis.TIME: _axis(AxisBehavior.ADDITIVE)},
        )


def test_ratio_of_sums_rolls_up_both_operands_before_division() -> None:
    operand = AggregationPolicy(
        kind=AggregationKind.SUM,
        available_axes=frozenset({SemanticAxis.TIME}),
        axes={SemanticAxis.TIME: _axis(AxisBehavior.ADDITIVE)},
    )
    ratio = RatioOfSumsPolicy(numerator=operand, denominator=operand)
    ratio.require_operand_rollup(frozenset({SemanticAxis.TIME}))


def test_ratio_of_sums_requires_both_operands_to_accept_a_filter() -> None:
    numerator = AggregationPolicy(
        kind=AggregationKind.SUM,
        available_axes=frozenset({SemanticAxis.CHANNEL}),
        axes={SemanticAxis.CHANNEL: _axis(AxisBehavior.ADDITIVE)},
    )
    denominator = AggregationPolicy(
        kind=AggregationKind.SUM,
        available_axes=frozenset({SemanticAxis.CHANNEL}),
        axes={
            SemanticAxis.CHANNEL: _axis(
                AxisBehavior.ADDITIVE,
                (True, False, True),
            )
        },
    )
    ratio = RatioOfSumsPolicy(numerator=numerator, denominator=denominator)
    with pytest.raises(SemanticCompatibilityError, match="channel is not filterable"):
        ratio.require_filterable(frozenset({SemanticAxis.CHANNEL}))


def test_ratio_of_sums_refuses_a_non_sum_operand() -> None:
    additive = AggregationPolicy(
        kind=AggregationKind.SUM,
        available_axes=frozenset({SemanticAxis.TIME}),
        axes={SemanticAxis.TIME: _axis(AxisBehavior.ADDITIVE)},
    )
    exact = AggregationPolicy(
        kind=AggregationKind.EXACT_DISTINCT,
        available_axes=frozenset({SemanticAxis.TIME}),
        axes={SemanticAxis.TIME: _axis(AxisBehavior.EXACT_ONLY)},
    )
    with pytest.raises(SemanticCompatibilityError, match="denominator must use SUM"):
        RatioOfSumsPolicy(numerator=additive, denominator=exact)


def test_same_measurement_signatures_can_sum() -> None:
    result = validate_composition(
        _signature(),
        _signature(),
        CompositionRule(operation=CompositionOperation.SUM),
    )
    assert result.differences == ()
    assert result.required_qualifiers == frozenset()


def test_sum_rule_cannot_authorize_cross_currency_arithmetic() -> None:
    with pytest.raises(SemanticCompatibilityError, match="sum can never permit: currency"):
        CompositionRule(
            operation=CompositionOperation.SUM,
            allowed_differences=frozenset({CompatibilityField.CURRENCY}),
            required_qualifiers=frozenset({CompatibilityField.CURRENCY}),
        )


def test_permitted_difference_requires_a_structured_qualifier() -> None:
    with pytest.raises(SemanticCompatibilityError, match="needs a structured qualifier"):
        CompositionRule(
            operation=CompositionOperation.RATIO,
            allowed_differences=frozenset({CompatibilityField.METRIC_SEMANTICS}),
        )


def test_daily_and_weekly_measurements_cannot_sum_silently() -> None:
    weekly = replace(
        _signature(),
        time_class=FactTimeClass.PERIODIC,
        time_grain=TimeGrain.WEEK,
    )
    with pytest.raises(SemanticCompatibilityError, match="time_class, time_grain"):
        validate_composition(
            _signature(), weekly, CompositionRule(operation=CompositionOperation.SUM)
        )


def test_cross_currency_ratio_is_refused_even_though_output_is_dimensionless() -> None:
    other = replace(_signature(), metric_semantics="ad_spend", currency="EUR")
    rule = CompositionRule(
        operation=CompositionOperation.RATIO,
        allowed_differences=frozenset({CompatibilityField.METRIC_SEMANTICS}),
        required_qualifiers=frozenset({CompatibilityField.METRIC_SEMANTICS}),
    )
    with pytest.raises(SemanticCompatibilityError, match="currency"):
        validate_composition(_signature(), other, rule)


def test_explicit_reconciliation_preserves_method_qualifiers() -> None:
    other = replace(
        _signature(),
        measurement_method="amc_retail_purchases",
        metric_semantics="revenue_purchased",
    )
    qualified = frozenset(
        {CompatibilityField.METRIC_SEMANTICS, CompatibilityField.MEASUREMENT_METHOD}
    )
    result = validate_composition(
        _signature(),
        other,
        CompositionRule(
            operation=CompositionOperation.RECONCILIATION,
            allowed_differences=qualified,
            required_qualifiers=qualified,
        ),
    )
    assert {difference.field for difference in result.differences} == qualified
    assert result.required_qualifiers == qualified


def test_attribution_window_difference_requires_explicit_permission() -> None:
    first = replace(_signature(), attribution_window_days=14)
    second = replace(_signature(), attribution_window_days=30)
    with pytest.raises(SemanticCompatibilityError, match="attribution_window"):
        validate_composition(
            first, second, CompositionRule(operation=CompositionOperation.COMPARISON)
        )


def test_ratio_of_aggregates_allows_exact_operand_only_without_rollup() -> None:
    additive = AggregationPolicy(
        AggregationKind.SUM,
        frozenset({SemanticAxis.TIME}),
        {SemanticAxis.TIME: _axis(AxisBehavior.ADDITIVE)},
    )
    exact = AggregationPolicy(
        AggregationKind.EXACT_DISTINCT,
        frozenset({SemanticAxis.TIME}),
        {SemanticAxis.TIME: _axis(AxisBehavior.EXACT_ONLY)},
    )
    ratio = RatioOfAggregatesPolicy(additive, exact)
    ratio.require_operand_rollup(frozenset(), frozenset())
    with pytest.raises(SemanticCompatibilityError, match="exact_distinct cannot be rolled up"):
        ratio.require_operand_rollup(frozenset(), frozenset({SemanticAxis.TIME}))
