# SPDX-License-Identifier: MIT-0
"""Deterministic analytical page-schema declarations and catalog compatibility."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from adapters.jobs.presentation_validation.bindings import PAGE_BINDINGS_PATH, load_page_bindings
from adapters.jobs.presentation_validation.generate import (
    generated_document,
    section_metric_roles,
)
from adapters.jobs.presentation_validation.loader import (
    PAGE_SCHEMAS_PATH,
    load_page_schemas,
    load_v2_page_schemas,
)
from adapters.outbound.attribution_models import load_attribution_models
from adapters.outbound.metric_dictionary import load_catalog
from domain.presentation.page_schemas import (
    AnalyticalPageSchema,
    Cardinality,
    MetricRole,
    PageGrain,
    PageMetric,
    PageMetricVariant,
    PageMode,
    PageSchemaError,
    PageSchemaSet,
    PageSection,
    PageTime,
    PageValidationContext,
    PresentationArtifactDeclaration,
    PresentationPageMode,
    PresentationPageSchema,
    PresentationPageSchemaSet,
    PresentationTemporalPolicy,
    TemporalComparisonPolicy,
    TemporalPresetId,
    validate_page_schemas,
)

pytestmark = [pytest.mark.unit]
ROOT = Path(__file__).resolve().parents[2]
EXPECTED_PAGES = {
    "decision-overview",
    "amazon-channel-performance",
    "campaign-attribution-detail",
    "marketplace-comparison",
    "data-health-detail",
    "scorecard",
    "new-customers",
    "reach",
    "retention",
    "audiences",
}
EXPECTED_DISPLAY_NAMES = {
    "decision-overview": "Marketplace evidence snapshot",
    "amazon-channel-performance": "Amazon channel performance",
    "campaign-attribution-detail": "Campaign attribution detail",
    "marketplace-comparison": "Marketplace comparison",
    "data-health-detail": "Data health",
    "scorecard": "Scorecard",
    "new-customers": "New customers",
    "reach": "Reach and funnel",
    "retention": "Retention",
    "audiences": "Audiences",
}


def test_committed_page_schemas_match_governed_metric_grains_and_bases() -> None:
    schemas = load_page_schemas(ROOT)
    catalog = load_catalog(ROOT)
    attribution_models = load_attribution_models(ROOT)

    assert {page.page_id for page in schemas.pages} == EXPECTED_PAGES
    assert {page.page_id: page.display_name for page in schemas.pages} == EXPECTED_DISPLAY_NAMES
    context = PageValidationContext(catalog, attribution_models)
    # New customers' step-2 slots declare periods, so they validate beside the v2 declaration.
    assert validate_page_schemas(schemas, context, load_v2_page_schemas(ROOT)) == ()


def test_reach_declares_only_source_slots_and_exact_period_sections() -> None:
    schemas = load_page_schemas(ROOT)
    reach = next(page for page in schemas.pages if page.page_id == "reach")
    assert reach.grain.time is PageTime.DECLARED_PERIOD
    assert reach.grain.dimensions == ("marketplace",)
    assert tuple(metric.name for metric in reach.metrics) == (
        "shoppers_reached",
        "search_shoppers_in_window",
        "purchasers_in_window",
        "revenue_incl_halo_in_window",
        "shoppers_in_frequency_bucket",
        "purchasers_in_frequency_bucket",
        "revenue_incl_halo_in_frequency_bucket",
        "dsp_window_spend_in_frequency_bucket",
        "sponsored_window_spend_in_frequency_bucket",
    )
    assert not {
        "overlap_share",
        "search_rate",
        "purchaser_rate",
        "purchaser_rate_by_frequency",
        "revenue_per_exposed_shopper",
        "purchaser_rate_index_vs_sponsored_only",
        "roas_incl_halo_by_frequency",
    } & {metric.name for metric in reach.metrics}
    assert tuple(
        (
            metric.name,
            tuple(period.value for period in metric.declared_periods),
            tuple((variant.variant_id, variant.selectors) for variant in metric.variants),
        )
        for metric in reach.metrics
    ) == tuple(
        (
            name,
            periods,
            tuple((grain, (("time_grain", grain),)) for grain in grains),
        )
        for name, periods, grains in (
            *(
                (name, ("last_complete_week", "last_complete_month"), ("week", "month"))
                for name in (
                    "shoppers_reached",
                    "search_shoppers_in_window",
                    "purchasers_in_window",
                    "revenue_incl_halo_in_window",
                )
            ),
            *(
                (name, ("last_complete_month", "last_complete_quarter"), ("month", "quarter"))
                for name in (
                    "shoppers_in_frequency_bucket",
                    "purchasers_in_frequency_bucket",
                    "revenue_incl_halo_in_frequency_bucket",
                    "dsp_window_spend_in_frequency_bucket",
                    "sponsored_window_spend_in_frequency_bucket",
                )
            ),
        )
    )

    presentation = next(
        page for page in load_v2_page_schemas(ROOT).pages if page.page_id == "reach"
    )
    assert tuple(preset.value for preset in presentation.temporal_policy.candidate_presets) == (
        "last_complete_week",
        "last_complete_month",
        "last_complete_quarter",
    )
    assert presentation.temporal_policy.default_preset.value == "last_complete_month"
    assert presentation.temporal_policy.custom_enabled is False
    assert presentation.temporal_policy.comparison_policy.value == "prior_calendar_period"
    assert tuple(grain.value for grain in presentation.temporal_policy.display_grains) == (
        "declared_period",
    )
    assert tuple(section.section_id for section in presentation.sections) == (
        "workspace-scope",
        "overlap",
        "funnel",
        "reach-insights",
        "frequency",
        "evidence-trace",
        "boundary",
    )
    periods_and_anchors = {
        section.section_id: (
            tuple(period.value for period in section.declared_periods),
            section.period_anchor,
        )
        for section in presentation.sections
        if section.declared_periods
    }
    assert periods_and_anchors == {
        "overlap": (("last_complete_week", "last_complete_month"), True),
        "funnel": (("last_complete_week", "last_complete_month"), True),
        "frequency": (("last_complete_month", "last_complete_quarter"), False),
    }
    assert presentation.navigation is not None
    assert (
        presentation.navigation.hub_id,
        presentation.navigation.tab_label,
        presentation.navigation.tab_description,
        presentation.navigation.order,
    ) == (
        "reach",
        "Reach and funnel",
        "How many shoppers your ads reached, how often, and what they did next",
        1,
    )


def test_marketplace_comparison_declares_exact_v1_artifact_outcomes_and_no_actions() -> None:
    schemas = load_page_schemas(ROOT)
    page = next(item for item in schemas.pages if item.page_id == "marketplace-comparison")

    assert page.artifact is not None
    assert page.artifact.artifact_type == "MarketplaceComparisonArtifact"
    assert page.artifact.version == 1
    assert page.artifact.outcomes == (
        "comparable",
        "one_marketplace",
        "insufficient",
        "no_coverage",
    )
    assert page.actions == ()


def test_data_health_declares_exact_v1_artifact_outcomes_and_sections() -> None:
    schemas = load_page_schemas(ROOT)
    page = next(item for item in schemas.pages if item.page_id == "data-health-detail")

    assert page.artifact is not None
    assert page.artifact.artifact_type == "DataHealthDetail"
    assert page.artifact.version == 1
    assert page.artifact.outcomes == (
        "healthy",
        "qualified",
        "insufficient",
        "no_coverage",
    )
    assert tuple(section.section_id for section in page.sections) == (
        "health-summary",
        "condition-evidence",
        "boundary",
    )


def test_campaign_attribution_declares_exact_v1_artifact_sections_and_action() -> None:
    schemas = load_page_schemas(ROOT)
    page = next(item for item in schemas.pages if item.page_id == "campaign-attribution-detail")

    assert page.artifact is not None
    assert page.artifact.artifact_type == "CampaignAttribution"
    assert page.artifact.version == 1
    assert page.artifact.outcomes == (
        "choices",
        "empty",
        "ambiguous",
        "selected",
        "insufficient",
    )
    assert tuple(section.section_id for section in page.sections) == (
        "selected-summary",
        "purchase-series",
        "evidence-trace",
        "boundary",
    )
    assert tuple((action.action_id, action.target_page) for action in page.actions) == (
        ("inspect-channel-context", "amazon-channel-performance"),
    )


def test_amazon_channel_copy_is_limited_to_roas_ranking_robustness() -> None:
    schemas = load_page_schemas(ROOT)
    page = next(item for item in schemas.pages if item.page_id == "amazon-channel-performance")

    assert page.purpose == (
        "Assess deduplicated Amazon Ads channel ROAS-ranking robustness across first-touch, "
        "last-touch, and linear interpretations."
    )
    assert page.primary_job == (
        "Identify whether channel ROAS rankings remain robust across attribution models before "
        "campaign investigation."
    )
    assert page.artifact is not None
    assert page.artifact.version == 2


@pytest.mark.covers("ADR-055")
def test_amazon_channel_declares_every_model_as_an_independent_exact_query() -> None:
    schemas = load_page_schemas(ROOT)
    page = next(item for item in schemas.pages if item.page_id == "amazon-channel-performance")
    metric = next(item for item in page.metrics if item.name == "roas_deduplicated_incl_halo_14d")

    assert tuple(variant.variant_id for variant in metric.variants) == (
        "first_touch_v1",
        "last_touch_v1",
        "linear_v1",
    )
    assert tuple(variant.selectors for variant in metric.variants) == (
        (("attribution_model_key", "first_touch_v1"),),
        (("attribution_model_key", "last_touch_v1"),),
        (("attribution_model_key", "linear_v1"),),
    )


@pytest.mark.covers("ADR-055")
def test_page_validation_refuses_an_incomplete_model_comparison() -> None:
    schemas = load_page_schemas(ROOT)
    page = next(item for item in schemas.pages if item.page_id == "amazon-channel-performance")
    metric = next(item for item in page.metrics if item.name == "roas_deduplicated_incl_halo_14d")
    incomplete_metric = replace(metric, variants=metric.variants[:-1])
    incomplete_page = replace(
        page,
        metrics=tuple(incomplete_metric if item is metric else item for item in page.metrics),
    )
    candidate = replace(
        schemas,
        pages=tuple(incomplete_page if item is page else item for item in schemas.pages),
    )

    findings = validate_page_schemas(
        candidate,
        PageValidationContext(load_catalog(ROOT), load_attribution_models(ROOT)),
    )

    assert any(
        finding.rule == "selector" and "every registered attribution model" in finding.message
        for finding in findings
    )


def test_period_status_page_refuses_nonadditive_reach_and_frequency() -> None:
    schemas = load_page_schemas(ROOT)
    catalog = load_catalog(ROOT)
    health = next(page for page in schemas.pages if page.page_id == "data-health-detail")
    invalid = replace(
        health,
        grain=replace(health.grain, time=PageTime.PERIOD, dimensions=("marketplace",)),
    )
    candidate = replace(
        schemas,
        pages=tuple(invalid if page is health else page for page in schemas.pages),
    )

    findings = validate_page_schemas(
        candidate,
        PageValidationContext(catalog, load_attribution_models(ROOT)),
        load_v2_page_schemas(ROOT),
    )

    assert {finding.rule for finding in findings} == {"time"}
    assert all(
        "reach_users" in finding.message or "avg_frequency" in finding.message
        for finding in findings
    )


def test_parallel_overview_cannot_be_relabelled_single_basis() -> None:
    schemas = load_page_schemas(ROOT)
    catalog = load_catalog(ROOT)
    overview = next(page for page in schemas.pages if page.page_id == "decision-overview")
    invalid = replace(overview, mode=PageMode.SINGLE_BASIS)
    candidate = replace(
        schemas,
        pages=tuple(invalid if page is overview else page for page in schemas.pages),
    )

    findings = validate_page_schemas(
        candidate,
        PageValidationContext(catalog, load_attribution_models(ROOT)),
        load_v2_page_schemas(ROOT),
    )

    assert len(findings) == 1
    assert findings[0].rule == "basis"


FREQUENCY_SLOTS = ("purchasers_in_frequency_bucket", "roas_incl_halo_by_frequency")
ROAS_SELECTOR_FINDING = (
    "reach-and-funnel",
    "selector",
    "roas_incl_halo_by_frequency/month has an invalid selector value.",
)
"""Why the bake, not a slot, computes return by frequency: it reads account IDs only, and a
slot's variant pins `time_grain` alone (`docs/ui/reach-and-funnel/low-level-design.md`, Page
declarations). Independent of the grain check."""


def _reach_frequency_page(
    *dimensions: str, metrics: tuple[str, ...] = FREQUENCY_SLOTS
) -> tuple[PageSchemaSet, PresentationPageSchemaSet]:
    """Build a synthetic Reach and funnel frequency page, monthly, at the given dimensions."""
    month = TemporalPresetId.LAST_COMPLETE_MONTH
    slots = tuple(
        PageMetric(
            name,
            (MetricRole.TABLE,),
            variants=(PageMetricVariant("month", (("time_grain", "month"),)),),
        )
        for name in metrics
    )
    sections = (
        PageSection(
            "frequency",
            Cardinality.EXACTLY_ONE,
            declared_periods=(month,),
            metrics=tuple(slot.name for slot in slots),
        ),
    )
    v1_page = AnalyticalPageSchema(
        page_id="reach-and-funnel",
        display_name="Reach and funnel",
        route="/reach-and-funnel/",
        purpose="Synthetic FEAT-007 page.",
        primary_job="Prove the per-channel return refusal.",
        mode=PageMode.SINGLE_BASIS,
        boundary_required=False,
        grain=PageGrain(PageTime.DECLARED_PERIOD, dimensions),
        preferred_window_duration_days=30,
        metrics=slots,
        sections=sections,
    )
    v2_page = PresentationPageSchema(
        page_id="reach-and-funnel",
        display_name="Reach and funnel",
        route="/reach-and-funnel/",
        purpose="Synthetic FEAT-007 page.",
        primary_job="Prove the per-channel return refusal.",
        mode=PresentationPageMode.SELECTABLE,
        boundary_required=False,
        time_policy=PresentationTemporalPolicy(
            candidate_presets=(month,),
            default_preset=month,
            custom_enabled=False,
            custom_min_duration_days=1,
            custom_max_duration_days=90,
            comparison_policy=TemporalComparisonPolicy.NONE,
            display_grains=(PageTime.DECLARED_PERIOD,),
        ),
        artifacts=(
            PresentationArtifactDeclaration("main", "ReachAndFunnelArtifact", 1, ("complete",)),
        ),
        visuals=(),
        sections=sections,
        actions=(),
        compatibility_routes=(),
    )
    v1 = load_page_schemas(ROOT)
    v2 = load_v2_page_schemas(ROOT)
    return (
        replace(v1, pages=(*v1.pages, v1_page)),
        replace(v2, temporal_control_version=2, pages=(*v2.pages, v2_page)),
    )


def test_a_page_binding_return_by_frequency_per_channel_is_refused_for_its_own_reason() -> None:
    """FEAT-007 (plan D12): per-channel return uses credited revenue, which pack 7 doesn't build."""
    context = PageValidationContext(load_catalog(ROOT), load_attribution_models(ROOT))
    v1, v2 = _reach_frequency_page("marketplace", "channel")
    findings = validate_page_schemas(v1, context, v2)
    assert [(finding.page_id, finding.rule, finding.message) for finding in findings] == [
        (
            "reach-and-funnel",
            "grain",
            "dimensions channel are not shared by every metric. roas_incl_halo_by_frequency is "
            "refused at channel because per-channel return uses credited revenue.",
        ),
        ROAS_SELECTOR_FINDING,
    ]
    # Positive control: the same page at the platform grain raises no grain finding.
    v1, v2 = _reach_frequency_page("marketplace")
    findings = validate_page_schemas(v1, context, v2)
    assert [(finding.page_id, finding.rule, finding.message) for finding in findings] == [
        ROAS_SELECTOR_FINDING
    ]
    # Positive control: a frequency count per channel, which pack 7 answers, passes every check.
    v1, v2 = _reach_frequency_page(
        "marketplace", "channel", metrics=("purchasers_in_frequency_bucket",)
    )
    assert validate_page_schemas(v1, context, v2) == ()
    # Control: a metric with no reason of its own keeps the shared-grain finding unchanged.
    overview = next(page for page in v1.pages if page.page_id == "marketplace-comparison")
    widened = replace(overview, grain=replace(overview.grain, dimensions=("campaign",)))
    messages = [
        finding.message
        for finding in validate_page_schemas(replace(v1, pages=(widened,)), context)
        if finding.rule == "grain"
    ]
    assert messages == ["dimensions campaign are not shared by every metric."]


