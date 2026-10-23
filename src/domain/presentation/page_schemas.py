# SPDX-License-Identifier: MIT-0
"""Deterministic analytical page declarations and rendering validation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from domain.catalog.capability import PERIOD_GRAIN_SELECTOR, Catalog, Metric, MetricKind
from domain.measurement.periodic import periodic_leaves
from domain.presentation.starter_questions import StarterQuestionTemplates
from domain.semantics.attribution import AttributionModelRegistry
from domain.semantics.time import FactTimeClass, TimeGrain

__all__ = [
    "ROLLING_PRESET_IDS",
    "AnalyticalPageSchema",
    "Cardinality",
    "MetricRole",
    "ObservedAction",
    "ObservedMetricSlot",
    "ObservedPage",
    "PageAction",
    "PageArtifactDeclaration",
    "PageGrain",
    "PageMetric",
    "PageMetricVariant",
    "PageMode",
    "PageSchemaError",
    "PageSchemaFinding",
    "PageSchemaSet",
    "PageSection",
    "PageTime",
    "PageValidationContext",
    "PresentationArtifactDeclaration",
    "PresentationPageMode",
    "PresentationPageSchema",
    "PresentationPageSchemaSet",
    "PresentationSectionRole",
    "PresentationTemporalPolicy",
    "PresentationVintagePolicy",
    "PresentationVisual",
    "TemporalComparisonPolicy",
    "TemporalPresetException",
    "TemporalPresetId",
    "calendar_preset_grain",
    "is_calendar_preset",
    "validate_page_rendering",
    "validate_page_schemas",
]


_MULTIPLE_BASES = 2
_V2_PAGE_SCHEMA_VERSION = 2
_V2_PRESENTATION_RESULT_VERSION = 2
_V1_TEMPORAL_CONTROL_VERSION = 1
_V2_TEMPORAL_CONTROL_VERSION = 2
"""The one bump ADR-100 and forecasting share (C-15). A declaration at version 1 may use only the
version-1 grammar; calendar presets, `prior_calendar_period`, `vintage_policy` and section
`declared_periods` need version 2, because the browser's version-1 decoder can't read them."""
_MAX_CUSTOM_DURATION_DAYS = 90


class PageSchemaError(ValueError):
    """A page declaration is internally contradictory or malformed."""


class PageMode(StrEnum):
    """How metrics on one page relate to their measurement bases."""

    SINGLE_BASIS = "single_basis"
    PARALLEL = "parallel"
    STATUS = "status"


class PageTime(StrEnum):
    """Time behavior a page claims for its displayed evidence."""

    PERIOD = "period"
    DAY = "day"
    EXACT_SOURCE = "exact_source"
    DECLARED_PERIOD = "declared_period"
    """The whole page shows complete calendar periods only (ADR-100 point 7)."""
    AS_OF_VINTAGE = "as_of_vintage"
    """The whole page shows one `as_of_month` vintage of one fact (ADR-100 point 7)."""


class MetricRole(StrEnum):
    """The deterministic visual responsibility of one metric binding."""

    HEADLINE = "headline"
    SUMMARY = "summary"
    TREND = "trend"
    TABLE = "table"
    STATUS = "status"


class Cardinality(StrEnum):
    """How often a declared section must appear in one rendering."""

    EXACTLY_ONE = "exactly_one"
    ONE_OR_MORE = "one_or_more"
    ZERO_OR_ONE = "zero_or_one"
    ZERO_OR_MORE = "zero_or_more"


@dataclass(frozen=True, slots=True)
class PageGrain:
    """Time behavior and semantic dimensions one page promises."""

    time: PageTime
    dimensions: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(set(self.dimensions)) != len(self.dimensions):
            raise PageSchemaError("page grain dimensions must be unique.")
        if any(not dimension.strip() for dimension in self.dimensions):
            raise PageSchemaError("page grain dimensions must be nonblank.")


@dataclass(frozen=True, slots=True)
class PageMetricVariant:
    """One independently queried exact-selector variant of a displayed metric."""

    variant_id: str
    selectors: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not self.variant_id.strip() or not self.selectors:
            raise PageSchemaError("page metric variant requires an id and selectors.")
        keys = tuple(key for key, _value in self.selectors)
        if len(set(keys)) != len(keys):
            raise PageSchemaError(f"{self.variant_id}: selector names must be unique.")
        if any(not key.strip() or not value.strip() for key, value in self.selectors):
            raise PageSchemaError(f"{self.variant_id}: selectors must be nonblank.")


@dataclass(frozen=True, slots=True)
class PageMetric:
    """One governed metric and the visual roles it must fill."""

    name: str
    roles: tuple[MetricRole, ...]
    variants: tuple[PageMetricVariant, ...] = ()
    declared_periods: tuple[TemporalPresetId, ...] = ()
    """The calendar presets on which this slot is served (ADR-100 point 7). On a `period` page
    such a slot is left out of the three daily period checks and checked as periodic instead."""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise PageSchemaError("page metric requires a nonblank name.")
        _require_calendar_presets(self.declared_periods, f"{self.name}: declared_periods")
        if not self.roles or len(set(self.roles)) != len(self.roles):
            raise PageSchemaError(f"{self.name}: metric roles must be nonempty and unique.")
        variant_ids = tuple(variant.variant_id for variant in self.variants)
        if len(set(variant_ids)) != len(variant_ids):
            raise PageSchemaError(f"{self.name}: variant ids must be unique.")


@dataclass(frozen=True, slots=True)
class PageAction:
    """One evidence action and the canonical page it opens."""

    action_id: str
    target_page: str

    def __post_init__(self) -> None:
        if not self.action_id.strip() or not self.target_page.strip():
            raise PageSchemaError("page action requires nonblank action and target ids.")


@dataclass(frozen=True, slots=True)
class PageSection:
    """One stable page section and its count."""

    section_id: str
    cardinality: Cardinality
    declared_periods: tuple[TemporalPresetId, ...] = ()
    """V2: the calendar presets on which this section binds periodic metrics (ADR-100)."""
    period_anchor: bool | None = None
    """V2: whether this section decides the page's resolved period. Defaults to true on a
    section that declares `declared_periods`; meaningless on one that doesn't."""
    metrics: tuple[str, ...] = ()
    """V2: the governed metrics this section binds, so the section rule can check them."""

    def __post_init__(self) -> None:
        if not self.section_id.strip():
            raise PageSchemaError("page section requires a nonblank id.")
        _require_calendar_presets(self.declared_periods, f"{self.section_id}: declared_periods")
        if self.period_anchor is not None and not self.declared_periods:
            raise PageSchemaError(
                f"{self.section_id}: period_anchor needs declared_periods on the section."
            )
        if len(set(self.metrics)) != len(self.metrics) or any(
            not name.strip() for name in self.metrics
        ):
            raise PageSchemaError(
                f"{self.section_id}: section metrics must be unique and nonblank."
            )

    @property
    def anchors(self) -> bool:
        """Whether the section anchors the resolved period (default true when declared)."""
        return bool(self.declared_periods) and self.period_anchor is not False


