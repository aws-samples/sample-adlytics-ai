# SPDX-License-Identifier: MIT-0
"""Retained-truth compilation of capability-free Scorecard preset bodies (design §6.1).

The Scorecard materializer, under the ADR-075 pattern the Campaigns and Retail Health bakers
follow. Per declared preset it measures, from retained truth only:

- the channel batch: `ad_spend`, `attributed_revenue_14d`, `roas_attributed_14d` and
  `roas_attributed_incl_halo_14d` at ecosystem x platform x channel x marketplace;
- the deduplicated headline and ranking: `roas_deduplicated_incl_halo_14d` under every
  registered model, at network grain and at channel grain;
- the TACoS operands, through `tacos` at ecosystem x marketplace.

Every request is pinned to one governed marketplace (the commerce identity bridge's canonical
populations), so each answer carries one marketplace's currency and each card is built from
its own answers. A workspace with marketplaces in two currencies gets one card per marketplace,
never converted (design §6.1); the measurement layer refuses an answer spanning two currencies
(`MIXED_CURRENCY`), so a workspace-wide request could never reach that state. The batch is the
16-plan cohort below once per marketplace.

Spend across channels and across networks is totalled by the domain, inside one marketplace
and currency. No attributed figure is requested at a grain that spans networks or channels,
and the planner would refuse one (ADR-106).

The source composes only while the v2 page entry `scorecard` exists. That entry, the route
and the browser rendering belong to the UI build slice, so until it lands this materializer
is registered but never invoked: the baker only receives declared page intents.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, cast

from aws_lambda_powertools import Logger

from adapters.campaign_insight_requests import FindingSource, predicate_findings
from adapters.inbound.tool_handlers.gateway import contracts_root
from adapters.inbound.tool_handlers.measurement import (
    MeasurementExecutionTooling,
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
    execute_presentation_batch,
    prepare_batch,
)
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.presentation_validation.loader import load_v2_page_schemas
from adapters.jobs.preset_artifact_publisher.campaign_source import (
    SUPERSEDED_HISTORY_RETENTION,
)
from adapters.outbound.alert_rules import load_alert_rules
from adapters.outbound.attribution_models import load_attribution_models
from adapters.outbound.bake_sources import source_closed
from adapters.outbound.catalog import PageWindowCatalog
from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.outbound.media_taxonomy import load_media_taxonomy
from adapters.outbound.metric_dictionary import load_catalog
from adapters.outbound.publication_selection import (
    PerPackPresentationEvidenceInspection,
    PublicationSelector,
)
from adapters.outbound.source_pack import load_source_packs
from adapters.outbound.telemetry.reporting import Report, report_error
from domain.catalog.freshness import FreshnessStatus
from domain.measurement.planning import GRAIN_COLUMNS, MEASURE_ALIAS
from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.alert_rules import AlertRule, validate_alert_rules
from domain.presentation.change import (
    Change,
    ChangeOperands,
    ChangeSemantics,
    build_change,
)
from domain.presentation.compared_metrics import scorecard_change_semantics
from domain.presentation.evidence_status import (
    CoverageFact,
    CoverageKind,
    EvidenceStatus,
    write_evidence_status,
)
from domain.presentation.insights import (
    InsightContract,
    InsightEvidenceInput,
    InsightSlotId,
    OperandBasis,
    finding_is_material,
    horizon_role_for,
)
from domain.presentation.page_schemas import TemporalPresetId, is_calendar_preset
from domain.presentation.preset_artifacts import (
    PresetArtifactBody,
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactPointerAdvanceRequest,
    PresetArtifactPublicationRequest,
    PresetArtifactRevision,
    PresetArtifactVariant,
    SourceUnchanged,
    canonical_preset_artifact_json,
)
from domain.presentation.scorecard import (
    CHANNEL_FIGURE_MISSING_REASON,
    CHANNEL_RETURN_NO_SPEND_REASON,
    DEDUPLICATED_MISSING_REASON,
    DEDUPLICATED_MODELS_DISAGREE_REASON,
    DEDUPLICATED_WITHHELD_REASON,
    HEADLINE_LABEL,
    SCORECARD_ALERT_FINDINGS,
    SCORECARD_COMPARED_METRICS,
    SPEND_LABEL,
    TACOS_LABEL,
    TACOS_NEEDS_VENDOR,
    NetworkCard,
    NetworkKey,
    NetworkScorecardArtifact,
    NetworkScorecardSlotRequest,
    ScorecardAbsentChannel,
    ScorecardBasis,
    ScorecardBasisKind,
    ScorecardChannelRow,
    ScorecardComparison,
    ScorecardFinding,
    ScorecardFindingCandidate,
    ScorecardHeadline,
    ScorecardModelValue,
    ScorecardOutcome,
    ScorecardTacosRow,
    also_changed_rows,
    card_id,
    cross_network_spend_rows,
    network_scorecard_artifact_payload,
    network_scorecard_finding_candidates,
    network_scorecard_slot,
    network_spend_total,
    scorecard_calendar_period,
    scorecard_insight_cards,
    scorecard_marketplace_label,
    scorecard_narrated,
    scorecard_ranking,
)
from domain.presentation.temporal import (
    TemporalComparisonStatus,
    TemporalResolutionAvailable,
    TemporalResolutionRequest,
    TemporalSelection,
    TemporalSelectionKind,
    resolve_temporal,
    temporal_preset_label_v2,
)
from domain.semantics.identity import ChannelKey, EcosystemKey, PlatformKey
from domain.shared_kernel.values import DateRange
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionPlan,
    PresentationEvidenceInspectionRequest,
)
from ports.outbound.query import BAKE_MAX_ROWS, RowCeiling
from ports.outbound.semantic_state import scope_horizon_policy
from ports.outbound.source_coverage import (
    SourceCoverageRequest,
    SourceCoverageRequirement,
    metric_coverage_requirement,
)

if TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

    from adapters.config import PresetArtifactBakeSettings
    from adapters.inbound.tool_handlers.measurement import (
        PreparedMeasurementBatch,
        PresentationMeasurementAnswerSlot,
    )
    from adapters.outbound.bake_sources import BakeSourceReads
    from domain.catalog.capability import Catalog
    from domain.data_contracts.contract import DataContract
    from domain.presentation.page_schemas import (
        PresentationPageSchemaSet,
        PresentationTemporalPolicy,
    )
    from domain.semantics.media_taxonomy import MediaTaxonomy
    from ports.outbound.preset_artifacts import PresetArtifactPointerStorePort
    from ports.outbound.publication_selection import PresentationEvidenceInspectionPort
    from ports.outbound.query import PeriodSeriesQueryPort
    from ports.outbound.semantic_state import CurrentPublicationInspectionPort
    from ports.outbound.source_coverage import SourceCoveragePort

__all__ = [
    "SCORECARD_PAGE_ID",
    "SCORECARD_PRESET_RENDER_FORMAT",
    "GroupedAnswer",
    "RetainedScorecardPresetArtifactSource",
    "ScorecardArtifactRenderer",
    "ScorecardCompiler",
    "ScorecardLabels",
    "ScorecardMeasurementBatch",
    "ScorecardMeasurementBatchRequest",
    "ScorecardRenderInput",
    "ScorecardRenderResult",
    "grouped_answer_complete",
    "parse_grouped_answer",
    "retained_scorecard_preset_artifact_source",
    "scorecard_coverage_requirements",
    "scorecard_labels",
    "scorecard_measurement_batch",
]

SCORECARD_PAGE_ID = "scorecard"
SCORECARD_PRESET_RENDER_FORMAT = 7
"""The Scorecard renderer's output generation (ADR-070 render format). Bump on byte changes.

