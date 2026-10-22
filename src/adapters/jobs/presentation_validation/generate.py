# SPDX-License-Identifier: MIT-0
"""Generate frontend page-binding projections from governed V1 or V2 declarations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from adapters.jobs.presentation_validation.bindings import PAGE_BINDINGS_PATH
from adapters.jobs.presentation_validation.loader import load_page_schemas, load_v2_page_schemas
from adapters.outbound.attribution_models import load_attribution_models
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from domain.presentation.page_schemas import (
    AnalyticalPageSchema,
    MetricRole,
    PageSchemaError,
    PresentationPageSchema,
    PresentationSectionRole,
    PresentationTemporalPolicy,
)

_V2_SCHEMA_VERSION = 2

_SECTION_METRIC_ROLES: dict[tuple[str, str], MetricRole | None] = {
    ("decision-overview", "decision"): MetricRole.HEADLINE,
    ("decision-overview", "reviewed-evidence"): MetricRole.HEADLINE,
    ("decision-overview", "boundary"): None,
    ("amazon-channel-performance", "summary"): MetricRole.SUMMARY,
    ("amazon-channel-performance", "channel-table"): MetricRole.TABLE,
    ("amazon-channel-performance", "boundary"): None,
    ("campaign-attribution-detail", "selected-summary"): MetricRole.SUMMARY,
    ("campaign-attribution-detail", "purchase-series"): MetricRole.TREND,
    ("campaign-attribution-detail", "evidence-trace"): None,
    ("campaign-attribution-detail", "boundary"): None,
    ("marketplace-comparison", "scope"): None,
    ("marketplace-comparison", "comparison-table"): MetricRole.TABLE,
    ("marketplace-comparison", "boundary"): None,
    ("data-health-detail", "health-summary"): MetricRole.STATUS,
    ("data-health-detail", "condition-evidence"): None,
    ("data-health-detail", "boundary"): None,
    ("scorecard", "network-card"): MetricRole.SUMMARY,
    ("scorecard", "channel-table"): MetricRole.TABLE,
    ("scorecard", "boundary"): None,
    ("new-customers", "new-to-brand"): MetricRole.TABLE,
    ("new-customers", "new-to-brand-step-2"): MetricRole.SUMMARY,
    ("new-customers", "gateway-products"): MetricRole.TABLE,
    ("new-customers", "boundary"): None,
    ("reach", "overlap"): MetricRole.SUMMARY,
    ("reach", "funnel"): MetricRole.TABLE,
    ("reach", "frequency"): MetricRole.TABLE,
    ("reach", "boundary"): None,
    ("retention", "path"): None,
    ("retention", "triangle"): MetricRole.TABLE,
    ("retention", "retention-curve"): MetricRole.TREND,
    ("retention", "boundary"): None,
    ("audiences", "profile-path"): None,
    ("audiences", "segment-ranking"): MetricRole.TABLE,
    ("audiences", "campaign-audiences"): MetricRole.TABLE,
    ("audiences", "boundary"): None,
}

_V2_SECTION_ROLES: dict[tuple[str, str], PresentationSectionRole] = {
    ("campaigns", "workspace-scope"): PresentationSectionRole.SCOPE,
    ("campaigns", "campaign-catalog"): PresentationSectionRole.SELECTION,
    ("campaigns", "selected-summary"): PresentationSectionRole.SUMMARY,
    ("campaigns", "purchase-series"): PresentationSectionRole.TREND,
    ("campaigns", "delivery-evidence"): PresentationSectionRole.EVIDENCE,
    ("campaigns", "evidence-trace"): PresentationSectionRole.EVIDENCE,
    ("campaigns", "boundary"): PresentationSectionRole.BOUNDARY,
    ("channel-performance", "workspace-scope"): PresentationSectionRole.SCOPE,
    ("channel-performance", "summary"): PresentationSectionRole.SUMMARY,
    ("channel-performance", "channel-table"): PresentationSectionRole.TABLE,
    ("channel-performance", "channel-daily-evidence"): PresentationSectionRole.EVIDENCE,
    ("channel-performance", "boundary"): PresentationSectionRole.BOUNDARY,
    ("workspace", "workspace-scope"): PresentationSectionRole.SCOPE,
    ("workspace", "comparison-table"): PresentationSectionRole.TABLE,
    ("workspace", "evidence-detail"): PresentationSectionRole.EVIDENCE,
    ("workspace", "boundary"): PresentationSectionRole.BOUNDARY,
    ("data-health", "workspace-scope"): PresentationSectionRole.SCOPE,
    ("data-health", "health-summary"): PresentationSectionRole.SUMMARY,
    ("data-health", "condition-evidence"): PresentationSectionRole.EVIDENCE,
    ("data-health", "source-disclosure"): PresentationSectionRole.EVIDENCE,
    ("data-health", "boundary"): PresentationSectionRole.BOUNDARY,
    ("media-efficiency", "workspace-scope"): PresentationSectionRole.SCOPE,
    ("media-efficiency", "media-scope-list"): PresentationSectionRole.SELECTION,
    ("media-efficiency", "delivery-summary"): PresentationSectionRole.SUMMARY,
    ("media-efficiency", "delivery-evidence"): PresentationSectionRole.EVIDENCE,
    ("media-efficiency", "dsp-viewability"): PresentationSectionRole.EVIDENCE,
    ("media-efficiency", "audience-availability"): PresentationSectionRole.AVAILABILITY,
    ("media-efficiency", "evidence-detail"): PresentationSectionRole.EVIDENCE,
    ("media-efficiency", "boundary"): PresentationSectionRole.BOUNDARY,
    ("retail-health", "workspace-scope"): PresentationSectionRole.SCOPE,
    ("retail-health", "retail-totals"): PresentationSectionRole.SUMMARY,
    ("retail-health", "retail-insights"): PresentationSectionRole.EVIDENCE,
    ("retail-health", "retail-trend"): PresentationSectionRole.TREND,
    ("retail-health", "product-table"): PresentationSectionRole.TABLE,
    ("retail-health", "reconciliation"): PresentationSectionRole.EVIDENCE,
    ("retail-health", "product-summary"): PresentationSectionRole.SUMMARY,
    ("retail-health", "product-insights"): PresentationSectionRole.EVIDENCE,
    ("retail-health", "product-daily"): PresentationSectionRole.TREND,
    ("retail-health", "product-ad-attributed"): PresentationSectionRole.TABLE,
    ("retail-health", "evidence-trace"): PresentationSectionRole.EVIDENCE,
    ("retail-health", "boundary"): PresentationSectionRole.BOUNDARY,
    ("scorecard", "workspace-scope"): PresentationSectionRole.SCOPE,
    ("scorecard", "network-card"): PresentationSectionRole.SUMMARY,
    ("scorecard", "model-ranking"): PresentationSectionRole.TABLE,
    ("scorecard", "scorecard-insights"): PresentationSectionRole.EVIDENCE,
    ("scorecard", "also-changed"): PresentationSectionRole.EVIDENCE,
    ("scorecard", "channel-table"): PresentationSectionRole.TABLE,
    ("scorecard", "pointers"): PresentationSectionRole.AVAILABILITY,
    ("scorecard", "ad-spend-and-retail-sales"): PresentationSectionRole.SUMMARY,
    ("scorecard", "evidence-trace"): PresentationSectionRole.EVIDENCE,
    ("scorecard", "boundary"): PresentationSectionRole.BOUNDARY,
    ("new-customers", "workspace-scope"): PresentationSectionRole.SCOPE,
    ("new-customers", "new-to-brand"): PresentationSectionRole.TABLE,
    ("new-customers", "new-customers-insights"): PresentationSectionRole.EVIDENCE,
    ("new-customers", "new-to-brand-step-2"): PresentationSectionRole.SUMMARY,
    ("new-customers", "gateway-products"): PresentationSectionRole.TABLE,
    ("new-customers", "pointers"): PresentationSectionRole.AVAILABILITY,
    ("new-customers", "evidence-trace"): PresentationSectionRole.EVIDENCE,
    ("new-customers", "boundary"): PresentationSectionRole.BOUNDARY,
    ("reach", "workspace-scope"): PresentationSectionRole.SCOPE,
    ("reach", "overlap"): PresentationSectionRole.SUMMARY,
    ("reach", "funnel"): PresentationSectionRole.TABLE,
    ("reach", "reach-insights"): PresentationSectionRole.EVIDENCE,
    ("reach", "frequency"): PresentationSectionRole.TREND,
    ("reach", "evidence-trace"): PresentationSectionRole.EVIDENCE,
    ("reach", "boundary"): PresentationSectionRole.BOUNDARY,
    ("retention", "workspace-scope"): PresentationSectionRole.SCOPE,
    ("retention", "path"): PresentationSectionRole.SELECTION,
    ("retention", "triangle"): PresentationSectionRole.TABLE,
    ("retention", "retention-curve"): PresentationSectionRole.TREND,
    ("retention", "retention-insights"): PresentationSectionRole.EVIDENCE,
    ("retention", "evidence-trace"): PresentationSectionRole.EVIDENCE,
    ("retention", "boundary"): PresentationSectionRole.BOUNDARY,
    ("audiences", "workspace-scope"): PresentationSectionRole.SCOPE,
    ("audiences", "profile-path"): PresentationSectionRole.SELECTION,
    ("audiences", "segment-ranking"): PresentationSectionRole.TABLE,
    ("audiences", "audience-insights"): PresentationSectionRole.EVIDENCE,
    ("audiences", "campaign-audiences"): PresentationSectionRole.TABLE,
    ("audiences", "evidence-trace"): PresentationSectionRole.EVIDENCE,
    ("audiences", "boundary"): PresentationSectionRole.BOUNDARY,
}


def section_metric_roles(page: AnalyticalPageSchema) -> tuple[MetricRole | None, ...]:
    """Derive every V1 rendered section role from an exhaustive presentation mapping."""
    roles: list[MetricRole | None] = []
    for section in page.sections:
        key = (page.page_id, section.section_id)
        if key not in _SECTION_METRIC_ROLES:
            raise RuntimeError(
                f"rendered section role is not mapped: {page.page_id}/{section.section_id}"
            )
        roles.append(_SECTION_METRIC_ROLES[key])

    declared = {role for metric in page.metrics for role in metric.roles}
    rendered = {role for role in roles if role is not None}
    missing = sorted(role.value for role in declared - rendered)
    if missing:
        raise RuntimeError(
            f"metric roles have no rendered section on {page.page_id}: {', '.join(missing)}"
        )
    unexpected = sorted(role.value for role in rendered - declared)
    if unexpected:
        raise RuntimeError(
            f"rendered sections have undeclared metric roles on {page.page_id}: "
            f"{', '.join(unexpected)}"
        )
    return tuple(roles)


def section_roles_v2(page: PresentationPageSchema) -> tuple[PresentationSectionRole, ...]:
    """Derive every V2 rendered section role from an exhaustive projection mapping."""
    roles: list[PresentationSectionRole] = []
    for section in page.sections:
        key = (page.page_id, section.section_id)
        if key not in _V2_SECTION_ROLES:
            raise RuntimeError(
                f"V2 rendered section role is not mapped: {page.page_id}/{section.section_id}"
            )
        roles.append(_V2_SECTION_ROLES[key])
    return tuple(roles)


def _v1_generated_document(root: Path) -> dict[str, object]:
    schemas = load_page_schemas(root)
    catalog = load_catalog(root)
    models = load_attribution_models(root)
    routes = {page.page_id: page.route for page in schemas.pages}
    pages: list[dict[str, object]] = []
    for page in schemas.pages:
        section_roles = section_metric_roles(page)
        metrics: list[dict[str, object]] = []
        for declared in page.metrics:
            metric = catalog.metric(declared.name)
            if metric is None:
                raise RuntimeError(f"governed metric is missing: {declared.name}")
            variants = declared.variants or (None,)
            for role in declared.roles:
                for variant in variants:
                    label = metric.label
                    selectors: dict[str, str] = {}
                    variant_id = None
                    if variant is not None:
                        variant_id = variant.variant_id
                        selectors = dict(variant.selectors)
                        model_key = selectors.get("attribution_model_key")
                        model = None if model_key is None else models.model(model_key)
                        if model is not None:
                            label = f"{label} — {model.label}"
                    metrics.append(
                        {
                            "label": label,
                            "name": declared.name,
                            "role": role.value,
                            "selectors": selectors,
                            "variant_id": variant_id,
                        }
                    )
        pages.append(
            {
                "actions": [
                    {"action_id": action.action_id, "target_route": routes[action.target_page]}
                    for action in page.actions
                ],
                "artifact": (
                    None
                    if page.artifact is None
                    else {
                        "outcomes": list(page.artifact.outcomes),
                        "type": page.artifact.artifact_type,
                        "version": page.artifact.version,
                    }
                ),
                "boundary_required": page.boundary_required,
                "display_name": page.display_name,
                "metrics": metrics,
                "mode": page.mode.value,
                "page_id": page.page_id,
                "preferred_window_duration_days": page.preferred_window_duration_days,
                "primary_job": page.primary_job,
                "purpose": page.purpose,
                "route": page.route,
                "sections": [
                    {
                        "cardinality": section.cardinality.value,
                        "metric_role": None if role is None else role.value,
                        "section_id": section.section_id,
                    }
                    for section, role in zip(page.sections, section_roles, strict=True)
                ],
            }
        )
    return {
        "context_fields": list(schemas.context_fields),
        "pages": pages,
        "version": schemas.version,
    }


def _v2_time_policy(page: PresentationPageSchema) -> dict[str, object]:
    """Project exactly one declared preset or vintage policy."""
    policy = page.time_policy
    if isinstance(policy, PresentationTemporalPolicy):
        return {
            "temporal_policy": {
                "candidate_presets": [preset.value for preset in policy.candidate_presets],
                "comparison_intent": policy.comparison_intent,
                "comparison_policy": policy.comparison_policy.value,
                "custom": {
                    "enabled": policy.custom_enabled,
                    "max_duration_days": policy.custom_max_duration_days,
                    "min_duration_days": policy.custom_min_duration_days,
                },
                "default_preset": policy.default_preset.value,
                "display_grains": [grain.value for grain in policy.display_grains],
                "preset_exceptions": [
                    {
                        "availability": exception.availability,
                        "preset_id": exception.preset_id.value,
                        "reason_code": exception.reason_code,
                    }
                    for exception in policy.preset_exceptions
                ],
            }
        }
    vintage = page.vintage_policy
    if vintage is None:
        raise RuntimeError(f"V2 page {page.page_id} declares no temporal policy.")
    return {
        "vintage_policy": {
            "comparison": vintage.comparison_policy.value,
            "default": vintage.default,
            "selector": vintage.selector,
        }
    }


def _v2_generated_document(root: Path) -> dict[str, object]:
    schemas = load_v2_page_schemas(root)
    routes = {page.page_id: page.route for page in schemas.pages}
    pages: list[dict[str, object]] = []
    for page in schemas.pages:
        section_roles = section_roles_v2(page)
        pages.append(
            {
                "actions": [
                    {"action_id": action.action_id, "target_route": routes[action.target_page]}
                    for action in page.actions
                ],
                "artifacts": [
                    {
                        "artifact_id": artifact.artifact_id,
                        "outcomes": list(artifact.outcomes),
                        "type": artifact.artifact_type,
                        "version": artifact.version,
                    }
                    for artifact in page.artifacts
                ],
                "boundary_required": page.boundary_required,
                "compatibility_routes": list(page.compatibility_routes),
                "display_name": page.display_name,
                "mode": page.mode.value,
                "page_id": page.page_id,
                "primary_job": page.primary_job,
                "purpose": page.purpose,
                "route": page.route,
                "sections": [
                    {
                        "cardinality": section.cardinality.value,
                        "role": role.value,
                        "section_id": section.section_id,
                    }
                    for section, role in zip(page.sections, section_roles, strict=True)
                ],
                **_v2_time_policy(page),
                "visuals": [
                    {
                        "metric_cohort": list(visual.metric_cohort),
                        "narration_eligible": visual.narration_eligible,
                        "visual_id": visual.visual_id,
                    }
                    for visual in page.visuals
                ],
            }
        )
    return {
        "ad_product_channels": _v2_ad_product_channels(root),
        "metric_display": _v2_metric_display(root),
        "pages": pages,
        "presentation_result_version": schemas.presentation_result_version,
        "temporal_control_version": schemas.temporal_control_version,
        "version": schemas.version,
    }


def _v2_ad_product_channels(root: Path) -> dict[str, str]:
    """Project each governed ad product's channel from `media-taxonomy.yaml`.

    The browser joins a New customers ad-product row to its Scorecard channel row on these
    governed keys, never by name (UI refresh 2.3). One ad product per channel, so the join is
    exact both ways.
    """
    pairs = {
        str(term.ad_product): str(term.channel)
        for term in load_media_taxonomy(root).terms
        if term.ad_product is not None and term.channel is not None and term.key == term.ad_product
    }
    if not pairs or len(set(pairs.values())) != len(pairs):
        raise PageSchemaError("Each governed channel must have exactly one ad product to join on.")
    return dict(sorted(pairs.items()))


def _v2_metric_display(root: Path) -> dict[str, dict[str, str | None]]:
    """Project each dictionary metric's unit and ratio scale so the browser formats by contract.

    A rate renders as a percentage and a multiplier with a times sign, rounded only for display;
    the exact served value stays in the exact-values disclosure (plan D4, ADR-121).
    """
    return {
        metric.name: {
            "ratio_scale": None if metric.ratio_scale is None else metric.ratio_scale.value,
            "units": metric.unit_kind.value,
        }
        for metric in sorted(load_catalog(root).metrics, key=lambda item: item.name)
    }


def generated_document(root: Path, *, schema_version: int = 1) -> dict[str, object]:
    """Project the requested schema version into its strict frontend binding document."""
    if schema_version == 1:
        return _v1_generated_document(root)
    if schema_version == _V2_SCHEMA_VERSION:
        return _v2_generated_document(root)
    raise ValueError(f"schema_version must be 1 or {_V2_SCHEMA_VERSION}.")


def rendered(root: Path, *, schema_version: int = 1) -> str:
    """Return stable UTF-8 JSON text for the requested binding projection."""
    document = generated_document(root, schema_version=schema_version)
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    """Write one projection, or fail when --check detects drift."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--schema-version", choices=(1, 2), default=1, type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path.cwd()
    path = root / (PAGE_BINDINGS_PATH if args.output is None else args.output)
    expected = rendered(root, schema_version=args.schema_version)
    if args.check:
        if not path.is_file() or path.read_text() != expected:
            print(f"frontend page bindings are stale: {path}")  # noqa: T201
            return 1
        print(f"frontend page bindings are current: {path}")  # noqa: T201
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(expected)
    print(f"wrote {path}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
