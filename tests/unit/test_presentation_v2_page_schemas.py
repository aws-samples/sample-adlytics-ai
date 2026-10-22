# SPDX-License-Identifier: MIT-0
"""V2 governed Presentation declaration and generated binding compatibility."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from adapters.jobs.presentation_validation.bindings import (
    PAGE_BINDINGS_PATH,
    load_v2_page_bindings,
)
from adapters.jobs.presentation_validation.generate import (
    generated_document,
    rendered,
    section_roles_v2,
)
from adapters.jobs.presentation_validation.loader import (
    PRESENTATION_PAGE_SCHEMAS_PATH,
    load_page_schemas,
    load_v2_page_schemas,
)
from adapters.jobs.preset_artifact_publisher.deployment_generation import (
    PUBLISHER_INPUT_ROOTS,
    publisher_runtime_generation,
)
from domain.presentation.page_schemas import PageSchemaError, PresentationSectionRole

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_negotiated_declarations_load_frozen_v1_and_canonical_v2_independently() -> None:
    v1 = load_page_schemas(ROOT)
    v2 = load_v2_page_schemas(ROOT)

    assert v1.version == 1
    assert v2.version == 2
    assert v2.presentation_result_version == 2
    assert v2.temporal_control_version == 2  # ADR-100 point 8, browser half in UI slice 9a
    assert tuple(page.page_id for page in v2.pages) == (
        "campaigns",
        "channel-performance",
        "workspace",
        "data-health",
        "media-efficiency",
        "retail-health",
        "scorecard",
        "new-customers",
        "reach",
        "retention",
        "audiences",
    )
    assert all(page.route.startswith("/") and page.route.endswith("/") for page in v2.pages)
    owners = {
        page.page_id: page.compatibility_routes for page in v2.pages if page.compatibility_routes
    }
    assert owners == {"scorecard": ("/",)}


def test_v2_generator_projects_temporal_artifacts_visuals_and_section_roles(tmp_path: Path) -> None:
    document = generated_document(ROOT, schema_version=2)
    path = tmp_path / "v2-bindings.json"
    path.write_text(json.dumps(document))

    bindings = load_v2_page_bindings(tmp_path, path=Path(path.name))
    campaigns = next(page for page in bindings.pages if page.page_id == "campaigns")
    media = next(page for page in bindings.pages if page.page_id == "media-efficiency")

    assert tuple(artifact.artifact_id for artifact in campaigns.artifacts) == (
        "campaign_catalog",
        "campaign_detail",
    )
    assert tuple(visual.visual_id for visual in media.visuals) == (
        "media_delivery_daily",
        "media_dsp_viewability_daily",
    )
    assert media.sections[-2] == (
        "evidence-detail",
        media.sections[-2][1],
        PresentationSectionRole.EVIDENCE,
    )
    assert media.sections[-1][2] is PresentationSectionRole.BOUNDARY


def test_v2_binding_validator_rejects_unknown_temporal_policy_field(tmp_path: Path) -> None:
    document = generated_document(ROOT, schema_version=2)
    pages = cast("list[dict[str, object]]", document["pages"])
    temporal_policy = cast("dict[str, object]", pages[0]["temporal_policy"])
    temporal_policy["raw_visible_window"] = True
    path = tmp_path / "v2-bindings.json"
    path.write_text(json.dumps(document))

    with pytest.raises(PageSchemaError, match="V2 temporal policy binding must contain exactly"):
        load_v2_page_bindings(tmp_path, path=Path(path.name))


def test_v2_generator_projects_each_metric_unit_and_ratio_scale(tmp_path: Path) -> None:
    """Plan D4: the browser formats a rate as % and a multiplier with a times sign."""
    document = generated_document(ROOT, schema_version=2)
    display = cast("dict[str, dict[str, object]]", document["metric_display"])
    assert display["ctr"] == {"ratio_scale": "rate", "units": "ratio"}
    assert display["roas_attributed_14d"] == {"ratio_scale": "multiplier", "units": "ratio"}
    assert display["impressions"] == {"ratio_scale": None, "units": "count"}
    assert display["ad_spend"] == {"ratio_scale": None, "units": "currency"}
    path = tmp_path / "v2-bindings.json"
    broken_entries: tuple[dict[str, object], ...] = (
        {"ratio_scale": None, "units": "ratio"},
        {"ratio_scale": "rate", "units": "count"},
    )
    for broken in broken_entries:
        display["ctr"] = broken
        path.write_text(json.dumps(document))
        with pytest.raises(PageSchemaError, match="must scale exactly a ratio"):
            load_v2_page_bindings(tmp_path, path=Path(path.name))


def test_v2_section_role_projection_is_exhaustive_for_every_declared_section() -> None:
    schemas = load_v2_page_schemas(ROOT)

    projected = {page.page_id: section_roles_v2(page) for page in schemas.pages}

    assert tuple(role.value for role in projected["campaigns"]) == (
        "scope",
        "selection",
        "summary",
        "trend",
        "evidence",
        "evidence",
        "boundary",
    )
    assert tuple(role.value for role in projected["data-health"]) == (
        "scope",
        "summary",
        "evidence",
        "evidence",
        "boundary",
    )


def test_lambda_staging_requires_and_copies_both_declaration_generations() -> None:
    script = (ROOT / "scripts/build-lambdas.sh").read_text()

    assert "for generation in v1 v2; do" in script
    assert 'test -f "$ROOT/presentation/pages/$generation/pages.yaml"' in script
    copy_command = (
        'cp -R "$ROOT/presentation/pages/$generation" "$OUT/src/presentation/pages/$generation"'
    )
    assert copy_command in script


def test_v2_declaration_preserves_exact_temporal_artifact_visual_and_action_contracts() -> None:
    schemas = load_v2_page_schemas(ROOT)

    assert tuple(
        (
            page.page_id,
            page.route,
            tuple(preset.value for preset in page.temporal_policy.candidate_presets),
            page.temporal_policy.custom_enabled,
            page.temporal_policy.custom_min_duration_days,
            page.temporal_policy.custom_max_duration_days,
            page.temporal_policy.comparison_policy.value,
            page.temporal_policy.comparison_intent,
        )
        for page in schemas.pages
        if page.vintage_policy is None
    ) == (
        (
            "campaigns",
            "/campaigns/",
            ("1d", "7d", "14d", "30d"),
            False,
            2,
            90,
            "required_equal_prior",
            None,
        ),
        (
            "channel-performance",
            "/channel-performance/",
            ("1d", "7d", "14d", "30d"),
            True,
            1,
            90,
            "required_equal_prior",
            None,
        ),
        (
            "workspace",
            "/workspace/",
            ("1d", "7d", "14d", "30d"),
            True,
            1,
            90,
            "required_equal_prior",
            None,
        ),
        ("data-health", "/data-health/", ("1d", "7d", "14d", "30d"), True, 1, 90, "none", None),
        (
            "media-efficiency",
            "/media-efficiency/",
            ("1d", "7d", "14d", "30d"),
            True,
            1,
            90,
            "optional_equal_prior",
            "compare_previous",
        ),
        (
            "retail-health",
            "/retail-health/",
            ("1d", "7d", "14d"),
            False,
            1,
            90,
            "required_equal_prior",
            None,
        ),
        (
            "scorecard",
            "/scorecard/",
            ("7d", "30d", "last_complete_week", "last_complete_month"),
            False,
            1,
            90,
            "optional_equal_prior",
            None,
        ),
        (
            "new-customers",
            "/new-customers/",
            ("7d", "30d", "last_complete_week", "last_complete_month"),
            False,
            1,
            90,
            "optional_equal_prior",
            None,
        ),
        (
            "reach",
            "/reach/",
            ("last_complete_week", "last_complete_month", "last_complete_quarter"),
            False,
            1,
            90,
            "prior_calendar_period",
            None,
        ),
        (
            "audiences",
            "/audiences/",
            ("last_complete_month",),
            False,
            1,
            90,
            "prior_calendar_period",
            None,
        ),
    )
    retention = next(page for page in schemas.pages if page.page_id == "retention")
    assert retention.vintage_policy is not None
    assert (
        retention.vintage_policy.selector,
        retention.vintage_policy.default,
        retention.vintage_policy.comparison_policy.value,
    ) == ("as_of_month", "latest_with_current_publication", "none")
    assert {
        page.page_id: tuple(
            (artifact.artifact_id, artifact.artifact_type, artifact.version, artifact.outcomes)
            for artifact in page.artifacts
        )
        for page in schemas.pages
    } == {
        "campaigns": (
            (
                "campaign_catalog",
                "CampaignPortfolioArtifact",
                1,
                ("complete", "empty", "insufficient"),
            ),
            ("campaign_detail", "CampaignAttributionDetail", 2, ("selected", "insufficient")),
        ),
        "channel-performance": (
            (
                "channel_robustness",
                "ChannelAttributionRobustness",
                2,
                ("robust_leader", "model_sensitive", "insufficient"),
            ),
            (
                "selected_channel_daily",
                "ChannelAttributionRobustnessDaily",
                1,
                ("complete", "partial", "insufficient"),
            ),
        ),
        "workspace": (
            (
                "marketplace_comparison",
                "MarketplaceComparisonArtifact",
                1,
                ("comparable", "one_marketplace", "insufficient", "no_coverage"),
            ),
        ),
        "data-health": (
            (
                "evidence_health",
                "DataHealthDetail",
                1,
                ("healthy", "qualified", "insufficient", "no_coverage"),
            ),
        ),
        "media-efficiency": (
            ("media_scope_list", "MediaEfficiencyScopeList", 1, ("choices", "empty")),
            (
                "media_efficiency",
                "MediaEfficiencyArtifact",
                1,
                ("complete", "qualified", "insufficient"),
            ),
        ),
        "retail-health": (
            ("retail_health", "RetailHealthArtifact", 2, ("complete", "empty", "insufficient")),
            ("retail_product_detail", "RetailProductDetail", 1, ("selected", "insufficient")),
        ),
        "scorecard": (
            (
                "network_scorecard",
                "NetworkScorecardArtifact",
                1,
                ("complete", "empty"),
            ),
        ),
        "new-customers": (
            (
                "new_customers",
                "NewCustomersArtifact",
                2,
                ("complete", "empty", "insufficient"),
            ),
        ),
        "reach": (
            (
                "reach",
                "ReachAndFunnelArtifact",
                1,
                ("complete", "empty", "insufficient"),
            ),
        ),
        "retention": (
            (
                "retention",
                "RetentionCohortArtifact",
                1,
                ("complete", "empty", "insufficient"),
            ),
        ),
        "audiences": (
            (
                "audiences",
                "AudienceProfileArtifact",
                1,
                ("complete", "empty", "insufficient"),
            ),
        ),
    }
    assert {
        page.page_id: (
            tuple((visual.visual_id, visual.metric_cohort) for visual in page.visuals),
            tuple((action.action_id, action.target_page) for action in page.actions),
        )
        for page in schemas.pages
    } == {
        "campaigns": (
            (
                (
                    "campaign_portfolio_trend",
                    ("ad_spend", "portfolio_roas_attributed_14d", "purchases"),
                ),
                (
                    "campaign_delivery_daily",
                    ("ad_spend", "impressions", "clicks", "ctr", "cpc", "cpm"),
                ),
            ),
            # ADR-127: the channel step is a served open_page step to the Scorecard.
            (),
        ),
        "channel-performance": (
            (
                ("channel_roas_model_matrix", ("ad_spend", "roas_deduplicated_incl_halo_14d")),
                ("channel_daily_evidence", ("ad_spend", "roas_deduplicated_incl_halo_14d")),
            ),
            (),
        ),
        "workspace": ((), ()),
        "data-health": ((), ()),
        "media-efficiency": (
            (
                (
                    "media_delivery_daily",
                    ("ad_spend", "impressions", "clicks", "ctr", "cpc", "cpm"),
                ),
                (
                    "media_dsp_viewability_daily",
                    (
                        "measurable_impressions",
                        "viewable_impressions",
                        "viewable_impressions_estimated",
                        "viewable_rate",
                    ),
                ),
            ),
            (),
        ),
        "retail-health": (
            (
                (
                    "retail_health_daily",
                    (
                        "retail_revenue_shipped",
                        "units_shipped",
                        "customer_returns",
                        "glance_views",
                    ),
                ),
                ("product_sales_daily", ("retail_revenue_shipped",)),
                ("product_traffic_daily", ("glance_views",)),
            ),
            (),
        ),
        "scorecard": ((), ()),
        "new-customers": ((), ()),
        "reach": ((), ()),
        "retention": ((), ()),
        "audiences": ((), ()),
    }


def test_v2_loader_rejects_unknown_declaration_field(tmp_path: Path) -> None:
    path = tmp_path / PRESENTATION_PAGE_SCHEMAS_PATH
    path.parent.mkdir(parents=True)
    path.write_text(
        (ROOT / PRESENTATION_PAGE_SCHEMAS_PATH)
        .read_text()
        .replace("version: 2\n", "version: 2\nunexpected: true\n", 1)
    )

    with pytest.raises(PageSchemaError, match="unknown unexpected"):
        load_v2_page_schemas(tmp_path)


def test_committed_v2_binding_matches_its_generated_projection() -> None:
    bindings = load_v2_page_bindings(ROOT)
    declarations = load_v2_page_schemas(ROOT)

    assert (ROOT / PAGE_BINDINGS_PATH).read_text() == rendered(ROOT, schema_version=2)
    assert tuple((page.page_id, page.route) for page in bindings.pages) == tuple(
        (page.page_id, page.route) for page in declarations.pages
    )
    assert all(page.boundary_required for page in bindings.pages)


def test_publisher_generation_binds_packaged_rendering_code_and_contract_bytes() -> None:
    script = (ROOT / "scripts/build-lambdas.sh").read_text()

    assert "publisher-runtime-generation" in script
    assert "preset_artifact_publisher.deployment_generation" in script
    assert '>> "$publisher_input"' in script


def test_publisher_runtime_generation_changes_for_every_consumed_input_root(
    tmp_path: Path,
) -> None:
    publisher_input = tmp_path / "presentation/pages/v2/pages.yaml"
    for relative in PUBLISHER_INPUT_ROOTS:
        path = tmp_path / relative / ("pages.yaml" if relative == "presentation/pages" else "input")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"initial:{relative}")
    publisher_input.parent.mkdir(parents=True, exist_ok=True)
    publisher_input.write_text("v2 declaration")
    baseline = publisher_runtime_generation(tmp_path, publisher_input)

    for relative in PUBLISHER_INPUT_ROOTS:
        candidate = (
            tmp_path / relative / ("pages.yaml" if relative == "presentation/pages" else "input")
        )
        original = candidate.read_text()
        candidate.write_text(f"{original}:changed")
        assert publisher_runtime_generation(tmp_path, publisher_input) != baseline
        candidate.write_text(original)

    publisher_input.write_text("outer hash owns this change")
    assert publisher_runtime_generation(tmp_path, publisher_input) == baseline