@dataclass(frozen=True, slots=True)
class PageArtifactDeclaration:
    """Closed artifact identity and allowed deterministic page outcomes."""

    artifact_type: str
    version: int
    outcomes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.artifact_type.strip() or self.version < 1:
            raise PageSchemaError("page artifact requires type and positive version.")
        if not self.outcomes or len(set(self.outcomes)) != len(self.outcomes):
            raise PageSchemaError("page artifact outcomes must be nonempty and unique.")
        if any(not outcome.strip() for outcome in self.outcomes):
            raise PageSchemaError("page artifact outcomes must be nonblank.")


@dataclass(frozen=True, slots=True)
class AnalyticalPageSchema:
    """Purpose, evidence, structure, and transitions for one analytical page."""

    page_id: str
    display_name: str
    route: str
    purpose: str
    primary_job: str
    mode: PageMode
    boundary_required: bool
    grain: PageGrain
    preferred_window_duration_days: int
    metrics: tuple[PageMetric, ...]
    sections: tuple[PageSection, ...]
    artifact: PageArtifactDeclaration | None = None
    actions: tuple[PageAction, ...] = ()

    def __post_init__(self) -> None:
        for label, value in (
            ("page id", self.page_id),
            ("display name", self.display_name),
            ("route", self.route),
            ("purpose", self.purpose),
            ("primary job", self.primary_job),
        ):
            if not value.strip():
                raise PageSchemaError(f"analytical page requires a nonblank {label}.")
        if (
            isinstance(self.preferred_window_duration_days, bool)
            or self.preferred_window_duration_days < 1
        ):
            raise PageSchemaError(
                f"{self.page_id}: preferred window duration must be a positive integer."
            )
        if self.artifact is not None and not isinstance(
            cast("object", self.artifact), PageArtifactDeclaration
        ):
            raise PageSchemaError(f"{self.page_id}: artifact declaration is invalid.")
        metric_names = tuple(metric.name for metric in self.metrics)
        section_ids = tuple(section.section_id for section in self.sections)
        action_ids = tuple(action.action_id for action in self.actions)
        for label, values in (
            ("metrics", metric_names),
            ("sections", section_ids),
            ("actions", action_ids),
        ):
            if len(set(values)) != len(values):
                raise PageSchemaError(f"{self.page_id}: {label} must be unique.")
        if not self.metrics or not self.sections:
            raise PageSchemaError(f"{self.page_id}: metrics and sections are required.")
        has_boundary_section = any(section.section_id == "boundary" for section in self.sections)
        if self.boundary_required != has_boundary_section:
            raise PageSchemaError(
                f"{self.page_id}: boundary_required must match the declared boundary section."
            )
        if self.grain.time is PageTime.EXACT_SOURCE and self.mode is not PageMode.STATUS:
            raise PageSchemaError(f"{self.page_id}: exact-source time requires status mode.")
        if self.mode is PageMode.STATUS and any(
            role is not MetricRole.STATUS for metric in self.metrics for role in metric.roles
        ):
            raise PageSchemaError(f"{self.page_id}: status pages may expose status roles only.")


@dataclass(frozen=True, slots=True)
class PageSchemaSet:
    """One active version of every analytical page declaration."""

    version: int
    context_fields: tuple[str, ...]
    pages: tuple[AnalyticalPageSchema, ...]

    def __post_init__(self) -> None:
        if self.version != 1:
            raise PageSchemaError("page schema version must be integer 1.")
        if not self.context_fields or len(set(self.context_fields)) != len(self.context_fields):
            raise PageSchemaError("page context fields must be nonempty and unique.")
        if any(not field.strip() for field in self.context_fields):
            raise PageSchemaError("page context fields must be nonblank.")
        if not self.pages:
            raise PageSchemaError("page schema set must declare at least one page.")
        for label, values in (
            ("page ids", tuple(page.page_id for page in self.pages)),
            ("routes", tuple(page.route for page in self.pages)),
        ):
            if len(set(values)) != len(values):
                raise PageSchemaError(f"page schema {label} must be unique.")
        page_ids = {page.page_id for page in self.pages}
        for page in self.pages:
            dangling = sorted(
                action.target_page for action in page.actions if action.target_page not in page_ids
            )
            if dangling:
                raise PageSchemaError(
                    f"{page.page_id}: action targets unknown pages {', '.join(dangling)}."
                )


@dataclass(frozen=True, slots=True, order=True)
class PageSchemaFinding:
    """One deterministic declaration or rendering defect."""

    page_id: str
    rule: str
    message: str


@dataclass(frozen=True, slots=True)
class PageValidationContext:
    """Governed vocabularies needed to validate page declarations and renderings."""

    catalog: Catalog
    attribution_models: AttributionModelRegistry


@dataclass(frozen=True, slots=True)
class _MetricVariantValidationRequest:
    page: AnalyticalPageSchema
    declared: PageMetric
    metric: Metric
    context: PageValidationContext


@dataclass(frozen=True, slots=True)
class ObservedMetricSlot:
    """One metric-role-label binding extracted from a rendered page."""

    name: str
    role: MetricRole
    label: str
    variant_id: str | None = None
    selectors: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ObservedAction:
    """One rendered action and the route it opens."""

    action_id: str
    target_route: str


