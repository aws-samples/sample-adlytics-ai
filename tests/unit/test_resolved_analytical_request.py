# SPDX-License-Identifier: MIT-0
"""All-or-nothing analytical intent resolution before measurement."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, replace
from datetime import date

import pytest

from adapters.outbound.metric_dictionary import load_catalog
from domain.catalog.capability import Catalog
from domain.semantics.analytical_request import (
    AnalyticalCurrencyMode,
    AnalyticalCurrencyRequest,
    AnalyticalOperationKind,
    AnalyticalResolutionResult,
    CandidateAnalyticalRequest,
    EntityIntent,
    FieldOrigin,
    PeriodIntent,
    ReportLensReference,
    SelectorReference,
    SourceSelector,
    resolve_analytical_request,
)
from domain.semantics.analytical_resolution import (
    AuthorizedAnalyticalScope,
    EffectiveLabel,
    EntityAlias,
    EntityKind,
    EntityReference,
    MappingEvidence,
    ResolutionOutcome,
)
from domain.semantics.identity import (
    BrandKey,
    CampaignKey,
    ChannelKey,
    CommerceScopeKey,
    MarketplaceKey,
    PlatformKey,
    SourceDatasetKey,
    SourceSystemKey,
)
from domain.shared_kernel.values import DateRange

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = load_catalog(ROOT)
TODAY = date(2026, 8, 19)
CURRENT = DateRange(date(2026, 8, 10), date(2026, 8, 16))
PRIOR = DateRange(date(2026, 8, 3), date(2026, 8, 9))
SCOPE = CommerceScopeKey("amazon_retail_us")
PLATFORM = PlatformKey("amazon_ads")
SPONSORED = ChannelKey("amazon_sponsored_products")
US = MarketplaceKey("amazon_us")


def _candidate(**overrides: object) -> CandidateAnalyticalRequest:
    fields: dict[str, object] = {
        "catalog": CATALOG,
        "today": TODAY,
        "scope": AuthorizedAnalyticalScope(frozenset({SCOPE})),
        "authorized_aliases": (),
        "metric_reference": "impressions",
        "operation_reference": "breakdown",
        "current_period": PeriodIntent(window=CURRENT),
        "grains": ("date",),
    }
    fields.update(overrides)
    return CandidateAnalyticalRequest(**fields)  # type: ignore[arg-type]


def _alias(
    kind: EntityKind,
    key: MarketplaceKey | ChannelKey | CampaignKey | BrandKey,
    label: str,
) -> EntityAlias:
    channel = key if isinstance(key, ChannelKey) else None
    marketplace = key if isinstance(key, MarketplaceKey) else None
    if isinstance(key, CampaignKey):
        channel = SPONSORED
        marketplace = US
    return EntityAlias(
        kind=kind,
        key=key,
        commerce_scope=SCOPE,
        labels=(EffectiveLabel(label, date(2026, 1, 1)),),
        source_identifiers=(),
        evidence=MappingEvidence(f"fictional-{kind.value}-catalog", 1),
        valid_from=date(2026, 1, 1),
        platform=PLATFORM if kind is EntityKind.CAMPAIGN else None,
        channel=channel,
        marketplace=marketplace,
    )


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("impressions", "impressions"),
        ("Impressions", "impressions"),
        ("ad attributed ROAS", "roas_attributed_14d"),
    ],
)
def test_metric_name_label_and_governed_alias_match_exactly(
    reference: str,
    expected: str,
) -> None:
    result = resolve_analytical_request(_candidate(metric_reference=reference))

    assert result.outcome is ResolutionOutcome.RESOLVED
    assert result.request is not None
    assert result.request.metric.value is not None
    assert result.request.metric.value.name == expected
    assert result.request.metric.origin is FieldOrigin.EXPLICIT_CUSTOMER_TEXT


@pytest.mark.covers("ADR-017")
def test_bare_roas_uses_the_governed_default_and_retains_default_origin() -> None:
    result = resolve_analytical_request(_candidate(metric_reference="ROAS"))

    assert result.outcome is ResolutionOutcome.RESOLVED
    assert result.request is not None
    assert result.request.metric.value == CATALOG.default_roas()
    assert result.request.metric.origin is FieldOrigin.GOVERNED_DEFAULT


def test_shared_metric_alias_is_ambiguous_and_fuzzy_text_is_not_found() -> None:
    metrics = tuple(
        replace(metric, aliases=(*metric.aliases, "shared fictional metric"))
        if metric.name in {"impressions", "clicks"}
        else metric
        for metric in CATALOG.metrics
    )
    ambiguous_catalog = Catalog(
        metrics=metrics,
        deferred=CATALOG.deferred,
        inventory_categories=CATALOG.inventory_categories,
    )

    ambiguous = resolve_analytical_request(
        _candidate(catalog=ambiguous_catalog, metric_reference="shared fictional metric")
    )
    fuzzy = resolve_analytical_request(_candidate(metric_reference="impression"))

    assert ambiguous.outcome is ResolutionOutcome.AMBIGUOUS
    assert ambiguous.request is None
    metric_record = ambiguous.field("metric")
    assert metric_record is not None
    assert {
        dict(candidate.differentiators)["metric"] for candidate in metric_record.candidates
    } == {
        "clicks",
        "impressions",
    }
    assert fuzzy.outcome is ResolutionOutcome.NOT_FOUND
    assert fuzzy.request is None


def test_periods_end_at_the_latest_closed_day_and_bare_last_week_is_ambiguous() -> None:
    open_day = resolve_analytical_request(
        _candidate(current_period=PeriodIntent(window=DateRange(TODAY, TODAY)))
    )
    last_week = resolve_analytical_request(
        _candidate(current_period=PeriodIntent(reference="last week"))
    )

    assert open_day.outcome is ResolutionOutcome.INVALID_CONTEXT
    assert open_day.request is None
    assert last_week.outcome is ResolutionOutcome.AMBIGUOUS
    assert last_week.request is None
    period = last_week.field("current_period")
    assert period is not None
    assert {candidate.label for candidate in period.candidates} == {
        "the 7 days ending yesterday",
        "last week (calendar, Monday to Sunday)",
    }


@pytest.mark.parametrize(
    ("today", "reference"),
    [
        (date(2026, 8, 17), "this week so far (from Monday)"),
        (date(2026, 8, 1), "this month so far"),
    ],
)
def test_empty_current_week_and_month_are_not_found(today: date, reference: str) -> None:
    result = resolve_analytical_request(
        _candidate(today=today, current_period=PeriodIntent(reference=reference))
    )

    assert result.outcome is ResolutionOutcome.NOT_FOUND
    assert result.request is None


@dataclass(frozen=True, slots=True)
class _OperationCase:
    reference: str
    grains: tuple[str, ...]
    prior: PeriodIntent | None
    n: int | None
    expected: AnalyticalOperationKind


@pytest.mark.parametrize(
    "case",
    [
        _OperationCase("breakdown", ("date",), None, None, AnalyticalOperationKind.BREAKDOWN),
        _OperationCase("trend", ("date",), None, None, AnalyticalOperationKind.TREND),
        _OperationCase(
            "comparison",
            ("date",),
            PeriodIntent(window=PRIOR),
            None,
            AnalyticalOperationKind.COMPARISON,
        ),
        _OperationCase("top value", ("campaign",), None, 5, AnalyticalOperationKind.TOP_VALUE),
        _OperationCase(
            "top increase",
            ("campaign",),
            PeriodIntent(window=PRIOR),
            5,
            AnalyticalOperationKind.TOP_INCREASE,
        ),
    ],
)
def test_supported_operations_resolve_with_required_prior_and_rank(
    case: _OperationCase,
) -> None:
    result = resolve_analytical_request(
        _candidate(
            operation_reference=case.reference,
            grains=case.grains,
            prior_period=case.prior,
            rank_n=case.n,
        )
    )

    assert result.outcome is ResolutionOutcome.RESOLVED
    assert result.request is not None
    assert result.request.operation.value is not None
    assert result.request.operation.value.kind is case.expected


@pytest.mark.parametrize("operation", ["bottom", "decline", "causal", "reconciliation"])
def test_unsupported_operations_refuse_and_unknown_operations_are_not_found(
    operation: str,
) -> None:
    unsupported = resolve_analytical_request(_candidate(operation_reference=operation))
    unknown = resolve_analytical_request(_candidate(operation_reference="fictional operation"))

    assert unsupported.outcome is ResolutionOutcome.UNSUPPORTED_CAPABILITY
    assert unsupported.request is None
    assert unknown.outcome is ResolutionOutcome.NOT_FOUND
    assert unknown.request is None


def test_comparison_and_top_increase_require_prior_and_rank() -> None:
    comparison = resolve_analytical_request(_candidate(operation_reference="comparison"))
    increase = resolve_analytical_request(
        _candidate(operation_reference="top increase", grains=("campaign",), rank_n=3)
    )
    missing_rank = resolve_analytical_request(
        _candidate(operation_reference="top value", grains=("campaign",))
    )

    assert comparison.outcome is ResolutionOutcome.NOT_FOUND
    assert increase.outcome is ResolutionOutcome.NOT_FOUND
    assert missing_rank.outcome is ResolutionOutcome.INVALID_CONTEXT


@pytest.mark.parametrize("kind", [EntityKind.BRAND, EntityKind.ADVERTISER])
def test_brand_and_advertiser_filters_are_unsupported_until_dimension_joins(
    kind: EntityKind,
) -> None:
    key = BrandKey("brand:fictional", SCOPE)
    result = resolve_analytical_request(
        _candidate(
            entities=(EntityIntent(kind, EntityReference(canonical_key=key)),),
        )
    )

    assert result.outcome is ResolutionOutcome.UNSUPPORTED_CAPABILITY
    assert result.request is None


def test_explicit_advertiser_grain_and_advertiser_entity_remain_distinct() -> None:
    grain = resolve_analytical_request(_candidate(grains=("advertiser",)))
    entity = resolve_analytical_request(
        _candidate(
            grains=("campaign",),
            entities=(
                EntityIntent(
                    EntityKind.ADVERTISER,
                    EntityReference(canonical_key=BrandKey("brand:fictional", SCOPE)),
                ),
            ),
        )
    )

    assert grain.outcome is ResolutionOutcome.NOT_FOUND
    assert grain.request is None
    assert grain.records[-1].field == "grains"
    assert entity.outcome is ResolutionOutcome.UNSUPPORTED_CAPABILITY
    assert entity.request is None
    assert entity.records[-1].field == "advertiser"
    resolved_grains = entity.field("grains")
    assert resolved_grains is not None
    assert resolved_grains.outcome is ResolutionOutcome.RESOLVED


def test_grains_are_nonempty_unique_typed_and_metric_compatible() -> None:
    empty = resolve_analytical_request(_candidate(grains=()))
    repeated = resolve_analytical_request(_candidate(grains=("date", "date")))
    unknown = resolve_analytical_request(_candidate(grains=("fictional",)))
    unsupported = resolve_analytical_request(_candidate(grains=("product",)))
    resolved = resolve_analytical_request(_candidate(grains=("campaign",)))

    assert empty.outcome is ResolutionOutcome.INVALID_CONTEXT
    assert repeated.outcome is ResolutionOutcome.INVALID_CONTEXT
    assert unknown.outcome is ResolutionOutcome.NOT_FOUND
    assert unsupported.outcome is ResolutionOutcome.UNSUPPORTED_CAPABILITY
    assert resolved.request is not None
    resolved_grains = resolved.request.grains.value
    assert resolved_grains is not None
    assert resolved_grains[0].column == "campaign_key"


def test_explicit_source_selection_is_unsupported_until_execution_can_apply_it() -> None:
    unsupported = resolve_analytical_request(
        _candidate(
            source=SourceSelector(
                SourceSystemKey("amc"),
                SourceDatasetKey("dsp_impressions"),
            )
        )
    )
    incompatible = resolve_analytical_request(
        _candidate(
            source=SourceSelector(
                SourceSystemKey("vendor_central"),
                SourceDatasetKey("GET_VENDOR_SALES_REPORT"),
            )
        )
    )

    assert unsupported.outcome is ResolutionOutcome.UNSUPPORTED_CAPABILITY
    assert unsupported.request is None
    assert unsupported.records[-1].origin is FieldOrigin.EXPLICIT_CUSTOMER_TEXT
    assert incompatible.outcome is ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE
    assert incompatible.request is None


@pytest.mark.covers("ADR-027")
def test_report_lenses_share_governed_values_retain_origins_and_satisfy_requires() -> None:
    defaulted = resolve_analytical_request(
        _candidate(metric_reference="units_ordered", grains=("marketplace",))
    )
    incompatible = resolve_analytical_request(
        _candidate(
            metric_reference="units_ordered",
            grains=("marketplace",),
            report_lenses=(ReportLensReference("distributor_view", "SOURCING"),),
        )
    )
    irrelevant = resolve_analytical_request(
        _candidate(
            report_lenses=(ReportLensReference("report_period", "DAY"),),
        )
    )

    assert defaulted.outcome is ResolutionOutcome.RESOLVED
    assert defaulted.request is not None
    assert {record.field: record.origin for record in defaulted.request.report_lenses} == {
        "distributor_view": FieldOrigin.GOVERNED_DEFAULT,
        "selling_program": FieldOrigin.GOVERNED_DEFAULT,
        "report_period": FieldOrigin.GOVERNED_DEFAULT,
    }
    assert incompatible.outcome is ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE
    assert irrelevant.outcome is ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE


@pytest.mark.covers("ADR-055")
def test_attribution_selector_is_exact_explicit_and_has_no_default() -> None:
    base = {
        "metric_reference": "roas_deduplicated_incl_halo_14d",
        "grains": ("channel", "marketplace"),
    }
    missing = resolve_analytical_request(_candidate(**base))
    unknown = resolve_analytical_request(
        _candidate(
            **base,
            selectors=(SelectorReference("attribution_model_key", "invented_touch"),),
        )
    )
    irrelevant = resolve_analytical_request(
        _candidate(selectors=(SelectorReference("attribution_model_key", "first_touch_v1"),))
    )

    assert missing.outcome is ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE
    assert missing.field("attribution_model_key") is not None
    assert unknown.outcome is ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE
    assert irrelevant.outcome is ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE


@pytest.mark.covers("ADR-055")
@pytest.mark.parametrize("model_key", ("first_touch_v1", "last_touch_v1", "linear_v1"))
def test_each_attribution_model_resolves_as_explicit_selector(model_key: str) -> None:
    result = resolve_analytical_request(
        _candidate(
            metric_reference="roas_deduplicated_incl_halo_14d",
            grains=("channel", "marketplace"),
            selectors=(SelectorReference("attribution_model_key", model_key),),
        )
    )

    assert result.outcome is ResolutionOutcome.RESOLVED
    assert result.request is not None
    assert len(result.request.selectors) == 1
    selector = result.request.selectors[0]
    assert selector.origin is FieldOrigin.EXPLICIT_CUSTOMER_TEXT
    assert selector.value == SelectorReference("attribution_model_key", model_key)


@pytest.mark.covers("ADR-004")
def test_native_money_requires_marketplace_selector_or_grain_and_target_fx_is_unsupported() -> None:
    unscoped = resolve_analytical_request(_candidate(metric_reference="ad_spend", grains=("date",)))
    grouped = resolve_analytical_request(
        _candidate(metric_reference="ad_spend", grains=("marketplace",))
    )
    target = resolve_analytical_request(
        _candidate(
            currency=AnalyticalCurrencyRequest(AnalyticalCurrencyMode.TARGET, "EUR"),
        )
    )

    assert unscoped.outcome is ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE
    assert grouped.outcome is ResolutionOutcome.RESOLVED
    assert target.outcome is ResolutionOutcome.UNSUPPORTED_CAPABILITY
    assert target.request is None


def test_every_final_field_has_origin_and_entity_mapping_evidence_survives() -> None:
    marketplace_alias = _alias(EntityKind.MARKETPLACE, US, "United States")
    result = resolve_analytical_request(
        _candidate(
            metric_reference="retail_revenue_shipped",
            grains=("product",),
            authorized_aliases=(marketplace_alias,),
            entities=(EntityIntent(EntityKind.MARKETPLACE, EntityReference(text="United States")),),
        )
    )

    assert result.outcome is ResolutionOutcome.RESOLVED
    assert result.request is not None
    request = result.request
    records = (
        request.scope,
        request.metric,
        request.current_period,
        request.operation,
        request.grains,
        request.sources,
        request.currency,
        *request.entities,
        *request.report_lenses,
    )
    assert all(record.origin is not None for record in records)
    marketplace = next(record for record in request.entities if record.field == "marketplace")
    assert marketplace.evidence == (MappingEvidence("fictional-marketplace-catalog", 1),)


@pytest.mark.covers("ADR-062")
def test_a_deferred_customer_label_resolves_to_unsupported_capability_not_not_found() -> None:
    """The deployed failure surface: a deferred label is a roadmap item, not a typo."""
    result = resolve_analytical_request(_candidate(metric_reference="incremental ROAS"))

    assert result.outcome is ResolutionOutcome.UNSUPPORTED_CAPABILITY
    assert result.request is None


@pytest.mark.covers("ADR-062")
def test_metric_not_found_stops_before_later_period_and_operation_blockers() -> None:
    result = resolve_analytical_request(
        _candidate(
            metric_reference="fictional unknown metric",
            current_period=None,
            operation_reference="fictional operation",
        )
    )

    assert result.outcome is ResolutionOutcome.NOT_FOUND
    assert result.request is None
    assert tuple(record.field for record in result.records) == ("scope", "metric")


@pytest.mark.covers("ADR-062")
def test_overlapping_prior_period_is_incompatible_with_comparison() -> None:
    result = resolve_analytical_request(
        _candidate(
            operation_reference="comparison",
            prior_period=PeriodIntent(window=DateRange(date(2026, 8, 9), date(2026, 8, 10))),
        )
    )

    assert result.outcome is ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE
    assert result.request is None
    prior = result.field("prior_period")
    assert prior is not None
    assert prior.outcome is ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE


@pytest.mark.covers("ADR-027")
def test_duplicate_report_lens_is_invalid_before_metric_compatibility() -> None:
    result = resolve_analytical_request(
        _candidate(
            metric_reference="units_ordered",
            grains=("marketplace",),
            report_lenses=(
                ReportLensReference("distributor_view", "MANUFACTURING"),
                ReportLensReference("distributor_view", "SOURCING"),
            ),
        )
    )

    assert result.outcome is ResolutionOutcome.INVALID_CONTEXT
    assert result.request is None
    lens = result.field("report_lenses")
    assert lens is not None
    assert lens.outcome is ResolutionOutcome.INVALID_CONTEXT


@pytest.mark.covers("ADR-062")
def test_unknown_metric_blocks_before_period_operation_and_grain_resolution() -> None:
    result = resolve_analytical_request(
        _candidate(
            metric_reference="fictional unknown metric",
            current_period=PeriodIntent(window=DateRange(TODAY, TODAY)),
            operation_reference="fictional operation",
            grains=("fictional",),
        )
    )

    assert result.outcome is ResolutionOutcome.NOT_FOUND
    assert result.request is None
    assert tuple(record.field for record in result.records) == ("scope", "metric")


@pytest.mark.covers("ADR-062")
def test_non_comparison_operation_rejects_an_unneeded_prior_period() -> None:
    result = resolve_analytical_request(_candidate(prior_period=PeriodIntent(window=PRIOR)))

    assert result.outcome is ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE
    assert result.request is None
    prior = result.field("prior_period")
    assert prior is not None
    assert prior.outcome is ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE


@pytest.mark.covers("ADR-062")
def test_duplicate_selector_is_invalid_before_selector_value_resolution() -> None:
    result = resolve_analytical_request(
        _candidate(
            metric_reference="roas_deduplicated_incl_halo_14d",
            grains=("channel", "marketplace"),
            selectors=(
                SelectorReference("attribution_model_key", "first_touch_v1"),
                SelectorReference("attribution_model_key", "last_touch_v1"),
            ),
        )
    )

    assert result.outcome is ResolutionOutcome.INVALID_CONTEXT
    assert result.request is None
    selector = result.field("selectors")
    assert selector is not None
    assert selector.outcome is ResolutionOutcome.INVALID_CONTEXT


SEPTEMBER_INTENT = PeriodIntent(window=DateRange(date(2026, 9, 1), date(2026, 9, 30)))


def _selectors(result: AnalyticalResolutionResult) -> list[tuple[str, str, FieldOrigin]]:
    request = result.request
    assert request is not None
    found: list[tuple[str, str, FieldOrigin]] = []
    for item in request.selectors:
        assert item.value is not None
        found.append((item.value.name, item.value.value, item.origin))
    return found


def test_a_required_selector_with_one_value_is_pinned_from_the_metric_not_refused() -> None:
    """segment_impressions is served for one complete month only: time_grain has one value."""
    result = resolve_analytical_request(
        _candidate(
            metric_reference="segment_impressions",
            grains=("audience_segment", "marketplace"),
            current_period=SEPTEMBER_INTENT,
            today=date(2026, 10, 17),
        )
    )
    assert result.outcome is ResolutionOutcome.RESOLVED
    assert _selectors(result) == [("time_grain", "month", FieldOrigin.GOVERNED_DEFAULT)]
    # A stated value still resolves as the customer's own.
    explicit = resolve_analytical_request(
        _candidate(
            metric_reference="segment_impressions",
            grains=("audience_segment", "marketplace"),
            current_period=SEPTEMBER_INTENT,
            today=date(2026, 10, 17),
            selectors=(SelectorReference("time_grain", "month"),),
        )
    )
    assert _selectors(explicit) == [("time_grain", "month", FieldOrigin.EXPLICIT_CUSTOMER_TEXT)]


def test_a_required_selector_with_several_values_is_still_never_assumed() -> None:
    """shoppers_reached holds week and month rows; segment_index three profile paths."""
    reach = resolve_analytical_request(
        _candidate(
            metric_reference="shoppers_reached",
            grains=("exposure_group", "identity_space", "marketplace"),
            current_period=SEPTEMBER_INTENT,
            today=date(2026, 10, 17),
        )
    )
    index = resolve_analytical_request(
        _candidate(
            metric_reference="segment_index",
            grains=("audience_segment", "marketplace"),
            current_period=SEPTEMBER_INTENT,
            today=date(2026, 10, 17),
        )
    )
    assert reach.outcome is ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE
    assert reach.field("time_grain") is not None
    assert index.outcome is ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE
    profile = index.field("profile_path")
    assert profile is not None
    assert profile.outcome is ResolutionOutcome.SEMANTICALLY_INCOMPATIBLE
