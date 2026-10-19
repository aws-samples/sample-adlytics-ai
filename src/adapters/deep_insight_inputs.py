# SPDX-License-Identifier: MIT-0
"""Supply every declared deep-tier input line from the exact served artifact (ADR-076 §1).

Each contract's `deep.allowed_inputs` lists the input classes its deep analysis may read.
`ALLOWED_INPUT_SUPPLIERS` maps every declared line to the slots this module supplies it for,
so a contract line without a supplier fails `make verify`. The supply is citable findings
(series points, totals, driver operands) plus context entries (scope, counts, qualifiers).

Page-slot deep never reads a served segment as the whole portfolio (R3-4): every entry built
from rows carries " (campaigns in view)" and every entry built from totals carries
" (whole portfolio)", unless one segment holds every campaign, when rows are the whole
portfolio too.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from aws_lambda_powertools import Logger

from adapters.inbound.correlation import current_correlation_id
from adapters.inbound.presentation_handlers.insights import InsightAssemblyRequest
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.insight_finding_units import dictionary_id
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.outbound.telemetry.reporting import Report, report_error
from domain.catalog.capability import Catalog
from domain.presentation.campaign_attribution import (
    CampaignAttributionDetailArtifact,
    CampaignDelivery,
)
from domain.presentation.campaign_portfolio import (
    CampaignPortfolioArtifact,
    CampaignPortfolioMetric,
    campaign_population,
    campaigns_in_view_sentence,
    portfolio_row_operand_labels,
    portfolio_spend_change_split,
)
from domain.presentation.currency_display import currency_display_text
from domain.presentation.insights import (
    CrossHorizonDirectionRequest,
    DeterministicFinding,
    InsightContextEntry,
    InsightSlotId,
    cross_horizon_direction_entry,
)
from domain.presentation.metric_focus import (
    CAMPAIGN_DETAIL_FOCUS_METRICS,
    RETAIL_FOCUS_METRICS,
    CampaignDetailFocusRequest,
    MetricFocusIngredients,
    campaign_detail_focus_ingredients,
    campaign_portfolio_focus_ingredients,
    direction_counts_sentence,
    portfolio_focus_metrics,
    retail_health_focus_ingredients,
)
from domain.presentation.page_schemas import TemporalPresetId
from domain.presentation.preset_artifacts import PresetArtifactPointer
from domain.presentation.retail_health import RetailHealthArtifact
from domain.presentation.temporal import temporal_preset_label
from domain.shared_kernel.values import DateRange

__all__ = [
    "ALLOWED_INPUT_SUPPLIERS",
    "DEEP_SIBLING_METRIC",
    "IN_VIEW_SUFFIX",
    "MAX_DEEP_DRIVERS",
    "MAX_DEEP_SERIES_POINTS",
    "WHOLE_PORTFOLIO_SUFFIX",
    "DeepInputs",
    "SiblingDirectionRequest",
    "attribution_timing_entry",
    "deep_enrichment",
    "detail_deep_inputs",
    "portfolio_deep_inputs",
    "retail_deep_inputs",
    "servable_context",
    "sibling_direction_entries",
]

_LOGGER: Final = Logger(service="adlytics-presentation-insights")
_OPERATION: Final = "presentation.deep_insight_inputs"

IN_VIEW_SUFFIX: Final = " (campaigns in view)"
WHOLE_PORTFOLIO_SUFFIX: Final = " (whole portfolio)"
MAX_DEEP_DRIVERS: Final = 50
"""At most one artifact segment of rows (ADR-071)."""
MAX_DEEP_SERIES_POINTS: Final = 14
"""At most 14 days per metric, each with its aligned prior."""

SIBLING_LINE: Final = (
    "sibling preset directions from the same current pointer roster, direction only with "
    "no second-window figures"
)
SERIES_LINE: Final = "complete daily series for every metric this surface serves"
DRIVERS_LINE: Final = "every server-computed driver and cross-metric total for the window"
QUALIFICATIONS_LINE: Final = "lag, estimate, and basis qualifications carried by the artifact"

_PORTFOLIO_PAGE: Final = frozenset(
    {InsightSlotId.CONTRIBUTION, InsightSlotId.DIVERGENCE, InsightSlotId.EXPOSURE}
)
_RETAIL_PAGE: Final = frozenset(
    {
        InsightSlotId.DEMAND_SHAPE,
        InsightSlotId.RETURNS_PRESSURE,
        InsightSlotId.ORDERED_VS_SHIPPED,
    }
)
_DETAIL: Final = frozenset(
    {
        InsightSlotId.TREND_SHAPE,
        InsightSlotId.EFFICIENCY_DRIFT,
        InsightSlotId.FUNNEL,
        InsightSlotId.NEXT_CHECK,
    }
)
_FOCUS: Final = frozenset({InsightSlotId.METRIC_FOCUS, InsightSlotId.RETAIL_METRIC_FOCUS})

ALLOWED_INPUT_SUPPLIERS: Final[Mapping[str, frozenset[InsightSlotId]]] = {
    # Detail deep is bound to one redeemed selection and window; no sibling roster exists.
    SIBLING_LINE: _PORTFOLIO_PAGE | _RETAIL_PAGE | _FOCUS,
    SERIES_LINE: _PORTFOLIO_PAGE | _RETAIL_PAGE | _DETAIL | _FOCUS,
    DRIVERS_LINE: _PORTFOLIO_PAGE | _RETAIL_PAGE | _DETAIL | _FOCUS,
    QUALIFICATIONS_LINE: _PORTFOLIO_PAGE | _RETAIL_PAGE | _DETAIL | _FOCUS,
}
"""Every declared deep input line, and the slots this module (or the focus deep) supplies."""

DEEP_SIBLING_METRIC: Final[Mapping[InsightSlotId, str]] = {
    InsightSlotId.CONTRIBUTION: CampaignPortfolioMetric.SPEND.value,
    InsightSlotId.EXPOSURE: CampaignPortfolioMetric.SPEND.value,
    # W10: the portfolio ROAS total's own id, which the focus extractor parses.
    InsightSlotId.DIVERGENCE: CampaignPortfolioMetric.PORTFOLIO_ROAS.value,
    InsightSlotId.DEMAND_SHAPE: "retail_revenue_shipped",
    InsightSlotId.ORDERED_VS_SHIPPED: "revenue_ordered",
    InsightSlotId.RETURNS_PRESSURE: "customer_returns",
}
"""One representative served metric per page slot for the sibling direction entry."""

_PORTFOLIO_DRIVER_METRICS: Final[Mapping[InsightSlotId, tuple[CampaignPortfolioMetric, ...]]] = {
    InsightSlotId.CONTRIBUTION: (CampaignPortfolioMetric.SPEND,),
    InsightSlotId.DIVERGENCE: (
        CampaignPortfolioMetric.ATTRIBUTED_ROAS,
        CampaignPortfolioMetric.SPEND,
    ),
    InsightSlotId.EXPOSURE: (CampaignPortfolioMetric.SPEND,),
}
_RETAIL_DRIVER_METRICS: Final[Mapping[InsightSlotId, tuple[str, ...]]] = {
    InsightSlotId.DEMAND_SHAPE: ("retail_revenue_shipped", "units_shipped", "glance_views"),
    InsightSlotId.RETURNS_PRESSURE: ("customer_returns", "units_shipped"),
    InsightSlotId.ORDERED_VS_SHIPPED: ("retail_revenue_shipped",),
}


ATTRIBUTION_CONTRACT: Final = "amc-attributed-conversion"
"""The governed contract whose restatement window says attribution may still be credited."""


@functools.cache
def _attribution_restatement_days() -> int:
    return next(
        contract.restatement_window_days
        for _, contract in load_contracts(contracts_root())
        if contract.contract_id == ATTRIBUTION_CONTRACT
    )


def attribution_timing_entry(
    period: DateRange, *, as_of: date | None = None, restatement_days: int | None = None
) -> InsightContextEntry | None:
    """RL-2, deep only: the window days attribution may still credit (ADR-086 §11).

    The governed rule is the attributed-conversion contract's restatement window, applied
    from the latest served day (``as_of``, the window end by default). It names every day of
    every served window up to that width, so it does not discriminate between cards and is
    supplied to deep analysis only.
    """
    end = period.end if as_of is None else as_of
    width = _attribution_restatement_days() if restatement_days is None else restatement_days
    days = [
        period.start + timedelta(days=offset)
        for offset in range(period.days)
        if (end - (period.start + timedelta(days=offset))).days < width
    ]
    if not days:
        return None
    span = days[0].isoformat() if len(days) == 1 else f"{days[0]} to {days[-1]}"
    return InsightContextEntry(
        "attribution timing",
        f"Attributed purchases for {span} are still being credited; later reports usually "
        "add purchases.",
    )


@dataclass(frozen=True, slots=True)
class DeepInputs:
    """The deep tier's additional citable findings and context for one slot."""

    findings: tuple[DeterministicFinding, ...]
    context: tuple[InsightContextEntry, ...]