@dataclass(frozen=True, slots=True)
class ObservedPage:
    """Technology-neutral analytical structure extracted from one rendering."""

    page_id: str
    display_name: str
    route: str
    mode: PageMode
    boundary_required: bool
    preferred_window_duration_days: int
    context_fields: tuple[str, ...]
    metrics: tuple[ObservedMetricSlot, ...]
    sections: tuple[str, ...]
    actions: tuple[ObservedAction, ...]

    def __post_init__(self) -> None:
        if not self.display_name.strip():
            raise PageSchemaError("observed page display name must be nonblank.")
        if self.preferred_window_duration_days < 1:
            raise PageSchemaError("observed page preferred duration must be positive.")


def _finding(page: AnalyticalPageSchema, rule: str, message: str) -> PageSchemaFinding:
    return PageSchemaFinding(page.page_id, rule, message)


def _validate_metric_variants(
    request: _MetricVariantValidationRequest,
) -> tuple[PageSchemaFinding, ...]:
    page = request.page
    declared = request.declared
    metric = request.metric
    catalog = request.context.catalog
    attribution_models = request.context.attribution_models
    required = dict(metric.required_selectors)
    if not required:
        return (
            (_finding(page, "selector", f"{metric.name} declares unnecessary variants."),)
            if declared.variants
            else ()
        )
    if not declared.variants:
        return (_finding(page, "selector", f"{metric.name} requires query variants."),)

    findings: list[PageSchemaFinding] = []
    for variant in declared.variants:
        selectors = dict(variant.selectors)
        if set(selectors) != set(required):
            findings.append(
                _finding(
                    page,
                    "selector",
                    f"{metric.name}/{variant.variant_id} selectors differ from requirements.",
                )
            )
            continue
        pinned = (
            {**dict(metric.requires), **selectors}
            if page.grain.time is PageTime.AS_OF_VINTAGE
            else selectors
        )
        if catalog.refusal_for(metric.name, filters=pinned) is not None:
            findings.append(
                _finding(
                    page,
                    "selector",
                    f"{metric.name}/{variant.variant_id} has an invalid selector value.",
                )
            )
        model_key = selectors.get("attribution_model_key")
        if model_key is not None and variant.variant_id != model_key:
            findings.append(
                _finding(
                    page,
                    "selector",
                    f"{metric.name}/{variant.variant_id} must use its model key as variant id.",
                )
            )

    model_values = required.get("attribution_model_key")
    if model_values is not None:
        selected = tuple(
            dict(variant.selectors)["attribution_model_key"] for variant in declared.variants
        )
        if selected != attribution_models.keys or model_values != attribution_models.keys:
            findings.append(
                _finding(
                    page,
                    "selector",
                    f"{metric.name} must compare every registered attribution model in order.",
                )
            )
    return tuple(findings)


_VINTAGE_SELECTOR = "as_of_month"


def _period_grains_of(catalog: Catalog, metric: Metric) -> tuple[TimeGrain, ...] | None:
    """Return the grains every periodic operand shares, or None when it reads none."""
    leaves = periodic_leaves(catalog, metric)
    if not leaves:
        return None
    return tuple(
        grain
        for grain in leaves[0].period_grains
        if all(grain in leaf.period_grains for leaf in leaves)
    )


def _selects_period_grain(catalog: Catalog, metric: Metric) -> bool:
    """(iii): the metric, or every periodic operand of a ratio, requires `time_grain`."""
    if PERIOD_GRAIN_SELECTOR in dict(metric.required_selectors):
        return True
    leaves = periodic_leaves(catalog, metric)
    return (
        metric.kind is MetricKind.RATIO
        and bool(leaves)
        and all(PERIOD_GRAIN_SELECTOR in dict(leaf.required_selectors) for leaf in leaves)
    )


def _reads_vintage(catalog: Catalog, metric: Metric) -> bool:
    """Whether every periodic operand is measured per cohort vintage (ADR-100 point 7, ADR-104).

    A cohort vintage is the `custom` period grain; serving pins its `as_of_month` (latest by
    default), so a leaf measured only per vintage reads one.
    """
    leaves = periodic_leaves(catalog, metric)
    return bool(leaves) and all(
        _VINTAGE_SELECTOR in dict(leaf.required_selectors)
        or leaf.period_grains == (TimeGrain.CUSTOM,)
        for leaf in leaves
    )


@dataclass(frozen=True, slots=True)
class _SlotCheck:
    page: AnalyticalPageSchema
    declared: PageMetric
    metric: Metric
    catalog: Catalog
    presentation: PresentationPageSchema | None


def _declared_period_slot_findings(check: _SlotCheck) -> tuple[PageSchemaFinding, ...]:
    """Rules (i)-(iii) for a `declared_periods` slot on a v1 `period` page (ADR-100 point 7)."""
    page, declared, metric = check.page, check.declared, check.metric
    findings: list[PageSchemaFinding] = []
    sections = (
        ()
        if check.presentation is None
        else tuple(
            section for section in check.presentation.sections if metric.name in section.metrics
        )
    )
    if not sections or any(
        section.declared_periods != declared.declared_periods for section in sections
    ):
        findings.append(
            _finding(
                page,
                "time",
                f"{metric.name} declares declared_periods, so it must be bound only in v2 "
                "sections with the same declared_periods.",
            )
        )
    grains = _period_grains_of(check.catalog, metric) or ()
    uncovered = sorted(
        preset.value
        for preset in declared.declared_periods
        if calendar_preset_grain(preset) not in grains
    )
    if uncovered:
        findings.append(
            _finding(
                page,
                "time",
                f"{metric.name} declares no period_grains for {', '.join(uncovered)}.",
            )
        )
    if not _selects_period_grain(check.catalog, metric):
        findings.append(
            _finding(
                page,
                "time",
                f"{metric.name} declares declared_periods without the {PERIOD_GRAIN_SELECTOR} "
                "exact selector.",
            )
        )
    return tuple(findings)