2 (UI slice 9a): the browser-shaped body with display values, change blocks, server texts and
the coverage block. No format-1 body was ever baked (the source failed closed without its page
entry), so a stray one can't decode as format 2.
4 (slice 9a round 3): two-place multiplier display values, the short marketplace name, the
retail rows' marketplace identity, and no ranked ratio over zero spend.
5 (slice 9a round 4): insight cards and Also changed rows carry their marketplace and name.
7 (2026-10-22): a ranking cell with no deduplicated credit says so and takes no rank, and a
headline that differs by model, or no credited channel under any model, makes the ranking
insufficient."""

_CHANNEL_GRAINS = ("ecosystem", "platform", "channel", "marketplace")
_NETWORK_GRAINS = ("ecosystem", "platform", "marketplace")
_TACOS_GRAINS = ("ecosystem", "marketplace")
_SPEND = "ad_spend"
_REVENUE = "attributed_revenue_14d"
_ROAS = "roas_attributed_14d"
_ROAS_HALO = "roas_attributed_incl_halo_14d"
_DEDUPLICATED = "roas_deduplicated_incl_halo_14d"
_TACOS = "tacos"
_CHANNEL_METRICS = (_SPEND, _REVENUE, _ROAS, _ROAS_HALO)
_COVERAGE_METRICS = (_SPEND, _REVENUE)
"""The metrics a bake needs coverage for. TACoS degrades to its typed absence instead."""
_MODEL_SELECTOR = "attribution_model_key"
MARKETPLACE_SELECTOR = "marketplace_key"
"""The canonical column every Scorecard request pins to one marketplace."""
_CURRENCY_METRICS = frozenset({_SPEND, _REVENUE})
_OPERATION = "scorecard_artifact_render"
_CORRELATION_ID = "scorecard-artifact"
LOGGER = Logger(service="adlytics-preset-artifact-baker")

type _Key = tuple[str, ...]
type _AnswerLabel = tuple[str, str, str | None, str]
"""(section, metric, model key or None, role) for one answer inside one marketplace."""
type _SlotLabel = tuple[str, str, str, str | None, str]
"""(marketplace, section, metric, model key or None, role) for one prepared plan."""


@dataclass(frozen=True, slots=True)
class ScorecardLabels:
    """Governed display names and the registered models, read from the contracts."""

    ecosystems: Mapping[str, str]
    platforms: Mapping[str, str]
    channels: Mapping[str, str]
    channels_by_platform: Mapping[str, tuple[str, ...]]
    models: tuple[tuple[str, str], ...]
    attribution_window_days: int
    marketplaces: tuple[str, ...]
    """The governed canonical marketplaces, each measured on its own (one currency each)."""
    marketplace_labels: Mapping[str, str] = field(default_factory=dict[str, str])
    """Each marketplace's governed display name, which a card header names."""

    def __post_init__(self) -> None:
        if not self.models:
            raise ValueError("the scorecard needs at least one registered attribution model.")
        if not self.marketplaces or tuple(sorted(set(self.marketplaces))) != self.marketplaces:
            raise ValueError("the scorecard measures each governed marketplace once, in order.")


def scorecard_labels(root: Path, catalog: Catalog) -> ScorecardLabels:
    """Read media names from the taxonomy, models from the registry, the window from metrics."""
    taxonomy: MediaTaxonomy = load_media_taxonomy(root)
    ecosystems: dict[str, str] = {}
    platforms: dict[str, str] = {}
    channels: dict[str, str] = {}
    by_platform: dict[str, list[str]] = {}
    for term in taxonomy.terms:
        key = term.key
        if isinstance(key, EcosystemKey):
            ecosystems[key.value] = term.name
        elif isinstance(key, PlatformKey):
            platforms[key.value] = term.name
        elif isinstance(key, ChannelKey) and term.platform is not None:
            channels[key.value] = term.name
            by_platform.setdefault(term.platform.value, []).append(key.value)
    registry = load_attribution_models(root)
    bridge = load_commerce_identity_bridge(root)
    revenue = catalog.metric(_REVENUE)
    window = None if revenue is None else revenue.attribution_window_days
    if window is None:
        raise RuntimeError("attributed_revenue_14d must declare its attribution window.")
    return ScorecardLabels(
        ecosystems=ecosystems,
        platforms=platforms,
        channels=channels,
        channels_by_platform={key: tuple(value) for key, value in by_platform.items()},
        models=tuple((model.key, model.label) for model in registry.models),
        attribution_window_days=window,
        marketplaces=tuple(
            sorted({mapping.target.value for mapping in bridge.marketplace_mappings})
        ),
        marketplace_labels={
            item.marketplace.value: item.value for item in bridge.marketplace_display_names
        },
    )


@dataclass(frozen=True, slots=True)
class ScorecardMeasurementBatchRequest:
    """Inputs the fixed Scorecard measurement cohort is prepared from."""

    catalog: Catalog
    contracts: tuple[DataContract, ...]
    models: tuple[str, ...]
    period: DateRange
    prior_period: DateRange | None
    computed_at: datetime
    marketplaces: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScorecardMeasurementBatch:
    """The prepared Scorecard cohort plus one stable label per prepared plan."""

    prepared: PreparedMeasurementBatch
    slots: tuple[_SlotLabel, ...]


def _request(  # noqa: PLR0913 — the marketplace and model selectors are keyword-only
    request: ScorecardMeasurementBatchRequest,
    metric: str,
    grains: tuple[str, ...],
    *,
    marketplace: str,
    model: str | None = None,
    compare_model: bool = False,
) -> MeasurementToolRequest:
    compare = request.prior_period is not None and (model is None or compare_model)
    filters = {MARKETPLACE_SELECTOR: marketplace}
    if model is not None:
        filters[_MODEL_SELECTOR] = model
    return MeasurementToolRequest(
        tool=MeasurementTool.COMPARE_WINDOWS if compare else MeasurementTool.METRIC_BY_GRAIN,
        catalog=request.catalog,
        contracts=request.contracts,
        metric_name=metric,
        grains=grains,
        window=request.period,
        prior_window=request.prior_period if compare else None,
        computed_at=request.computed_at,
        correlation_id=_CORRELATION_ID,
        filters=filters,
        max_rows=BAKE_MAX_ROWS,
        row_ceiling=RowCeiling.BAKE,
    )