def _label(catalog: Catalog, metric_id: str) -> str:
    declared = catalog.metric(dictionary_id(metric_id))
    return metric_id if declared is None else declared.label


def _series_findings(
    label: str, ingredients: MetricFocusIngredients, suffix: str = ""
) -> list[DeterministicFinding]:
    """Name each day's current and aligned prior value, at most 14 days."""
    if ingredients.series is None:
        return []
    found: list[DeterministicFinding] = []
    for point in ingredients.series.points[-MAX_DEEP_SERIES_POINTS:]:
        day = point.day.isoformat()
        found.append(
            DeterministicFinding(f"{label} on {day} (current){suffix}", Decimal(point.current))
        )
        if point.prior is not None:
            found.append(
                DeterministicFinding(f"{label} on {day} (prior){suffix}", Decimal(point.prior))
            )
    return found


def _total_findings(
    label: str, values: tuple[object, object, object], suffix: str = ""
) -> list[DeterministicFinding]:
    """Name one total's current, prior and change; an undefined side contributes nothing."""
    current, prior, change = values
    if current is None or prior is None:
        return []
    found = [
        DeterministicFinding(f"{label} current{suffix}", Decimal(str(current))),
        DeterministicFinding(f"{label} prior{suffix}", Decimal(str(prior))),
    ]
    if change is not None:
        found.append(DeterministicFinding(f"{label} change{suffix}", Decimal(str(change))))
    return found


