# SPDX-License-Identifier: MIT-0
# ruff: noqa: ARG002, D102, PLR0911, PLR0913, PLR0917, RUF005
"""Capability-free governed Campaign artifact rendering shared by pages and precompute."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import cast

from adapters.campaign_measurement import (
    DSP_QUALITY_METRICS,
    DSP_QUALITY_NOT_MEASURED_FOR_CHANNEL,
    VIEWABILITY_OPERANDS,
    CampaignArtifactMeasurementService,
    CampaignDeliveryMeasurementBatch,
    CampaignDetailMeasurementBatch,
    CampaignDetailMeasurementInput,
    CampaignMeasurementSelection,
    CampaignPortfolioMeasurementBatch,
    CampaignPortfolioMeasurementInput,
)
from adapters.inbound.tool_handlers.measurement import (
    MeasurementExecutionTooling,
    MeasurementTooling,
    PreparedMeasurementBatch,
    PresentationMeasurementAnswerSlot,
    PresentationMeasurementExecutionResult,
    execute_presentation_batch,
)
from domain.catalog.freshness import FreshnessStatus
from domain.measurement.planning import MEASURE_ALIAS
from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.analytical_pages import PresentationDimensionLabel
from domain.presentation.campaign_attribution import (
    PURCHASE_SERIES_GRAIN,
    SUMMARY_GRAIN,
    CampaignAttributionDetailArtifact,
    CampaignAttributionDetailRequest,
    CampaignDelivery,
    CampaignDeliveryAnswer,
    CampaignDeliverySlot,
    CampaignDeliveryValue,
    CampaignDspQuality,
    CampaignEvidenceCondition,
    CampaignEvidenceSlot,
    CampaignEvidenceStatusCondition,
    CampaignMetricValue,
    CampaignPurchasePoint,
    CampaignPurchaseSeriesBuildRequest,
    CampaignRecoveryAction,
    CampaignSelectionCapability,
    CampaignSlotEvidence,
    CampaignSummaryBuildRequest,
    build_campaign_attribution_detail,
    build_campaign_prior_comparison,
    build_campaign_purchase_series,
    build_campaign_summary,
    campaign_delivery_accepted_statuses,
    campaign_delivery_comparison,
    derive_campaign_cpc,
    safe_campaign_signature,
)
from domain.presentation.campaign_portfolio import (
    CAMPAIGN_PORTFOLIO_CHANNEL_GRAIN,
    CAMPAIGN_PORTFOLIO_ROW_GRAIN,
    CAMPAIGN_PORTFOLIO_TOTAL_GRAIN,
    DSP_CHANNEL_KEY,
    SPONSORED_VIEWABILITY_REASON,
    CampaignChannelDeliveryRow,
    CampaignChannelFilter,
    CampaignChannelTotalsRow,
    CampaignListEvidence,
    CampaignMultiChannelView,
    CampaignNotAddedTile,
    CampaignPortfolioArtifact,
    CampaignPortfolioComparison,
    CampaignPortfolioMetric,
    CampaignPortfolioMetricValue,
    CampaignPortfolioReductionRequest,
    CampaignPortfolioRow,
    CampaignPortfolioSort,
    CampaignPortfolioTrend,
    CampaignViewabilityCell,
    build_campaign_portfolio,
    campaign_channel_filter_options,
    campaign_portfolio_comparison,
    campaign_portfolio_evidence_status,
    channels_for_filter,
    multi_channel_insights_line,
    reduce_campaign_portfolio,
)
from domain.presentation.campaigns import (
    CampaignPresentation,
    CampaignPresentationRequest,
    present_campaign,
)
from domain.presentation.change import ChangeSemantics
from domain.presentation.compared_metrics import presentation_change_semantics, presentation_labels
from domain.semantics.analytical_resolution import EntityAlias
from domain.semantics.compatibility import MeasurementBasis
from domain.semantics.identity import CampaignKey
from domain.shared_kernel.provenance import Suppression
from domain.shared_kernel.values import DateRange
from ports.outbound.publication_selection import (
    PresentationEvidenceInspectionPlan,
    PresentationEvidenceInspectionPort,
    PresentationEvidenceInspectionRequest,
)


@dataclass(frozen=True, slots=True)
class CampaignArtifactRenderInput:
    """Closed server-selected cohort, comparator, and non-issued row references."""

    selections: tuple[CampaignMeasurementSelection, ...]
    period: DateRange
    prior_period: DateRange
    sort: CampaignPortfolioSort
    capabilities: tuple[CampaignSelectionCapability, ...]
    channel_order: tuple[str, ...] = ()
    """Governed channel keys in taxonomy order, the order channel filters are offered in."""

    def __post_init__(self) -> None:
        if not self.selections or len(self.capabilities) != len(self.selections):
            raise ValueError("campaign rendering requires one selection and capability per row.")
        if len({item.campaign for item in self.selections}) != len(self.selections):
            raise ValueError("campaign rendering selections must be unique.")


@dataclass(frozen=True, slots=True)
class CampaignArtifactRenderResult:
    """A complete portfolio plus, per selection, either a complete detail pair or none.

    A campaign whose conversion evidence is typed absent (covered, no rows) has no complete
    detail; it stays in the portfolio at zero and simply contributes no detail artifact. No
    partially reduced artifact ever crosses this boundary.
    """

    portfolio: CampaignPortfolioArtifact
    details: tuple[CampaignAttributionDetailArtifact | None, ...]
    deliveries: tuple[CampaignDelivery | None, ...]

    def __post_init__(self) -> None:
        if len(self.details) != len(self.deliveries):
            raise ValueError("campaign artifact details and deliveries must align.")
        if any(
            (detail is None) != (delivery is None)
            for detail, delivery in zip(self.details, self.deliveries, strict=True)
        ):
            raise ValueError(
                "a campaign detail and its delivery are complete together or absent together."
            )


@dataclass(frozen=True, slots=True)
class CampaignPortfolioRenderResult:
    """A portfolio-only reduction and the exact publication identities that produced it."""

    portfolio: CampaignPortfolioArtifact | None
    publication_identities: tuple[str, ...]
    siblings: tuple[CampaignPortfolioArtifact, ...] = ()
    """Every channel filter sibling after All channels, in option order (ADR-127)."""

    def __post_init__(self) -> None:
        if self.portfolio is not None and not self.publication_identities:
            raise ValueError("a rendered portfolio must name the publications it consumed.")
        if self.siblings and self.portfolio is None:
            raise ValueError("filter siblings render only beside All channels.")


@dataclass(frozen=True, slots=True)
class CampaignArtifactRenderer:
    """Execute and reduce governed Campaign evidence without page/session custody."""

    measurements: CampaignArtifactMeasurementService
    evidence: PresentationEvidenceInspectionPort
    tooling: MeasurementTooling

    @property
    def change_semantics(self) -> ChangeSemantics:
        """The compared roster's governed change semantics, from the catalog this renders with."""
        return presentation_change_semantics(self.measurements.dependencies.catalog)

    def render_portfolio(
        self, request: CampaignArtifactRenderInput
    ) -> CampaignPortfolioRenderResult:
        """Measure and reduce only the portfolio surface over the full supplied cohort.

        The result carries the deduplicated, sorted publication identities bound by evidence
        inspection so a background baker can derive its immutable artifact revision from the
        exact retained truth it consumed.
        """
        portfolio_batch = self.measurements.portfolio_batch(
            CampaignPortfolioMeasurementInput(
                request.selections, request.period, request.prior_period, request.sort
            )
        )
        prepared = portfolio_batch.prepared
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
        identities = tuple(
            sorted(
                {
                    publication.publication_id.value
                    for slot in inspection.slots
                    if slot.bound is not None
                    for publication in slot.bound.all_publications
                }
            )
        )
        if not identities:
            return CampaignPortfolioRenderResult(None, ())
        filters = self.reduce_filters(portfolio_batch, executed, request)
        if filters is None:
            return CampaignPortfolioRenderResult(None, identities)
        return CampaignPortfolioRenderResult(filters[0], identities, filters[1:])

    def render(self, request: CampaignArtifactRenderInput) -> CampaignArtifactRenderResult | None:
        """Return only an all-complete portfolio/detail/delivery rendering.

        All independent operations are combined before execution, preserving the single
        configured bounded fan-out and exact plan order supplied by the measurement seam.
        """
        portfolio_batch = self.measurements.portfolio_batch(
            CampaignPortfolioMeasurementInput(
                request.selections, request.period, request.prior_period, request.sort
            )
        )
        detail_inputs = tuple(
            CampaignDetailMeasurementInput(item, request.period, request.prior_period)
            for item in request.selections
        )
        detail_batches = tuple(self.measurements.detail_batch(item) for item in detail_inputs)
        delivery_batches = tuple(self.measurements.delivery_batch(item) for item in detail_inputs)
        prepared = _combined_batch(
            (portfolio_batch.prepared,)
            + tuple(item.prepared for item in detail_batches)
            + tuple(item.prepared for item in delivery_batches)
        )
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
        groups = _split_executed(
            executed,
            (portfolio_batch.prepared,)
            + tuple(item.prepared for item in detail_batches)
            + tuple(item.prepared for item in delivery_batches),
        )
        portfolio = self.reduce_portfolio(
            portfolio_batch,
            groups[0],
            request.selections,
            request.capabilities,
            request.period,
            request.prior_period,
            request.sort,
        )
        if portfolio is None:
            return None
        details = tuple(
            self.reduce_detail(selection, batch, result, request.period, request.prior_period)
            for selection, batch, result in zip(
                request.selections, detail_batches, groups[1 : 1 + len(detail_batches)], strict=True
            )
        )
        deliveries = tuple(
            self.reduce_delivery(selection, batch, result, request.period, request.prior_period)
            for selection, batch, result in zip(
                request.selections, delivery_batches, groups[1 + len(detail_batches) :], strict=True
            )
        )
        # A campaign is command-center ready only when both halves reduced; otherwise it keeps
        # its portfolio row and yields no detail rather than a half-rendered one.
        ready = tuple(
            detail is not None and delivery is not None
            for detail, delivery in zip(details, deliveries, strict=True)
        )
        return CampaignArtifactRenderResult(
            portfolio,
            tuple(detail if ok else None for detail, ok in zip(details, ready, strict=True)),
            tuple(delivery if ok else None for delivery, ok in zip(deliveries, ready, strict=True)),
        )

    def reduce_portfolio(
        self,
        batch: CampaignPortfolioMeasurementBatch,
        executed: PresentationMeasurementExecutionResult,
        selections: tuple[CampaignMeasurementSelection, ...],
        capabilities: tuple[CampaignSelectionCapability, ...],
        period: DateRange,
        prior_period: DateRange,
        sort: CampaignPortfolioSort,
    ) -> CampaignPortfolioArtifact | None:
        """Reduce the unfiltered portfolio the request-time and precompute paths serve."""
        parsed = _parse_portfolio(batch, executed, selections)
        if parsed is None:
            return None
        return self._reduce_subset(
            _SubsetRequest(parsed, selections, capabilities, period, prior_period, sort)
        )

    def reduce_filters(
        self,
        batch: CampaignPortfolioMeasurementBatch,
        executed: PresentationMeasurementExecutionResult,
        request: CampaignArtifactRenderInput,
    ) -> tuple[CampaignPortfolioArtifact, ...] | None:
        """Render every channel filter sibling from the one measured batch (ADR-127).

        All channels first, then each declared option in order. Each sibling reduces only its
        own campaigns' operands; a multi-channel sibling carries no attributed total. Any
        sibling that can't be reduced refuses the whole bake, never a partial filter set.
        """
        parsed = _parse_portfolio(batch, executed, request.selections)
        if parsed is None:
            return None
        return self.filters_from_parsed(parsed, request)

    def filters_from_parsed(
        self, parsed: ParsedPortfolio, request: CampaignArtifactRenderInput
    ) -> tuple[CampaignPortfolioArtifact, ...] | None:
        """Reduce every channel filter sibling from one parsed batch, All channels first."""
        channels = _present_channels(request.selections, request.channel_order)
        options = campaign_channel_filter_options(channels)
        keys = tuple(key for key, _ in channels)
        artifacts: list[CampaignPortfolioArtifact] = []
        for option in options:
            spans = frozenset(channels_for_filter(option.filter_id, keys))
            members = tuple(
                index
                for index, item in enumerate(request.selections)
                if item.channel.value in spans
            )
            artifact = self._reduce_subset(
                _SubsetRequest(
                    parsed,
                    tuple(request.selections[index] for index in members),
                    tuple(request.capabilities[index] for index in members),
                    request.period,
                    request.prior_period,
                    request.sort,
                    CampaignChannelFilter(option.filter_id, options),
                    tuple(item for item in channels if item[0] in spans),
                )
            )
            if artifact is None:
                return None
            artifacts.append(artifact)
        return tuple(artifacts)

    def _reduce_subset(self, request: _SubsetRequest) -> CampaignPortfolioArtifact | None:
        answers = request.parsed.answers
        ratios = request.parsed.ratios
        selections, period, prior_period = request.selections, request.period, request.prior_period
        multi_channel = request.channel_filter is not None and request.channel_filter.multi_channel
        operands = ("ad_spend", "attributed_revenue_14d", "purchases", "impressions")
        identities = frozenset((item.campaign.value, item.marketplace.value) for item in selections)
        semantics = self.change_semantics
        try:
            reduction = reduce_campaign_portfolio(
                CampaignPortfolioReductionRequest(
                    identities,
                    {metric: answers[("portfolio", metric, "current")][0] for metric in operands},
                    {metric: answers[("portfolio", metric, "prior")][0] for metric in operands},
                    {metric: answers[("trend", metric, "current")][0] for metric in operands},
                    period,
                )
            )
        except ValueError:
            return None
        rows: list[CampaignPortfolioRow] = []
        for selection, capability in zip(selections, request.capabilities, strict=True):
            identity = (selection.campaign.value, selection.marketplace.value)
            comparisons: list[CampaignPortfolioComparison] = []
            qualifiers: set[str] = set()
            for metric, public_metric in (
                ("ad_spend", CampaignPortfolioMetric.SPEND),
                ("roas_attributed_14d", CampaignPortfolioMetric.ATTRIBUTED_ROAS),
                ("purchases", CampaignPortfolioMetric.PURCHASES),
                ("ctr", CampaignPortfolioMetric.CTR),
            ):
                current_rows, current_units, current_qualifiers = answers[
                    ("row", metric, "current")
                ]
                prior_rows, prior_units, prior_qualifiers = answers[("row", metric, "prior")]
                # Absence from a fully covered additive answer is zero, never incompatible scope.
                # A covered ratio answer omits exactly the identities whose denominator is zero,
                # so a ratio's absence is typed absent, never 0 (ADR-087).
                absent: Decimal | int | None = None if metric in ratios else 0
                current_value, prior_value = (
                    current_rows.get(identity, absent),
                    prior_rows.get(identity, absent),
                )
                current = CampaignPortfolioMetricValue(
                    current_value,
                    current_units,
                    MeasurementBasis.AD_ATTRIBUTED,
                    period,
                    CAMPAIGN_PORTFOLIO_ROW_GRAIN,
                )
                prior = CampaignPortfolioMetricValue(
                    prior_value,
                    prior_units,
                    MeasurementBasis.AD_ATTRIBUTED,
                    prior_period,
                    CAMPAIGN_PORTFOLIO_ROW_GRAIN,
                )
                comparisons.append(
                    campaign_portfolio_comparison(public_metric, current, prior, semantics)
                )
                qualifiers.update((*current_qualifiers, *prior_qualifiers))
            # A row carries no spend spark: the table draws none, and a campaign's daily spend
            # has its home in detail (ADR-127).
            rows.append(
                CampaignPortfolioRow(
                    selection.signature, capability, tuple(comparisons), tuple(sorted(qualifiers))
                )
            )
        headline = (
            (
                (CampaignPortfolioMetric.SPEND, "ad_spend"),
                (CampaignPortfolioMetric.IMPRESSIONS, "impressions"),
            )
            if multi_channel
            else (
                (CampaignPortfolioMetric.SPEND, "ad_spend"),
                (CampaignPortfolioMetric.PURCHASES, "purchases"),
                (CampaignPortfolioMetric.IMPRESSIONS, "impressions"),
                (CampaignPortfolioMetric.PORTFOLIO_ROAS, None),
            )
        )
        totals: list[CampaignPortfolioComparison] = []
        for public_metric, operand in headline:
            units = (
                Units(UnitKind.RATIO)
                if operand is None
                else answers[("portfolio", operand, "current")][1]
            )
            current_value, prior_value = (
                reduction.current[public_metric],
                reduction.prior[public_metric],
            )
            totals.append(
                campaign_portfolio_comparison(
                    public_metric,
                    CampaignPortfolioMetricValue(
                        current_value,
                        units,
                        MeasurementBasis.AD_ATTRIBUTED,
                        period,
                        CAMPAIGN_PORTFOLIO_TOTAL_GRAIN,
                    ),
                    CampaignPortfolioMetricValue(
                        prior_value,
                        units,
                        MeasurementBasis.AD_ATTRIBUTED,
                        prior_period,
                        CAMPAIGN_PORTFOLIO_TOTAL_GRAIN,
                    ),
                    semantics,
                )
            )
        spend_units = answers[("trend", "ad_spend", "current")][1]
        trend = (
            CampaignPortfolioTrend(
                period,
                tuple(
                    replace(point, portfolio_roas=None, purchases=None)
                    for point in reduction.trend_points
                ),
                spend_units,
                None,
                None,
                MeasurementBasis.AD_ATTRIBUTED,
            )
            if multi_channel
            else CampaignPortfolioTrend(
                period,
                reduction.trend_points,
                spend_units,
                Units(UnitKind.RATIO),
                answers[("trend", "purchases", "current")][1],
                MeasurementBasis.AD_ATTRIBUTED,
            )
        )
        view: CampaignMultiChannelView | None = None
        viewability_missing: ViewabilityMissing | None = None
        if multi_channel and rows and request.channel_filter is not None:
            view, viewability_missing = _channel_view(
                _ChannelViewRequest(
                    request.parsed,
                    selections,
                    request.channels,
                    request.channel_filter.option.label,
                    period,
                    prior_period,
                    semantics,
                )
            )
        labels = presentation_labels(self.measurements.dependencies.catalog)
        return build_campaign_portfolio(
            period=period,
            sort=request.sort,
            rows=tuple(rows),
            totals=tuple(totals),
            trend=trend,
            evidence=(CampaignListEvidence("Governed campaign catalog", period, (period,)),),
            authorized_count=len(selections),
            # Every answer above covers its window end to end (holes are refused), so each
            # figure's coverage is complete (ADR-119); only the DSP viewability source may
            # leave days out, and the block names them.
            evidence_status=campaign_portfolio_evidence_status(
                period,
                labels,
                multi_channel=view is not None,
                viewability_missing=None
                if viewability_missing is None
                else viewability_missing.current,
                viewability_prior_missing=()
                if viewability_missing is None
                else viewability_missing.prior,
            )
            if rows
            else None,
            channel_filter=request.channel_filter,
            channel_view=view,
        )

    def reduce_detail(
        self,
        selection: CampaignMeasurementSelection,
        batch: CampaignDetailMeasurementBatch,
        executed: PresentationMeasurementExecutionResult,
        period: DateRange,
        prior_period: DateRange,
    ) -> CampaignAttributionDetailArtifact | None:
        slots = (
            CampaignEvidenceSlot.PURCHASES,
            CampaignEvidenceSlot.PRIOR_PURCHASES,
            CampaignEvidenceSlot.ATTRIBUTED_ROAS,
            CampaignEvidenceSlot.PRIOR_ATTRIBUTED_ROAS,
            CampaignEvidenceSlot.SPEND,
            CampaignEvidenceSlot.PRIOR_SPEND,
            CampaignEvidenceSlot.PURCHASE_SERIES,
        )
        evidence: dict[CampaignEvidenceSlot, CampaignSlotEvidence] = {}
        values: dict[
            CampaignEvidenceSlot, CampaignMetricValue | tuple[CampaignPurchasePoint, ...]
        ] = {}
        for slot, answer in zip(slots, executed.slots, strict=True):
            selected_period = (
                prior_period
                if slot
                in {
                    CampaignEvidenceSlot.PRIOR_PURCHASES,
                    CampaignEvidenceSlot.PRIOR_ATTRIBUTED_ROAS,
                    CampaignEvidenceSlot.PRIOR_SPEND,
                }
                else period
            )
            parsed = _detail_answer(selection, slot, answer, selected_period)
            if parsed is None:
                return None
            evidence[slot], values[slot] = parsed
        summary = build_campaign_summary(
            CampaignSummaryBuildRequest(
                period,
                cast("CampaignMetricValue", values[CampaignEvidenceSlot.PURCHASES]),
                cast("CampaignMetricValue", values[CampaignEvidenceSlot.ATTRIBUTED_ROAS]),
                cast("CampaignMetricValue", values[CampaignEvidenceSlot.SPEND]),
            )
        )
        prior = build_campaign_summary(
            CampaignSummaryBuildRequest(
                prior_period,
                cast("CampaignMetricValue", values[CampaignEvidenceSlot.PRIOR_PURCHASES]),
                cast("CampaignMetricValue", values[CampaignEvidenceSlot.PRIOR_ATTRIBUTED_ROAS]),
                cast("CampaignMetricValue", values[CampaignEvidenceSlot.PRIOR_SPEND]),
            )
        )
        series_evidence = evidence[CampaignEvidenceSlot.PURCHASE_SERIES]
        series = build_campaign_purchase_series(
            CampaignPurchaseSeriesBuildRequest(
                period,
                cast(
                    "tuple[CampaignPurchasePoint, ...]",
                    values[CampaignEvidenceSlot.PURCHASE_SERIES],
                ),
                cast("Units", series_evidence.units),
                cast("MeasurementBasis", series_evidence.basis),
            )
        )
        try:
            comparison = build_campaign_prior_comparison(summary, prior, self.change_semantics)
        except ValueError:
            return None
        detail = build_campaign_attribution_detail(
            CampaignAttributionDetailRequest(
                selection.signature,
                period,
                summary,
                series,
                tuple(
                    evidence[slot]
                    for slot in (
                        CampaignEvidenceSlot.PURCHASES,
                        CampaignEvidenceSlot.ATTRIBUTED_ROAS,
                        CampaignEvidenceSlot.SPEND,
                        CampaignEvidenceSlot.PURCHASE_SERIES,
                        CampaignEvidenceSlot.PRIOR_PURCHASES,
                        CampaignEvidenceSlot.PRIOR_ATTRIBUTED_ROAS,
                        CampaignEvidenceSlot.PRIOR_SPEND,
                    )
                ),
                prior_period,
                comparison,
            )
        )
        return detail if detail.outcome.value == "selected" else None

    def reduce_delivery(
        self,
        selection: CampaignMeasurementSelection,
        batch: CampaignDeliveryMeasurementBatch,
        executed: PresentationMeasurementExecutionResult,
        period: DateRange,
        prior_period: DateRange,
    ) -> CampaignDelivery | None:
        values: dict[
            tuple[CampaignDeliverySlot | None, str],
            CampaignMetricValue | tuple[tuple[date, Decimal | int], ...],
        ] = {}
        # DSP media quality is measured only for Amazon DSP delivery: another channel's batch
        # requests none, so every operand the batch didn't request is typed absent, never zero.
        requested = {role for slot, role, _ in batch.slots if slot is None}
        quality_absent: set[str] = {
            metric for metric in DSP_QUALITY_METRICS if metric not in requested
        }
        for answer, (slot, role, expected_period) in zip(executed.slots, batch.slots, strict=True):
            parsed = _delivery_answer(selection, slot, role, answer, expected_period)
            if parsed is None:
                # A DSP campaign's complete, unsuppressed, empty viewability answer is typed
                # absence too, never zero.
                if (
                    slot is None
                    and role in DSP_QUALITY_METRICS
                    and _covered_empty(answer, expected_period)
                ):
                    quality_absent.add(role)
                    continue
                return None
            values[(slot, role)] = parsed
        if quality_absent and quality_absent != set(DSP_QUALITY_METRICS):
            return None
        for role in ("current", "prior", "daily_current", "daily_prior"):
            spend = values[(CampaignDeliverySlot.AD_SPEND, role)]
            derived = derive_campaign_cpc(
                CampaignDeliveryAnswer(spend, CampaignEvidenceStatusCondition.AVAILABLE),
                CampaignDeliveryAnswer(
                    values[(CampaignDeliverySlot.CLICKS, role)],
                    CampaignEvidenceStatusCondition.AVAILABLE,
                ),
            )
            if derived.value is None:
                return None
            if isinstance(derived.value, CampaignMetricValue):
                values[(CampaignDeliverySlot.CPC, role)] = derived.value
            else:
                # Densified as every daily answer is: a day with no clicks displays zero.
                points = dict(derived.value)
                values[(CampaignDeliverySlot.CPC, role)] = tuple(
                    (day, points.get(day, 0))
                    for day, _ in cast("tuple[tuple[date, Decimal | int], ...]", spend)
                )
        summary = tuple(
            CampaignDeliveryValue(slot, cast("CampaignMetricValue", values[(slot, "current")]))
            for slot in CampaignDeliverySlot
        )
        comparison = campaign_delivery_comparison(
            summary,
            tuple(
                cast("CampaignMetricValue", values[(slot, "prior")])
                for slot in CampaignDeliverySlot
            ),
            self.change_semantics,
        )
        daily_current = {
            slot: cast("tuple[tuple[date, Decimal | int], ...]", values[(slot, "daily_current")])
            for slot in CampaignDeliverySlot
        }
        daily_prior = {
            slot: cast("tuple[tuple[date, Decimal | int], ...]", values[(slot, "daily_prior")])
            for slot in CampaignDeliverySlot
        }
        summaries = {value.slot: cast("CampaignMetricValue", value.value) for value in summary}
        comparisons = {value.slot: cast("CampaignMetricValue", value.value) for value in comparison}
        if any(
            sum((Decimal(value) for _, value in daily_series[slot]), start=Decimal(0))
            != Decimal(aggregates_by_slot[slot].value)
            for daily_series, aggregates_by_slot in (
                (daily_current, summaries),
                (daily_prior, comparisons),
            )
            for slot in (
                CampaignDeliverySlot.IMPRESSIONS,
                CampaignDeliverySlot.CLICKS,
            )
        ):
            return None
        rows = tuple(
            {
                "current": {
                    slot.value: next(
                        value
                        for day, value in daily_current[slot]
                        if day == period.start + timedelta(days=offset)
                    )
                    for slot in CampaignDeliverySlot
                },
                "date": (period.start + timedelta(days=offset)).isoformat(),
                "prior": {
                    slot.value: next(
                        value
                        for day, value in daily_prior[slot]
                        if day == prior_period.start + timedelta(days=offset)
                    )
                    for slot in CampaignDeliverySlot
                },
            }
            for offset in range(period.days)
        )
        typed_rows = tuple(cast("dict[str, object]", row) for row in rows)
        if quality_absent:
            # Core delivery is complete; only DSP media quality is typed absent, which the
            # contract names `qualified` rather than `complete`.
            dsp_quality = CampaignDspQuality(
                "unavailable", None, None, None, None, DSP_QUALITY_NOT_MEASURED_FOR_CHANNEL
            )
            return CampaignDelivery(
                "qualified",
                summary,
                prior_period,
                comparison,
                typed_rows,
                typed_rows,
                dsp_quality,
                campaign_delivery_accepted_statuses(),
                CampaignRecoveryAction.CHANGE_PERIOD.value,
            )
        quality = {
            metric: cast("CampaignMetricValue", values[(None, metric)])
            for metric in DSP_QUALITY_METRICS
        }
        dsp_quality = CampaignDspQuality(
            "available",
            quality["measurable_impressions"],
            quality["viewable_impressions"],
            quality["viewable_impressions_estimated"],
            quality["viewable_rate"],
            None,
        )
        return CampaignDelivery(
            "complete",
            summary,
            prior_period,
            comparison,
            typed_rows,
            typed_rows,
            dsp_quality,
            campaign_delivery_accepted_statuses(),
            CampaignRecoveryAction.CHANGE_PERIOD.value,
        )