def scorecard_measurement_batch(
    request: ScorecardMeasurementBatchRequest,
) -> ScorecardMeasurementBatch:
    """Build the one Scorecard bake batch in stable slot order.

    The renderer executes exactly this batch, so a plan count taken from it can't drift
    from what the baker runs. A rolling preset compares each channel figure and TACoS with
    the equal prior; the deduplicated figures are measured for the window only, because the
    ranking shows the window only. The cohort repeats once per governed marketplace, each
    request pinned to it, so no answer spans two currencies.
    """
    if not request.marketplaces:
        raise RuntimeError("the Scorecard measures at least one governed marketplace.")
    roles = ("current", "prior") if request.prior_period is not None else ("current",)
    measurements: list[MeasurementToolRequest] = []
    slots: list[_SlotLabel] = []
    for market in request.marketplaces:
        for metric in _CHANNEL_METRICS:
            measurements.append(_request(request, metric, _CHANNEL_GRAINS, marketplace=market))
            slots.extend((market, "channel", metric, None, role) for role in roles)
        for model in request.models:
            # The headline compares with its equal prior (ADR-121 change chip); the ranking
            # shows the window only.
            measurements.append(
                _request(
                    request,
                    _DEDUPLICATED,
                    _NETWORK_GRAINS,
                    marketplace=market,
                    model=model,
                    compare_model=True,
                )
            )
            slots.extend((market, "headline", _DEDUPLICATED, model, role) for role in roles)
            measurements.append(
                _request(request, _DEDUPLICATED, _CHANNEL_GRAINS, marketplace=market, model=model)
            )
            slots.append((market, "ranking", _DEDUPLICATED, model, "current"))
        measurements.append(_request(request, _TACOS, _TACOS_GRAINS, marketplace=market))
        slots.extend((market, "tacos", _TACOS, None, role) for role in roles)
    try:
        prepared = prepare_batch(tuple(measurements))
    except (KeyError, ValueError) as error:
        raise RuntimeError("the Scorecard requires its declared metric cohort.") from error
    if len(prepared.plans) != len(slots):
        raise RuntimeError("the Scorecard batch and its slot labels must align.")
    return ScorecardMeasurementBatch(prepared, tuple(slots))


@dataclass(frozen=True, slots=True)
class ScorecardRenderInput:
    """The exact resolved window, its equal prior on a rolling preset, and the preset's labels."""

    period: DateRange
    prior_period: DateRange | None
    preset_label: str = "7 days"
    period_label: str | None = None
    """A calendar preset's server-built period label; None on a rolling preset."""


@dataclass(frozen=True, slots=True)
class ScorecardRenderResult:
    """A reduced Scorecard artifact and the exact publication identities used."""

    artifact: NetworkScorecardArtifact | None
    publication_identities: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.artifact is not None and not self.publication_identities:
            raise ValueError("a rendered Scorecard artifact must name its publications.")


class ScorecardCompiler(Protocol):
    """Measure and reduce one window into a Scorecard artifact."""

    def render_scorecard(self, request: ScorecardRenderInput) -> ScorecardRenderResult:
        """Return the reduced artifact and the exact publication identities consumed."""
        ...


@dataclass(frozen=True, slots=True)
class GroupedAnswer:
    """One answer's rows keyed by its group columns, with its units and evidence state."""

    rows: dict[_Key, Decimal | None]
    units: Units | None
    suppressed: bool
    never_loaded: bool
    holes: tuple[DateRange, ...]
    covered: tuple[DateRange, ...]


def parse_grouped_answer(
    answer: PresentationMeasurementAnswerSlot, period: DateRange, grains: tuple[str, ...]
) -> GroupedAnswer | None:
    """Parse one grouped answer, or none when it's incoherent (truncated, wrong grain)."""
    shaped = answer.answer
    group = tuple(GRAIN_COLUMNS[name] for name in grains)
    if shaped is None:
        return GroupedAnswer(
            {}, None, suppressed=False, never_loaded=True, holes=(period,), covered=()
        )
    metadata, data = shaped.metadata, shaped.data
    if (
        metadata.truncated
        or data.grain.dimensions != group
        or metadata.requested_coverage_intervals != (period,)
        or metadata.row_count != len(data.values)
    ):
        return None
    rows: dict[_Key, Decimal | None] = {}
    for raw_row in data.values:
        raw = cast("dict[str, object]", raw_row)
        key = tuple(raw.get(column) for column in group)
        value = raw.get(MEASURE_ALIAS)
        if (
            set(raw) != {*group, MEASURE_ALIAS}
            or any(not isinstance(item, str) or not item.strip() for item in key)
            or isinstance(value, bool)
            or (value is not None and not isinstance(value, (int, Decimal)))
            or key in rows
        ):
            return None
        rows[cast("_Key", key)] = None if value is None else Decimal(value)
    return GroupedAnswer(
        rows,
        data.units,
        suppressed=metadata.suppression.rows_suppressed,
        never_loaded=metadata.freshness_status is FreshnessStatus.NEVER_LOADED,
        holes=metadata.coverage_holes,
        covered=metadata.actual_coverage_intervals,
    )


def grouped_answer_complete(parsed: GroupedAnswer, period: DateRange) -> bool:
    """Whether an answer covers its whole window with nothing withheld."""
    return (
        not parsed.never_loaded
        and not parsed.holes
        and not parsed.suppressed
        and parsed.covered == (period,)
    )


@dataclass(frozen=True, slots=True)
class _Answers:
    """One marketplace's parsed answers, by (section, metric, model, role)."""

    marketplace: str
    by_label: dict[_AnswerLabel, GroupedAnswer]
    period: DateRange
    prior_period: DateRange | None

    def get(self, label: tuple[str, str, str], model: str | None = None) -> GroupedAnswer:
        """Return one parsed answer by (section, metric, role) and its model."""
        section, metric, role = label
        return self.by_label[(section, metric, model, role)]