def _period_page_findings(
    page: AnalyticalPageSchema,
    resolved: tuple[tuple[PageMetric, Metric], ...],
    context: tuple[Catalog, PresentationPageSchema | None],
) -> list[PageSchemaFinding]:
    """Run the three v1 period checks on every slot without `declared_periods`, as before."""
    catalog, presentation = context
    findings: list[PageSchemaFinding] = []
    daily = [metric for declared, metric in resolved if not declared.declared_periods]
    if daily:
        shared = set(daily[0].valid_grains)
        for metric in daily[1:]:
            shared.intersection_update(metric.valid_grains)
        if "date" not in shared:
            findings.append(_finding(page, "time", "period evidence requires date grain."))
    nonadditive = sorted(metric.name for metric in daily if catalog.is_non_additive(metric))
    wrong_time = sorted(
        metric.name for metric in daily if metric.time_class is not FactTimeClass.DAILY_ADDITIVE
    )
    if nonadditive:
        findings.append(
            _finding(page, "time", f"period evidence cannot aggregate {', '.join(nonadditive)}.")
        )
    if wrong_time:
        findings.append(
            _finding(
                page,
                "time",
                f"period evidence requires daily-additive metrics: {', '.join(wrong_time)}.",
            )
        )
    for declared, metric in resolved:
        if declared.declared_periods:
            findings.extend(
                _declared_period_slot_findings(
                    _SlotCheck(page, declared, metric, catalog, presentation)
                )
            )
    return findings


def _declared_period_page_findings(
    page: AnalyticalPageSchema,
    resolved: tuple[tuple[PageMetric, Metric], ...],
    context: tuple[Catalog, PresentationPageSchema | None],
) -> list[PageSchemaFinding]:
    """Require each slot's declared calendar periods to fit the page and metric."""
    catalog, presentation = context
    presets = (
        ()
        if presentation is None or presentation.vintage_policy is not None
        else presentation.temporal_policy.calendar_presets
    )
    if not presets:
        return [_finding(page, "time", "a declared_period page needs v2 calendar presets.")]
    findings: list[PageSchemaFinding] = []
    covered: set[TemporalPresetId] = set()
    for declared, metric in resolved:
        slot_presets = declared.declared_periods or presets
        outside = sorted(preset.value for preset in slot_presets if preset not in presets)
        if outside:
            findings.append(
                _finding(
                    page,
                    "time",
                    f"{metric.name} declares periods outside the page presets: "
                    f"{', '.join(outside)}.",
                )
            )
        covered.update(preset for preset in slot_presets if preset in presets)
        grains = _period_grains_of(catalog, metric)
        if grains is None:
            if metric.time_class is not FactTimeClass.DAILY_ADDITIVE:
                findings.append(
                    _finding(page, "time", f"{metric.name} is neither periodic nor daily-additive.")
                )
        else:
            needed = {calendar_preset_grain(preset) for preset in slot_presets}
            if not needed <= set(grains):
                findings.append(
                    _finding(page, "time", f"{metric.name} period_grains don't cover its presets.")
                )
            if declared.declared_periods and not _selects_period_grain(catalog, metric):
                findings.append(
                    _finding(
                        page,
                        "time",
                        f"{metric.name} declares periods without the {PERIOD_GRAIN_SELECTOR} "
                        "exact selector.",
                    )
                )
    uncovered = sorted(preset.value for preset in set(presets) - covered)
    if uncovered:
        findings.append(
            _finding(page, "time", f"no metric slot covers page presets: {', '.join(uncovered)}.")
        )
    return findings


def _presentation_section_findings(
    page: PresentationPageSchema, catalog: Catalog
) -> list[PageSchemaFinding]:
    """Check the section rule, on both page kinds (ADR-100 point 7; design §4.7 item 3)."""
    findings: list[PageSchemaFinding] = []
    for section in page.sections:
        for name in section.metrics:
            metric = catalog.metric(name)
            where = f"{section.section_id}/{name}"
            if metric is None:
                findings.append(PageSchemaFinding(page.page_id, "section", f"{where} is unknown."))
                continue
            findings.extend(_curve_findings(page, section.section_id, metric))
            grains = _period_grains_of(catalog, metric)
            if grains is None:
                continue
            if page.vintage_policy is not None:
                # A vintage page has no presets: each periodic binding reads the vintage.
                if not _reads_vintage(catalog, metric):
                    findings.append(
                        PageSchemaFinding(
                            page.page_id, "section", f"{where} reads no as_of_month vintage."
                        )
                    )
                continue
            if not section.declared_periods:
                findings.append(
                    PageSchemaFinding(
                        page.page_id,
                        "section",
                        f"{where}: a section binding a periodic metric declares declared_periods.",
                    )
                )
            uncovered = sorted(
                preset.value
                for preset in section.declared_periods
                if calendar_preset_grain(preset) not in grains
            )
            if uncovered:
                findings.append(
                    PageSchemaFinding(
                        page.page_id,
                        "section",
                        f"{where}: no period_grains entry for {', '.join(uncovered)}.",
                    )
                )
    return findings


RETENTION_CURVE_SECTION = "retention-curve"
"""The Retention repeat curve's section (docs/ui/retention/low-level-design.md)."""
ACCOUNT_ID_CURVE_METRICS = frozenset(
    {"cumulative_repeat_rate_account_ids", "repeat_buyer_rate_account_ids"}
)
"""The rates a cross-group repeat curve may bind: both sides Amazon account IDs (DS5-2)."""


def _curve_findings(
    page: PresentationPageSchema, section_id: str, metric: Metric
) -> list[PageSchemaFinding]:
    """Refuse an all-ID rate on the Retention curve, which compares acquisition groups.

    A DSP-won shopper under a device or browser ID can't match a repeat purchase, so an all-ID
    rate would depress the DSP group's line; the curve binds account-ID rates only (design §6.6).
    """
    if section_id != RETENTION_CURVE_SECTION or metric.kind is not MetricKind.RATIO:
        return []
    if metric.name in ACCOUNT_ID_CURVE_METRICS:
        return []
    return [
        PageSchemaFinding(
            page.page_id,
            "section",
            f"{section_id}/{metric.name}: the repeat curve compares acquisition groups, so it "
            "binds Amazon account ID rates only.",
        )
    ]


def _own_grain_reasons(metrics: tuple[Metric, ...], unsupported: list[str]) -> str:
    """Name each metric's own reason for refusing a page dimension, where it declares one.

    The same `unsupported_grain_reason` the catalog's GRAIN_NOT_SUPPORTED refusal carries, so a
    page binding `roas_incl_halo_by_frequency` per channel is refused for the reason chat gives.
    Empty when no metric missing a dimension declares a reason of its own.
    """
    reasons = [
        f" {metric.name} is refused at {', '.join(missing)} because "
        f"{metric.unsupported_grain_reason}."
        for metric in metrics
        if metric.unsupported_grain_reason is not None
        and (missing := [name for name in unsupported if name not in metric.valid_grains])
    ]
    return "".join(reasons)