def retained_capabilities(
    selections: tuple[CampaignMeasurementSelection, ...],
) -> tuple[CampaignSelectionCapability, ...]:
    """Produce non-redeemable retained placeholders; issuance remains in the page handler."""
    expiry = datetime.max.replace(tzinfo=UTC)
    return tuple(
        CampaignSelectionCapability(f"retained:{item.campaign.value}", expiry)
        for item in selections
    )


def retained_campaign_selections(
    aliases: tuple[EntityAlias, ...],
    window: DateRange,
    labels: tuple[PresentationDimensionLabel, ...],
) -> tuple[CampaignMeasurementSelection, ...]:
    """Group retained aliases by canonical campaign and project safe measurement selections.

    Signatures use the shared governed-label derivation, so a baked custody row is
    byte-identical to the signature redemption re-derives from the same catalog history.
    """
    grouped: dict[CampaignKey, list[EntityAlias]] = {}
    for alias in aliases:
        if not isinstance(alias.key, CampaignKey):
            raise ValueError("retained campaign aliases contain a non-campaign key.")
        grouped.setdefault(alias.key, []).append(alias)
    campaigns = tuple(
        present_campaign(CampaignPresentationRequest(tuple(items), window))
        for _, items in sorted(grouped.items(), key=lambda item: item[0].value)
    )
    return tuple(_retained_selection(campaign, window, labels) for campaign in campaigns)


