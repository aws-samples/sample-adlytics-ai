# SPDX-License-Identifier: MIT-0
"""Load the committed frontend page-binding projection without inspecting rendered screens."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from domain.presentation.page_schemas import (
    Cardinality,
    MetricRole,
    ObservedAction,
    ObservedMetricSlot,
    ObservedPage,
    PageMode,
    PageSchemaError,
    PresentationPageMode,
    PresentationSectionRole,
    TemporalComparisonPolicy,
    TemporalPresetId,
)

PAGE_BINDINGS_PATH = Path("frontend/generated/page-bindings.json")
_V2_VERSION = 2
_V2_PRESENTATION_RESULT_VERSION = 2
_V2_TEMPORAL_CONTROL_VERSIONS = frozenset({1, 2})
"""Version 2 (ADR-100 point 8) once a page declares a calendar preset; version 1 otherwise."""
_MAX_CUSTOM_DURATION_DAYS = 90


def _record(value: object, keys: set[str], context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PageSchemaError(f"{context} must contain exactly {', '.join(sorted(keys))}.")
    mapping = cast("dict[object, object]", value)
    if not all(isinstance(key, str) for key in mapping) or set(mapping) != keys:
        raise PageSchemaError(f"{context} must contain exactly {', '.join(sorted(keys))}.")
    return cast("dict[str, object]", mapping)


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PageSchemaError(f"{context} must be nonblank text.")
    return value


def _positive_integer(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PageSchemaError(f"{context} must be a positive integer.")
    return value


def _enum[
    EnumValue: (
        PageMode,
        MetricRole,
        Cardinality,
        TemporalPresetId,
        TemporalComparisonPolicy,
        PresentationSectionRole,
        PresentationPageMode,
    )
](value: object, enum_type: type[EnumValue], context: str) -> EnumValue:
    candidate = _text(value, context)
    try:
        return enum_type(candidate)
    except ValueError as error:
        raise PageSchemaError(f"{context} has an unknown value.") from error


def _metric_role(value: object, context: str) -> MetricRole | None:
    return None if value is None else _enum(value, MetricRole, context)


def _artifact(value: object) -> None:
    if value is None:
        return
    artifact = _record(
        value,
        {"outcomes", "type", "version"},
        "page artifact binding",
    )
    outcomes = artifact["outcomes"]
    if not isinstance(outcomes, list):
        raise PageSchemaError("page artifact outcomes must be an array.")
    outcome_values = tuple(
        _text(item, "page artifact outcome") for item in cast("list[object]", outcomes)
    )
    if not outcome_values or len(set(outcome_values)) != len(outcome_values):
        raise PageSchemaError("page artifact outcomes must be nonempty and unique.")
    _text(artifact["type"], "page artifact type")
    _positive_integer(artifact["version"], "page artifact version")


def _context_metadata(page: dict[str, object]) -> str:
    display_name = _text(page["display_name"], "page display name")
    _artifact(page["artifact"])
    return display_name


def load_page_bindings(root: Path) -> tuple[ObservedPage, ...]:  # noqa: PLR0912
    """Load exact bindings; each branch rejects one malformed wire-shape class."""
    path = root / PAGE_BINDINGS_PATH
    if not path.is_file():
        raise PageSchemaError(f"frontend page bindings are missing at {path}.")
    try:
        document = cast("object", json.loads(path.read_text()))
    except (OSError, ValueError) as error:
        raise PageSchemaError(f"frontend page bindings are invalid: {error}") from error
    top = _record(document, {"context_fields", "pages", "version"}, "page bindings")
    context_values = top["context_fields"]
    page_values = top["pages"]
    if (
        top["version"] != 1
        or not isinstance(context_values, list)
        or not isinstance(page_values, list)
    ):
        raise PageSchemaError("frontend page bindings have invalid version or collections.")
    contexts = tuple(_text(item, "context field") for item in cast("list[object]", context_values))
    if len(set(contexts)) != len(contexts):
        raise PageSchemaError("frontend page bindings repeat a context field.")

    pages: list[ObservedPage] = []
    for raw_page in cast("list[object]", page_values):
        page = _record(
            raw_page,
            {
                "actions",
                "artifact",
                "boundary_required",
                "display_name",
                "metrics",
                "mode",
                "page_id",
                "preferred_window_duration_days",
                "primary_job",
                "purpose",
                "route",
                "sections",
            },
            "page binding",
        )
        metric_values = page["metrics"]
        section_values = page["sections"]
        action_values = page["actions"]
        if (
            not isinstance(metric_values, list)
            or not isinstance(section_values, list)
            or not isinstance(action_values, list)
        ):
            raise PageSchemaError("page binding collections must be arrays.")
        if not isinstance(page["boundary_required"], bool):
            raise PageSchemaError("page boundary_required must be boolean.")
        display_name = _context_metadata(page)

        metrics: list[ObservedMetricSlot] = []
        for raw_metric in cast("list[object]", metric_values):
            metric = _record(
                raw_metric, {"label", "name", "role", "selectors", "variant_id"}, "metric binding"
            )
            selector_values = metric["selectors"]
            if not isinstance(selector_values, dict):
                raise PageSchemaError("metric selectors must be an object.")
            selector_mapping = cast("dict[object, object]", selector_values)
            variant = metric["variant_id"]
            if variant is not None:
                variant = _text(variant, "metric variant")
            selectors = tuple(
                sorted(
                    (_text(key, "selector name"), _text(value, "selector value"))
                    for key, value in selector_mapping.items()
                )
            )
            metrics.append(
                ObservedMetricSlot(
                    _text(metric["name"], "metric name"),
                    _enum(metric["role"], MetricRole, "metric role"),
                    _text(metric["label"], "metric label"),
                    variant,
                    selectors,
                )
            )

        sections: list[str] = []
        for raw_section in cast("list[object]", section_values):
            section = _record(
                raw_section,
                {"cardinality", "metric_role", "section_id"},
                "section binding",
            )
            _enum(section["cardinality"], Cardinality, "section cardinality")
            _metric_role(section["metric_role"], "section metric role")
            sections.append(_text(section["section_id"], "section id"))

        actions: list[ObservedAction] = []
        for raw_action in cast("list[object]", action_values):
            action = _record(raw_action, {"action_id", "target_route"}, "action binding")
            actions.append(
                ObservedAction(
                    _text(action["action_id"], "action id"),
                    _text(action["target_route"], "target route"),
                )
            )

        pages.append(
            ObservedPage(
                page_id=_text(page["page_id"], "page id"),
                display_name=display_name,
                route=_text(page["route"], "route"),
                mode=_enum(page["mode"], PageMode, "page mode"),
                boundary_required=page["boundary_required"],
                preferred_window_duration_days=_positive_integer(
                    page["preferred_window_duration_days"],
                    "page preferred window duration",
                ),
                context_fields=contexts,
                metrics=tuple(metrics),
                sections=tuple(sections),
                actions=tuple(actions),
            )
        )
    if len({page.page_id for page in pages}) != len(pages):
        raise PageSchemaError("frontend page bindings repeat a page id.")
    if len({page.route for page in pages}) != len(pages):
        raise PageSchemaError("frontend page bindings repeat a route.")
    return tuple(pages)


@dataclass(frozen=True, slots=True)
class ObservedPresentationArtifact:
    """One V2 generated parent or child artifact binding."""

    artifact_id: str
    artifact_type: str
    version: int
    outcomes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ObservedPresentationVisual:
    """One V2 generated visual binding."""

    visual_id: str
    metric_cohort: tuple[str, ...]
    narration_eligible: bool


@dataclass(frozen=True, slots=True)
class ObservedPresentationPage:
    """One exact generated V2 page binding."""

    page_id: str
    display_name: str
    route: str
    compatibility_routes: tuple[str, ...]
    mode: PresentationPageMode
    boundary_required: bool
    artifacts: tuple[ObservedPresentationArtifact, ...]
    visuals: tuple[ObservedPresentationVisual, ...]
    sections: tuple[tuple[str, Cardinality, PresentationSectionRole], ...]
    actions: tuple[ObservedAction, ...]


@dataclass(frozen=True, slots=True)
class ObservedPresentationBindingSet:
    """The exact V2 generated binding and transport versions."""

    version: int
    presentation_result_version: int
    temporal_control_version: int
    pages: tuple[ObservedPresentationPage, ...]


def _v2_temporal_policy(value: object) -> None:
    policy = _record(
        value,
        {
            "candidate_presets",
            "comparison_intent",
            "comparison_policy",
            "custom",
            "default_preset",
            "display_grains",
            "preset_exceptions",
        },
        "V2 temporal policy binding",
    )
    presets = policy["candidate_presets"]
    grains = policy["display_grains"]
    exceptions = policy["preset_exceptions"]
    if not all(isinstance(collection, list) for collection in (presets, grains, exceptions)):
        raise PageSchemaError("V2 temporal policy collections must be arrays.")
    grains = cast("list[object]", grains)
    preset_values = tuple(
        _enum(item, TemporalPresetId, "V2 temporal candidate preset")
        for item in cast("list[object]", presets)
    )
    if not preset_values or len(set(preset_values)) != len(preset_values):
        raise PageSchemaError("V2 temporal candidate presets must be nonempty and unique.")
    default = _enum(policy["default_preset"], TemporalPresetId, "V2 temporal default preset")
    if default not in preset_values:
        raise PageSchemaError("V2 temporal default preset must be a candidate preset.")
    _enum(policy["comparison_policy"], TemporalComparisonPolicy, "V2 comparison policy")
    if policy["comparison_intent"] not in {None, "compare_previous"}:
        raise PageSchemaError("V2 comparison intent is invalid.")
    custom = _record(
        policy["custom"],
        {"enabled", "max_duration_days", "min_duration_days"},
        "V2 temporal Custom binding",
    )
    if not isinstance(custom["enabled"], bool):
        raise PageSchemaError("V2 temporal Custom enabled must be boolean.")
    minimum = _positive_integer(custom["min_duration_days"], "V2 Custom minimum duration")
    maximum = _positive_integer(custom["max_duration_days"], "V2 Custom maximum duration")
    if maximum < minimum or maximum > _MAX_CUSTOM_DURATION_DAYS:
        raise PageSchemaError("V2 Custom duration bounds are invalid.")
    if not grains or len(set(grains)) != len(grains):
        raise PageSchemaError("V2 display grains must be nonempty and unique.")
    for grain in grains:
        if grain not in {"period", "day", "declared_period"}:
            raise PageSchemaError("V2 display grain is invalid.")
    exception_presets: list[TemporalPresetId] = []
    for raw_exception in cast("list[object]", exceptions):
        exception = _record(
            raw_exception,
            {"availability", "preset_id", "reason_code"},
            "V2 preset exception binding",
        )
        preset = _enum(exception["preset_id"], TemporalPresetId, "V2 exception preset")
        if exception["availability"] != "unavailable" or not _text(
            exception["reason_code"], "V2 exception reason"
        ):
            raise PageSchemaError("V2 preset exceptions must be unavailable with a reason.")
        exception_presets.append(preset)
    if len(set(exception_presets)) != len(exception_presets) or any(
        preset not in preset_values for preset in exception_presets
    ):
        raise PageSchemaError("V2 preset exceptions must uniquely name candidate presets.")


def _v2_vintage_policy(value: object) -> None:
    policy = _record(
        value,
        {"comparison", "default", "selector"},
        "V2 vintage policy binding",
    )
    if (
        policy["selector"] != "as_of_month"
        or policy["default"] != "latest_with_current_publication"
        or policy["comparison"] != "none"
    ):
        raise PageSchemaError("V2 vintage policy has an unknown value.")


_METRIC_UNITS = frozenset({"count", "currency", "ratio"})
_RATIO_SCALES = frozenset({"rate", "multiplier"})


def _metric_display(raw: object) -> None:
    """Refuse a metric display projection that isn't one unit, with a scale exactly on ratios."""
    if not isinstance(raw, dict) or not raw:
        raise PageSchemaError("V2 page bindings metric_display must be a nonempty object.")
    for name, spec in cast("dict[object, object]", raw).items():
        entry = _record(spec, {"ratio_scale", "units"}, f"V2 metric_display {name!s}")
        units, scale = entry["units"], entry["ratio_scale"]
        if not isinstance(name, str) or units not in _METRIC_UNITS:
            raise PageSchemaError(f"V2 metric_display {name!s} has an unknown unit.")
        if (units == "ratio") != (scale in _RATIO_SCALES):
            raise PageSchemaError(f"V2 metric_display {name!s} must scale exactly a ratio.")
        if units != "ratio" and scale is not None:
            raise PageSchemaError(f"V2 metric_display {name!s} must scale exactly a ratio.")