def validate_page_schemas(  # noqa: PLR0912 -- reports every independent contract defect
    schema_set: PageSchemaSet,
    context: PageValidationContext,
    presentation: PresentationPageSchemaSet | None = None,
) -> tuple[PageSchemaFinding, ...]:
    """Validate every page metric, grain, basis, and time claim against the catalog.

    With the v2 declaration, also check every v2 section binding and each v1 slot's v2 sections.
    """
    catalog = context.catalog
    v2_pages = {} if presentation is None else {page.page_id: page for page in presentation.pages}
    findings: list[PageSchemaFinding] = []
    for page in v2_pages.values():
        findings.extend(_presentation_section_findings(page, catalog))
    for page in schema_set.pages:
        resolved: list[tuple[PageMetric, Metric]] = []
        for declared in page.metrics:
            metric = catalog.metric(declared.name)
            if metric is None:
                state = "deferred" if catalog.is_deferred(declared.name) else "unknown"
                findings.append(
                    _finding(page, "metric", f"{declared.name} is {state}, not computable.")
                )
            else:
                resolved.append((declared, metric))
                findings.extend(
                    _validate_metric_variants(
                        _MetricVariantValidationRequest(page, declared, metric, context)
                    )
                )
        if len(resolved) != len(page.metrics):
            continue
        metrics = tuple(metric for _declared, metric in resolved)

        shared_grains = set(metrics[0].valid_grains)
        for metric in metrics[1:]:
            shared_grains.intersection_update(metric.valid_grains)
        unsupported = sorted(set(page.grain.dimensions) - shared_grains)
        if unsupported:
            findings.append(
                _finding(
                    page,
                    "grain",
                    f"dimensions {', '.join(unsupported)} are not shared by every metric."
                    + _own_grain_reasons(metrics, unsupported),
                )
            )

        time_context = (catalog, v2_pages.get(page.page_id))
        if page.grain.time is PageTime.PERIOD:
            findings.extend(_period_page_findings(page, tuple(resolved), time_context))
        elif page.grain.time is PageTime.DECLARED_PERIOD:
            findings.extend(_declared_period_page_findings(page, tuple(resolved), time_context))
        elif page.grain.time is PageTime.AS_OF_VINTAGE:
            findings.extend(
                _finding(page, "time", f"{metric.name} reads no as_of_month vintage.")
                for metric in metrics
                if not _reads_vintage(catalog, metric)
            )
        elif any(declared.declared_periods for declared, _metric in resolved):
            findings.append(
                _finding(page, "time", "declared_periods slots belong on period pages only.")
            )

        bases = catalog.bases_across(metrics)
        if page.mode is PageMode.SINGLE_BASIS and len(bases) != 1:
            findings.append(
                _finding(page, "basis", "single-basis page resolves to multiple bases.")
            )
        if page.mode is PageMode.PARALLEL and len(bases) < _MULTIPLE_BASES:
            findings.append(
                _finding(page, "basis", "parallel page requires multiple measurement bases.")
            )
        if page.mode in {PageMode.PARALLEL, PageMode.STATUS} and not page.boundary_required:
            findings.append(
                _finding(page, "boundary", "parallel and status pages require a boundary.")
            )
    return tuple(sorted(findings))


def _validate_rendered_metrics(
    schema: AnalyticalPageSchema,
    rendered: ObservedPage,
    context: PageValidationContext,
) -> tuple[PageSchemaFinding, ...]:
    expected_slots: list[tuple[str, MetricRole, str | None, tuple[tuple[str, str], ...]]] = []
    for metric in schema.metrics:
        for role in metric.roles:
            if metric.variants:
                expected_slots.extend(
                    (metric.name, role, variant.variant_id, variant.selectors)
                    for variant in metric.variants
                )
            else:
                expected_slots.append((metric.name, role, None, ()))
    actual_slots = [
        (metric.name, metric.role, metric.variant_id, metric.selectors)
        for metric in rendered.metrics
    ]
    findings: list[PageSchemaFinding] = []
    if actual_slots != expected_slots:
        findings.append(_finding(schema, "metrics", "rendered metric roles or order differ."))

    declared_metrics = {metric.name: metric for metric in schema.metrics}
    for slot in rendered.metrics:
        metric = context.catalog.metric(slot.name)
        expected_label = None if metric is None else metric.label
        declared = declared_metrics.get(slot.name)
        if metric is not None and declared is not None and slot.variant_id is not None:
            variant = next(
                (item for item in declared.variants if item.variant_id == slot.variant_id),
                None,
            )
            if variant is not None:
                model_key = dict(variant.selectors).get("attribution_model_key")
                model = None if model_key is None else context.attribution_models.model(model_key)
                if model is not None:
                    expected_label = f"{metric.label} — {model.label}"
        if expected_label is not None and slot.label != expected_label:
            findings.append(
                _finding(
                    schema,
                    "label",
                    f"{slot.name}/{slot.role.value} renders {slot.label!r}, expected "
                    f"{expected_label!r}.",
                )
            )
    return tuple(findings)


def _validate_rendered_context_metadata(
    schema: AnalyticalPageSchema,
    rendered: ObservedPage,
) -> tuple[PageSchemaFinding, ...]:
    findings: list[PageSchemaFinding] = []
    if rendered.display_name != schema.display_name:
        findings.append(_finding(schema, "display_name", "rendered display name differs."))
    return tuple(findings)