def test_loader_rejects_unknown_fields_and_duplicate_yaml_keys(tmp_path: Path) -> None:
    path = tmp_path / PAGE_SCHEMAS_PATH
    path.parent.mkdir(parents=True)
    path.write_text("version: 1\nversion: 1\ncontext_fields: []\npages: []\n")
    with pytest.raises(PageSchemaError, match="duplicate YAML key"):
        load_page_schemas(tmp_path)

    path.write_text("version: 1\ncontext_fields: []\npages: []\nunknown: true\n")
    with pytest.raises(PageSchemaError, match="unknown unknown"):
        load_page_schemas(tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        (
            "    display_name: Marketplace evidence snapshot\n",
            "    display_name: '   '\n",
            "nonblank display name",
        ),
    ],
)
def test_loader_rejects_invalid_page_context_metadata(
    tmp_path: Path,
    mutation: tuple[str, str, str],
) -> None:
    original, replacement, message = mutation
    path = tmp_path / PAGE_SCHEMAS_PATH
    path.parent.mkdir(parents=True)
    manifest = (ROOT / PAGE_SCHEMAS_PATH).read_text().replace(original, replacement, 1)
    path.write_text(manifest)

    with pytest.raises(PageSchemaError, match=message):
        load_page_schemas(tmp_path)


