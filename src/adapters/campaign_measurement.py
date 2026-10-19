# SPDX-License-Identifier: MIT-0
# ruff: noqa: PLR0913, PLR0917
"""Capability-free deterministic Campaign Portfolio and detail measurement inputs.

This adapter boundary owns only server-selected measurement inputs.  Browser page
records, opaque selection references, owners, and conversation sessions are
intentionally absent: callers provide a re-attested canonical cohort and receive
only governed batches that can be reduced into presentation artifacts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from adapters.inbound.tool_handlers.measurement import (
    DEFAULT_MAX_ROWS,
    MeasurementTool,
    MeasurementToolRequest,
    PreparedMeasurementBatch,
    attach_roster,
    prepare_batch,
)
from domain.catalog.capability import Catalog
from domain.data_contracts.contract import DataContract
from domain.presentation.campaign_attribution import (
    CampaignDeliverySlot,
    SafeCampaignDisplaySignature,
)
from domain.presentation.campaign_portfolio import DSP_CHANNEL_KEY, CampaignPortfolioSort
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.semantics.identity import CampaignKey, ChannelKey, MarketplaceKey
from domain.shared_kernel.errors import ErrorCode, InvariantError
from domain.shared_kernel.values import DateRange
from ports.outbound.publication_selection import PresentationEvidenceRole
from ports.outbound.query import MAX_ROWS_CEILING
from ports.outbound.rosters import RosterPort

ARTIFACT_MALFORMED = ErrorCode("ARTIFACT", 1)
VIEWABILITY_OPERANDS = ("viewable_impressions", "measurable_impressions")
"""The Amazon DSP viewable rate's operands, summed per channel before dividing."""
DSP_QUALITY_METRICS = (
    "measurable_impressions",
    "viewable_impressions",
    "viewable_impressions_estimated",
    "viewable_rate",
)
"""A campaign's DSP media-quality operands; all four are measured together or not at all."""
DSP_QUALITY_NOT_MEASURED_FOR_CHANNEL = "dsp_quality_not_measured_for_channel"
"""The typed absence a campaign outside Amazon DSP carries for its DSP media quality."""


def measures_dsp_quality(channel: ChannelKey) -> bool:
    """Whether a campaign's delivery measures DSP media quality: only Amazon DSP's does.

    The DSP views source stamps every row `channel_key = amazon_dsp`, so a side pinned to
    another channel binds no pack (`_binds`) and a request for it could never be answered.
    """
    return channel.value == DSP_CHANNEL_KEY


@dataclass(frozen=True, slots=True)
class CampaignMeasurementDependencies:
    """Server-owned dependencies shared by interactive and retained rendering."""

    scope: AuthorizedAnalyticalScope
    catalog: Catalog
    contracts: tuple[DataContract, ...]
    clock: Callable[[], datetime]
    rosters: RosterPort | None = None
    """The ad products or campaigns with spend in a period (plan D4, `attach_roster`).

    Every request is prepared through `attach_roster`, so a roster-graded figure reads each
    rostered key with no row "not available" here as in chat and the New customers bake. None
    leaves such a figure refused typed (`ROSTER_UNAVAILABLE`); no fixed Campaign cohort reads one.
    """


@dataclass(frozen=True, slots=True)
class CampaignMeasurementSelection:
    """A re-attested canonical Campaign identity and its safe display projection."""

    campaign: CampaignKey
    channel: ChannelKey
    marketplace: MarketplaceKey
    signature: SafeCampaignDisplaySignature


@dataclass(frozen=True, slots=True)
class CampaignPortfolioMeasurementInput:
    """The complete server-owned cohort and exact periods for one portfolio."""

    selections: tuple[CampaignMeasurementSelection, ...]
    period: DateRange
    prior_period: DateRange
    sort: CampaignPortfolioSort

    def __post_init__(self) -> None:
        identities = tuple((item.campaign, item.marketplace) for item in self.selections)
        if len(set(identities)) != len(identities):
            raise ValueError("campaign portfolio selections must be canonical-identity unique.")


@dataclass(frozen=True, slots=True)
class CampaignDetailMeasurementInput:
    """The selected canonical campaign and equal-length server-derived comparator."""

    selection: CampaignMeasurementSelection
    period: DateRange
    prior_period: DateRange

    def __post_init__(self) -> None:
        expected_end = self.period.start.fromordinal(self.period.start.toordinal() - 1)
        expected_start = expected_end.fromordinal(expected_end.toordinal() - self.period.days + 1)
        if self.prior_period != DateRange(expected_start, expected_end):
            raise ValueError("campaign detail prior period must be equal-length and preceding.")