def _retained_selection(
    campaign: CampaignPresentation,
    window: DateRange,
    labels: tuple[PresentationDimensionLabel, ...],
) -> CampaignMeasurementSelection:
    """Reject an incomplete catalog context before any measurement is submitted."""
    if campaign.channel is None or campaign.marketplace is None:
        raise ValueError("retained campaign context is incomplete.")
    return CampaignMeasurementSelection(
        campaign=campaign.campaign,
        channel=campaign.channel,
        marketplace=campaign.marketplace,
        signature=safe_campaign_signature(campaign, window, labels),
    )


_EXPECTED_KINDS = {
    "ad_spend": UnitKind.CURRENCY,
    "roas_attributed_14d": UnitKind.RATIO,
    "purchases": UnitKind.COUNT,
    "ctr": UnitKind.RATIO,
    "impressions": UnitKind.COUNT,
    "attributed_revenue_14d": UnitKind.CURRENCY,
    "clicks": UnitKind.COUNT,
}
_QUOTIENT_STEP = Decimal("1e-12")
"""Delivery ratios keep twelve places, so a change is computed from (effectively) unrounded
operands and never from the two-decimal display (ADR-121)."""
_NOT_ADDED_LABELS = (
    (CampaignPortfolioMetric.PURCHASES, "Attributed purchases"),
    (CampaignPortfolioMetric.PORTFOLIO_ROAS, "ROAS"),
)
_VIEWABILITY_UNAVAILABLE = "Not available: Amazon DSP viewability hasn't arrived for this window"