def _tacos_absence(parsed: GroupedAnswer, period: DateRange) -> str:
    """Return the TACoS typed absence: no Vendor Central tier, or days that didn't arrive."""
    if parsed.never_loaded or parsed.covered == ():
        return TACOS_NEEDS_VENDOR
    if parsed.holes:
        missing = ", ".join(
            hole.start.isoformat()
            if hole.start == hole.end
            else f"{hole.start.isoformat()} to {hole.end.isoformat()}"
            for hole in parsed.holes
        )
        latest = max(interval.end for interval in parsed.covered)
        return (
            f"Vendor Central sales for {missing} didn't arrive. The latest available is "
            f"{min(latest, period.end).isoformat()}."
        )
    return TACOS_NEEDS_VENDOR


@dataclass(frozen=True, slots=True)
class _CardContext:
    answers: _Answers
    labels: ScorecardLabels
    network: NetworkKey
    network_label: str
    currency: str
    semantics: ChangeSemantics


SCORECARD_CHANGED_METRICS = SCORECARD_COMPARED_METRICS


def _change(  # noqa: PLR0913, PLR0917 -- a metric, its two sides and their semantics
    semantics: ChangeSemantics,
    metric: str,
    values: tuple[Decimal | None, Decimal | None],
    units: Units,
) -> Change | None:
    """Build a figure's ADR-121 change block, or none without a prior or off the chip roster."""
    current, prior = values
    if metric not in SCORECARD_CHANGED_METRICS or current is None or prior is None:
        return None
    return build_change(ChangeOperands(current, prior, units), semantics.for_metric(metric))


def _figure(  # noqa: PLR0913 — one figure's coordinates, by keyword
    context: _CardContext,
    metric: str,
    *,
    channel: str,
    basis: ScorecardBasis,
    spend: tuple[Decimal, Decimal | None],
) -> ScorecardComparison:
    """One channel figure; a covered answer with no row is a true zero, never invented."""
    key = (
        context.network.ecosystem_key,
        context.network.platform_key,
        channel,
        context.network.marketplace_key,
    )
    units = (
        Units(UnitKind.CURRENCY, context.currency)
        if metric in _CURRENCY_METRICS
        else Units(UnitKind.RATIO)
    )
    values: list[Decimal | None] = []
    roles = ("current", "prior") if context.answers.prior_period is not None else ("current",)
    for role, spent in zip(roles, spend, strict=False):
        rows = context.answers.get(("channel", metric, role)).rows
        value = rows.get(key)
        if value is None and key not in rows:
            # A ratio over no spend is undefined; an additive figure with no row is zero.
            value = Decimal(0) if metric in _CURRENCY_METRICS or spent else None
        values.append(value)
    current = values[0]
    prior = values[1] if len(values) > 1 else None
    if current is None:
        # A ratio over no spend has nothing to divide; any other gap is a row that didn't arrive.
        reason = (
            CHANNEL_RETURN_NO_SPEND_REASON
            if metric not in _CURRENCY_METRICS and not spend[0]
            else CHANNEL_FIGURE_MISSING_REASON
        )
        return ScorecardComparison.absent(metric, basis, reason)
    return ScorecardComparison(
        metric,
        current,
        prior,
        units,
        basis,
        change=_change(context.semantics, metric, (current, prior), units),
    )


def _channel_rows(context: _CardContext) -> tuple[ScorecardChannelRow, ...]:
    network = context.network
    label = context.network_label
    window = context.labels.attribution_window_days
    spend_current = context.answers.get(("channel", _SPEND, "current")).rows
    spend_prior = (
        context.answers.get(("channel", _SPEND, "prior")).rows
        if context.answers.prior_period is not None
        else None
    )
    rows: list[ScorecardChannelRow] = []
    # Rows follow the governed media taxonomy's channel order, as the approved Scorecard
    # mockups draw them (Amazon DSP, Sponsored Products, Sponsored Brands, Sponsored Display);
    # a channel the taxonomy doesn't list for this platform sorts after, by key.
    governed = context.labels.channels_by_platform.get(network.platform_key, ())

    def taxonomy_order(key: _Key) -> tuple[int, _Key]:
        channel = key[2]
        return (governed.index(channel) if channel in governed else len(governed), key)

    for key in sorted(spend_current, key=taxonomy_order):
        ecosystem, platform, channel, marketplace = key
        if (ecosystem, platform, marketplace) != (
            network.ecosystem_key,
            network.platform_key,
            network.marketplace_key,
        ):
            continue
        current = spend_current[key] or Decimal(0)
        prior = None if spend_prior is None else spend_prior.get(key, Decimal(0))
        spend = (current, prior)
        rows.append(
            ScorecardChannelRow(
                channel,
                context.labels.channels.get(channel, channel),
                ScorecardComparison(
                    _SPEND,
                    current,
                    prior,
                    Units(UnitKind.CURRENCY, context.currency),
                    ScorecardBasis(ScorecardBasisKind.SPEND, label),
                    change=_change(
                        context.semantics,
                        _SPEND,
                        (current, prior),
                        Units(UnitKind.CURRENCY, context.currency),
                    ),
                ),
                _figure(
                    context,
                    _REVENUE,
                    channel=channel,
                    basis=ScorecardBasis(ScorecardBasisKind.ATTRIBUTED, label, window),
                    spend=spend,
                ),
                _figure(
                    context,
                    _ROAS,
                    channel=channel,
                    basis=ScorecardBasis(ScorecardBasisKind.ATTRIBUTED, label, window),
                    spend=spend,
                ),
                _figure(
                    context,
                    _ROAS_HALO,
                    channel=channel,
                    basis=ScorecardBasis(ScorecardBasisKind.ATTRIBUTED_INCL_HALO, label, window),
                    spend=spend,
                ),
            )
        )
    return tuple(rows)


def _headline_prior(
    context: _CardContext, model: str, network_key: tuple[str, str, str]
) -> Decimal | None:
    """Return the headline's equal-prior value, or none when it is partial or withheld."""
    prior_period = context.answers.prior_period
    if prior_period is None:
        return None
    prior = context.answers.get(("headline", _DEDUPLICATED, "prior"), model)
    if not grouped_answer_complete(prior, prior_period):
        return None
    return prior.rows.get(network_key, Decimal(0))


def _ranking_value(
    ranking: GroupedAnswer, key: tuple[str, ...], row: ScorecardChannelRow
) -> Decimal | None:
    """Return a channel's deduplicated ROAS in a complete answer, by the rule `_figure` applies.

    A complete answer with no row for a channel that spent is a true zero. With no spend the
    ratio is undefined, so it is absent rather than a ranked 0x (G9A-1).
    """
    value = ranking.rows.get(key)
    if value is None and key not in ranking.rows:
        return Decimal(0) if row.spend.current else None
    return value