@dataclass(frozen=True, slots=True)
class CampaignPortfolioMeasurementBatch:
    """Prepared portfolio operations plus stable role/metric labels for reduction."""

    prepared: PreparedMeasurementBatch
    slots: tuple[tuple[str, str, str, DateRange], ...]


@dataclass(frozen=True, slots=True)
class CampaignDetailMeasurementBatch:
    """Prepared attribution-detail operations in canonical evidence-slot order."""

    prepared: PreparedMeasurementBatch


@dataclass(frozen=True, slots=True)
class CampaignDeliveryMeasurementBatch:
    """Prepared V2 delivery operations in exact presentation order."""

    prepared: PreparedMeasurementBatch
    slots: tuple[tuple[CampaignDeliverySlot | None, str, DateRange], ...]


@dataclass(frozen=True, slots=True)
class CampaignArtifactMeasurementService:
    """Build fixed governed Campaign measurement cohorts without custody dependencies."""

    dependencies: CampaignMeasurementDependencies

    def portfolio_batch(
        self, request: CampaignPortfolioMeasurementInput
    ) -> CampaignPortfolioMeasurementBatch:
        """Prepare the unfiltered authorized-population portfolio cohort."""
        current, prior = request.period, request.prior_period
        population_max_rows = len(request.selections)
        measurements: list[MeasurementToolRequest] = []
        slots: list[tuple[str, str, str, DateRange]] = []
        for metric in ("ad_spend", "roas_attributed_14d", "purchases", "ctr"):
            measurements.append(
                self._compare(
                    metric,
                    current,
                    prior,
                    {},
                    max_rows=population_max_rows,
                )
            )
            slots.extend((("row", metric, "current", current), ("row", metric, "prior", prior)))
        # Delivery per channel (ADR-127 decision 4): clicks per campaign, so each channel's CTR,
        # CPC and CPM come from its own summed operands; and, when the cohort has Amazon DSP
        # campaigns, its viewability operands from the DSP views dataset.
        delivery = ["clicks"]
        if any(item.channel.value == DSP_CHANNEL_KEY for item in request.selections):
            delivery.extend(VIEWABILITY_OPERANDS)
        for metric in delivery:
            measurements.append(
                self._compare(metric, current, prior, {}, max_rows=population_max_rows)
            )
            slots.extend(
                (("delivery", metric, "current", current), ("delivery", metric, "prior", prior))
            )
        for metric in ("ad_spend", "attributed_revenue_14d", "purchases", "impressions"):
            measurements.append(
                self._compare(
                    metric,
                    current,
                    prior,
                    {},
                    max_rows=population_max_rows,
                )
            )
            slots.extend(
                (("portfolio", metric, "current", current), ("portfolio", metric, "prior", prior))
            )
            for daily in self._portfolio_daily(metric, current, len(request.selections)):
                measurements.append(daily)
                slots.append(("trend", metric, "current", daily.window))
        prepared = self._prepare(tuple(measurements), "Campaign Portfolio")
        return CampaignPortfolioMeasurementBatch(prepared, tuple(slots))

    def detail_batch(
        self, request: CampaignDetailMeasurementInput
    ) -> CampaignDetailMeasurementBatch:
        """Prepare the fixed selected-campaign attribution cohort."""
        filters = self._filters(request.selection)
        measurements = (
            self._compare("purchases", request.period, request.prior_period, filters),
            self._compare("roas_attributed_14d", request.period, request.prior_period, filters),
            self._compare("ad_spend", request.period, request.prior_period, filters),
            self._daily("purchases", request.period, filters),
        )
        return CampaignDetailMeasurementBatch(self._prepare(measurements, "Campaign detail"))

    def delivery_batch(
        self, request: CampaignDetailMeasurementInput
    ) -> CampaignDeliveryMeasurementBatch:
        """Prepare the fixed V2 delivery cohort, plus DSP quality for an Amazon DSP campaign.

        Another channel's batch carries no DSP-quality slot, as the portfolio batch requests
        viewability only for a cohort with DSP campaigns; its reduction types quality absent.
        Cost per click is not planned: each reduction derives it from the accepted ad spend and
        clicks (`derive_campaign_cpc`), because Amazon DSP writes them in separate publications.
        """
        filters = self._filters(request.selection)
        measurements: list[MeasurementToolRequest] = []
        slots: list[tuple[CampaignDeliverySlot | None, str, DateRange]] = []
        planned = tuple(
            slot for slot in CampaignDeliverySlot if slot is not CampaignDeliverySlot.CPC
        )
        for slot in planned:
            measurements.append(
                self._compare(slot.value, request.period, request.prior_period, filters)
            )
            slots.extend(((slot, "current", request.period), (slot, "prior", request.prior_period)))
        for slot in planned:
            measurements.append(self._daily(slot.value, request.period, filters))
            slots.append((slot, "daily_current", request.period))
            measurements.append(self._daily(slot.value, request.prior_period, filters))
            slots.append((slot, "daily_prior", request.prior_period))
        if measures_dsp_quality(request.selection.channel):
            for metric in DSP_QUALITY_METRICS:
                measurements.append(self._single(metric, request.period, filters))
                slots.append((None, metric, request.period))
        return CampaignDeliveryMeasurementBatch(
            self._prepare(tuple(measurements), "Campaign V2 delivery"), tuple(slots)
        )

    def _compare(
        self,
        metric: str,
        period: DateRange,
        prior: DateRange,
        filters: dict[str, str],
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> MeasurementToolRequest:
        return MeasurementToolRequest(
            tool=MeasurementTool.COMPARE_WINDOWS,
            catalog=self.dependencies.catalog,
            contracts=self.dependencies.contracts,
            metric_name=metric,
            grains=("campaign", "marketplace"),
            window=period,
            prior_window=prior,
            computed_at=self.dependencies.clock(),
            correlation_id="campaign-artifact",
            filters=filters,
            max_rows=max_rows,
        )

    def _portfolio_daily(
        self,
        metric: str,
        period: DateRange,
        campaign_count: int,
    ) -> tuple[MeasurementToolRequest, ...]:
        """Chunk campaign-day rows under the query ceiling without per-campaign queries."""
        if campaign_count < 1:
            raise ValueError("portfolio daily measurement requires a campaign population.")
        days_per_request = max(1, MAX_ROWS_CEILING // campaign_count)
        requests: list[MeasurementToolRequest] = []
        start = period.start
        while start <= period.end:
            end = min(period.end, start + timedelta(days=days_per_request - 1))
            window = DateRange(start, end)
            requests.append(
                self._daily(
                    metric,
                    window,
                    {},
                    max_rows=campaign_count * window.days,
                )
            )
            start = end + timedelta(days=1)
        return tuple(requests)

    def _daily(
        self,
        metric: str,
        period: DateRange,
        filters: dict[str, str],
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> MeasurementToolRequest:
        return MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN,
            catalog=self.dependencies.catalog,
            contracts=self.dependencies.contracts,
            metric_name=metric,
            grains=("date", "campaign", "marketplace"),
            window=period,
            computed_at=self.dependencies.clock(),
            correlation_id="campaign-artifact",
            filters=filters,
            max_rows=max_rows,
        )

    def _single(
        self, metric: str, period: DateRange, filters: dict[str, str]
    ) -> MeasurementToolRequest:
        return MeasurementToolRequest(
            tool=MeasurementTool.METRIC_BY_GRAIN,
            catalog=self.dependencies.catalog,
            contracts=self.dependencies.contracts,
            metric_name=metric,
            grains=("campaign", "marketplace"),
            window=period,
            computed_at=self.dependencies.clock(),
            correlation_id="campaign-artifact",
            filters=filters,
            max_rows=DEFAULT_MAX_ROWS,
        )

    def _filters(self, selection: CampaignMeasurementSelection) -> dict[str, str]:
        return {
            "campaign_key": selection.campaign.value,
            "channel_key": selection.channel.value,
            "marketplace_key": selection.marketplace.value,
        }

    def _prepare(
        self, requests: tuple[MeasurementToolRequest, ...], label: str
    ) -> PreparedMeasurementBatch:
        rosters = self.dependencies.rosters
        try:
            prepared = prepare_batch(tuple(attach_roster(item, rosters) for item in requests))
        except (KeyError, ValueError) as error:
            raise RuntimeError(f"{label} requires a declared metric cohort.") from error
        if any(
            plan.role not in {PresentationEvidenceRole.CURRENT, PresentationEvidenceRole.COMPARATOR}
            for plan in prepared.plans
        ):
            raise InvariantError(ARTIFACT_MALFORMED, f"{label} plans have an unknown role.")
        return prepared