def validate_page_rendering(
    schema_set: PageSchemaSet,
    context: PageValidationContext,
    observed_pages: tuple[ObservedPage, ...],
) -> tuple[PageSchemaFinding, ...]:
    """Compare one rendering with the complete analytical page declaration."""
    findings: list[PageSchemaFinding] = []
    schemas = {page.page_id: page for page in schema_set.pages}
    observed = {page.page_id: page for page in observed_pages}
    for missing in sorted(set(schemas) - set(observed)):
        findings.append(PageSchemaFinding(missing, "page", "declared page is not rendered."))
    for extra in sorted(set(observed) - set(schemas)):
        findings.append(PageSchemaFinding(extra, "page", "rendered page is not declared."))

    routes = {page.route: page.page_id for page in schema_set.pages}
    for page_id in sorted(set(schemas).intersection(observed)):
        schema = schemas[page_id]
        rendered = observed[page_id]
        if rendered.route != schema.route:
            findings.append(_finding(schema, "route", f"rendered route is {rendered.route}."))
        findings.extend(_validate_rendered_context_metadata(schema, rendered))
        if rendered.mode is not schema.mode:
            findings.append(_finding(schema, "mode", f"rendered mode is {rendered.mode.value}."))
        if rendered.boundary_required is not schema.boundary_required:
            findings.append(_finding(schema, "boundary", "rendered boundary truth differs."))
        if rendered.context_fields != schema_set.context_fields:
            findings.append(_finding(schema, "context", "rendered context fields or order differ."))

        if rendered.preferred_window_duration_days != schema.preferred_window_duration_days:
            findings.append(
                _finding(schema, "window", "rendered preferred window duration differs.")
            )
        findings.extend(_validate_rendered_metrics(schema, rendered, context))

        expected_sections = tuple(section.section_id for section in schema.sections)
        if rendered.sections != expected_sections:
            findings.append(
                _finding(schema, "section", "rendered section associations or order differ.")
            )

        expected_actions = tuple(
            ObservedAction(action.action_id, schemas[action.target_page].route)
            for action in schema.actions
        )
        if rendered.actions != expected_actions:
            findings.append(
                _finding(schema, "actions", "rendered evidence actions or order differ.")
            )
        for action in rendered.actions:
            if action.target_route not in routes:
                findings.append(
                    _finding(schema, "actions", f"{action.action_id} targets unknown route.")
                )
    return tuple(sorted(findings))


class TemporalPresetId(StrEnum):
    """Closed server-declared reporting-window preset identifiers."""

    ONE_DAY = "1d"
    SEVEN_DAYS = "7d"
    FOURTEEN_DAYS = "14d"
    THIRTY_DAYS = "30d"
    LAST_COMPLETE_WEEK = "last_complete_week"
    """Calendar preset: the newest ISO week whose anchors have landed (ADR-100 point 4)."""
    LAST_COMPLETE_MONTH = "last_complete_month"
    LAST_COMPLETE_QUARTER = "last_complete_quarter"


ROLLING_PRESET_IDS = (
    TemporalPresetId.ONE_DAY,
    TemporalPresetId.SEVEN_DAYS,
    TemporalPresetId.FOURTEEN_DAYS,
    TemporalPresetId.THIRTY_DAYS,
)
"""The rolling presets, in canonical order: the whole preset set of transport version 1."""

_CALENDAR_PRESET_GRAINS = {
    TemporalPresetId.LAST_COMPLETE_WEEK: TimeGrain.WEEK,
    TemporalPresetId.LAST_COMPLETE_MONTH: TimeGrain.MONTH,
    TemporalPresetId.LAST_COMPLETE_QUARTER: TimeGrain.QUARTER,
}


def is_calendar_preset(preset_id: TemporalPresetId) -> bool:
    """Whether a preset resolves to a complete calendar period rather than rolling days."""
    return preset_id in _CALENDAR_PRESET_GRAINS


def calendar_preset_grain(preset_id: TemporalPresetId) -> TimeGrain:
    """Return the calendar grain a calendar preset resolves at."""
    grain = _CALENDAR_PRESET_GRAINS.get(preset_id)
    if grain is None:
        raise PageSchemaError(f"{preset_id.value} isn't a calendar preset.")
    return grain


def _require_calendar_presets(presets: tuple[TemporalPresetId, ...], where: str) -> None:
    if len(set(presets)) != len(presets) or any(
        not is_calendar_preset(preset) for preset in presets
    ):
        raise PageSchemaError(f"{where} must name unique calendar presets.")


class TemporalComparisonPolicy(StrEnum):
    """Whether a page requires, permits, or forbids a prior comparison."""

    REQUIRED_EQUAL_PRIOR = "required_equal_prior"
    OPTIONAL_EQUAL_PRIOR = "optional_equal_prior"
    NONE = "none"
    PRIOR_CALENDAR_PERIOD = "prior_calendar_period"
    """The previous calendar period of the same grain; only when every candidate preset is a
    calendar preset (ADR-100 point 4)."""


class PresentationPageMode(StrEnum):
    """V2 interaction shape for a governed presentation page."""

    SELECTABLE = "selectable"
    SELECTABLE_PARENT_CHILD = "selectable_parent_child"


class PresentationSectionRole(StrEnum):
    """One deterministic V2 responsibility for a declared page section."""

    AVAILABILITY = "availability"
    BOUNDARY = "boundary"
    EVIDENCE = "evidence"
    SELECTION = "selection"
    SCOPE = "scope"
    SUMMARY = "summary"
    TABLE = "table"
    TREND = "trend"


@dataclass(frozen=True, slots=True)
class TemporalPresetException:
    """One declaration-time availability exception for an otherwise candidate preset."""

    preset_id: TemporalPresetId
    availability: str
    reason_code: str

    def __post_init__(self) -> None:
        if self.availability != "unavailable" or not self.reason_code.strip():
            raise PageSchemaError(
                "temporal preset exceptions must declare unavailable availability and a reason."
            )