@dataclass(frozen=True, slots=True)
class Viewability:
    """One window's DSP viewability operand by campaign, and the days its source hasn't sent."""

    rows: dict[tuple[str, ...], Decimal | int]
    missing: tuple[date, ...]


@dataclass(frozen=True, slots=True)
class ParsedPortfolio:
    """Every portfolio answer parsed once, keyed by (section, metric, role)."""

    answers: dict[tuple[str, str, str], _PortfolioAnswer]
    ratios: frozenset[str]
    viewability: dict[tuple[str, str], Viewability | None]


@dataclass(frozen=True, slots=True)
class _SubsetRequest:
    parsed: ParsedPortfolio
    selections: tuple[CampaignMeasurementSelection, ...]
    capabilities: tuple[CampaignSelectionCapability, ...]
    period: DateRange
    prior_period: DateRange
    sort: CampaignPortfolioSort
    channel_filter: CampaignChannelFilter | None = None
    channels: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class _ChannelViewRequest:
    parsed: ParsedPortfolio
    selections: tuple[CampaignMeasurementSelection, ...]
    channels: tuple[tuple[str, str], ...]
    option_label: str
    period: DateRange
    prior_period: DateRange
    semantics: ChangeSemantics


def _parse_portfolio(
    batch: CampaignPortfolioMeasurementBatch,
    executed: PresentationMeasurementExecutionResult,
    selections: tuple[CampaignMeasurementSelection, ...],
) -> ParsedPortfolio | None:
    """Parse every answer once; viewability alone may be partly reported or absent."""
    answers: dict[tuple[str, str, str], _PortfolioAnswer] = {}
    viewability: dict[tuple[str, str], Viewability | None] = {}
    # Which metrics are typed absent when a row is missing (ratios, and exact distinct
    # counts), read from the metric dictionary the plan carries, never from a metric name
    # (ADR-087, widened by ADR-099).
    ratios = frozenset(
        metric
        for answer, (_, metric, _, _) in zip(executed.slots, batch.slots, strict=True)
        if answer.logical.metric.absence_is_typed
    )
    for answer, (section, metric, role, expected_period) in zip(
        executed.slots, batch.slots, strict=True
    ):
        if metric in VIEWABILITY_OPERANDS:
            viewability[(metric, role)] = _viewability_answer(answer, expected_period)
            continue
        group = (
            ("campaign_key", "marketplace_key")
            if section in {"row", "portfolio", "delivery"}
            else ("date_key", "campaign_key", "marketplace_key")
        )
        parsed = _portfolio_answer(answer, expected_period, group, _EXPECTED_KINDS[metric])
        if parsed is None:
            return None
        key = (section, metric, role)
        existing = answers.get(key)
        if existing is None:
            answers[key] = parsed
            continue
        merged = _merge_portfolio_answer(existing, parsed)
        if merged is None:
            return None
        answers[key] = merged
    if len({item.marketplace for item in selections}) != 1:
        return None
    operands = ("ad_spend", "attributed_revenue_14d", "purchases", "impressions")
    if any(
        answers[("portfolio", metric, "current")][1] != answers[("portfolio", metric, "prior")][1]
        for metric in operands
    ):
        return None
    return ParsedPortfolio(answers, ratios, viewability)