def _unique(findings: Iterable[DeterministicFinding]) -> tuple[DeterministicFinding, ...]:
    seen: set[str] = set()
    kept: list[DeterministicFinding] = []
    for item in findings:
        if item.name not in seen:
            seen.add(item.name)
            kept.append(item)
    return tuple(kept)


def _qualifier_context(
    boundary: str | None, qualifiers: Iterable[str], suffix: str = ""
) -> list[InsightContextEntry]:
    entries: list[InsightContextEntry] = []
    if boundary:
        entries.append(InsightContextEntry("basis", boundary))
    distinct = sorted(set(qualifiers))
    if distinct:
        entries.append(InsightContextEntry(f"qualifications{suffix}", "; ".join(distinct)))
    return entries


def portfolio_deep_inputs(
    artifact: CampaignPortfolioArtifact, slot_id: InsightSlotId, catalog: Catalog
) -> DeepInputs:
    """Supply a portfolio page slot's series, totals, drivers, scope and qualifiers."""
    whole = len(artifact.rows) == campaign_population(artifact)
    row_suffix = WHOLE_PORTFOLIO_SUFFIX if whole else IN_VIEW_SUFFIX
    findings: list[DeterministicFinding] = []
    boundary: str | None = None
    # Only the focus metrics this sibling declares: a multi-channel one totals no attributed
    # figure (ADR-127), so it never reaches for purchases or ROAS.
    for metric in portfolio_focus_metrics(artifact):
        try:
            ingredients = campaign_portfolio_focus_ingredients(artifact, metric)
        except ValueError:
            continue
        boundary = boundary or ingredients.boundary
        findings.extend(
            _series_findings(_label(catalog, metric), ingredients, WHOLE_PORTFOLIO_SUFFIX)
        )
    for total in artifact.totals:
        findings.extend(
            _total_findings(
                _label(catalog, total.metric.value),
                (total.current.value, total.prior.value, total.absolute_change),
                WHOLE_PORTFOLIO_SUFFIX,
            )
        )
    rows = artifact.rows[:MAX_DEEP_DRIVERS]
    names = portfolio_row_operand_labels(artifact.rows)
    context: list[InsightContextEntry] = [
        InsightContextEntry("campaigns in view", campaigns_in_view_sentence(artifact))
    ]
    for metric in _PORTFOLIO_DRIVER_METRICS.get(slot_id, ()):
        label = _label(catalog, metric.value)
        for name, row in zip(names, rows, strict=False):
            comparison = row.comparison_for(metric)
            findings.extend(
                _total_findings(
                    f"{label} for {name}",
                    (comparison.current.value, comparison.prior.value, comparison.absolute_change),
                    row_suffix,
                )
            )
    if slot_id is InsightSlotId.DIVERGENCE:
        roas = tuple(
            Decimal(change)
            for row in rows
            for change in (
                row.comparison_for(CampaignPortfolioMetric.ATTRIBUTED_ROAS).absolute_change,
            )
            if change is not None
        )
        if roas:
            context.append(
                InsightContextEntry(
                    f"ROAS direction counts{row_suffix}", direction_counts_sentence(roas)
                )
            )
    if slot_id in {InsightSlotId.CONTRIBUTION, InsightSlotId.DIVERGENCE}:
        split = portfolio_spend_change_split(artifact)
        currency = next(
            (
                total.current.units.currency_code
                for total in artifact.totals
                if total.metric is CampaignPortfolioMetric.SPEND
            ),
            None,
        )
        if split is not None:
            context.append(
                InsightContextEntry(
                    f"spend change in view{row_suffix}", currency_display_text(split[0], currency)
                )
            )
            if not whole:
                context.append(
                    InsightContextEntry(
                        "spend change outside the view", currency_display_text(split[1], currency)
                    )
                )
    context.extend(
        _qualifier_context(
            boundary, (item for row in rows for item in row.freshness_qualifiers), row_suffix
        )
    )
    timing = attribution_timing_entry(artifact.period)
    if timing is not None:
        context.append(timing)
    return DeepInputs(_unique(findings), tuple(context))


