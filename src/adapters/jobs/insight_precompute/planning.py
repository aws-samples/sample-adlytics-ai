# SPDX-License-Identifier: MIT-0
"""Retained-publication planning guards shared by the precompute worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

from adapters.inbound.presentation_handlers.insights import InsightAssemblyRequest
from adapters.outbound.telemetry.reporting import LogSeverity, OperationLog, report_operation
from adapters.outbound.telemetry.tracing import INSIGHT_PRECOMPUTE
from domain.catalog.capability import Catalog
from domain.presentation.insights import InsightSlotId
from domain.presentation.page_schemas import PageSchemaSet, PresentationTemporalPolicy
from domain.presentation.temporal import (
    TemporalComparisonStatus,
    TemporalResolutionAvailable,
    TemporalResolutionRequest,
    TemporalSelection,
    TemporalSelectionKind,
    resolve_temporal,
)
from domain.semantics.analytical_resolution import (
    AuthorizedAnalyticalScope,
    EntityAlias,
    EntityContext,
    EntityKind,
)
from domain.semantics.publication import SuccessfulPublication
from domain.shared_kernel.values import DateRange
from ports.outbound.entity_catalog import (
    AnalyticalEntityCatalogPort,
    EntityCatalogBrowseRequest,
)
from ports.outbound.source_coverage import (
    SourceCoveragePort,
    SourceCoverageRequest,
    SourceCoverageRequirement,
    metric_coverage_requirement,
)

if TYPE_CHECKING:
    from aws_lambda_powertools import Logger


@dataclass(frozen=True, slots=True)
class RetainedPublicationPlanInput:
    """One server-owned available preset and its exact authorized campaign population."""

    publication: SuccessfulPublication
    window: DateRange
    prior_window: DateRange | None
    campaign_aliases: tuple[EntityAlias, ...]


@dataclass(frozen=True, slots=True)
class RetainedPresetRequests:
    """One preset's model slots, split so the planner can admit them to the fan-out bound.

    ``details`` holds one tuple per campaign, in campaign_key order, so admission takes whole
    campaigns in a stable order.
    """

    portfolio: tuple[InsightAssemblyRequest, ...] = ()
    details: tuple[tuple[InsightAssemblyRequest, ...], ...] = ()


class RetainedPublicationRequestBuilder(Protocol):
    """Build cache-compatible requests from an already measured, rendered artifact."""

    def requests_for(self, request: RetainedPublicationPlanInput) -> RetainedPresetRequests:
        """Return portfolio and renderable campaign-detail model slots, never MOVERS."""
        ...


def admit_to_budget(
    presets: tuple[RetainedPresetRequests, ...], max_requests: int
) -> tuple[InsightAssemblyRequest, ...]:
    """Admit every preset's portfolio slots first, then whole campaign details to the budget.

    Details are taken in preset order, then campaign_key order, and admission stops at the first
    campaign whose slots no longer fit. A detail left out is not precomputed; it is served at
    request time, as an uncached detail always is.
    """
    portfolio = tuple(item for preset in presets for item in preset.portfolio)
    admitted = list(portfolio[:max_requests])
    for campaign in (group for preset in presets for group in preset.details):
        if len(admitted) + len(campaign) > max_requests:
            break
        admitted.extend(campaign)
    return tuple(admitted)


@dataclass(frozen=True, slots=True)
class RetainedPublicationPlanner:
    """Enumerate only declared complete V2 preset work from retained state."""

    coverage: SourceCoveragePort
    coverage_requirements: tuple[SourceCoverageRequirement, ...]
    temporal_policy: PresentationTemporalPolicy
    catalog: AnalyticalEntityCatalogPort
    scope: AuthorizedAnalyticalScope
    request_builder: RetainedPublicationRequestBuilder
    max_requests: int
    logger: Logger

    def __post_init__(self) -> None:
        if not self.coverage_requirements:
            raise ValueError("retained insight precompute requires declared source coverage.")
        if self.max_requests < 1:
            raise ValueError("retained campaign request limit must be positive.")

    def requests_for(
        self, publication: SuccessfulPublication
    ) -> tuple[InsightAssemblyRequest, ...]:
        """Plan all cacheable slots and admit them to the publication's fan-out bound."""
        source_coverage = self.coverage.source_coverage(
            SourceCoverageRequest(self.coverage_requirements)
        )
        presets: list[RetainedPresetRequests] = []
        for preset_id in self.temporal_policy.candidate_presets:
            resolved = resolve_temporal(
                TemporalResolutionRequest(
                    policy=self.temporal_policy,
                    coverage_intervals=source_coverage.bounds.intervals,
                    selection=TemporalSelection(
                        kind=TemporalSelectionKind.PRESET,
                        preset_id=preset_id,
                    ),
                    comparison_requested=False,
                )
            )
            if not isinstance(resolved, TemporalResolutionAvailable):
                continue
            control = resolved.control
            if control.comparison.status is TemporalComparisonStatus.UNAVAILABLE:
                continue
            window = control.resolved.window
            catalog = self.catalog.browse(
                EntityCatalogBrowseRequest(
                    scope=self.scope,
                    kinds=frozenset({EntityKind.CAMPAIGN}),
                    window=window,
                    current_on=None,
                    context=EntityContext(),
                    max_candidates=50,
                )
            )
            # A portfolio must reduce the entire authorized cohort; a partial catalog is not one.
            if catalog.truncated:
                return ()
            prior = (
                DateRange(
                    window.start - timedelta(days=window.days),
                    window.start - timedelta(days=1),
                )
                if control.comparison.status is TemporalComparisonStatus.AVAILABLE
                else None
            )
            presets.append(
                self.request_builder.requests_for(
                    RetainedPublicationPlanInput(
                        publication=publication,
                        window=window,
                        prior_window=prior,
                        campaign_aliases=tuple(catalog.aliases),
                    )
                )
            )
        requests = admit_to_budget(tuple(presets), self.max_requests)
        if any(request.key.slot_id is InsightSlotId.MOVERS for request in requests):
            raise ValueError("retained insight precompute cannot submit deterministic movers.")
        self._report_admission(tuple(presets), len(requests))
        return requests

    def _report_admission(self, presets: tuple[RetainedPresetRequests, ...], admitted: int) -> None:
        """Record what the bound admitted, so a skipped detail is counted, never silent."""
        portfolio = sum(len(preset.portfolio) for preset in presets)
        details = sum(len(group) for preset in presets for group in preset.details)
        skipped = portfolio + details - admitted
        report_operation(
            OperationLog(
                operation=INSIGHT_PRECOMPUTE,
                component="insight_precompute",
                phase="admission",
                outcome="bounded" if skipped else "admitted",
                severity=LogSeverity.INFO,
                details={
                    "requested": portfolio + details,
                    "portfolio_requested": portfolio,
                    "detail_requested": details,
                    "admitted": admitted,
                    "skipped": skipped,
                    "max_requests": self.max_requests,
                },
            ),
            self.logger,
        )


def campaign_coverage_requirements(
    schemas: PageSchemaSet, catalog: Catalog
) -> tuple[SourceCoverageRequirement, ...]:
    """Derive the retained campaign cohort's source coverage from its governed detail schema."""
    campaign = next(
        (page for page in schemas.pages if page.page_id == "campaign-attribution-detail"),
        None,
    )
    if campaign is None:
        raise RuntimeError("campaign attribution schema is unavailable for retained precompute.")
    requirements: list[SourceCoverageRequirement] = []
    for metric in campaign.metrics:
        declared = catalog.metric(metric.name)
        if declared is None:
            raise RuntimeError(f"campaign precompute metric {metric.name} is unavailable.")
        for variant in metric.variants or (None,):
            requirement = metric_coverage_requirement(
                catalog, declared, () if variant is None else tuple(sorted(variant.selectors))
            )
            if requirement not in requirements:
                requirements.append(requirement)
    return tuple(requirements)