def _present_channels(
    selections: tuple[CampaignMeasurementSelection, ...], order: tuple[str, ...]
) -> tuple[tuple[str, str], ...]:
    """Each governed channel the cohort has campaigns in, with its label, in taxonomy order."""
    labels: dict[str, str] = {}
    for item in selections:
        labels.setdefault(item.channel.value, item.signature.channel)
    rank = {key: index for index, key in enumerate(order)}
    return tuple(sorted(labels.items(), key=lambda item: (rank.get(item[0], len(rank)), item[0])))


def _sum(
    values: dict[tuple[str, ...], Decimal | int], identities: frozenset[tuple[str, str]]
) -> Decimal:
    """Sum an additive campaign-grain operand; a covered campaign with no row delivered nothing."""
    return sum((Decimal(values.get(identity, 0)) for identity in identities), Decimal())


def _quotient(numerator: Decimal, denominator: Decimal, scale: int = 1) -> Decimal | None:
    """One channel's ratio from its own summed operands; undefined over zero (ADR-087)."""
    if not denominator:
        return None
    return (numerator * scale / denominator).quantize(_QUOTIENT_STEP, rounding=ROUND_HALF_EVEN)


def _channel_value(
    value: Decimal | None, units: Units, period: DateRange
) -> CampaignPortfolioMetricValue:
    return CampaignPortfolioMetricValue(
        value, units, MeasurementBasis.AD_ATTRIBUTED, period, CAMPAIGN_PORTFOLIO_CHANNEL_GRAIN
    )