def _card(context: _CardContext) -> NetworkCard | None:
    network = context.network
    label = context.network_label
    models = context.labels.models
    window = context.labels.attribution_window_days
    channel_rows = _channel_rows(context)
    if not channel_rows:
        return None
    current, prior = network_spend_total(channel_rows, context.currency)
    network_key = (network.ecosystem_key, network.platform_key, network.marketplace_key)
    headline_values: list[ScorecardModelValue] = []
    reasons: set[str] = set()
    ranking_values: dict[str, dict[str, Decimal | None]] = {
        row.channel_key: {} for row in channel_rows
    }
    for model_key, model_label in models:
        headline = context.answers.get(("headline", _DEDUPLICATED, "current"), model_key)
        ranking = context.answers.get(("ranking", _DEDUPLICATED, "current"), model_key)
        period = context.answers.period
        complete = grouped_answer_complete(headline, period) and grouped_answer_complete(
            ranking, period
        )
        if not complete:
            # Withheld by Amazon's threshold, or not arrived: two different reasons.
            reasons.add(
                DEDUPLICATED_WITHHELD_REASON
                if headline.suppressed or ranking.suppressed
                else DEDUPLICATED_MISSING_REASON
            )
        # A complete answer with no row for this network or channel is a true zero.
        value = headline.rows.get(network_key, Decimal(0)) if complete else None
        prior_value = _headline_prior(context, model_key, network_key) if complete else None
        headline_values.append(
            ScorecardModelValue(
                model_key,
                model_label,
                value,
                prior=prior_value,
                change=_change(
                    context.semantics, _DEDUPLICATED, (value, prior_value), Units(UnitKind.RATIO)
                ),
            )
        )
        for row in channel_rows:
            ranking_values[row.channel_key][model_key] = (
                _ranking_value(ranking, (*network_key[:2], row.channel_key, network_key[2]), row)
                if complete
                else None
            )
    absence = (
        None
        if not reasons
        else DEDUPLICATED_WITHHELD_REASON
        if DEDUPLICATED_WITHHELD_REASON in reasons
        else DEDUPLICATED_MISSING_REASON
    )
    spent = {row.channel_key: bool(row.spend.current) for row in channel_rows}
    unranked = {
        channel
        for channel, values in ranking_values.items()
        if any(value is None for value in values.values())
    }
    # A complete delivery with a channel whose ratio has no spend to divide: the ranking can't
    # be stated (G9A-1), for the reason the channel table gives that channel's ROAS. A channel
    # that spent but whose deduplicated ROAS is served null is a hole in the deduplicated
    # figure the ranking ranks, not in the channel table's attributed figures (which still
    # render), so it takes the deduplicated reason (SC9A-GATE-005).
    # Every model credits each deduplicated purchase once, so complete headlines that differ by
    # model are malformed input (the rotating dev feed of 2026-10-22): the ranking names that
    # rather than conclude a leader from it, and the bake still publishes the rest of the page.
    disagree = absence is None and len({item.value for item in headline_values}) > 1
    ranking_absence = absence or (
        DEDUPLICATED_MODELS_DISAGREE_REASON
        if disagree
        else None
        if not unranked
        else CHANNEL_RETURN_NO_SPEND_REASON
        if not any(spent[channel] for channel in unranked)
        else DEDUPLICATED_MISSING_REASON
    )
    delivering = {row.channel_key for row in channel_rows}
    absent = tuple(
        ScorecardAbsentChannel(channel, context.labels.channels.get(channel, channel))
        for channel in context.labels.channels_by_platform.get(network.platform_key, ())
        if channel not in delivering
    )
    return NetworkCard(
        network=network,
        network_label=label,
        currency_code=context.currency,
        marketplace_label=scorecard_marketplace_label(
            network.marketplace_key,
            context.labels.marketplace_labels.get(network.marketplace_key, network.marketplace_key),
        ),
        network_spend=ScorecardComparison(
            _SPEND,
            current,
            prior,
            Units(UnitKind.CURRENCY, context.currency),
            ScorecardBasis(ScorecardBasisKind.NETWORK_SPEND, label),
            change=_change(
                context.semantics,
                _SPEND,
                (current, prior),
                Units(UnitKind.CURRENCY, context.currency),
            ),
        ),
        headline=ScorecardHeadline(
            values=tuple(headline_values),
            default_model_key=models[0][0],
            basis=tuple(
                ScorecardBasis(ScorecardBasisKind.DEDUPLICATED, label, window, model_label)
                for _, model_label in models
            ),
            absence_reason=absence,
        ),
        ranking=scorecard_ranking(
            tuple(
                (
                    row.channel_key,
                    row.channel_label,
                    row.spend.current or Decimal(0),
                    ranking_values[row.channel_key],
                )
                for row in channel_rows
            ),
            models,
            ranking_absence,
        ),
        channel_rows=channel_rows,
        absent_channels=absent,
    )


def _tacos_rows(
    answers: _Answers, cards: tuple[NetworkCard, ...], semantics: ChangeSemantics
) -> tuple[ScorecardTacosRow, ...]:
    """TACoS per ecosystem in this marketplace; a prior over a partial window isn't compared."""
    current = answers.get(("tacos", _TACOS, "current"))
    prior_period = answers.prior_period
    prior = (
        answers.get(("tacos", _TACOS, "prior"))
        if prior_period is not None
        and grouped_answer_complete(answers.get(("tacos", _TACOS, "prior")), prior_period)
        else None
    )
    rows: list[ScorecardTacosRow] = []
    for ecosystem, marketplace in sorted(
        {(card.network.ecosystem_key, card.network.marketplace_key) for card in cards}
    ):
        label = next(
            card.network_label
            for card in cards
            if (card.network.ecosystem_key, card.network.marketplace_key)
            == (ecosystem, marketplace)
        )
        basis = ScorecardBasis(ScorecardBasisKind.MIXED, label)
        value = current.rows.get((ecosystem, marketplace))
        if value is None or not grouped_answer_complete(current, answers.period):
            figure = ScorecardComparison.absent(
                _TACOS, basis, _tacos_absence(current, answers.period)
            )
        else:
            prior_value = None if prior is None else prior.rows.get((ecosystem, marketplace))
            figure = ScorecardComparison(
                _TACOS,
                value,
                prior_value,
                Units(UnitKind.RATIO),
                basis,
                change=_change(semantics, _TACOS, (value, prior_value), Units(UnitKind.RATIO)),
            )
        rows.append(ScorecardTacosRow(ecosystem, marketplace, figure))
    return tuple(rows)


