# SPDX-License-Identifier: MIT-0
"""Retained-state campaign artifact request builder for insight precomputation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from adapters.campaign_insight_requests import (
    DetailInsightRequestDerivationInput,
    detail_insight_request_derivation,
    portfolio_insight_request_derivation,
)
from adapters.inbound.presentation_handlers.insights import InsightAssemblyRequest
from adapters.jobs.insight_precompute.planning import (
    RetainedPresetRequests,
    RetainedPublicationPlanInput,
)
from domain.presentation.campaign_attribution import (
    CampaignAttributionDetailArtifact,
    CampaignDelivery,
    CampaignDetailOutcome,
    CampaignEvidenceCondition,
)
from domain.presentation.campaign_portfolio import (
    CAMPAIGN_PORTFOLIO_MAX_ROWS,
    CampaignPortfolioArtifact,
    CampaignPortfolioOutcome,
)
from domain.presentation.insights import InsightContract, InsightSlotId
from domain.semantics.analytical_resolution import (
    AuthorizedAnalyticalScope,
    EntityKind,
)


@dataclass(frozen=True, slots=True)
class RetainedCampaignDetailArtifact:
    """One server-rendered campaign detail with its canonical non-browser subject."""

    subject: str
    detail: CampaignAttributionDetailArtifact
    delivery: CampaignDelivery

    def __post_init__(self) -> None:
        if not self.subject.strip():
            raise ValueError("retained campaign detail requires a canonical subject.")


@dataclass(frozen=True, slots=True)
class RetainedCampaignInsightArtifacts:
    """Measured campaign artifacts eligible for one retained publication/preset plan."""

    portfolio: CampaignPortfolioArtifact | None
    details: tuple[RetainedCampaignDetailArtifact, ...]

    def __post_init__(self) -> None:
        subjects = tuple(item.subject for item in self.details)
        if len(set(subjects)) != len(subjects):
            raise ValueError("retained campaign detail artifacts repeat a subject.")


class RetainedCampaignArtifactSource(Protocol):
    """Read or render only measured server artifacts for an already re-attested plan."""

    def artifacts_for(
        self, request: RetainedPublicationPlanInput
    ) -> RetainedCampaignInsightArtifacts:
        """Return only artifacts built from retained publication state and governed evidence."""
        ...


def _complete_portfolio(
    artifact: CampaignPortfolioArtifact, request: RetainedPublicationPlanInput
) -> bool:
    """Admit only an untruncated complete portfolio for this exact retained cohort."""
    return (
        artifact.outcome is CampaignPortfolioOutcome.COMPLETE
        and artifact.period == request.window
        and artifact.prior_period == request.prior_window
        and artifact.pagination is not None
        and len(artifact.rows) <= CAMPAIGN_PORTFOLIO_MAX_ROWS
        and artifact.total_count == len(artifact.rows)
    )


def _complete_detail(
    artifact: RetainedCampaignDetailArtifact, request: RetainedPublicationPlanInput
) -> bool:
    """Reject a detail when any evidence is partial, suppressed, or incompatible."""
    return (
        artifact.detail.outcome is CampaignDetailOutcome.SELECTED
        and artifact.detail.period == request.window
        and artifact.detail.prior_period == request.prior_window
        and artifact.delivery.comparison_period == request.prior_window
        and artifact.delivery.outcome == "complete"
        and all(
            evidence.condition is CampaignEvidenceCondition.AVAILABLE
            for evidence in artifact.detail.evidence_by_slot
        )
    )


@dataclass(frozen=True, slots=True)
class CampaignRetainedPublicationRequestBuilder:
    """Convert retained measured campaign artifacts into revision-bound model requests."""

    artifacts: RetainedCampaignArtifactSource
    scope: AuthorizedAnalyticalScope
    contracts: Mapping[InsightSlotId, InsightContract]

    def requests_for(self, request: RetainedPublicationPlanInput) -> RetainedPresetRequests:
        """Return only complete, authorized portfolio/detail model slots for the preset."""
        authorized_subjects = frozenset(
            alias.key.value
            for alias in request.campaign_aliases
            if alias.kind is EntityKind.CAMPAIGN
        )
        if len(authorized_subjects) > CAMPAIGN_PORTFOLIO_MAX_ROWS:
            return RetainedPresetRequests()
        rendered = self.artifacts.artifacts_for(request)
        portfolio_requests: tuple[InsightAssemblyRequest, ...] = ()
        portfolio = rendered.portfolio
        if portfolio is not None and _complete_portfolio(portfolio, request):
            derivation, _ = portfolio_insight_request_derivation(
                artifact=portfolio,
                scope=self.scope,
                contracts=self.contracts,
            )
            portfolio_requests = derivation.requests
        details: list[tuple[InsightAssemblyRequest, ...]] = []
        for artifact in sorted(rendered.details, key=lambda item: item.subject):
            if artifact.subject not in authorized_subjects or not _complete_detail(
                artifact, request
            ):
                continue
            derivation = detail_insight_request_derivation(
                DetailInsightRequestDerivationInput(
                    detail=artifact.detail,
                    delivery=artifact.delivery,
                    subject=artifact.subject,
                    scope=self.scope,
                    contracts=self.contracts,
                )
            )
            if derivation.requests:
                details.append(derivation.requests)
        if any(
            item.key.slot_id is InsightSlotId.MOVERS
            for item in (*portfolio_requests, *(entry for group in details for entry in group))
        ):
            raise ValueError("retained campaign request builder cannot submit movers.")
        return RetainedPresetRequests(portfolio_requests, tuple(details))
