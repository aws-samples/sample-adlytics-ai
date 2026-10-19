# SPDX-License-Identifier: MIT-0
"""Concrete retained Campaign artifact materialization and cache-request admission."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from publication_support import StubPublications

from adapters.campaign_insight_requests import (
    DetailInsightRequestDerivationInput,
    detail_insight_request_derivation,
    portfolio_insight_request_derivation,
)
from adapters.campaign_measurement import (
    CampaignArtifactMeasurementService,
    CampaignMeasurementDependencies,
)
from adapters.inbound.presentation_handlers.insights import InsightAssemblyRequest
from adapters.inbound.tool_handlers.measurement import MeasurementTooling
from adapters.jobs.contract_validation.insight_loader import load_insight_contracts
from adapters.jobs.contract_validation.loader import load_contracts
from adapters.jobs.insight_precompute.materializer import RetainedCampaignArtifactMaterializer
from adapters.jobs.insight_precompute.planning import RetainedPublicationPlanInput
from adapters.jobs.insight_precompute.requests import (
    CampaignRetainedPublicationRequestBuilder,
    RetainedCampaignInsightArtifacts,
)
from adapters.outbound.metric_dictionary import load_catalog
from domain.measurement.planning import measure_alias
from domain.presentation.analytical_pages import PresentationDimensionLabel
from domain.presentation.campaign_portfolio import CAMPAIGN_PORTFOLIO_ROW_GRAIN
from domain.presentation.insights import InsightSlotId
from domain.semantics.analytical_resolution import (
    AuthorizedAnalyticalScope,
    EffectiveLabel,
    EntityAlias,
    EntityKind,
    MappingEvidence,
)
from domain.semantics.identity import (
    CampaignKey,
    ChannelKey,
    CommerceScopeKey,
    MarketplaceKey,
    PlatformKey,
    SourceIdentifier,
)
from domain.semantics.publication import SuccessfulPublication
from domain.shared_kernel.provenance import Suppression
from domain.shared_kernel.values import DateRange
from ports.outbound.entity_catalog import (
    AnalyticalEntityCatalogPort,
    EntityCatalogBrowseRequest,
    EntityCatalogBrowseResult,
)
from ports.outbound.query import QueryRequest, QueryResult, QueryRow

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCOPE = CommerceScopeKey("commerce-fictional")
CAMPAIGN = CampaignKey("fictional-campaign", SCOPE, PlatformKey("sponsored"))
CHANNEL = ChannelKey("amazon_dsp")
"""Amazon DSP: the one channel whose delivery measures DSP media quality, so it is complete."""
SPONSORED = ChannelKey("amazon_sponsored_products")
"""A channel whose delivery measures no DSP media quality, so it is qualified."""
DSP_QUALITY = (
    "measurable_impressions",
    "viewable_impressions",
    "viewable_impressions_estimated",
    "viewable_rate",
)
MARKETPLACE = MarketplaceKey("us")
NOW = datetime(2026, 8, 22, tzinfo=UTC)
PERIOD = DateRange(date(2026, 8, 15), date(2026, 8, 21))
PRIOR = DateRange(date(2026, 8, 8), date(2026, 8, 14))


@dataclass
class Catalog:
    aliases: tuple[EntityAlias, ...]
    calls: list[EntityCatalogBrowseRequest] = field(
        default_factory=list[EntityCatalogBrowseRequest]
    )

    def browse(self, request: EntityCatalogBrowseRequest) -> EntityCatalogBrowseResult:
        self.calls.append(request)
        return EntityCatalogBrowseResult(self.aliases, authorized_count=len(self.aliases))


@dataclass
class CompleteQueries:
    """Fictional exact rows for every governed Campaign query shape."""

    suppress: bool = False
    divergent_prior_delivery: bool = False
    zero_purchases: bool = False
    calls: list[QueryRequest] = field(default_factory=list[QueryRequest])

    def run(self, request: QueryRequest) -> QueryResult:
        self.calls.append(request)
        plan = request.plan.logical
        suppressed = self.suppress and plan.metric.name == "purchases"
        if suppressed:
            return QueryResult((), 1024, False, Suppression(True, "fictional threshold"))
        if self.zero_purchases and plan.metric.name == "purchases":
            # A grouped conversion query returns no row for a campaign with zero attributed
            # purchases; the result is complete, unsuppressed, and simply empty.
            return QueryResult((), 1024, False, Suppression(False))
        base = Decimal(plan.window.days)
        row = {
            column: plan.filters.get(
                column,
                CAMPAIGN.value if column == "campaign_key" else MARKETPLACE.value,
            )
            for column in plan.group_by
        }
        row.update(dict.fromkeys(plan.carried_columns, "USD"))
        divergent_prior_delivery_value = (
            Decimal("2")
            if (
                self.divergent_prior_delivery
                and plan.metric.name == "impressions"
                and plan.window == PRIOR
                and "date_key" in plan.group_by
            )
            else Decimal(1)
        )
        if "date_key" in plan.group_by:
            rows = tuple(
                {
                    **row,
                    "date_key": (plan.window.start + timedelta(days=offset)).isoformat(),
                    **{
                        measure_alias(index): divergent_prior_delivery_value
                        for index in range(2 if plan.join is not None else len(plan.measures))
                    },
                }
                for offset in range(plan.window.days)
            )
        else:
            rows = (
                {
                    **row,
                    **{
                        measure_alias(index): base
                        for index in range(2 if plan.join is not None else len(plan.measures))
                    },
                },
            )
        return QueryResult(cast("tuple[QueryRow, ...]", rows), 1024, False, Suppression(False))


def _alias(channel: ChannelKey = CHANNEL) -> EntityAlias:
    return EntityAlias(
        kind=EntityKind.CAMPAIGN,
        key=CAMPAIGN,
        commerce_scope=SCOPE,
        labels=(EffectiveLabel("Fictional Campaign", date(2026, 1, 1)),),
        source_identifiers=(SourceIdentifier("fictional.campaign", CAMPAIGN.value),),
        evidence=MappingEvidence("fictional-retained-materializer", 1),
        valid_from=date(2026, 1, 1),
        platform=CAMPAIGN.platform,
        channel=channel,
        marketplace=MARKETPLACE,
    )


def _materializer(
    queries: CompleteQueries, channel: ChannelKey = CHANNEL
) -> RetainedCampaignArtifactMaterializer:
    contracts = tuple(contract for _, contract in load_contracts(ROOT))
    scope = AuthorizedAnalyticalScope(frozenset({SCOPE}))
    return RetainedCampaignArtifactMaterializer(
        catalog=cast("AnalyticalEntityCatalogPort", Catalog((_alias(channel),))),
        scope=scope,
        measurements=CampaignArtifactMeasurementService(
            CampaignMeasurementDependencies(scope, load_catalog(ROOT), contracts, lambda: NOW)
        ),
        evidence=StubPublications(),
        tooling=MeasurementTooling(
            queries=queries,
            publications=StubPublications(),
            query_batch_max_concurrency=8,
        ),
        labels=(
            PresentationDimensionLabel(
                "platform_key", CAMPAIGN.platform.value, "platform", "Sponsored"
            ),
            PresentationDimensionLabel("channel_key", channel.value, "channel", "Sponsored"),
            PresentationDimensionLabel(
                "marketplace_key", MARKETPLACE.value, "marketplace", "United States"
            ),
        ),
    )


def _request(channel: ChannelKey = CHANNEL) -> RetainedPublicationPlanInput:
    return RetainedPublicationPlanInput(
        publication=cast("SuccessfulPublication", object()),
        window=PERIOD,
        prior_window=PRIOR,
        campaign_aliases=(_alias(channel),),
    )


@pytest.mark.covers("ADR-068")
def test_materializer_returns_complete_portfolio_detail_and_delivery() -> None:
    queries = CompleteQueries()
    rendered = _materializer(queries).artifacts_for(_request())

    assert rendered.portfolio is not None
    assert rendered.portfolio.outcome.value == "complete"
    assert rendered.portfolio.total_count == 1
    assert len(rendered.details) == 1
    assert rendered.details[0].detail.outcome.value == "selected"
    assert rendered.details[0].delivery.outcome == "complete"
    delivery = rendered.details[0].delivery
    assert tuple(slot.slot.value for slot in delivery.summary) == (
        "ad_spend",
        "impressions",
        "clicks",
        "ctr",
        "cpc",
        "cpm",
    )
    first_row = delivery.series_points[0]
    assert first_row["date"] == PERIOD.start.isoformat()
    assert first_row["current"] == first_row["prior"]
    assert set(cast("dict[str, object]", first_row["current"])) == {
        "ad_spend",
        "impressions",
        "clicks",
        "ctr",
        "cpc",
        "cpm",
    }
    daily_delivery_windows = {
        request.plan.logical.window
        for request in queries.calls
        if request.plan.logical.metric.name
        in {"ad_spend", "impressions", "clicks", "ctr", "cpc", "cpm"}
        and "date_key" in request.plan.logical.group_by
    }
    assert daily_delivery_windows == {PERIOD, PRIOR}


@pytest.mark.covers("ADR-068")
def test_materializer_retains_a_sponsored_campaign_with_dsp_quality_typed_absent() -> None:
    """A Sponsored Products campaign keeps its detail and a qualified delivery (cause A).

    Its batch requests no DSP media quality, so no such query runs, and the delivery is
    qualified with quality typed absent for the channel: retained, never withheld.
    """
    queries = CompleteQueries()
    rendered = _materializer(queries, SPONSORED).artifacts_for(_request(SPONSORED))

    assert rendered.portfolio is not None
    assert rendered.portfolio.outcome.value == "complete"
    assert len(rendered.details) == 1
    assert rendered.details[0].detail.outcome.value == "selected"
    delivery = rendered.details[0].delivery
    assert delivery.outcome == "qualified"
    assert (delivery.dsp_quality.outcome, delivery.dsp_quality.reason_code) == (
        "unavailable",
        "dsp_quality_not_measured_for_channel",
    )
    assert tuple(slot.slot.value for slot in delivery.summary) == (
        "ad_spend",
        "impressions",
        "clicks",
        "ctr",
        "cpc",
        "cpm",
    )
    assert all(slot.value is not None for slot in delivery.summary)
    assert {
        request.plan.logical.metric.name
        for request in queries.calls
        if request.plan.logical.metric.name in DSP_QUALITY
    } == set()


@pytest.mark.covers("ADR-068")
def test_materializer_output_builds_cache_compatible_portfolio_and_detail_slot_keys() -> None:
    materializer = _materializer(CompleteQueries())
    builder = CampaignRetainedPublicationRequestBuilder(
        artifacts=materializer,
        scope=AuthorizedAnalyticalScope(frozenset({SCOPE})),
        contracts={contract.id: contract for _, contract in load_insight_contracts(ROOT)},
    )

    requests = builder.requests_for(_request())

    assert tuple(request.key.slot_id for request in requests.portfolio) == (InsightSlotId.EXPOSURE,)
    assert tuple(
        tuple(request.key.slot_id for request in campaign) for campaign in requests.details
    ) == ((InsightSlotId.FUNNEL,),)
    assert all(
        request.key.artifact_revision for request in (*requests.portfolio, *requests.details[0])
    )


@pytest.mark.covers("ADR-068")
def test_materializer_treats_a_campaign_with_zero_attributed_purchases_as_zero() -> None:
    """Spend with no conversions is ordinary; it must not discard the whole portfolio."""
    rendered = _materializer(CompleteQueries(zero_purchases=True)).artifacts_for(_request())

    assert rendered.portfolio is not None
    assert rendered.portfolio.outcome.value == "complete"
    assert rendered.portfolio.total_count == 1
    row = rendered.portfolio.rows[0]
    purchases = next(item for item in row.comparisons if item.metric.value == "purchases")
    assert purchases.current.value == 0
    assert purchases.current.units.kind.value == "count"
    assert purchases.current.grain == CAMPAIGN_PORTFOLIO_ROW_GRAIN
    spend = next(item for item in row.comparisons if item.metric.value == "spend")
    assert spend.current.number > 0
    total_purchases = next(
        item for item in rendered.portfolio.totals if item.metric.value == "purchases"
    )
    assert total_purchases.current.value == 0
    assert rendered.portfolio.trend is not None
    assert all(point.purchases == 0 for point in rendered.portfolio.trend.points)
    # Its conversion evidence is typed absent, so no command-center detail is precomputed for
    # it; the portfolio row is never withheld because of that.
    assert rendered.details == ()


@pytest.mark.covers("ADR-068")
def test_materializer_refuses_suppressed_governed_evidence_without_partial_artifacts() -> None:
    rendered = _materializer(CompleteQueries(suppress=True)).artifacts_for(_request())

    assert rendered.portfolio is None
    assert rendered.details == ()


@pytest.mark.covers("ADR-068")
def test_materializer_refuses_divergent_prior_daily_delivery_without_partial_artifacts() -> None:
    """A delivery failing its own reconciliation yields no detail for that campaign.

    The portfolio never read that delivery, so it stays complete rather than being withheld for
    evidence it did not use.
    """
    rendered = _materializer(CompleteQueries(divergent_prior_delivery=True)).artifacts_for(
        _request()
    )

    assert rendered.portfolio is not None
    assert rendered.portfolio.outcome.value == "complete"
    assert rendered.details == ()


@dataclass(frozen=True)
class _MaterializedArtifacts:
    """Keep retained-builder input identical to the interactive derivation input."""

    value: RetainedCampaignInsightArtifacts

    def artifacts_for(
        self, request: RetainedPublicationPlanInput
    ) -> RetainedCampaignInsightArtifacts:
        _ = request
        return self.value


def _interactive_derivation_requests(
    artifacts: RetainedCampaignInsightArtifacts,
) -> tuple[InsightAssemblyRequest, ...]:
    """Derive the interactive request set directly from the measured artifacts."""
    if artifacts.portfolio is None:
        raise AssertionError("the fictional materialization must include a complete portfolio")
    contracts = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
    derivation, _ = portfolio_insight_request_derivation(
        artifact=artifacts.portfolio,
        scope=AuthorizedAnalyticalScope(frozenset({SCOPE})),
        contracts=contracts,
    )
    return derivation.requests + tuple(
        request
        for detail in artifacts.details
        for request in detail_insight_request_derivation(
            DetailInsightRequestDerivationInput(
                detail=detail.detail,
                delivery=detail.delivery,
                subject=detail.subject,
                scope=AuthorizedAnalyticalScope(frozenset({SCOPE})),
                contracts=contracts,
            )
        ).requests
    )


@pytest.mark.covers("ADR-068")
def test_retained_and_interactive_derivations_match_for_same_materialized_artifacts() -> None:
    materialized = _materializer(CompleteQueries()).artifacts_for(_request())
    assert materialized.portfolio is not None

    contracts = {contract.id: contract for _, contract in load_insight_contracts(ROOT)}
    retained = CampaignRetainedPublicationRequestBuilder(
        artifacts=_MaterializedArtifacts(materialized),
        scope=AuthorizedAnalyticalScope(frozenset({SCOPE})),
        contracts=contracts,
    ).requests_for(_request())
    interactive = _interactive_derivation_requests(materialized)

    assert tuple(
        (request.key.subject, request.key.artifact_revision, request.key, request.findings)
        for request in (
            *retained.portfolio,
            *(item for campaign in retained.details for item in campaign),
        )
    ) == tuple(
        (request.key.subject, request.key.artifact_revision, request.key, request.findings)
        for request in interactive
    )


@pytest.mark.covers("ADR-068")
def test_retained_builder_splits_the_portfolio_from_per_campaign_details_in_key_order() -> None:
    """28 complete DSP details, rendered out of order, come back one group each by campaign_key.

    The planner admits these groups to the publication bound, so the split and the order are the
    contract: the portfolio apart, then whole campaigns sorted by their canonical subject.
    """
    materialized = _materializer(CompleteQueries()).artifacts_for(_request())
    (detail,) = materialized.details
    assert detail.delivery.outcome == "complete"
    subjects = tuple(f"fictional-campaign-{index:02d}" for index in range(28))
    aliases = tuple(
        replace(_alias(), key=CampaignKey(subject, SCOPE, PlatformKey("sponsored")))
        for subject in subjects
    )
    rendered = RetainedCampaignInsightArtifacts(
        materialized.portfolio,
        tuple(replace(detail, subject=subject) for subject in reversed(subjects)),
    )

    requests = CampaignRetainedPublicationRequestBuilder(
        artifacts=_MaterializedArtifacts(rendered),
        scope=AuthorizedAnalyticalScope(frozenset({SCOPE})),
        contracts={contract.id: contract for _, contract in load_insight_contracts(ROOT)},
    ).requests_for(replace(_request(), campaign_aliases=aliases))

    assert tuple((request.key.slot_id, request.key.subject) for request in requests.portfolio) == (
        (InsightSlotId.EXPOSURE, "campaign-portfolio"),
    )
    assert tuple(
        tuple((request.key.slot_id, request.key.subject) for request in campaign)
        for campaign in requests.details
    ) == tuple(((InsightSlotId.FUNNEL, subject),) for subject in subjects)