def _channel_view(
    request: _ChannelViewRequest,
) -> tuple[CampaignMultiChannelView, ViewabilityMissing | None]:
    """By channel and Delivery per channel for one multi-channel option (ADR-127 d3, d4)."""
    answers = request.parsed.answers
    period, prior, semantics = request.period, request.prior_period, request.semantics
    spend_units = answers[("portfolio", "ad_spend", "current")][1]
    impression_units = answers[("portfolio", "impressions", "current")][1]
    click_units = answers[("delivery", "clicks", "current")][1]
    ratio = Units(UnitKind.RATIO)

    def compare(
        metric: CampaignPortfolioMetric,
        units: Units,
        current: Decimal | None,
        previous: Decimal | None,
    ) -> CampaignPortfolioComparison:
        return campaign_portfolio_comparison(
            metric,
            _channel_value(current, units, period),
            _channel_value(previous, units, prior),
            semantics,
        )

    viewability = any(key == DSP_CHANNEL_KEY for key, _ in request.channels)
    viewability_missing: ViewabilityMissing | None = None
    by_channel: list[CampaignChannelTotalsRow] = []
    delivery: list[CampaignChannelDeliveryRow] = []
    for key, label in request.channels:
        ids = frozenset(
            (item.campaign.value, item.marketplace.value)
            for item in request.selections
            if item.channel.value == key
        )
        sums = {
            (metric, role): _sum(answers[(section, metric, role)][0], ids)
            for section, metric in (
                ("portfolio", "ad_spend"),
                ("portfolio", "impressions"),
                ("delivery", "clicks"),
            )
            for role in ("current", "prior")
        }
        spend, impressions, clicks = (
            (sums[(metric, "current")], sums[(metric, "prior")])
            for metric in ("ad_spend", "impressions", "clicks")
        )
        by_channel.append(
            CampaignChannelTotalsRow(
                key,
                label,
                compare(CampaignPortfolioMetric.SPEND, spend_units, *spend),
                compare(CampaignPortfolioMetric.IMPRESSIONS, impression_units, *impressions),
            )
        )
        cell: CampaignViewabilityCell | None = None
        if viewability:
            if key == DSP_CHANNEL_KEY:
                cell, viewability_missing = _viewability_cell(request, ids)
            else:
                cell = CampaignViewabilityCell(None, None, SPONSORED_VIEWABILITY_REASON)
        delivery.append(
            CampaignChannelDeliveryRow(
                key,
                label,
                compare(CampaignPortfolioMetric.CLICKS, click_units, *clicks),
                compare(
                    CampaignPortfolioMetric.CTR,
                    ratio,
                    _quotient(clicks[0], impressions[0]),
                    _quotient(clicks[1], impressions[1]),
                ),
                compare(
                    CampaignPortfolioMetric.CPC,
                    spend_units,
                    _quotient(spend[0], clicks[0]),
                    _quotient(spend[1], clicks[1]),
                ),
                compare(
                    CampaignPortfolioMetric.CPM,
                    spend_units,
                    _quotient(spend[0], impressions[0], 1000),
                    _quotient(spend[1], impressions[1], 1000),
                ),
                cell,
            )
        )
    view = CampaignMultiChannelView(
        tuple(by_channel),
        tuple(delivery),
        viewability,
        tuple(CampaignNotAddedTile(metric, label) for metric, label in _NOT_ADDED_LABELS),
        multi_channel_insights_line(request.option_label),
    )
    return view, viewability_missing


@dataclass(frozen=True, slots=True)
class ViewabilityMissing:
    """The days the DSP views source hasn't sent, in this window and in the prior one."""

    current: tuple[date, ...]
    prior: tuple[date, ...] = ()