def _material(
    contract: InsightContract,
    candidates: tuple[ScorecardFindingCandidate, ...],
    period: DateRange,
) -> tuple[ScorecardFinding, ...]:
    """Apply the contract's own materiality to each finding, at the window's horizon."""
    role = horizon_role_for(period.days)
    found: list[ScorecardFinding] = []
    for candidate in candidates:
        (bound,) = predicate_findings(
            (candidate.finding,),
            candidate.kind,
            InsightEvidenceInput.NETWORK_CHANNEL_COMPARISONS,
            source=FindingSource(
                InsightSlotId.NETWORK_SCORECARD, OperandBasis.CHANNEL_WINDOW, candidate.metric_id
            ),
            priors=None if candidate.prior is None else {candidate.finding.name: candidate.prior},
        )
        found.append(
            ScorecardFinding(
                candidate.finding_id,
                candidate.network,
                candidate.channel_key,
                candidate.finding.value,
                finding_is_material(contract.predicate.materiality, bound, role),
            )
        )
    return tuple(found)


_COVERAGE_ROLE_CARD = "Network card"
_COVERAGE_ROLE_CHANNELS = "By channel"
_COVERAGE_ROLE_RETAIL = "Ad spend and retail sales"
_COVERAGE_RECOVERY = "It shows by itself when the data arrives."


def _tacos_fact(row: ScorecardTacosRow, parsed: GroupedAnswer, period: DateRange) -> CoverageFact:
    """TACoS coverage: its missing Vendor Central days, or the reason it isn't shown at all."""
    slot = f"tacos:{row.ecosystem_key}:{row.marketplace_key}"
    if row.tacos.current is not None:
        return CoverageFact(slot, TACOS_LABEL, _COVERAGE_ROLE_RETAIL, CoverageKind.RATIO)
    reason = row.tacos.absence_reason or TACOS_NEEDS_VENDOR
    missing = tuple(
        hole.start + timedelta(days=offset)
        for hole in parsed.holes
        for offset in range(hole.days)
        if period.start <= hole.start + timedelta(days=offset) <= period.end
    )
    return CoverageFact(
        slot,
        TACOS_LABEL,
        _COVERAGE_ROLE_RETAIL,
        CoverageKind.RATIO,
        missing_dates=missing,
        unavailable=(reason, _COVERAGE_RECOVERY),
        unsent=not missing,
    )


def scorecard_evidence_status(  # noqa: PLR0913, PLR0917 -- the bake's window and its parts
    period: DateRange,
    prior: DateRange | None,
    cards: tuple[NetworkCard, ...],
    tacos: tuple[ScorecardTacosRow, ...],
    answers: tuple[_Answers, ...],
) -> EvidenceStatus:
    """Write the ADR-119 coverage block of a complete bake.

    A bake measures channel figures only over a window covered end to end, so those are
    complete; the headline is unavailable when the deduplicated delivery is withheld or late,
    and TACoS names the Vendor Central days that didn't arrive.
    """
    facts: list[CoverageFact] = []
    roles = ("current", "prior") if prior is not None else ("current",)
    for card in cards:
        key = card_id(card.network)
        for role in roles:
            facts.append(CoverageFact(f"spend:{key}", SPEND_LABEL, _COVERAGE_ROLE_CARD, role=role))
        headline = card.headline
        facts.append(
            CoverageFact(
                f"headline:{key}",
                HEADLINE_LABEL,
                _COVERAGE_ROLE_CARD,
                CoverageKind.RATIO,
                unavailable=None
                if headline.absence_reason is None
                else (headline.absence_reason, _COVERAGE_RECOVERY),
                unsent=headline.absence_reason is not None,
            )
        )
        for role in roles:
            facts.append(
                CoverageFact(
                    f"channels:{key}",
                    "Channel spend and ROAS",
                    _COVERAGE_ROLE_CHANNELS,
                    CoverageKind.RATIO,
                    role=role,
                    entity_word="channels",
                )
            )
    by_market = {market.marketplace: market for market in answers}
    for row in tacos:
        parsed = by_market[row.marketplace_key].get(("tacos", _TACOS, "current"))
        facts.append(_tacos_fact(row, parsed, period))
    return write_evidence_status(period, tuple(facts))