@dataclass(frozen=True, slots=True)
class PresentationTemporalPolicy:
    """Static V2 temporal policy; runtime coverage remains outside the declaration."""

    candidate_presets: tuple[TemporalPresetId, ...]
    default_preset: TemporalPresetId
    custom_enabled: bool
    custom_min_duration_days: int
    custom_max_duration_days: int
    comparison_policy: TemporalComparisonPolicy
    display_grains: tuple[PageTime, ...]
    preset_exceptions: tuple[TemporalPresetException, ...] = ()
    comparison_intent: str | None = None

    def __post_init__(self) -> None:
        if not self.candidate_presets or len(set(self.candidate_presets)) != len(
            self.candidate_presets
        ):
            raise PageSchemaError("temporal candidate presets must be nonempty and unique.")
        if self.default_preset not in self.candidate_presets:
            raise PageSchemaError("temporal default preset must be a candidate preset.")
        if (
            isinstance(self.custom_min_duration_days, bool)
            or isinstance(self.custom_max_duration_days, bool)
            or self.custom_min_duration_days < 1
            or self.custom_max_duration_days < self.custom_min_duration_days
            or self.custom_max_duration_days > _MAX_CUSTOM_DURATION_DAYS
        ):
            raise PageSchemaError("temporal Custom duration bounds are invalid.")
        if not self.display_grains or len(set(self.display_grains)) != len(self.display_grains):
            raise PageSchemaError("temporal display grains must be nonempty and unique.")
        exception_presets = tuple(exception.preset_id for exception in self.preset_exceptions)
        if len(set(exception_presets)) != len(exception_presets):
            raise PageSchemaError("temporal preset exceptions must be unique by preset.")
        if any(preset not in self.candidate_presets for preset in exception_presets):
            raise PageSchemaError("temporal preset exceptions must name candidate presets.")
        if self.comparison_intent not in {None, "compare_previous"}:
            raise PageSchemaError("temporal comparison intent is not supported.")
        if self.comparison_intent is not None and (
            self.comparison_policy is not TemporalComparisonPolicy.OPTIONAL_EQUAL_PRIOR
        ):
            raise PageSchemaError("comparison intent requires optional equal-prior policy.")
        self._check_calendar_presets()

    def _check_calendar_presets(self) -> None:
        """Hold the ADR-100 point 4 and 7 rules for calendar presets and page times."""
        calendar = tuple(preset for preset in self.candidate_presets if is_calendar_preset(preset))
        every_calendar = len(calendar) == len(self.candidate_presets)
        if (
            self.comparison_policy is TemporalComparisonPolicy.PRIOR_CALENDAR_PERIOD
            and not every_calendar
        ):
            raise PageSchemaError(
                "prior_calendar_period requires every candidate preset to be a calendar preset."
            )
        if calendar and self.comparison_policy is TemporalComparisonPolicy.REQUIRED_EQUAL_PRIOR:
            raise PageSchemaError(
                "a calendar preset can't require an equal prior window; use "
                "optional_equal_prior, prior_calendar_period or none."
            )
        if PageTime.AS_OF_VINTAGE in self.display_grains:
            raise PageSchemaError("as_of_vintage is declared by a vintage_policy, not presets.")
        if PageTime.DECLARED_PERIOD in self.display_grains and (
            not every_calendar or self.custom_enabled
        ):
            raise PageSchemaError(
                "a declared_period page offers calendar presets only, with Custom disabled."
            )

    @property
    def calendar_presets(self) -> tuple[TemporalPresetId, ...]:
        """The candidate presets that resolve to complete calendar periods."""
        return tuple(preset for preset in self.candidate_presets if is_calendar_preset(preset))

    @property
    def needs_version_2(self) -> bool:
        """Whether this policy uses grammar the version-1 transport can't carry."""
        return bool(self.calendar_presets) or (
            self.comparison_policy is TemporalComparisonPolicy.PRIOR_CALENDAR_PERIOD
            or PageTime.DECLARED_PERIOD in self.display_grains
        )


@dataclass(frozen=True, slots=True)
class PresentationVintagePolicy:
    """V2 alternative to `temporal_policy`: one `as_of_month` vintage per page (ADR-100).

    Closed values only. A page declares exactly one of `temporal_policy` or `vintage_policy`
    (forecasting's `forecast_horizon_policy` joins the same choice, C-15).
    """

    selector: str = "as_of_month"
    default: str = "latest_with_current_publication"
    comparison_policy: TemporalComparisonPolicy = TemporalComparisonPolicy.NONE

    def __post_init__(self) -> None:
        if (
            self.selector != "as_of_month"
            or self.default != "latest_with_current_publication"
            or self.comparison_policy is not TemporalComparisonPolicy.NONE
        ):
            raise PageSchemaError(
                "vintage_policy is selector as_of_month, default latest_with_current_publication "
                "and comparison none."
            )


@dataclass(frozen=True, slots=True)
class PresentationArtifactDeclaration:
    """One named V2 parent or child artifact identity and its closed outcomes."""

    artifact_id: str
    artifact_type: str
    version: int
    outcomes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.artifact_id.strip():
            raise PageSchemaError("presentation artifact requires a nonblank id.")
        PageArtifactDeclaration(self.artifact_type, self.version, self.outcomes)


@dataclass(frozen=True, slots=True)
class PresentationVisual:
    """One declared V2 visual and the governed metric cohort it may describe."""

    visual_id: str
    metric_cohort: tuple[str, ...]
    narration_eligible: bool

    def __post_init__(self) -> None:
        if not self.visual_id.strip() or not self.metric_cohort:
            raise PageSchemaError("presentation visual requires an id and metric cohort.")
        if len(set(self.metric_cohort)) != len(self.metric_cohort) or any(
            not metric.strip() for metric in self.metric_cohort
        ):
            raise PageSchemaError("presentation visual metric cohorts must be unique and nonblank.")


@dataclass(frozen=True, slots=True)
class PresentationPageNavigation:
    """ADR-115 decision 4: the tab a V2 page is in navigation; a page without it isn't routed."""

    hub_id: str
    tab_label: str
    tab_description: str
    order: int

    def __post_init__(self) -> None:
        for label, value in (
            ("hub id", self.hub_id),
            ("tab label", self.tab_label),
            ("tab description", self.tab_description),
        ):
            if not value.strip():
                raise PageSchemaError(f"page navigation requires a nonblank {label}.")
        if isinstance(self.order, bool) or self.order < 1:
            raise PageSchemaError("page navigation order must be a positive integer.")