def load_v2_page_bindings(  # noqa: PLR0912, PLR0915 -- exact wire-shape validation
    root: Path,
    *,
    path: Path = PAGE_BINDINGS_PATH,
) -> ObservedPresentationBindingSet:
    """Load a strict V2 binding for negotiated clients or the final V2-only state."""
    binding_path = root / path
    if not binding_path.is_file():
        raise PageSchemaError(f"frontend page bindings are missing at {binding_path}.")
    try:
        document = cast("object", json.loads(binding_path.read_text()))
    except (OSError, ValueError) as error:
        raise PageSchemaError(f"frontend page bindings are invalid: {error}") from error
    top = _record(
        document,
        {
            "metric_display",
            "pages",
            "presentation_result_version",
            "temporal_control_version",
            "version",
        },
        "V2 page bindings",
    )
    _metric_display(top["metric_display"])
    pages_raw = top["pages"]
    if (
        top["version"] != _V2_VERSION
        or top["presentation_result_version"] != _V2_PRESENTATION_RESULT_VERSION
        or top["temporal_control_version"] not in _V2_TEMPORAL_CONTROL_VERSIONS
        or not isinstance(pages_raw, list)
    ):
        raise PageSchemaError("V2 page bindings have invalid versions or pages.")

    pages: list[ObservedPresentationPage] = []
    for raw_page in cast("list[object]", pages_raw):
        if not isinstance(raw_page, dict):
            raise PageSchemaError("V2 page binding must be an object.")
        raw_keys = set(cast("dict[object, object]", raw_page))
        policy_keys = raw_keys & {"temporal_policy", "vintage_policy"}
        if len(policy_keys) != 1:
            raise PageSchemaError("V2 page binding requires exactly one temporal policy.")
        (policy_key,) = policy_keys
        page = _record(
            cast("object", raw_page),
            {
                "actions",
                "artifacts",
                "boundary_required",
                "compatibility_routes",
                "display_name",
                "mode",
                "page_id",
                "primary_job",
                "purpose",
                "route",
                "sections",
                cast("str", policy_key),
                "visuals",
            },
            "V2 page binding",
        )
        if not isinstance(page["boundary_required"], bool):
            raise PageSchemaError("V2 page boundary_required must be boolean.")
        route = _text(page["route"], "V2 page route")
        if not route.startswith("/") or not route.endswith("/"):
            raise PageSchemaError("V2 page routes must be canonical slash-delimited paths.")
        compatibility_raw = page["compatibility_routes"]
        if not isinstance(compatibility_raw, list):
            raise PageSchemaError("V2 compatibility routes must be an array.")
        compatibility_routes = tuple(
            _text(value, "V2 compatibility route")
            for value in cast("list[object]", compatibility_raw)
        )
        if (
            len(set(compatibility_routes)) != len(compatibility_routes)
            or route in compatibility_routes
            or any(
                not compatibility_route.startswith("/") or not compatibility_route.endswith("/")
                for compatibility_route in compatibility_routes
            )
        ):
            raise PageSchemaError(
                "V2 compatibility routes must be unique, noncanonical slash-delimited paths."
            )
        _text(page["display_name"], "V2 page display name")
        _text(page["primary_job"], "V2 page primary job")
        _text(page["purpose"], "V2 page purpose")
        if policy_key == "temporal_policy":
            _v2_temporal_policy(page["temporal_policy"])
        else:
            _v2_vintage_policy(page["vintage_policy"])

        artifacts_raw = page["artifacts"]
        visuals_raw = page["visuals"]
        sections_raw = page["sections"]
        actions_raw = page["actions"]
        if not all(
            isinstance(collection, list)
            for collection in (artifacts_raw, visuals_raw, sections_raw, actions_raw)
        ):
            raise PageSchemaError("V2 page binding collections must be arrays.")
        artifacts: list[ObservedPresentationArtifact] = []
        for raw_artifact in cast("list[object]", artifacts_raw):
            artifact = _record(
                raw_artifact,
                {"artifact_id", "outcomes", "type", "version"},
                "V2 artifact binding",
            )
            outcomes = artifact["outcomes"]
            if not isinstance(outcomes, list):
                raise PageSchemaError("V2 artifact outcomes must be an array.")
            outcome_values = tuple(
                _text(item, "V2 artifact outcome") for item in cast("list[object]", outcomes)
            )
            if not outcome_values or len(set(outcome_values)) != len(outcome_values):
                raise PageSchemaError("V2 artifact outcomes must be nonempty and unique.")
            artifacts.append(
                ObservedPresentationArtifact(
                    artifact_id=_text(artifact["artifact_id"], "V2 artifact id"),
                    artifact_type=_text(artifact["type"], "V2 artifact type"),
                    version=_positive_integer(artifact["version"], "V2 artifact version"),
                    outcomes=outcome_values,
                )
            )
        if not artifacts or len({artifact.artifact_id for artifact in artifacts}) != len(artifacts):
            raise PageSchemaError("V2 page artifacts must be nonempty and have unique ids.")

        visuals: list[ObservedPresentationVisual] = []
        for raw_visual in cast("list[object]", visuals_raw):
            visual = _record(
                raw_visual,
                {"metric_cohort", "narration_eligible", "visual_id"},
                "V2 visual binding",
            )
            cohort = visual["metric_cohort"]
            if not isinstance(cohort, list) or not isinstance(visual["narration_eligible"], bool):
                raise PageSchemaError(
                    "V2 visual binding has invalid cohort or narration eligibility."
                )
            metric_cohort = tuple(
                _text(item, "V2 visual metric") for item in cast("list[object]", cohort)
            )
            if not metric_cohort or len(set(metric_cohort)) != len(metric_cohort):
                raise PageSchemaError("V2 visual metric cohorts must be nonempty and unique.")
            visuals.append(
                ObservedPresentationVisual(
                    visual_id=_text(visual["visual_id"], "V2 visual id"),
                    metric_cohort=metric_cohort,
                    narration_eligible=visual["narration_eligible"],
                )
            )
        if len({visual.visual_id for visual in visuals}) != len(visuals):
            raise PageSchemaError("V2 page visual ids must be unique.")

        sections: list[tuple[str, Cardinality, PresentationSectionRole]] = []
        for raw_section in cast("list[object]", sections_raw):
            section = _record(
                raw_section,
                {"cardinality", "role", "section_id"},
                "V2 section binding",
            )
            sections.append(
                (
                    _text(section["section_id"], "V2 section id"),
                    _enum(section["cardinality"], Cardinality, "V2 section cardinality"),
                    _enum(section["role"], PresentationSectionRole, "V2 section role"),
                )
            )
        if not sections or len({section[0] for section in sections}) != len(sections):
            raise PageSchemaError("V2 page sections must be nonempty and unique.")
        if page["boundary_required"] != any(section[0] == "boundary" for section in sections):
            raise PageSchemaError("V2 boundary declaration does not match its boundary section.")

        actions: list[ObservedAction] = []
        for raw_action in cast("list[object]", actions_raw):
            action = _record(raw_action, {"action_id", "target_route"}, "V2 action binding")
            actions.append(
                ObservedAction(
                    _text(action["action_id"], "V2 action id"),
                    _text(action["target_route"], "V2 action target route"),
                )
            )
        if len({action.action_id for action in actions}) != len(actions):
            raise PageSchemaError("V2 page actions must have unique ids.")
        pages.append(
            ObservedPresentationPage(
                page_id=_text(page["page_id"], "V2 page id"),
                display_name=_text(page["display_name"], "V2 page display name"),
                route=route,
                compatibility_routes=compatibility_routes,
                mode=_enum(page["mode"], PresentationPageMode, "V2 page mode"),
                boundary_required=page["boundary_required"],
                artifacts=tuple(artifacts),
                visuals=tuple(visuals),
                sections=tuple(sections),
                actions=tuple(actions),
            )
        )
    if not pages or len({page.page_id for page in pages}) != len(pages):
        raise PageSchemaError("V2 page bindings repeat a page id.")
    routes = {page.route for page in pages}
    compatibility_routes = {route for page in pages for route in page.compatibility_routes}
    if len(routes) != len(pages):
        raise PageSchemaError("V2 page bindings repeat a route.")
    if len(compatibility_routes) != sum(len(page.compatibility_routes) for page in pages):
        raise PageSchemaError("V2 page bindings repeat a compatibility route.")
    if routes & compatibility_routes:
        raise PageSchemaError("V2 compatibility routes must not be analytical bindings.")
    root_owners = tuple(page.page_id for page in pages if "/" in page.compatibility_routes)
    # ADR-151: the composed Overview tab owns `/` in navigation.yaml, so no page binding may.
    if root_owners != ():
        raise PageSchemaError("V2 page bindings must not own the root route; the Overview does.")
    for page in pages:
        if any(action.target_route not in routes for action in page.actions):
            raise PageSchemaError("V2 action targets an unknown route.")
    return ObservedPresentationBindingSet(
        _V2_VERSION,
        _V2_PRESENTATION_RESULT_VERSION,
        cast("int", top["temporal_control_version"]),
        tuple(pages),
    )