def retail_deep_inputs(
    artifact: RetailHealthArtifact, slot_id: InsightSlotId, catalog: Catalog
) -> DeepInputs:
    """Supply a retail page slot's series, totals, product drivers and qualifiers."""
    findings: list[DeterministicFinding] = []
    boundary = artifact.boundary
    for metric in RETAIL_FOCUS_METRICS:
        try:
            ingredients = retail_health_focus_ingredients(artifact, metric)
        except ValueError:
            continue
        findings.extend(_series_findings(_label(catalog, metric), ingredients))
    for total in artifact.totals:
        findings.extend(
            _total_findings(
                _label(catalog, total.metric),
                (total.current.value, total.prior.value, total.absolute_change),
            )
        )
    rows = artifact.rows[:MAX_DEEP_DRIVERS]
    for metric in _RETAIL_DRIVER_METRICS.get(slot_id, ()):
        label = _label(catalog, metric)
        for row in rows:
            comparison = next(item for item in row.comparisons if item.metric == metric)
            findings.extend(
                _total_findings(
                    f"{label} for {row.title} ({row.child_asin})",
                    (comparison.current.value, comparison.prior.value, comparison.absolute_change),
                )
            )
    context = _qualifier_context(
        boundary, (item for row in rows for item in row.freshness_qualifiers)
    )
    return DeepInputs(_unique(findings), tuple(context))


def detail_deep_inputs(
    detail: CampaignAttributionDetailArtifact, delivery: CampaignDelivery, catalog: Catalog
) -> DeepInputs:
    """Supply every detail slot's series and comparison totals for the redeemed campaign.

    The four detail slots read the same campaign's served comparisons, so the supply does
    not depend on the slot.
    """
    findings: list[DeterministicFinding] = []
    boundary: str | None = None
    for metric in CAMPAIGN_DETAIL_FOCUS_METRICS:
        try:
            ingredients = campaign_detail_focus_ingredients(
                CampaignDetailFocusRequest(detail=detail, delivery=delivery, metric_id=metric)
            )
        except ValueError:
            continue
        label = _label(catalog, metric)
        boundary = boundary or ingredients.boundary
        findings.extend(_series_findings(label, ingredients))
        current, prior = Decimal(ingredients.current.value), Decimal(ingredients.prior.value)
        findings.extend(_total_findings(label, (current, prior, current - prior)))
    context = _qualifier_context(boundary, ())
    timing = attribution_timing_entry(detail.period)
    if timing is not None:
        context.append(timing)
    return DeepInputs(_unique(findings), tuple(context))