def test_loader_rejects_section_metric_role_as_an_unknown_field(tmp_path: Path) -> None:
    path = tmp_path / PAGE_SCHEMAS_PATH
    path.parent.mkdir(parents=True)
    manifest = (
        (ROOT / PAGE_SCHEMAS_PATH)
        .read_text()
        .replace(
            "        cardinality: exactly_one\n",
            "        cardinality: exactly_one\n        metric_role: headline\n",
            1,
        )
    )
    path.write_text(manifest)

    with pytest.raises(PageSchemaError, match="unknown metric_role"):
        load_page_schemas(tmp_path)


def _generated_section_roles() -> dict[str, tuple[tuple[str, str | None], ...]]:
    document = generated_document(ROOT)
    pages = cast("list[dict[str, object]]", document["pages"])
    result: dict[str, tuple[tuple[str, str | None], ...]] = {}
    for page in pages:
        sections = cast("list[dict[str, object]]", page["sections"])
        result[cast("str", page["page_id"])] = tuple(
            (
                cast("str", section["section_id"]),
                cast("str | None", section["metric_role"]),
            )
            for section in sections
        )
    return result


def test_generated_bindings_preserve_exact_ordered_section_role_mapping() -> None:
    assert _generated_section_roles() == {
        "decision-overview": (
            ("decision", "headline"),
            ("reviewed-evidence", "headline"),
            ("boundary", None),
        ),
        "amazon-channel-performance": (
            ("summary", "summary"),
            ("channel-table", "table"),
            ("boundary", None),
        ),
        "campaign-attribution-detail": (
            ("selected-summary", "summary"),
            ("purchase-series", "trend"),
            ("evidence-trace", None),
            ("boundary", None),
        ),
        "marketplace-comparison": (
            ("scope", None),
            ("comparison-table", "table"),
            ("boundary", None),
        ),
        "data-health-detail": (
            ("health-summary", "status"),
            ("condition-evidence", None),
            ("boundary", None),
        ),
        "scorecard": (
            ("network-card", "summary"),
            ("channel-table", "table"),
            ("boundary", None),
        ),
        "new-customers": (
            ("new-to-brand", "table"),
            ("new-to-brand-step-2", "summary"),
            ("gateway-products", "table"),
            ("boundary", None),
        ),
        "reach": (
            ("overlap", "summary"),
            ("funnel", "table"),
            ("frequency", "table"),
            ("boundary", None),
        ),
        "retention": (
            ("path", None),
            ("triangle", "table"),
            ("retention-curve", "trend"),
            ("boundary", None),
        ),
        "audiences": (
            ("profile-path", None),
            ("segment-ranking", "table"),
            ("campaign-audiences", "table"),
            ("boundary", None),
        ),
    }


