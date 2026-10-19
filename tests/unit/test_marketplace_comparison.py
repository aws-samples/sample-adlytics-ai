# SPDX-License-Identifier: MIT-0
"""Marketplace-comparison reduction, evidence invariants, and canonical payloads."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import cast

import pytest

from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.analytical_pages import PresentationDimensionLabel
from domain.presentation.marketplace_comparison import (
    MARKETPLACE_COMPARISON_ARTIFACT_TYPE,
    MARKETPLACE_COMPARISON_ARTIFACT_VERSION,
    MARKETPLACE_COMPARISON_BOUNDARY,
    MARKETPLACE_CURRENCY_POLICY,
    MARKETPLACE_GRAIN,
    MARKETPLACE_REASON_PRECEDENCE,
    MARKETPLACE_SOURCE_TIME_POLICY,
    MarketplaceComparability,
    MarketplaceComparisonArtifact,
    MarketplaceComparisonReason,
    MarketplaceComparisonRequest,
    MarketplaceComparisonState,
    MarketplaceDirection,
    MarketplaceEvidenceCondition,
    MarketplaceMetric,
    MarketplaceMetricEvidence,
    MarketplacePeriodEvidence,
    MarketplaceRelation,
    MarketplaceSourceTime,
    build_marketplace_comparison,
    marketplace_comparison_from_payload,
    marketplace_comparison_payload,
)
from domain.shared_kernel.provenance import FreshnessStatus, MeasurementBasis, Suppression
from domain.shared_kernel.values import DateRange

pytestmark = pytest.mark.unit

CURRENT = DateRange(date(2026, 8, 1), date(2026, 8, 3))
COMPARATOR = DateRange(date(2026, 7, 29), date(2026, 7, 31))
USD = Units(UnitKind.CURRENCY, "USD")
EUR = Units(UnitKind.CURRENCY, "EUR")
UNSUPPRESSED = Suppression(False)
AMC_TIME = MarketplaceSourceTime("UTC", "amc_utc_business_day")
RETAIL_TIME = MarketplaceSourceTime("America/Los_Angeles", "vendor_pst_business_day")
LABELS = (
    PresentationDimensionLabel("marketplace_key", "market-a", "marketplace", "Fictional North"),
    PresentationDimensionLabel("marketplace_key", "market-b", "marketplace", "Fictional South"),
    PresentationDimensionLabel("marketplace_key", "market-c", "marketplace", "Fictional West"),
)


def _semantic(metric: MarketplaceMetric) -> tuple[MeasurementBasis, str, MarketplaceSourceTime]:
    if metric is MarketplaceMetric.AD_SPEND:
        return MeasurementBasis.AD_ATTRIBUTED, "amc", AMC_TIME
    return MeasurementBasis.RETAIL_ACTUAL, "vendor_central", RETAIL_TIME


def _side(  # noqa: PLR0913
    metric: MarketplaceMetric,
    role: str,
    value: str | None,
    *,
    condition: MarketplaceEvidenceCondition = MarketplaceEvidenceCondition.AVAILABLE,
    units: Units | None = USD,
    source_time: MarketplaceSourceTime | None = None,
    basis: MeasurementBasis | None = None,
    source: str | None = None,
) -> MarketplacePeriodEvidence:
    period = CURRENT if role == "current" else COMPARATOR
    expected_basis, expected_source, expected_time = _semantic(metric)
    actual_coverage: tuple[DateRange, ...] = (period,)
    holes: tuple[DateRange, ...] = ()
    row_count = 1
    suppression = UNSUPPRESSED
    truncated = False
    represented = None if value is None else Decimal(value)
    freshness_status: FreshnessStatus | None = FreshnessStatus.FRESH
    if condition is MarketplaceEvidenceCondition.MISSING_PUBLICATION:
        freshness_status = None
        actual_coverage = ()
        row_count = 0
        represented = None
    elif condition is MarketplaceEvidenceCondition.MISSING:
        row_count = 0
        represented = None
    elif condition is MarketplaceEvidenceCondition.COVERAGE_HOLE:
        actual_coverage = ()
        holes = (period,)
        row_count = 0
        represented = None
    elif condition is MarketplaceEvidenceCondition.SUPPRESSED:
        row_count = 0
        represented = None
        suppression = Suppression(True, "fictional privacy threshold")
    elif condition is MarketplaceEvidenceCondition.TRUNCATED:
        truncated = True
    elif condition is MarketplaceEvidenceCondition.STALE:
        freshness_status = FreshnessStatus.STALE
    elif condition is MarketplaceEvidenceCondition.QUARANTINED:
        freshness_status = FreshnessStatus.QUARANTINED
    elif condition is MarketplaceEvidenceCondition.INCOMPATIBLE_SCOPE:
        actual_coverage = ()
    return MarketplacePeriodEvidence(
        condition=condition,
        freshness_status=freshness_status,
        requested_period=period,
        actual_coverage=actual_coverage,
        coverage_holes=holes,
        value=represented,
        units=units if represented is not None else None,
        grain=MARKETPLACE_GRAIN,
        basis=expected_basis if basis is None else basis,
        source=expected_source if source is None else source,
        source_time=expected_time if source_time is None else source_time,
        suppression=suppression,
        row_count=row_count,
        truncated=truncated,
    )


def _metric(  # noqa: PLR0913, PLR0917
    key: str,
    metric: MarketplaceMetric,
    current: str = "20.00",
    comparator: str = "10.00",
    *,
    current_condition: MarketplaceEvidenceCondition = MarketplaceEvidenceCondition.AVAILABLE,
    comparator_condition: MarketplaceEvidenceCondition = MarketplaceEvidenceCondition.AVAILABLE,
    current_units: Units = USD,
    comparator_units: Units = USD,
    current_time: MarketplaceSourceTime | None = None,
    comparator_time: MarketplaceSourceTime | None = None,
    current_basis: MeasurementBasis | None = None,
    comparator_basis: MeasurementBasis | None = None,
) -> MarketplaceMetricEvidence:
    return MarketplaceMetricEvidence(
        key,
        metric,
        _side(
            metric,
            "current",
            current,
            condition=current_condition,
            units=current_units,
            source_time=current_time,
            basis=current_basis,
        ),
        _side(
            metric,
            "comparator",
            comparator,
            condition=comparator_condition,
            units=comparator_units,
            source_time=comparator_time,
            basis=comparator_basis,
        ),
    )


def _pair(  # noqa: PLR0913
    key: str,
    *,
    ad_current: str = "20.00",
    ad_comparator: str = "10.00",
    retail_current: str = "30.00",
    retail_comparator: str = "20.00",
) -> tuple[MarketplaceMetricEvidence, MarketplaceMetricEvidence]:
    return (
        _metric(key, MarketplaceMetric.AD_SPEND, ad_current, ad_comparator),
        _metric(
            key,
            MarketplaceMetric.RETAIL_REVENUE_SHIPPED,
            retail_current,
            retail_comparator,
        ),
    )


def _build(*evidence: MarketplaceMetricEvidence) -> MarketplaceComparisonArtifact:
    return build_marketplace_comparison(
        MarketplaceComparisonRequest(CURRENT, COMPARATOR, tuple(evidence))
    )


def test_closed_vocabularies_and_reason_precedence_are_exact() -> None:
    assert tuple(MarketplaceComparisonState) == (
        MarketplaceComparisonState.COMPARABLE,
        MarketplaceComparisonState.ONE_MARKETPLACE,
        MarketplaceComparisonState.INSUFFICIENT,
        MarketplaceComparisonState.NO_COVERAGE,
    )
    assert tuple(MarketplaceDirection) == (
        MarketplaceDirection.INCREASE,
        MarketplaceDirection.DECREASE,
        MarketplaceDirection.FLAT,
    )
    assert tuple(MarketplaceRelation) == (
        MarketplaceRelation.SAME_DIRECTION,
        MarketplaceRelation.OPPOSITE_DIRECTION,
        MarketplaceRelation.BOTH_FLAT,
        MarketplaceRelation.MIXED_FLAT,
    )
    assert tuple(reason.value for reason in MARKETPLACE_REASON_PRECEDENCE) == (
        "missing_current_publication",
        "missing_comparator_publication",
        "missing_current",
        "missing_comparator",
        "coverage_hole",
        "suppressed",
        "truncated",
        "stale",
        "quarantined",
        "currency_changed",
        "incompatible_scope",
        "source_calendar_mismatch",
    )


@pytest.mark.parametrize(
    ("current", "comparator", "direction", "change"),
    [
        ("12.50", "10.00", MarketplaceDirection.INCREASE, Decimal("2.50")),
        ("7.50", "10.00", MarketplaceDirection.DECREASE, Decimal("-2.50")),
        ("10.00", "10.00", MarketplaceDirection.FLAT, Decimal("0.00")),
    ],
)
def test_exact_decimal_change_maps_to_all_directions(  # noqa: PLR0913, PLR0917
    current: str,
    comparator: str,
    direction: MarketplaceDirection,
    change: Decimal,
) -> None:
    artifact = _build(
        _metric("market-a", MarketplaceMetric.AD_SPEND, current, comparator),
        _metric("market-a", MarketplaceMetric.RETAIL_REVENUE_SHIPPED, "1.00", "1.00"),
    )

    movement = artifact.rows[0].ad_spend
    assert movement.absolute_change == change
    assert movement.current.units == USD
    assert movement.current.grain == MARKETPLACE_GRAIN
    assert movement.current.basis is MeasurementBasis.AD_ATTRIBUTED
    assert movement.direction is direction


@pytest.mark.parametrize(
    ("ad", "ad_prior", "retail", "retail_prior", "relation"),
    [
        ("2", "1", "4", "3", MarketplaceRelation.SAME_DIRECTION),
        ("2", "1", "3", "4", MarketplaceRelation.OPPOSITE_DIRECTION),
        ("1", "1", "3", "3", MarketplaceRelation.BOTH_FLAT),
        ("1", "1", "4", "3", MarketplaceRelation.MIXED_FLAT),
    ],
)
def test_paired_directions_map_to_all_relations(  # noqa: PLR0913, PLR0917
    ad: str,
    ad_prior: str,
    retail: str,
    retail_prior: str,
    relation: MarketplaceRelation,
) -> None:
    artifact = _build(
        *_pair(
            "market-a",
            ad_current=ad,
            ad_comparator=ad_prior,
            retail_current=retail,
            retail_comparator=retail_prior,
        )
    )

    assert artifact.rows[0].relation is relation
    assert artifact.rows[0].comparability is MarketplaceComparability.COMPARABLE


def test_prior_zero_preserves_absolute_direction_but_has_no_percentage() -> None:
    artifact = _build(
        _metric("market-a", MarketplaceMetric.AD_SPEND, "3.0000", "0.0000"),
        _metric("market-a", MarketplaceMetric.RETAIL_REVENUE_SHIPPED),
    )

    movement = artifact.rows[0].ad_spend
    assert movement.absolute_change == Decimal("3.0000")
    assert movement.percentage_change is None
    assert movement.direction is MarketplaceDirection.INCREASE
    assert movement.current.units == USD
    assert movement.current.grain == MARKETPLACE_GRAIN


def test_represented_zero_is_distinct_from_covered_missing_row() -> None:
    zero = _build(
        _metric("market-a", MarketplaceMetric.AD_SPEND, "0.00", "0.00"),
        _metric("market-a", MarketplaceMetric.RETAIL_REVENUE_SHIPPED, "0.00", "0.00"),
    )
    missing = _build(
        _metric(
            "market-a",
            MarketplaceMetric.AD_SPEND,
            current_condition=MarketplaceEvidenceCondition.MISSING,
        ),
        _metric("market-a", MarketplaceMetric.RETAIL_REVENUE_SHIPPED),
    )

    assert zero.rows[0].ad_spend.current.value == Decimal("0.00")
    assert zero.rows[0].relation is MarketplaceRelation.BOTH_FLAT
    assert zero.state is MarketplaceComparisonState.ONE_MARKETPLACE
    assert missing.rows[0].ad_spend.current.value is None
    assert missing.rows[0].ad_spend.reasons == (MarketplaceComparisonReason.MISSING_CURRENT,)
    assert missing.rows[0].relation is None
    assert missing.state is MarketplaceComparisonState.INSUFFICIENT


@pytest.mark.parametrize(
    ("condition", "role", "expected"),
    [
        (
            MarketplaceEvidenceCondition.MISSING_PUBLICATION,
            "current",
            MarketplaceComparisonReason.MISSING_CURRENT_PUBLICATION,
        ),
        (
            MarketplaceEvidenceCondition.MISSING_PUBLICATION,
            "comparator",
            MarketplaceComparisonReason.MISSING_COMPARATOR_PUBLICATION,
        ),
        (
            MarketplaceEvidenceCondition.MISSING,
            "current",
            MarketplaceComparisonReason.MISSING_CURRENT,
        ),
        (
            MarketplaceEvidenceCondition.MISSING,
            "comparator",
            MarketplaceComparisonReason.MISSING_COMPARATOR,
        ),
        (
            MarketplaceEvidenceCondition.COVERAGE_HOLE,
            "current",
            MarketplaceComparisonReason.COVERAGE_HOLE,
        ),
        (
            MarketplaceEvidenceCondition.SUPPRESSED,
            "current",
            MarketplaceComparisonReason.SUPPRESSED,
        ),
        (MarketplaceEvidenceCondition.TRUNCATED, "current", MarketplaceComparisonReason.TRUNCATED),
        (MarketplaceEvidenceCondition.STALE, "current", MarketplaceComparisonReason.STALE),
        (
            MarketplaceEvidenceCondition.QUARANTINED,
            "current",
            MarketplaceComparisonReason.QUARANTINED,
        ),
        (
            MarketplaceEvidenceCondition.INCOMPATIBLE_SCOPE,
            "current",
            MarketplaceComparisonReason.INCOMPATIBLE_SCOPE,
        ),
    ],
)
def test_every_evidence_failure_maps_to_its_exact_reason(
    condition: MarketplaceEvidenceCondition,
    role: str,
    expected: MarketplaceComparisonReason,
) -> None:
    if role == "current":
        artifact = _build(
            _metric(
                "market-a",
                MarketplaceMetric.AD_SPEND,
                current_condition=condition,
            ),
            _metric("market-a", MarketplaceMetric.RETAIL_REVENUE_SHIPPED),
        )
    else:
        artifact = _build(
            _metric(
                "market-a",
                MarketplaceMetric.AD_SPEND,
                comparator_condition=condition,
            ),
            _metric("market-a", MarketplaceMetric.RETAIL_REVENUE_SHIPPED),
        )

    movement = artifact.rows[0].ad_spend
    assert expected in movement.reasons
    assert movement.comparability is MarketplaceComparability.INVALID
    assert movement.absolute_change is None
    assert movement.direction is None
    assert artifact.rows[0].relation is None


def test_multi_failure_reasons_use_one_precedence_independent_of_input_order() -> None:
    ad = _metric(
        "market-a",
        MarketplaceMetric.AD_SPEND,
        current_condition=MarketplaceEvidenceCondition.SUPPRESSED,
        comparator_condition=MarketplaceEvidenceCondition.MISSING_PUBLICATION,
        current_units=EUR,
    )
    retail = _metric(
        "market-a",
        MarketplaceMetric.RETAIL_REVENUE_SHIPPED,
        current_condition=MarketplaceEvidenceCondition.STALE,
        comparator_time=MarketplaceSourceTime("UTC", "changed_calendar"),
        current_basis=MeasurementBasis.MODELLED,
    )

    artifact = _build(retail, ad)

    assert artifact.rows[0].reasons == (
        MarketplaceComparisonReason.MISSING_COMPARATOR_PUBLICATION,
        MarketplaceComparisonReason.SUPPRESSED,
        MarketplaceComparisonReason.STALE,
        MarketplaceComparisonReason.INCOMPATIBLE_SCOPE,
        MarketplaceComparisonReason.SOURCE_CALENDAR_MISMATCH,
    )


def test_currency_change_invalidates_only_that_metric_and_cross_market_currencies_do_not() -> None:
    changed = _metric(
        "market-a",
        MarketplaceMetric.AD_SPEND,
        current_units=EUR,
        comparator_units=USD,
    )
    market_b = tuple(
        replace(
            item,
            current=replace(item.current, units=EUR),
            comparator=replace(item.comparator, units=EUR),
        )
        for item in _pair("market-b")
    )
    artifact = _build(
        changed, _metric("market-a", MarketplaceMetric.RETAIL_REVENUE_SHIPPED), *market_b
    )

    assert changed.current.units is not None
    assert changed.current.units.currency_code == "EUR"
    assert artifact.rows[0].ad_spend.reasons == (MarketplaceComparisonReason.CURRENCY_CHANGED,)
    assert artifact.rows[0].relation is None
    assert artifact.rows[1].relation is MarketplaceRelation.SAME_DIRECTION
    assert not hasattr(artifact, "total")
    assert not hasattr(artifact.rows[1], "rank")


def test_known_source_calendars_allow_relation_but_changed_calendar_does_not() -> None:
    valid = _build(*_pair("market-a"))
    mismatch = _build(
        _metric(
            "market-a",
            MarketplaceMetric.AD_SPEND,
            comparator_time=MarketplaceSourceTime("America/New_York", "changed_calendar"),
        ),
        _metric("market-a", MarketplaceMetric.RETAIL_REVENUE_SHIPPED),
    )

    assert valid.rows[0].ad_spend.current.source_time == AMC_TIME
    assert valid.rows[0].retail_revenue_shipped.current.source_time == RETAIL_TIME
    assert valid.rows[0].relation is MarketplaceRelation.SAME_DIRECTION
    assert mismatch.rows[0].ad_spend.reasons == (
        MarketplaceComparisonReason.SOURCE_CALENDAR_MISMATCH,
    )
    assert mismatch.rows[0].relation is None


def test_wrong_metric_basis_is_incompatible_scope_without_cross_basis_arithmetic() -> None:
    artifact = _build(
        _metric(
            "market-a",
            MarketplaceMetric.AD_SPEND,
            current_basis=MeasurementBasis.RETAIL_ACTUAL,
        ),
        _metric("market-a", MarketplaceMetric.RETAIL_REVENUE_SHIPPED),
    )

    assert artifact.rows[0].ad_spend.reasons == (MarketplaceComparisonReason.INCOMPATIBLE_SCOPE,)
    assert artifact.rows[0].ad_spend.absolute_change is None
    assert artifact.rows[0].retail_revenue_shipped.current.basis is MeasurementBasis.RETAIL_ACTUAL


@pytest.mark.parametrize(
    "wrong",
    [
        DateRange(date(2026, 7, 28), date(2026, 7, 31)),
        DateRange(date(2026, 7, 25), date(2026, 7, 27)),
        DateRange(date(2026, 8, 1), date(2026, 8, 3)),
    ],
)
def test_unequal_nonpreceding_or_overlapping_windows_are_refused(wrong: DateRange) -> None:
    with pytest.raises(ValueError, match="immediately preceding equal-length"):
        MarketplaceComparisonRequest(CURRENT, wrong, _pair("market-a"))


def test_available_and_missing_coverage_semantics_are_strict() -> None:
    with pytest.raises(ValueError, match="condition contradicts"):
        replace(_side(MarketplaceMetric.AD_SPEND, "current", "1"), actual_coverage=())
    with pytest.raises(ValueError, match="condition contradicts"):
        replace(
            _side(
                MarketplaceMetric.AD_SPEND,
                "current",
                None,
                condition=MarketplaceEvidenceCondition.MISSING,
            ),
            actual_coverage=(),
        )
    with pytest.raises(ValueError, match="condition contradicts"):
        replace(
            _side(
                MarketplaceMetric.AD_SPEND,
                "current",
                None,
                condition=MarketplaceEvidenceCondition.COVERAGE_HOLE,
            ),
            coverage_holes=(),
        )


def test_represented_values_require_native_units_and_missing_sides_forbid_them() -> None:
    with pytest.raises(ValueError, match="requires native currency units"):
        replace(_side(MarketplaceMetric.AD_SPEND, "current", "1"), units=None)
    with pytest.raises(ValueError, match="cannot claim currency units"):
        replace(
            _side(
                MarketplaceMetric.AD_SPEND,
                "current",
                None,
                condition=MarketplaceEvidenceCondition.MISSING,
            ),
            units=USD,
        )
    with pytest.raises(ValueError, match="cannot claim currency units"):
        replace(
            _side(
                MarketplaceMetric.AD_SPEND,
                "current",
                None,
                condition=MarketplaceEvidenceCondition.MISSING_PUBLICATION,
            ),
            units=USD,
        )


def test_canonical_join_is_independent_of_metric_and_marketplace_input_order() -> None:
    evidence = (*_pair("market-b"), *_pair("market-a"))

    forward = _build(*evidence)
    reverse = _build(*reversed(evidence))

    assert tuple(row.marketplace_key for row in forward.rows) == ("market-a", "market-b")
    assert forward == reverse
    assert marketplace_comparison_payload(forward, LABELS) == marketplace_comparison_payload(
        reverse, tuple(reversed(LABELS))
    )


def test_duplicate_or_incomplete_canonical_join_is_refused() -> None:
    ad = _metric("market-a", MarketplaceMetric.AD_SPEND)
    with pytest.raises(ValueError, match="repeats"):
        MarketplaceComparisonRequest(CURRENT, COMPARATOR, (ad, ad))
    with pytest.raises(ValueError, match="both registered"):
        _build(ad)


def test_all_global_states_follow_current_coverage_and_valid_pair_count() -> None:
    one = _build(*_pair("market-a"))
    comparable = _build(*_pair("market-a"), *_pair("market-b"))
    insufficient = _build(
        _metric(
            "market-a",
            MarketplaceMetric.AD_SPEND,
            current_condition=MarketplaceEvidenceCondition.STALE,
        ),
        _metric("market-a", MarketplaceMetric.RETAIL_REVENUE_SHIPPED),
    )
    no_coverage = _build(
        _metric(
            "market-a",
            MarketplaceMetric.AD_SPEND,
            current_condition=MarketplaceEvidenceCondition.MISSING_PUBLICATION,
        ),
        _metric(
            "market-a",
            MarketplaceMetric.RETAIL_REVENUE_SHIPPED,
            current_condition=MarketplaceEvidenceCondition.MISSING,
        ),
    )

    assert one.state is MarketplaceComparisonState.ONE_MARKETPLACE
    assert comparable.state is MarketplaceComparisonState.COMPARABLE
    assert insufficient.state is MarketplaceComparisonState.INSUFFICIENT
    assert no_coverage.state is MarketplaceComparisonState.NO_COVERAGE


def test_artifact_rejects_fabricated_state_arithmetic_relation_and_policy() -> None:
    artifact = _build(*_pair("market-a"))
    row = artifact.rows[0]
    with pytest.raises(ValueError, match="state contradicts"):
        replace(artifact, state=MarketplaceComparisonState.COMPARABLE)
    with pytest.raises(ValueError, match="exact evidence"):
        replace(row.ad_spend, absolute_change=Decimal("999"))
    with pytest.raises(ValueError, match="server-derived"):
        replace(row, relation=MarketplaceRelation.OPPOSITE_DIRECTION)
    with pytest.raises(ValueError, match="fixed domain policy"):
        replace(artifact, currency_policy="perform_fx")


def test_canonical_payload_round_trips_exact_decimal_evidence_and_fixed_policy() -> None:
    artifact = _build(*_pair("market-a", ad_current="12.3400", ad_comparator="0.0000"))

    payload = marketplace_comparison_payload(artifact, LABELS)
    rows = cast("list[dict[str, object]]", payload["rows"])
    ad = cast("dict[str, object]", rows[0]["ad_spend"])
    current = cast("dict[str, object]", ad["current"])

    assert payload["artifact_type"] == MARKETPLACE_COMPARISON_ARTIFACT_TYPE
    assert payload["artifact_version"] == MARKETPLACE_COMPARISON_ARTIFACT_VERSION
    assert payload["currency_policy"] == MARKETPLACE_CURRENCY_POLICY
    assert payload["source_time_policy"] == MARKETPLACE_SOURCE_TIME_POLICY
    assert payload["boundary"] == MARKETPLACE_COMPARISON_BOUNDARY
    assert rows[0]["marketplace"] == "Fictional North"
    assert "marketplace_key" not in rows[0]
    assert current["value"] == "12.3400"
    assert current["units"] == {"currency_code": "USD", "kind": "currency"}
    assert current["grain"] == ["period", "marketplace"]
    assert current["basis"] == "ad_attributed"
    assert current["freshness_status"] == "fresh"
    assert ad["absolute_change"] == "12.3400"
    assert ad["percentage_change"] is None
    assert marketplace_comparison_from_payload(payload, LABELS) == artifact


def test_payload_projection_requires_unique_complete_governed_labels() -> None:
    artifact = _build(*_pair("market-a"))
    with pytest.raises(ValueError, match="lacks"):
        marketplace_comparison_payload(artifact, ())
    collision = (
        LABELS[0],
        PresentationDimensionLabel("marketplace_key", "market-b", "marketplace", "Fictional North"),
    )
    with pytest.raises(ValueError, match="unique"):
        marketplace_comparison_payload(artifact, collision)


@pytest.mark.parametrize(
    "mutation",
    ["extra", "unknown_enum", "decimal_number", "noncanonical_date", "reordered_rows", "selector"],
)
def test_strict_payload_decoder_rejects_noncanonical_or_forbidden_shapes(mutation: str) -> None:
    artifact = _build(*_pair("market-a"), *_pair("market-b"))
    payload = deepcopy(marketplace_comparison_payload(artifact, LABELS))
    rows = cast("list[dict[str, object]]", payload["rows"])
    if mutation == "extra":
        payload["extra"] = True
    elif mutation == "unknown_enum":
        payload["state"] = "unknown"
    elif mutation == "decimal_number":
        ad = cast("dict[str, object]", rows[0]["ad_spend"])
        current = cast("dict[str, object]", ad["current"])
        current["value"] = 20
    elif mutation == "noncanonical_date":
        period = cast("dict[str, object]", payload["current_window"])
        period["start"] = "2026-8-1"
    elif mutation == "reordered_rows":
        payload["rows"] = list(reversed(rows))
    else:
        rows[0]["marketplace_key"] = "forbidden"

    with pytest.raises(ValueError):
        marketplace_comparison_from_payload(payload, LABELS)


def test_empty_typed_evidence_preserves_global_publication_reasons_without_invented_rows() -> None:
    publication_reasons = (
        MarketplaceComparisonReason.MISSING_CURRENT_PUBLICATION,
        MarketplaceComparisonReason.MISSING_COMPARATOR_PUBLICATION,
    )
    artifact = build_marketplace_comparison(
        MarketplaceComparisonRequest(
            CURRENT,
            COMPARATOR,
            (),
            publication_reasons=publication_reasons,
        )
    )

    assert artifact.state is MarketplaceComparisonState.NO_COVERAGE
    assert artifact.rows == ()
    assert artifact.publication_reasons == publication_reasons
    payload = marketplace_comparison_payload(artifact, ())
    assert payload["rows"] == []
    assert payload["publication_reasons"] == [
        "missing_current_publication",
        "missing_comparator_publication",
    ]
    assert marketplace_comparison_from_payload(payload, ()) == artifact


def test_concurrent_evidence_signals_emit_every_reason_in_canonical_precedence() -> None:
    coverage_stale_truncated = replace(
        _side(MarketplaceMetric.AD_SPEND, "current", "20.00"),
        condition=MarketplaceEvidenceCondition.COVERAGE_HOLE,
        freshness_status=FreshnessStatus.STALE,
        actual_coverage=(CURRENT,),
        coverage_holes=(CURRENT,),
        truncated=True,
    )
    suppressed_truncated = replace(
        _side(MarketplaceMetric.RETAIL_REVENUE_SHIPPED, "current", "30.00"),
        condition=MarketplaceEvidenceCondition.SUPPRESSED,
        value=None,
        units=None,
        suppression=Suppression(True, "fictional privacy threshold"),
        row_count=0,
        truncated=True,
    )
    artifact = _build(
        MarketplaceMetricEvidence(
            "market-a",
            MarketplaceMetric.AD_SPEND,
            coverage_stale_truncated,
            _side(MarketplaceMetric.AD_SPEND, "comparator", "10.00"),
        ),
        MarketplaceMetricEvidence(
            "market-a",
            MarketplaceMetric.RETAIL_REVENUE_SHIPPED,
            suppressed_truncated,
            _side(MarketplaceMetric.RETAIL_REVENUE_SHIPPED, "comparator", "20.00"),
        ),
    )

    assert artifact.rows[0].ad_spend.reasons == (
        MarketplaceComparisonReason.COVERAGE_HOLE,
        MarketplaceComparisonReason.TRUNCATED,
        MarketplaceComparisonReason.STALE,
    )
    assert artifact.rows[0].retail_revenue_shipped.reasons == (
        MarketplaceComparisonReason.SUPPRESSED,
        MarketplaceComparisonReason.TRUNCATED,
    )
    assert artifact.rows[0].reasons == (
        MarketplaceComparisonReason.COVERAGE_HOLE,
        MarketplaceComparisonReason.SUPPRESSED,
        MarketplaceComparisonReason.TRUNCATED,
        MarketplaceComparisonReason.STALE,
    )


def test_condition_and_freshness_contradictions_are_rejected() -> None:
    available = _side(MarketplaceMetric.AD_SPEND, "current", "20.00")
    with pytest.raises(ValueError, match="condition contradicts"):
        replace(available, condition=MarketplaceEvidenceCondition.STALE)
    with pytest.raises(ValueError, match="condition contradicts"):
        replace(available, freshness_status=FreshnessStatus.STALE)
    with pytest.raises(ValueError, match="exact inspected freshness"):
        replace(available, freshness_status=FreshnessStatus.NEVER_LOADED)
    with pytest.raises(ValueError, match="missing publication"):
        replace(available, freshness_status=None)


def test_marketplace_decimal_artifact_bound_rejects_pathological_values() -> None:
    with pytest.raises(ValueError, match="Decimal bound"):
        _side(MarketplaceMetric.AD_SPEND, "current", "1" * 257)
    with pytest.raises(ValueError, match="Decimal bound"):
        _side(MarketplaceMetric.AD_SPEND, "current", "1E+10001")


def test_large_decimal_movement_and_percentage_remain_exact_beyond_default_context() -> None:
    current = "9" * 38 + ".0000"
    comparator = "8" * 38 + ".0000"
    artifact = _build(
        _metric("market-a", MarketplaceMetric.AD_SPEND, current, comparator),
        _metric("market-a", MarketplaceMetric.RETAIL_REVENUE_SHIPPED),
    )

    movement = artifact.rows[0].ad_spend
    assert movement.absolute_change == Decimal("1" * 38 + ".0000")
    assert movement.percentage_change == "+12.50%"
    assert movement.direction is MarketplaceDirection.INCREASE
    assert movement.current.units == USD
    assert movement.current.grain == MARKETPLACE_GRAIN