@dataclass(frozen=True, slots=True)
class SiblingDirectionRequest:
    """Every sibling read input: the fetched roster's pointers, one reader and one extractor."""

    pointers: tuple[PresetArtifactPointer | None, ...]
    selected_variant: str
    read_payload: Callable[[PresetArtifactPointer], dict[str, object]]
    """Read, attest and decode one sibling body into its JSON object."""
    extract: Callable[[dict[str, object]], tuple[str, Decimal, Decimal | None] | None]
    """Decode the sibling artifact and its focused metric; None only when the metric is typed
    absent on that sibling, and raise on any other failure."""


def sibling_direction_entries(request: SiblingDirectionRequest) -> tuple[InsightContextEntry, ...]:
    """Read sibling preset artifacts from the fetched roster into direction-only context.

    A sibling hint never blocks the deep serve: an absent pointer or a typed-absent metric
    (`extract` returns None) contributes nothing, and any other failure, a malformed or
    stale-format body included, is reported and contributes nothing. The direction is a hint,
    never a co-revision fact.
    """
    entries: list[InsightContextEntry] = []
    for pointer in request.pointers:
        if pointer is None or pointer.variant.value == request.selected_variant:
            continue
        try:
            extracted = request.extract(request.read_payload(pointer))
            if extracted is None:
                # The focused metric is not carried on this sibling: a typed absence.
                continue
            label, current, prior = extracted
            entries.append(
                cross_horizon_direction_entry(
                    CrossHorizonDirectionRequest(
                        temporal_preset_label(TemporalPresetId(pointer.variant.value)),
                        label,
                        current,
                        prior,
                    )
                )
            )
        except Exception as error:
            report_error(
                error,
                Report(
                    operation=_OPERATION, correlation_id=current_correlation_id(), logger=_LOGGER
                ),
            )
    return tuple(entries)


def deep_enrichment(
    extra_findings: tuple[DeterministicFinding, ...],
    extra_context: tuple[InsightContextEntry, ...] = (),
    context_supplier: Callable[[], tuple[InsightContextEntry, ...]] | None = None,
) -> Callable[[InsightAssemblyRequest], InsightAssemblyRequest]:
    """Compose the deep supply onto the card assembly, keeping the card's own names first."""

    def enrich(assembly: InsightAssemblyRequest) -> InsightAssemblyRequest:
        supplied_context = () if context_supplier is None else context_supplier()
        taken_findings = {finding.name for finding in assembly.findings}
        taken_context = {entry.name for entry in assembly.context}
        return replace(
            assembly,
            findings=(
                *assembly.findings,
                *(item for item in extra_findings if item.name not in taken_findings),
            ),
            context=(
                *assembly.context,
                *(
                    item
                    for item in (*extra_context, *supplied_context)
                    if item.name not in taken_context
                ),
            ),
        )

    return enrich


def servable_context(
    presets: tuple[TemporalPresetId, ...],
    metrics: tuple[tuple[str, MetricFocusIngredients], ...],
) -> InsightContextEntry:
    """State what the workspace can serve next, so deep never proposes an unserved view (C7).

    Built and tested, not wired: it ships only if the B1 harness A/B passes (design §4.8).
    """
    views = [temporal_preset_label(preset) for preset in presets]
    joined = views[0] if len(views) == 1 else f"{', '.join(views[:-1])} and {views[-1]}"
    parts = [f"the {joined} views"]
    for label, ingredients in metrics:
        has_series = ingredients.series is not None
        has_drivers = bool(ingredients.driver_candidates)
        served = (
            "a daily series and drivers"
            if has_series and has_drivers
            else "a daily series only"
            if has_series
            else "the total only"
        )
        parts.append(f"for {label}: {served}")
    return InsightContextEntry("servable views", "This workspace serves: " + "; ".join(parts))