def _viewability_cell(
    request: _ChannelViewRequest, ids: frozenset[tuple[str, str]]
) -> tuple[CampaignViewabilityCell, ViewabilityMissing]:
    """Serve the DSP row's viewable rate over the days its source sent.

    The change shows only when both windows are whole. A day missing from either window
    withholds it, and the coverage block names that day (ADR-118 decision 5).
    """
    period, prior_period = request.period, request.prior_period
    every_day = tuple(period.start + timedelta(days=offset) for offset in range(period.days))
    every_prior_day = tuple(
        prior_period.start + timedelta(days=offset) for offset in range(prior_period.days)
    )
    sides = {
        role: (
            request.parsed.viewability.get(("viewable_impressions", role)),
            request.parsed.viewability.get(("measurable_impressions", role)),
        )
        for role in ("current", "prior")
    }
    viewable, measurable = sides["current"]
    if viewable is None or measurable is None:
        return CampaignViewabilityCell(None, None, _VIEWABILITY_UNAVAILABLE), ViewabilityMissing(
            every_day
        )
    missing = tuple(sorted({*viewable.missing, *measurable.missing}))
    if len(missing) == period.days:
        return CampaignViewabilityCell(None, None, _VIEWABILITY_UNAVAILABLE), ViewabilityMissing(
            every_day
        )
    current = _quotient(_sum(viewable.rows, ids), _sum(measurable.rows, ids))
    prior_viewable, prior_measurable = sides["prior"]
    # A prior side the source never sent misses every day; a partly sent one is read over the
    # days that arrived, a missing day left out of both parts.
    prior_missing = (
        every_prior_day
        if prior_viewable is None or prior_measurable is None
        else tuple(sorted({*prior_viewable.missing, *prior_measurable.missing}))
    )
    prior = (
        None
        if prior_viewable is None
        or prior_measurable is None
        or len(prior_missing) == prior_period.days
        else _quotient(_sum(prior_viewable.rows, ids), _sum(prior_measurable.rows, ids))
    )
    ratio = Units(UnitKind.RATIO)
    current_value = _channel_value(current, ratio, period)
    prior_value = _channel_value(prior, ratio, prior_period)
    coverage = None if not missing else f"{period.days - len(missing)} of {period.days} days"
    if (missing or prior_missing) and current is not None and prior is not None:
        # A partly reported window withholds its change (ADR-118 decision 5).
        comparison = CampaignPortfolioComparison(
            CampaignPortfolioMetric.VIEWABLE_RATE,
            current_value,
            prior_value,
            None,
            None,
            None,
            withheld=True,
        )
    else:
        comparison = campaign_portfolio_comparison(
            CampaignPortfolioMetric.VIEWABLE_RATE, current_value, prior_value, request.semantics
        )
    return CampaignViewabilityCell(comparison, coverage, None), ViewabilityMissing(
        missing, prior_missing
    )


def _viewability_answer(
    answer: PresentationMeasurementAnswerSlot, period: DateRange
) -> Viewability | None:
    """Parse a DSP viewability operand that may cover only some of its window's days."""
    shaped = answer.answer
    if shaped is None:
        return None
    metadata, data = shaped.metadata, shaped.data
    group = ("campaign_key", "marketplace_key")
    if (
        metadata.requested_coverage_intervals != (period,)
        or metadata.suppression.rows_suppressed
        or metadata.truncated
        or metadata.freshness_status is FreshnessStatus.NEVER_LOADED
        or (data.values and data.grain.dimensions != group)
        or metadata.measurement_basis != (MeasurementBasis.AD_ATTRIBUTED,)
        or metadata.row_count != len(data.values)
    ):
        return None
    covered = {
        interval.start + timedelta(days=offset)
        for interval in metadata.actual_coverage_intervals
        for offset in range(interval.days)
    }
    days = tuple(period.start + timedelta(days=offset) for offset in range(period.days))
    rows: dict[tuple[str, ...], Decimal | int] = {}
    for raw in (cast("dict[str, object]", row) for row in data.values):
        key = tuple(raw.get(item) for item in group)
        value = raw.get(MEASURE_ALIAS)
        if (
            set(raw) != {*group, MEASURE_ALIAS}
            or any(not isinstance(item, str) or not item.strip() for item in key)
            or isinstance(value, bool)
            or not isinstance(value, (int, Decimal))
            or value < 0
            or key in rows
        ):
            return None
        rows[cast("tuple[str, ...]", key)] = value
    return Viewability(rows, tuple(day for day in days if day not in covered))


def _combined_batch(batches: tuple[PreparedMeasurementBatch, ...]) -> PreparedMeasurementBatch:
    first = batches[0]
    contracts = tuple(dict.fromkeys(contract for batch in batches for contract in batch.contracts))
    return PreparedMeasurementBatch(
        tuple(operation for batch in batches for operation in batch.operations),
        contracts,
        first.now,
    )


def _covered_empty(answer: PresentationMeasurementAnswerSlot, period: DateRange) -> bool:
    """Whether an answer is complete, unsuppressed, untruncated, and simply has no rows."""
    shaped = answer.answer
    if shaped is None:
        return False
    metadata = shaped.metadata
    return (
        not shaped.data.values
        and metadata.row_count == 0
        and metadata.requested_coverage_intervals == (period,)
        and metadata.actual_coverage_intervals == (period,)
        and not metadata.coverage_holes
        and not metadata.suppression.rows_suppressed
        and not metadata.truncated
        and metadata.freshness_status is not FreshnessStatus.NEVER_LOADED
    )


def _split_executed(
    executed: PresentationMeasurementExecutionResult, batches: tuple[PreparedMeasurementBatch, ...]
) -> tuple[PresentationMeasurementExecutionResult, ...]:
    offset = 0
    groups: list[PresentationMeasurementExecutionResult] = []
    for batch in batches:
        size = len(batch.plans)
        groups.append(
            PresentationMeasurementExecutionResult(executed.slots[offset : offset + size])
        )
        offset += size
    if offset != len(executed.slots):
        raise ValueError("campaign rendering execution is not aligned with its prepared batches.")
    return tuple(groups)


def _complete_answer(
    answer: PresentationMeasurementAnswerSlot,
    period: DateRange,
    group: tuple[str, ...],
    kind: UnitKind,
    rows: int | None = None,
) -> tuple[tuple[dict[str, object], ...], Units, tuple[str, ...]] | None:
    shaped = answer.answer
    if shaped is None:
        return None
    metadata, data = shaped.metadata, shaped.data
    units = data.units
    # A complete, unsuppressed cohort answer with no rows is a legitimate zero for a non-monetary
    # additive metric (no campaign converted); the envelope states no units for it, so the slot's
    # declared unit stands in. An exact-row detail answer and an empty monetary answer stay refused.
    if units is None and not data.values and rows is None and kind is not UnitKind.CURRENCY:
        units = Units(kind)
    if (
        metadata.requested_coverage_intervals != (period,)
        or metadata.actual_coverage_intervals != (period,)
        or metadata.coverage_holes
        or metadata.suppression.rows_suppressed
        or metadata.truncated
        or metadata.freshness_status is FreshnessStatus.NEVER_LOADED
        or data.grain.dimensions != group
        or metadata.measurement_basis != (MeasurementBasis.AD_ATTRIBUTED,)
        or units is None
        or units.kind is not kind
        or metadata.row_count != len(data.values)
        or (rows is not None and metadata.row_count != rows)
    ):
        return None
    qualifiers = tuple(
        sorted(
            {
                *metadata.qualified_dq,
                *(item.rendering for item in metadata.qualifiers),
                *(
                    ()
                    if metadata.freshness_status is FreshnessStatus.FRESH
                    else (metadata.freshness_status.value,)
                ),
            }
        )
    )
    return tuple(cast("dict[str, object]", row) for row in data.values), units, qualifiers


type _PortfolioAnswer = tuple[
    dict[tuple[str, ...], Decimal | int],
    Units,
    tuple[str, ...],
]


def _merge_portfolio_answer(
    existing: _PortfolioAnswer,
    additional: _PortfolioAnswer,
) -> _PortfolioAnswer | None:
    """Merge disjoint window chunks only when their units and qualifiers are identical."""
    existing_rows, existing_units, existing_qualifiers = existing
    rows, units, qualifiers = additional
    if (
        existing_units != units
        or existing_qualifiers != qualifiers
        or set(existing_rows).intersection(rows)
    ):
        return None
    return ({**existing_rows, **rows}, units, existing_qualifiers)