@dataclass(frozen=True, slots=True)
class PresentationPageSchema:
    """V2 canonical route, temporal policy, artifact, visual, and action declaration."""

    page_id: str
    display_name: str
    route: str
    purpose: str
    primary_job: str
    mode: PresentationPageMode
    boundary_required: bool
    time_policy: PresentationTemporalPolicy | PresentationVintagePolicy
    """Exactly one of the page's `temporal_policy` or `vintage_policy` declarations."""
    artifacts: tuple[PresentationArtifactDeclaration, ...]
    visuals: tuple[PresentationVisual, ...]
    sections: tuple[PageSection, ...]
    actions: tuple[PageAction, ...]
    compatibility_routes: tuple[str, ...]
    navigation: PresentationPageNavigation | None = None
    assistant: StarterQuestionTemplates | None = None
    """ADR-120 decision 3: a routed tab's governed starter-question templates."""

    def __post_init__(self) -> None:
        if (self.navigation is None) != (self.assistant is None):
            raise PageSchemaError(
                f"{self.page_id}: a routed tab, and only a routed tab, declares its assistant "
                "starter questions."
            )
        if (
            self.assistant is not None
            and self.assistant.detail
            and self.mode is not PresentationPageMode.SELECTABLE_PARENT_CHILD
        ):
            raise PageSchemaError(f"{self.page_id}: only a parent-child page has detail starters.")
        for label, value in (
            ("page id", self.page_id),
            ("display name", self.display_name),
            ("route", self.route),
            ("purpose", self.purpose),
            ("primary job", self.primary_job),
        ):
            if not value.strip():
                raise PageSchemaError(f"presentation page requires a nonblank {label}.")
        if not self.route.startswith("/") or not self.route.endswith("/"):
            raise PageSchemaError(
                f"{self.page_id}: V2 routes must be canonical slash-delimited paths."
            )
        if not self.artifacts or not self.sections:
            raise PageSchemaError(f"{self.page_id}: artifacts and sections are required.")
        if len(set(self.compatibility_routes)) != len(self.compatibility_routes):
            raise PageSchemaError(f"{self.page_id}: compatibility routes must be unique.")
        if self.route in self.compatibility_routes or any(
            not route.startswith("/") or not route.endswith("/")
            for route in self.compatibility_routes
        ):
            raise PageSchemaError(
                f"{self.page_id}: compatibility routes must be distinct slash-delimited paths."
            )
        for label, values in (
            ("artifact ids", tuple(artifact.artifact_id for artifact in self.artifacts)),
            ("visual ids", tuple(visual.visual_id for visual in self.visuals)),
            ("sections", tuple(section.section_id for section in self.sections)),
            ("actions", tuple(action.action_id for action in self.actions)),
        ):
            if len(set(values)) != len(values):
                raise PageSchemaError(f"{self.page_id}: {label} must be unique.")
        has_boundary_section = any(section.section_id == "boundary" for section in self.sections)
        if self.boundary_required != has_boundary_section:
            raise PageSchemaError(
                f"{self.page_id}: boundary_required must match the declared boundary section."
            )
        self._check_declared_periods()

    def _check_declared_periods(self) -> None:
        """Require a section's declared periods to be calendar presets of the page."""
        policy = self.time_policy
        calendar = policy.calendar_presets if isinstance(policy, PresentationTemporalPolicy) else ()
        for section in self.sections:
            outside = sorted(
                preset.value for preset in section.declared_periods if preset not in calendar
            )
            if outside:
                raise PageSchemaError(
                    f"{self.page_id}/{section.section_id}: declared_periods {', '.join(outside)} "
                    "aren't calendar presets of this page."
                )

    @property
    def temporal_policy(self) -> PresentationTemporalPolicy:
        """The page's preset policy. A vintage page has none, and asking for it is refused."""
        if not isinstance(self.time_policy, PresentationTemporalPolicy):
            raise PageSchemaError(f"{self.page_id}: a vintage page has no temporal_policy.")
        return self.time_policy

    @property
    def vintage_policy(self) -> PresentationVintagePolicy | None:
        """The page's vintage policy, or None on a preset page."""
        policy = self.time_policy
        return policy if isinstance(policy, PresentationVintagePolicy) else None

    @property
    def needs_version_2(self) -> bool:
        """Whether this page uses grammar only `temporal_control_version` 2 carries."""
        policy = self.time_policy
        return (
            isinstance(policy, PresentationVintagePolicy)
            or policy.needs_version_2
            or any(section.declared_periods for section in self.sections)
        )


@dataclass(frozen=True, slots=True)
class PresentationPageSchemaSet:
    """The V2 declaration and its transport-control versions."""

    version: int
    presentation_result_version: int
    temporal_control_version: int
    pages: tuple[PresentationPageSchema, ...]

    def __post_init__(self) -> None:
        if (
            self.version != _V2_PAGE_SCHEMA_VERSION
            or self.presentation_result_version != _V2_PRESENTATION_RESULT_VERSION
            or self.temporal_control_version
            not in {_V1_TEMPORAL_CONTROL_VERSION, _V2_TEMPORAL_CONTROL_VERSION}
        ):
            raise PageSchemaError("V2 presentation declaration has invalid transport versions.")
        if self.temporal_control_version == _V1_TEMPORAL_CONTROL_VERSION:
            needing = sorted(page.page_id for page in self.pages if page.needs_version_2)
            if needing:
                raise PageSchemaError(
                    f"{', '.join(needing)}: calendar presets, prior_calendar_period, "
                    "vintage_policy and section declared_periods need temporal_control_version 2."
                )
        if not self.pages:
            raise PageSchemaError("V2 presentation declaration must contain pages.")
        for label, values in (
            ("page ids", tuple(page.page_id for page in self.pages)),
            ("routes", tuple(page.route for page in self.pages)),
            (
                "compatibility routes",
                tuple(route for page in self.pages for route in page.compatibility_routes),
            ),
        ):
            if len(set(values)) != len(values):
                raise PageSchemaError(f"V2 presentation declaration {label} must be unique.")
        canonical_routes = {page.route for page in self.pages}
        if any(
            route in canonical_routes for page in self.pages for route in page.compatibility_routes
        ):
            raise PageSchemaError(
                "V2 presentation compatibility routes must not be canonical analytical routes."
            )
        root_owners = tuple(page.page_id for page in self.pages if "/" in page.compatibility_routes)
        # ADR-151: the composed Overview tab owns `/` in navigation.yaml, so no page entry may.
        if root_owners != ():
            raise PageSchemaError(
                "V2 presentation pages must not own the root route; the composed Overview does."
            )
        page_ids = {page.page_id for page in self.pages}
        for page in self.pages:
            dangling = sorted(
                action.target_page for action in page.actions if action.target_page not in page_ids
            )
            if dangling:
                raise PageSchemaError(
                    f"{page.page_id}: action targets unknown pages {', '.join(dangling)}."
                )
