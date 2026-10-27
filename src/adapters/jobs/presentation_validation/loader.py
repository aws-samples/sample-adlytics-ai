# SPDX-License-Identifier: MIT-0
"""Strictly load the active analytical page declaration from YAML."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml

from adapters.outbound import repository_yaml
from domain.presentation.page_schemas import (
    AnalyticalPageSchema,
    Cardinality,
    MetricRole,
    PageAction,
    PageArtifactDeclaration,
    PageGrain,
    PageMetric,
    PageMetricVariant,
    PageMode,
    PageSchemaError,
    PageSchemaSet,
    PageSection,
    PageTime,
    PresentationArtifactDeclaration,
    PresentationPageMode,
    PresentationPageNavigation,
    PresentationPageSchema,
    PresentationPageSchemaSet,
    PresentationTemporalPolicy,
    PresentationVintagePolicy,
    PresentationVisual,
    TemporalComparisonPolicy,
    TemporalPresetException,
    TemporalPresetId,
)
from domain.presentation.starter_questions import (
    StarterQuestionTemplateError,
    StarterQuestionTemplates,
)

PAGE_SCHEMAS_PATH = Path("presentation/pages/v1/pages.yaml")
PRESENTATION_PAGE_SCHEMAS_PATH = Path("presentation/pages/v2/pages.yaml")

type Mapping = dict[str, object]


def _mapping(value: object, where: str) -> Mapping:
    if not isinstance(value, dict):
        raise PageSchemaError(f"{where}: expected a mapping, got {type(value).__name__}.")
    return cast("Mapping", value)


def _sequence(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise PageSchemaError(f"{where}: expected a list, got {type(value).__name__}.")
    return cast("list[object]", value)


def _string(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise PageSchemaError(f"{where}: expected a string, got {type(value).__name__}.")
    return value


def _boolean(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise PageSchemaError(f"{where}: expected a boolean, got {type(value).__name__}.")
    return value


def _positive_integer(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PageSchemaError(f"{where}: expected a positive integer.")
    return value


def _strings(value: object, where: str) -> tuple[str, ...]:
    return tuple(_string(item, f"{where}[]") for item in _sequence(value, where))


def _exact(mapping: Mapping, expected: set[str], where: str) -> None:
    if set(mapping) != expected:
        missing = sorted(expected - set(mapping))
        extra = sorted(set(mapping) - expected)
        detail = [*(f"missing {key}" for key in missing), *(f"unknown {key}" for key in extra)]
        raise PageSchemaError(f"{where}: invalid fields ({', '.join(detail)}).")


def _enum[EnumValue: (PageMode, PageTime, MetricRole, Cardinality)](
    value: object, enum_type: type[EnumValue], where: str
) -> EnumValue:
    raw = _string(value, where)
    try:
        return enum_type(raw)
    except ValueError as error:
        raise PageSchemaError(f"{where}: unknown value {raw!r}.") from error


def _variant(value: object, where: str) -> PageMetricVariant:
    raw = _mapping(value, where)
    _exact(raw, {"id", "selectors"}, where)
    selectors = _mapping(raw["selectors"], f"{where}.selectors")
    return PageMetricVariant(
        variant_id=_string(raw["id"], f"{where}.id"),
        selectors=tuple(
            sorted(
                (
                    _string(name, f"{where}.selectors key"),
                    _string(item, f"{where}.selectors.{name}"),
                )
                for name, item in selectors.items()
            )
        ),
    )


def _metric(value: object, where: str) -> PageMetric:
    raw = _mapping(value, where)
    required = {"name", "roles"}
    allowed = required | {"variants", "declared_periods"}
    if required - set(raw) or set(raw) - allowed:
        raise PageSchemaError(
            f"{where}: metric requires name and roles; variants and declared_periods are optional."
        )
    return PageMetric(
        name=_string(raw["name"], f"{where}.name"),
        roles=tuple(
            _enum(item, MetricRole, f"{where}.roles[]")
            for item in _sequence(raw["roles"], f"{where}.roles")
        ),
        variants=tuple(
            _variant(item, f"{where}.variants[]")
            for item in _sequence(raw.get("variants", []), f"{where}.variants")
        ),
        declared_periods=_presets(raw.get("declared_periods", []), f"{where}.declared_periods"),
    )


def _presets(value: object, where: str) -> tuple[TemporalPresetId, ...]:
    return tuple(_v2_enum(item, TemporalPresetId, f"{where}[]") for item in _sequence(value, where))


def _artifact(value: object, where: str) -> PageArtifactDeclaration:
    raw = _mapping(value, where)
    _exact(raw, {"type", "version", "outcomes"}, where)
    return PageArtifactDeclaration(
        artifact_type=_string(raw["type"], f"{where}.type"),
        version=_positive_integer(raw["version"], f"{where}.version"),
        outcomes=_strings(raw["outcomes"], f"{where}.outcomes"),
    )


def _action(value: object, where: str) -> PageAction:
    raw = _mapping(value, where)
    _exact(raw, {"id", "target"}, where)
    return PageAction(
        action_id=_string(raw["id"], f"{where}.id"),
        target_page=_string(raw["target"], f"{where}.target"),
    )


def _section(value: object, where: str) -> PageSection:
    raw = _mapping(value, where)
    _exact(raw, {"id", "cardinality"}, where)
    return PageSection(
        section_id=_string(raw["id"], f"{where}.id"),
        cardinality=_enum(raw["cardinality"], Cardinality, f"{where}.cardinality"),
    )


def _grain(value: object, where: str) -> PageGrain:
    raw = _mapping(value, where)
    _exact(raw, {"time", "dimensions"}, where)
    return PageGrain(
        time=_enum(raw["time"], PageTime, f"{where}.time"),
        dimensions=_strings(raw["dimensions"], f"{where}.dimensions"),
    )


def _page(value: object, where: str) -> AnalyticalPageSchema:
    raw = _mapping(value, where)
    required = {
        "id",
        "display_name",
        "route",
        "purpose",
        "primary_job",
        "mode",
        "boundary_required",
        "preferred_window_duration_days",
        "grain",
        "metrics",
        "sections",
        "actions",
    }
    if required - set(raw) or set(raw) - (required | {"artifact"}):
        raise PageSchemaError(f"{where}: page fields are not exact.")
    return AnalyticalPageSchema(
        page_id=_string(raw["id"], f"{where}.id"),
        display_name=_string(raw["display_name"], f"{where}.display_name"),
        route=_string(raw["route"], f"{where}.route"),
        purpose=_string(raw["purpose"], f"{where}.purpose"),
        primary_job=_string(raw["primary_job"], f"{where}.primary_job"),
        mode=_enum(raw["mode"], PageMode, f"{where}.mode"),
        boundary_required=_boolean(raw["boundary_required"], f"{where}.boundary_required"),
        grain=_grain(raw["grain"], f"{where}.grain"),
        preferred_window_duration_days=_positive_integer(
            raw["preferred_window_duration_days"],
            f"{where}.preferred_window_duration_days",
        ),
        metrics=tuple(
            _metric(item, f"{where}.metrics[{index}]")
            for index, item in enumerate(_sequence(raw["metrics"], f"{where}.metrics"))
        ),
        sections=tuple(
            _section(item, f"{where}.sections[{index}]")
            for index, item in enumerate(_sequence(raw["sections"], f"{where}.sections"))
        ),
        artifact=(
            None if "artifact" not in raw else _artifact(raw["artifact"], f"{where}.artifact")
        ),
        actions=tuple(
            _action(item, f"{where}.actions[{index}]")
            for index, item in enumerate(_sequence(raw["actions"], f"{where}.actions"))
        ),
    )


def _reject_duplicate_keys(node: yaml.Node | None, where: str) -> None:
    if node is None:
        return
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            key = str(key_node.value)
            if key in seen:
                raise PageSchemaError(f"{where}: duplicate YAML key {key!r}.")
            seen.add(key)
            _reject_duplicate_keys(value_node, where)
    elif isinstance(node, yaml.SequenceNode):
        for child in node.value:
            _reject_duplicate_keys(child, where)


def load_page_schemas(root: Path) -> PageSchemaSet:
    """Load the one explicitly active page-schema version."""
    path = root / PAGE_SCHEMAS_PATH
    if not path.is_file():
        raise PageSchemaError(f"page schema manifest is missing at {path}.")
    text = path.read_text()
    composed = repository_yaml.compose(text)
    if composed is not None and not isinstance(composed, yaml.Node):
        raise PageSchemaError(f"{path}: parser returned an invalid YAML node.")
    _reject_duplicate_keys(composed, str(path))
    raw = _mapping(repository_yaml.safe_load(text), str(path))
    _exact(raw, {"version", "context_fields", "pages"}, str(path))
    version = raw["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise PageSchemaError(f"{path}: version must be an integer.")
    pages = _sequence(raw["pages"], f"{path}.pages")
    return PageSchemaSet(
        version=version,
        context_fields=_strings(raw["context_fields"], f"{path}.context_fields"),
        pages=tuple(_page(item, f"{path}.pages[{index}]") for index, item in enumerate(pages)),
    )


def _load_yaml_mapping(path: Path) -> Mapping:
    if not path.is_file():
        raise PageSchemaError(f"page schema manifest is missing at {path}.")
    text = path.read_text()
    composed = repository_yaml.compose(text)
    if composed is not None and not isinstance(composed, yaml.Node):
        raise PageSchemaError(f"{path}: parser returned an invalid YAML node.")
    _reject_duplicate_keys(composed, str(path))
    return _mapping(repository_yaml.safe_load(text), str(path))


def _v2_enum[
    EnumValue: (
        PresentationPageMode,
        TemporalComparisonPolicy,
        TemporalPresetId,
        PageTime,
    )
](value: object, enum_type: type[EnumValue], where: str) -> EnumValue:
    raw = _string(value, where)
    try:
        return enum_type(raw)
    except ValueError as error:
        raise PageSchemaError(f"{where}: unknown value {raw!r}.") from error


def _v2_artifact(value: object, where: str) -> PresentationArtifactDeclaration:
    raw = _mapping(value, where)
    _exact(raw, {"id", "type", "version", "outcomes"}, where)
    return PresentationArtifactDeclaration(
        artifact_id=_string(raw["id"], f"{where}.id"),
        artifact_type=_string(raw["type"], f"{where}.type"),
        version=_positive_integer(raw["version"], f"{where}.version"),
        outcomes=_strings(raw["outcomes"], f"{where}.outcomes"),
    )


def _v2_visual(value: object, where: str) -> PresentationVisual:
    raw = _mapping(value, where)
    _exact(raw, {"id", "metric_cohort", "narration_eligible"}, where)
    return PresentationVisual(
        visual_id=_string(raw["id"], f"{where}.id"),
        metric_cohort=_strings(raw["metric_cohort"], f"{where}.metric_cohort"),
        narration_eligible=_boolean(raw["narration_eligible"], f"{where}.narration_eligible"),
    )


def _v2_temporal_policy(value: object, where: str) -> PresentationTemporalPolicy:
    raw = _mapping(value, where)
    required = {
        "candidate_presets",
        "default_preset",
        "custom",
        "comparison_policy",
        "display_grains",
    }
    allowed = required | {"preset_exceptions", "comparison_intent"}
    if required - set(raw) or set(raw) - allowed:
        raise PageSchemaError(f"{where}: temporal policy fields are not exact.")
    custom = _mapping(raw["custom"], f"{where}.custom")
    _exact(custom, {"enabled", "min_duration_days", "max_duration_days"}, f"{where}.custom")
    exceptions: list[TemporalPresetException] = []
    raw_exceptions = _sequence(
        raw.get("preset_exceptions", []),
        f"{where}.preset_exceptions",
    )
    for index, item in enumerate(raw_exceptions):
        exception = _mapping(item, f"{where}.preset_exceptions[{index}]")
        _exact(
            exception,
            {"preset_id", "availability", "reason_code"},
            f"{where}.preset_exceptions[{index}]",
        )
        exceptions.append(
            TemporalPresetException(
                preset_id=_v2_enum(
                    exception["preset_id"],
                    TemporalPresetId,
                    f"{where}.preset_exceptions[{index}].preset_id",
                ),
                availability=_string(
                    exception["availability"],
                    f"{where}.preset_exceptions[{index}].availability",
                ),
                reason_code=_string(
                    exception["reason_code"],
                    f"{where}.preset_exceptions[{index}].reason_code",
                ),
            )
        )
    comparison_intent = raw.get("comparison_intent")
    if comparison_intent is not None:
        comparison_intent = _string(comparison_intent, f"{where}.comparison_intent")
    return PresentationTemporalPolicy(
        candidate_presets=tuple(
            _v2_enum(item, TemporalPresetId, f"{where}.candidate_presets[]")
            for item in _sequence(raw["candidate_presets"], f"{where}.candidate_presets")
        ),
        default_preset=_v2_enum(raw["default_preset"], TemporalPresetId, f"{where}.default_preset"),
        custom_enabled=_boolean(custom["enabled"], f"{where}.custom.enabled"),
        custom_min_duration_days=_positive_integer(
            custom["min_duration_days"], f"{where}.custom.min_duration_days"
        ),
        custom_max_duration_days=_positive_integer(
            custom["max_duration_days"], f"{where}.custom.max_duration_days"
        ),
        comparison_policy=_v2_enum(
            raw["comparison_policy"], TemporalComparisonPolicy, f"{where}.comparison_policy"
        ),
        display_grains=tuple(
            _v2_enum(item, PageTime, f"{where}.display_grains[]")
            for item in _sequence(raw["display_grains"], f"{where}.display_grains")
        ),
        preset_exceptions=tuple(exceptions),
        comparison_intent=comparison_intent,
    )


def _v2_section(value: object, where: str) -> PageSection:
    """Load a v2 section; `declared_periods`, `period_anchor` and `metrics` are optional."""
    raw = _mapping(value, where)
    required = {"id", "cardinality"}
    optional = {"declared_periods", "period_anchor", "metrics"}
    if required - set(raw) or set(raw) - (required | optional):
        raise PageSchemaError(f"{where}: section fields are not exact.")
    anchor = raw.get("period_anchor")
    return PageSection(
        section_id=_string(raw["id"], f"{where}.id"),
        cardinality=_enum(raw["cardinality"], Cardinality, f"{where}.cardinality"),
        declared_periods=_presets(raw.get("declared_periods", []), f"{where}.declared_periods"),
        period_anchor=None if anchor is None else _boolean(anchor, f"{where}.period_anchor"),
        metrics=_strings(raw.get("metrics", []), f"{where}.metrics"),
    )


def _v2_vintage_policy(value: object, where: str) -> PresentationVintagePolicy:
    raw = _mapping(value, where)
    _exact(raw, {"selector", "default", "comparison"}, where)
    return PresentationVintagePolicy(
        selector=_string(raw["selector"], f"{where}.selector"),
        default=_string(raw["default"], f"{where}.default"),
        comparison_policy=_v2_enum(
            raw["comparison"], TemporalComparisonPolicy, f"{where}.comparison"
        ),
    )


def _v2_time_policy(
    raw: Mapping, where: str
) -> PresentationTemporalPolicy | PresentationVintagePolicy:
    """Exactly one of `temporal_policy` or `vintage_policy` (C-15 adds a third)."""
    declared = sorted({"temporal_policy", "vintage_policy"} & set(raw))
    if len(declared) != 1:
        raise PageSchemaError(f"{where}: declare exactly one of temporal_policy or vintage_policy.")
    if declared[0] == "vintage_policy":
        return _v2_vintage_policy(raw["vintage_policy"], f"{where}.vintage_policy")
    return _v2_temporal_policy(raw["temporal_policy"], f"{where}.temporal_policy")


def _v2_action(value: object, where: str) -> PageAction:
    raw = _mapping(value, where)
    _exact(raw, {"id", "target_page"}, where)
    return PageAction(
        action_id=_string(raw["id"], f"{where}.id"),
        target_page=_string(raw["target_page"], f"{where}.target_page"),
    )


def _v2_page(value: object, where: str) -> PresentationPageSchema:
    raw = _mapping(value, where)
    required = {
        "id",
        "display_name",
        "route",
        "purpose",
        "primary_job",
        "mode",
        "boundary_required",
        "compatibility_routes",
        "artifacts",
        "visuals",
        "sections",
        "actions",
    }
    # ADR-115 decision 4: a page that is a navigation tab carries one `navigation` block, and
    # ADR-120 decision 3 its `assistant` starter-question templates.
    _exact(
        raw,
        required
        | ({"navigation"} if "navigation" in raw else set[str]())
        | ({"assistant"} if "assistant" in raw else set[str]())
        | ({"temporal_policy", "vintage_policy"} & set(raw)),
        where,
    )
    return PresentationPageSchema(
        page_id=_string(raw["id"], f"{where}.id"),
        display_name=_string(raw["display_name"], f"{where}.display_name"),
        route=_string(raw["route"], f"{where}.route"),
        purpose=_string(raw["purpose"], f"{where}.purpose"),
        primary_job=_string(raw["primary_job"], f"{where}.primary_job"),
        mode=_v2_enum(raw["mode"], PresentationPageMode, f"{where}.mode"),
        boundary_required=_boolean(raw["boundary_required"], f"{where}.boundary_required"),
        compatibility_routes=tuple(
            _string(route, f"{where}.compatibility_routes[]")
            for route in _sequence(raw["compatibility_routes"], f"{where}.compatibility_routes")
        ),
        time_policy=_v2_time_policy(raw, where),
        artifacts=tuple(
            _v2_artifact(item, f"{where}.artifacts[{index}]")
            for index, item in enumerate(_sequence(raw["artifacts"], f"{where}.artifacts"))
        ),
        visuals=tuple(
            _v2_visual(item, f"{where}.visuals[{index}]")
            for index, item in enumerate(_sequence(raw["visuals"], f"{where}.visuals"))
        ),
        sections=tuple(
            _v2_section(item, f"{where}.sections[{index}]")
            for index, item in enumerate(_sequence(raw["sections"], f"{where}.sections"))
        ),
        actions=tuple(
            _v2_action(item, f"{where}.actions[{index}]")
            for index, item in enumerate(_sequence(raw["actions"], f"{where}.actions"))
        ),
        navigation=(
            _v2_navigation(raw["navigation"], f"{where}.navigation")
            if "navigation" in raw
            else None
        ),
        assistant=(
            _v2_assistant(raw["assistant"], f"{where}.assistant") if "assistant" in raw else None
        ),
    )


def _v2_assistant(value: object, where: str) -> StarterQuestionTemplates:
    raw = _mapping(value, where)
    _exact(raw, {"starter_questions"}, where)
    starters = _mapping(raw["starter_questions"], f"{where}.starter_questions")
    _exact(starters, {"page"} | ({"detail"} if "detail" in starters else set()), where)

    def templates(key: str) -> tuple[str, ...]:
        if key not in starters:
            return ()
        label = f"{where}.starter_questions.{key}"
        return tuple(_string(item, f"{label}[]") for item in _sequence(starters[key], label))

    try:
        return StarterQuestionTemplates(page=templates("page"), detail=templates("detail"))
    except StarterQuestionTemplateError as error:
        raise PageSchemaError(f"{where}.starter_questions.{error}") from None


def _v2_navigation(value: object, where: str) -> PresentationPageNavigation:
    raw = _mapping(value, where)
    required = {"hub_id", "tab_label", "tab_description", "order"}
    # UI refresh 3.8: `question` is the one optional navigation key.
    _exact(raw, required | ({"question"} & set(raw)), where)
    return PresentationPageNavigation(
        hub_id=_string(raw["hub_id"], f"{where}.hub_id"),
        tab_label=_string(raw["tab_label"], f"{where}.tab_label"),
        tab_description=_string(raw["tab_description"], f"{where}.tab_description"),
        order=_positive_integer(raw["order"], f"{where}.order"),
        question=_string(raw["question"], f"{where}.question") if "question" in raw else None,
    )


def load_v2_page_schemas(root: Path) -> PresentationPageSchemaSet:
    """Load the V2 declaration used by negotiated and final V2-only presentation states."""
    path = root / PRESENTATION_PAGE_SCHEMAS_PATH
    raw = _load_yaml_mapping(path)
    _exact(
        raw,
        {"version", "presentation_result_version", "temporal_control_version", "pages"},
        str(path),
    )
    version_fields = ("version", "presentation_result_version", "temporal_control_version")
    versions: list[int] = []
    for field in version_fields:
        value = raw[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise PageSchemaError(f"{path}.{field}: version must be an integer.")
        versions.append(value)
    pages = _sequence(raw["pages"], f"{path}.pages")
    return PresentationPageSchemaSet(
        version=versions[0],
        presentation_result_version=versions[1],
        temporal_control_version=versions[2],
        pages=tuple(_v2_page(item, f"{path}.pages[{index}]") for index, item in enumerate(pages)),
    )