def _portfolio_answer(
    answer: PresentationMeasurementAnswerSlot,
    period: DateRange,
    group: tuple[str, ...],
    kind: UnitKind,
) -> tuple[dict[tuple[str, ...], Decimal | int], Units, tuple[str, ...]] | None:
    complete = _complete_answer(answer, period, group, kind)
    if complete is None:
        return None
    raw_rows, units, qualifiers = complete
    rows: dict[tuple[str, ...], Decimal | int] = {}
    for raw in raw_rows:
        key = tuple(raw.get(item) for item in group)
        value = raw.get(MEASURE_ALIAS)
        if (
            set(raw) != {*group, MEASURE_ALIAS}
            or any(not isinstance(item, str) or not item.strip() for item in key)
            or isinstance(value, bool)
            or not isinstance(value, (int, Decimal))
            or value < 0
            or key in rows
        ):
            return None
        rows[cast("tuple[str, ...]", key)] = value
    return rows, units, qualifiers


def _detail_answer(
    selection: CampaignMeasurementSelection,
    slot: CampaignEvidenceSlot,
    answer: PresentationMeasurementAnswerSlot,
    period: DateRange,
) -> tuple[CampaignSlotEvidence, CampaignMetricValue | tuple[CampaignPurchasePoint, ...]] | None:
    series = slot is CampaignEvidenceSlot.PURCHASE_SERIES
    group = (
        ("date_key", "campaign_key", "marketplace_key")
        if series
        else ("campaign_key", "marketplace_key")
    )
    kinds = {
        CampaignEvidenceSlot.PURCHASES: UnitKind.COUNT,
        CampaignEvidenceSlot.PRIOR_PURCHASES: UnitKind.COUNT,
        CampaignEvidenceSlot.ATTRIBUTED_ROAS: UnitKind.RATIO,
        CampaignEvidenceSlot.PRIOR_ATTRIBUTED_ROAS: UnitKind.RATIO,
        CampaignEvidenceSlot.SPEND: UnitKind.CURRENCY,
        CampaignEvidenceSlot.PRIOR_SPEND: UnitKind.CURRENCY,
        CampaignEvidenceSlot.PURCHASE_SERIES: UnitKind.COUNT,
    }
    # A daily count series is additive: a covered day with no row converted nothing, so the
    # series accepts any row count up to the period and densifies absent days to zero.
    complete = _complete_answer(answer, period, group, kinds[slot], None if series else 1)
    if complete is None:
        return None
    rows, units, qualifiers = complete
    if series and len(rows) > period.days:
        return None
    by_day: dict[date, Decimal | int] = {}
    value: Decimal | int | None = None
    for raw in rows:
        raw_value = raw.get(MEASURE_ALIAS)
        if (
            set(raw) != {*group, MEASURE_ALIAS}
            or raw.get("campaign_key") != selection.campaign.value
            or raw.get("marketplace_key") != selection.marketplace.value
            or isinstance(raw_value, bool)
            or not isinstance(raw_value, (int, Decimal))
            or raw_value < 0
        ):
            return None
        value = raw_value
        if series:
            try:
                day = date.fromisoformat(cast("str", raw.get("date_key")))
            except TypeError, ValueError:
                return None
            if day in by_day or not period.covers(DateRange(day, day)):
                return None
            by_day[day] = raw_value
    if series:
        if not by_day:
            return None
        output: CampaignMetricValue | tuple[CampaignPurchasePoint, ...] = tuple(
            CampaignPurchasePoint(
                period.start + timedelta(days=offset),
                by_day.get(period.start + timedelta(days=offset), 0),
            )
            for offset in range(period.days)
        )
        grain = PURCHASE_SERIES_GRAIN
        # The evidence describes the densified series it accompanies: one represented point per
        # covered day, so the detail contract's row/point reconciliation holds.
        represented = len(output)
    else:
        if value is None:
            return None
        output = CampaignMetricValue(value, units, MeasurementBasis.AD_ATTRIBUTED, period)
        grain = SUMMARY_GRAIN
        represented = len(rows)
    return CampaignSlotEvidence(
        slot,
        CampaignEvidenceCondition.AVAILABLE,
        period,
        (period,),
        (),
        units,
        grain,
        MeasurementBasis.AD_ATTRIBUTED,
        Suppression(False),
        represented,
        False,
        qualifiers,
    ), output


def _delivery_answer(
    selection: CampaignMeasurementSelection,
    slot: CampaignDeliverySlot | None,
    role: str,
    answer: PresentationMeasurementAnswerSlot,
    period: DateRange,
) -> CampaignMetricValue | tuple[tuple[date, Decimal | int], ...] | None:
    daily = role in {"daily_current", "daily_prior"}
    group = (
        ("date_key", "campaign_key", "marketplace_key")
        if daily
        else ("campaign_key", "marketplace_key")
    )
    metric = role if slot is None else slot.value
    kind = (
        UnitKind.COUNT
        if metric
        in {
            "impressions",
            "clicks",
            "measurable_impressions",
            "viewable_impressions",
            "viewable_impressions_estimated",
        }
        else UnitKind.RATIO
        if metric in {"ctr", "viewable_rate"}
        else UnitKind.CURRENCY
    )
    # A daily series densifies covered days without a row: an additive metric delivered nothing
    # that day, and a ratio over zero operands is displayed as zero for that day. The additive
    # daily sums are still reconciled to the period aggregates before any row is emitted.
    complete = _complete_answer(answer, period, group, kind, None if daily else 1)
    if complete is None:
        return None
    rows, units, _ = complete
    if daily and (not rows or len(rows) > period.days):
        return None
    by_day: dict[date, Decimal | int] = {}
    value: Decimal | int | None = None
    for raw in rows:
        raw_value = raw.get(MEASURE_ALIAS)
        if (
            set(raw) != {*group, MEASURE_ALIAS}
            or raw.get("campaign_key") != selection.campaign.value
            or raw.get("marketplace_key") != selection.marketplace.value
            or isinstance(raw_value, bool)
            or not isinstance(raw_value, (int, Decimal))
            or raw_value < 0
        ):
            return None
        value = raw_value
        if daily:
            try:
                day = date.fromisoformat(cast("str", raw.get("date_key")))
            except TypeError, ValueError:
                return None
            if day in by_day or not period.covers(DateRange(day, day)):
                return None
            by_day[day] = raw_value
    if daily:
        days = tuple(period.start + timedelta(days=offset) for offset in range(period.days))
        return tuple((day, by_day.get(day, 0)) for day in days)
    return (
        None
        if value is None
        else CampaignMetricValue(value, units, MeasurementBasis.AD_ATTRIBUTED, period)
    )