@dataclass(frozen=True, slots=True)
class ScorecardArtifactRenderer:
    """Execute and reduce governed Scorecard evidence without page or session custody."""

    catalog: Catalog
    contracts: tuple[DataContract, ...]
    evidence: PresentationEvidenceInspectionPort
    tooling: MeasurementTooling
    labels: ScorecardLabels
    insight_contract: InsightContract
    clock: Callable[[], datetime]
    semantics: ChangeSemantics
    """The compared figures' governed change semantics (ADR-121)."""
    alert_rules: tuple[AlertRule, ...] = ()
    """The governed alert rules "Also changed" lists findings by (ADR-108, validated)."""

    def __post_init__(self) -> None:
        if self.insight_contract.id is not InsightSlotId.NETWORK_SCORECARD:
            raise ValueError("the Scorecard renders the network-scorecard insight contract.")

    def render_scorecard(self, request: ScorecardRenderInput) -> ScorecardRenderResult:
        """Measure the cohort, reduce it per network, and bake the deterministic insight."""
        batch = scorecard_measurement_batch(
            ScorecardMeasurementBatchRequest(
                catalog=self.catalog,
                contracts=self.contracts,
                models=tuple(key for key, _ in self.labels.models),
                period=request.period,
                prior_period=request.prior_period,
                computed_at=self.clock(),
                marketplaces=self.labels.marketplaces,
            )
        )
        prepared = batch.prepared
        inspection = self.evidence.inspect(
            PresentationEvidenceInspectionRequest(
                plans=tuple(
                    PresentationEvidenceInspectionPlan(plan.role, plan.logical)
                    for plan in prepared.plans
                ),
                contracts=prepared.contracts,
                now=prepared.now,
            )
        )
        executed = execute_presentation_batch(
            prepared,
            inspection,
            MeasurementExecutionTooling(
                queries=self.tooling.queries,
                query_batch_max_concurrency=self.tooling.query_batch_max_concurrency,
            ),
        )
        measured = tuple(
            sorted(
                {
                    publication.publication_id.value
                    for slot in inspection.slots
                    if slot.bound is not None
                    for publication in slot.bound.all_publications
                }
            )
        )
        if not measured:
            return ScorecardRenderResult(None, ())
        parsed: dict[str, dict[_AnswerLabel, GroupedAnswer]] = {
            market: {} for market in self.labels.marketplaces
        }
        for answer, label in zip(executed.slots, batch.slots, strict=True):
            market, section, metric, model, role = label
            grains = {
                "channel": _CHANNEL_GRAINS,
                "headline": _NETWORK_GRAINS,
                "ranking": _CHANNEL_GRAINS,
                "tacos": _TACOS_GRAINS,
            }[section]
            window = request.period if role == "current" else request.prior_period
            item = parse_grouped_answer(answer, cast("DateRange", window), grains)
            if item is None or any(key[-1] != market for key in item.rows):
                # Truncated, the wrong grain, or a row outside the pinned marketplace.
                return ScorecardRenderResult(None, ())
            parsed[market][(section, metric, model, role)] = item
        answers = tuple(
            _Answers(market, by_label, request.period, request.prior_period)
            for market, by_label in parsed.items()
        )
        artifact = self._artifact(answers, measured, request)
        return ScorecardRenderResult(artifact, measured if artifact is not None else ())

    def _artifact(
        self,
        answers: tuple[_Answers, ...],
        measured: tuple[str, ...],
        request: ScorecardRenderInput,
    ) -> NetworkScorecardArtifact | None:
        period, prior = answers[0].period, answers[0].prior_period
        windows = {"current": period, "prior": prior}
        if not all(
            grouped_answer_complete(item, cast("DateRange", windows[label[3]]))
            for market in answers
            for label, item in market.by_label.items()
            if label[0] == "channel"
        ):
            # Channel delivery or attributed conversions are partial or withheld in some
            # marketplace: no bake, rather than a Scorecard missing that marketplace.
            return None
        revision = PresetArtifactRevision.from_publication_identities(
            measured, render_format=SCORECARD_PRESET_RENDER_FORMAT
        )
        silence = self.insight_contract.silence_condition
        try:
            built: list[NetworkCard] = []
            tacos: list[ScorecardTacosRow] = []
            for market in answers:
                cards = self._market_cards(market)
                if cards is None:
                    return None
                built.extend(cards)
                if cards:
                    tacos.extend(_tacos_rows(market, cards, self.semantics))
            if not built:
                return NetworkScorecardArtifact(
                    outcome=ScorecardOutcome.EMPTY,
                    period=period,
                    prior_period=prior,
                    cards=(),
                    tacos_rows=(),
                    cross_network_spend=(),
                    findings=(),
                    insights=(
                        network_scorecard_slot(
                            NetworkScorecardSlotRequest((), revision.value, silence)
                        ),
                    ),
                    preset_label=request.preset_label,
                    period_label=request.period_label,
                )
            cards = tuple(built)
            candidates = network_scorecard_finding_candidates(cards)
            findings = _material(self.insight_contract, candidates, period)
            material = tuple(
                candidate
                for candidate, finding in zip(candidates, findings, strict=True)
                if finding.material
            )
            narrated = scorecard_narrated(material)
            return NetworkScorecardArtifact(
                outcome=ScorecardOutcome.COMPLETE,
                period=period,
                prior_period=prior,
                cards=cards,
                tacos_rows=tuple(tacos),
                cross_network_spend=cross_network_spend_rows(cards),
                findings=findings,
                insights=(
                    network_scorecard_slot(
                        NetworkScorecardSlotRequest(material, revision.value, silence)
                    ),
                ),
                preset_label=request.preset_label,
                period_label=request.period_label,
                evidence_status=scorecard_evidence_status(
                    period, prior, cards, tuple(tacos), answers
                ),
                insight_cards=scorecard_insight_cards(narrated, cards, prior),
                also_changed=also_changed_rows(material, self.alert_rules, narrated, period, prior),
            )
        except ValueError as error:
            # An artifact invariant refused what the reduction built: report it, then bake
            # nothing, so the break alarms instead of reading as "nothing to bake".
            report_error(
                error,
                Report(operation=_OPERATION, correlation_id=_CORRELATION_ID, logger=LOGGER),
            )
            return None

    def _market_cards(self, answers: _Answers) -> tuple[NetworkCard, ...] | None:
        """One marketplace's cards in its own answers' currency, or none when it has no units."""
        spend = answers.get(("channel", _SPEND, "current"))
        if not spend.rows:
            return ()
        units = spend.units
        if units is None or units.kind is not UnitKind.CURRENCY or units.currency_code is None:
            return None
        cards: list[NetworkCard] = []
        for ecosystem, platform, marketplace in sorted(
            {(key[0], key[1], key[3]) for key in spend.rows}
        ):
            card = _card(
                _CardContext(
                    answers,
                    self.labels,
                    NetworkKey(ecosystem, platform, marketplace),
                    self.labels.ecosystems.get(ecosystem, ecosystem),
                    units.currency_code,
                    self.semantics,
                )
            )
            if card is not None:
                cards.append(card)
        return tuple(cards)


@dataclass(frozen=True, slots=True)
class RetainedScorecardPresetArtifactSource:
    """Compile one declared Scorecard preset variant from retained truth only.

    Rolling presets bake with their equal prior; calendar presets bake the latest complete
    period on daily coverage, with no comparison (temporal transport v2, UI slice 9a).
    """

    coverage: SourceCoveragePort
    coverage_requirements: tuple[SourceCoverageRequirement, ...]
    temporal_policy: PresentationTemporalPolicy
    compiler: ScorecardCompiler
    pointers: PresetArtifactPointerStorePort
    clock: Callable[[], datetime]
    page: PresetArtifactPage = field(default_factory=lambda: PresetArtifactPage(SCORECARD_PAGE_ID))

    def __post_init__(self) -> None:
        if not self.coverage_requirements:
            raise ValueError("scorecard preset baking requires declared source coverage.")

    def publication_for(
        self, variant: PresetArtifactVariant
    ) -> PresetArtifactPublicationRequest | SourceUnchanged | None:
        """Return a complete retained-materializer publication or no bakeable artifact."""
        windows = self._resolved_windows(variant)
        if windows is None:
            return None
        window, prior, preset, period_label = windows
        rendered = self.compiler.render_scorecard(
            ScorecardRenderInput(window, prior, temporal_preset_label_v2(preset), period_label)
        )
        if rendered.artifact is None:
            return None
        revision = PresetArtifactRevision.from_publication_identities(
            rendered.publication_identities, render_format=SCORECARD_PRESET_RENDER_FORMAT
        )
        current = self.pointers.current(self.page, variant)
        if current is not None and current.revision == revision:
            return SourceUnchanged(revision)
        body = PresetArtifactBody(
            self.page,
            variant,
            revision,
            canonical_preset_artifact_json(network_scorecard_artifact_payload(rendered.artifact)),
        )
        pointer = PresetArtifactPointer(
            self.page,
            variant,
            revision,
            body.digest,
            1 if current is None else current.forward_token + 1,
            None if current is None else current.revision,
            None,
            resolved_window=window,
            comparison_window=prior,
        )
        return PresetArtifactPublicationRequest(
            body,
            PresetArtifactPointerAdvanceRequest(
                pointer,
                None if current is None else self.clock() + SUPERSEDED_HISTORY_RETENTION,
            ),
            None,
        )

    def _resolved_windows(  # noqa: PLR0911 -- each unresolvable preset bakes nothing
        self, variant: PresetArtifactVariant
    ) -> tuple[DateRange, DateRange | None, TemporalPresetId, str | None] | None:
        """Resolve a declared preset: a rolling one with its equal prior, a calendar one alone.

        A rolling preset bakes only with an available equal prior, as before. A calendar
        preset resolves on daily coverage alone, with no prior (Scorecard LLD "Window
        policy"); with no complete period it bakes nothing, and the page serves the
        no-complete-period state from the same resolution.
        """
        preset_id = next(
            (
                candidate
                for candidate in self.temporal_policy.candidate_presets
                if candidate.value == variant.value
            ),
            None,
        )
        if preset_id is None:
            return None
        source_coverage = self.coverage.source_coverage(
            SourceCoverageRequest(self.coverage_requirements)
        )
        if is_calendar_preset(preset_id):
            calendar = scorecard_calendar_period(
                preset_id, self.clock().date(), source_coverage.bounds.intervals
            )
            if calendar.period is None:
                return None
            period = calendar.period
            return DateRange(period.start, period.end), None, preset_id, period.label
        try:
            resolved = resolve_temporal(
                TemporalResolutionRequest(
                    policy=self.temporal_policy,
                    coverage_intervals=source_coverage.bounds.intervals,
                    selection=TemporalSelection(
                        kind=TemporalSelectionKind.PRESET, preset_id=preset_id
                    ),
                    comparison_requested=True,
                )
            )
        except ValueError:
            return None
        if not isinstance(resolved, TemporalResolutionAvailable):
            return None
        control = resolved.control
        if control.comparison.status is not TemporalComparisonStatus.AVAILABLE:
            return None
        window = control.resolved.window
        prior = DateRange(
            window.start - timedelta(days=window.days), window.start - timedelta(days=1)
        )
        return window, prior, preset_id, None