def test_generation_refuses_a_metric_role_without_a_rendered_section() -> None:
    schemas = load_page_schemas(ROOT)
    overview = next(page for page in schemas.pages if page.page_id == "decision-overview")
    trend_metric = replace(overview.metrics[0], roles=(MetricRole.TREND,))
    invalid = replace(overview, metrics=(trend_metric, *overview.metrics[1:]))

    with pytest.raises(RuntimeError, match=r"metric roles have no rendered section.*trend"):
        section_metric_roles(invalid)


def test_scope_evidence_trace_and_boundary_sections_are_context_only() -> None:
    context_only = {
        (page_id, section_id): role
        for page_id, sections in _generated_section_roles().items()
        for section_id, role in sections
        if section_id in {"scope", "evidence-trace", "boundary"}
    }

    assert context_only == {
        ("decision-overview", "boundary"): None,
        ("amazon-channel-performance", "boundary"): None,
        ("campaign-attribution-detail", "boundary"): None,
        ("campaign-attribution-detail", "evidence-trace"): None,
        ("marketplace-comparison", "scope"): None,
        ("marketplace-comparison", "boundary"): None,
        ("data-health-detail", "boundary"): None,
        ("scorecard", "boundary"): None,
        ("new-customers", "boundary"): None,
        ("reach", "boundary"): None,
        ("retention", "boundary"): None,
        ("audiences", "boundary"): None,
    }