def scorecard_coverage_requirements(catalog: Catalog) -> tuple[SourceCoverageRequirement, ...]:
    """Spend and attributed conversions gate a bake; TACoS degrades to its typed absence."""
    requirements: list[SourceCoverageRequirement] = []
    for name in _COVERAGE_METRICS:
        declared = catalog.metric(name)
        if declared is None:
            raise RuntimeError(f"scorecard metric {name} is unavailable.")
        requirement = metric_coverage_requirement(catalog, declared)
        if requirement not in requirements:
            requirements.append(requirement)
    return tuple(requirements)


def _scorecard_policy(schemas: PresentationPageSchemaSet) -> PresentationTemporalPolicy | None:
    page = next((item for item in schemas.pages if item.page_id == SCORECARD_PAGE_ID), None)
    return None if page is None else page.temporal_policy


def _network_scorecard_contract(root: Path) -> InsightContract:
    for _, contract in load_insight_contracts(root):
        if contract.id is InsightSlotId.NETWORK_SCORECARD:
            return contract
    raise RuntimeError("the network-scorecard insight contract is undeclared.")


def scorecard_bake_plan_count(root: Path) -> int:
    """Count one rolling-preset Scorecard bake's plans, its largest batch (ADR-070 point 8).

    A rolling preset measures every compared figure twice (window and equal prior); a
    calendar preset measures each once, so the rolling batch bounds every preset.
    """
    catalog = load_catalog(root)
    labels = scorecard_labels(root, catalog)
    window = DateRange(date(2026, 1, 8), date(2026, 1, 14))
    batch = scorecard_measurement_batch(
        ScorecardMeasurementBatchRequest(
            catalog=catalog,
            contracts=tuple(contract for _, contract in load_contracts(root)),
            models=tuple(key for key, _ in labels.models),
            period=window,
            prior_period=DateRange(date(2026, 1, 1), date(2026, 1, 7)),
            computed_at=datetime(2026, 1, 15, tzinfo=UTC),
            marketplaces=labels.marketplaces,
        )
    )
    return len(batch.prepared.plans)


def scorecard_alert_rules(root: Path) -> tuple[AlertRule, ...]:
    """Load the governed alert rules and refuse any that contradicts its contract."""
    taxonomy = load_media_taxonomy(root)
    channels = frozenset(
        term.key.value for term in taxonomy.terms if isinstance(term.key, ChannelKey)
    )
    return validate_alert_rules(
        load_alert_rules(root),
        tuple(contract for _, contract in load_insight_contracts(root)),
        {"network-scorecard": SCORECARD_ALERT_FINDINGS},
        channels,
    )


def retained_scorecard_preset_artifact_source(  # noqa: PLR0913, PLR0917 -- the closed signature
    settings: PresetArtifactBakeSettings,
    pointers: PresetArtifactPointerStorePort,
    queries: PeriodSeriesQueryPort,
    current: BakeSourceReads,
) -> RetainedScorecardPresetArtifactSource | None:
    """Compose the retained Scorecard source, or none while its page or configuration is absent.

    The closed factory signature (A5.4): the invocation's admitted, captured `queries` and its
    one pinnable CURRENT reader, whose clock every renderer reads. Fails closed, logged once,
    without the retained-truth configuration or while the v2 page entry isn't declared.
    """
    if not all(
        (
            settings.semantic_publications_table,
            settings.semantic_v2_bucket,
            settings.athena_workgroup,
            settings.semantic_v2_current_database,
            settings.athena_result_reuse_max_age_minutes > 0,
            settings.athena_query_batch_max_concurrency > 0,
        )
    ):
        return source_closed(SCORECARD_PAGE_ID, "settings_incomplete")
    root = contracts_root()
    policy = _scorecard_policy(load_v2_page_schemas(root))
    if policy is None:
        return source_closed(SCORECARD_PAGE_ID, "page_entry_absent")
    contracts = tuple(contract for _, contract in load_contracts(root))
    source_packs = load_source_packs(root)
    bridge = load_commerce_identity_bridge(root)
    horizons = scope_horizon_policy(current)
    reader = cast("CurrentPublicationInspectionPort", current)
    metric_catalog = load_catalog(root)
    compiler = ScorecardArtifactRenderer(
        catalog=metric_catalog,
        contracts=contracts,
        evidence=PerPackPresentationEvidenceInspection(reader, source_packs, bridge),
        tooling=MeasurementTooling(
            queries=queries,
            publications=PublicationSelector(reader, source_packs, bridge, horizons=horizons),
            query_batch_max_concurrency=settings.athena_query_batch_max_concurrency,
        ),
        labels=scorecard_labels(root, metric_catalog),
        insight_contract=_network_scorecard_contract(root),
        clock=current.clock,
        semantics=scorecard_change_semantics(metric_catalog),
        alert_rules=scorecard_alert_rules(root),
    )
    return RetainedScorecardPresetArtifactSource(
        coverage=PageWindowCatalog(reader, source_packs, contracts, bridge, horizons),
        coverage_requirements=scorecard_coverage_requirements(metric_catalog),
        temporal_policy=policy,
        compiler=compiler,
        pointers=pointers,
        clock=current.clock,
    )