@pytest.mark.parametrize(
    ("section", "message"),
    [
        (
            {
                "cardinality": "exactly_one",
                "metric_role": None,
                "section_id": "fictional-section",
                "unexpected": True,
            },
            "section binding must contain exactly",
        ),
        (
            {
                "cardinality": "exactly_one",
                "section_id": "fictional-section",
            },
            "section binding must contain exactly",
        ),
        (
            {
                "cardinality": ["exactly_one"],
                "metric_role": None,
                "section_id": "fictional-section",
            },
            "section cardinality must be nonblank text",
        ),
    ],
)
def test_frontend_binding_loader_rejects_extra_or_malformed_section_fields(
    tmp_path: Path,
    section: dict[str, object],
    message: str,
) -> None:
    path = tmp_path / PAGE_BINDINGS_PATH
    path.parent.mkdir(parents=True)
    document = {
        "version": 1,
        "context_fields": ["fictional-context"],
        "pages": [
            {
                "actions": [],
                "artifact": None,
                "boundary_required": True,
                "display_name": "Fictional page",
                "preferred_window_duration_days": 7,
                "metrics": [],
                "mode": "single_basis",
                "page_id": "fictional-page",
                "primary_job": "Exercise the structural contract.",
                "purpose": "Exercise the structural contract.",
                "route": "fictional-route",
                "sections": [section],
            }
        ],
    }
    path.write_text(json.dumps(document))

    with pytest.raises(PageSchemaError, match=message):
        load_page_bindings(tmp_path)


@pytest.mark.parametrize(
    "case",
    [
        (
            "decision-overview",
            {"display_name": "   "},
            "page display name must be nonblank text",
        ),
    ],
)
def test_frontend_binding_loader_rejects_invalid_page_context_metadata(
    tmp_path: Path,
    case: tuple[str, dict[str, object], str],
) -> None:
    page_id, updates, message = case
    path = tmp_path / PAGE_BINDINGS_PATH
    path.parent.mkdir(parents=True)
    document = generated_document(ROOT)
    pages = cast("list[dict[str, object]]", document["pages"])
    page = next(item for item in pages if item["page_id"] == page_id)
    page.update(updates)
    path.write_text(json.dumps(document))

    with pytest.raises(PageSchemaError, match=message):
        load_page_bindings(tmp_path)


def test_frontend_binding_loader_rejects_expanded_json_contract(tmp_path: Path) -> None:
    path = tmp_path / PAGE_BINDINGS_PATH
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 1, "context_fields": [], "pages": [], "screens": []}\n')

    with pytest.raises(PageSchemaError, match="must contain exactly"):
        load_page_bindings(tmp_path)
