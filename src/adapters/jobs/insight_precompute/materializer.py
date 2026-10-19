# SPDX-License-Identifier: MIT-0
"""Retained and readiness-bound artifact materialization for insight precompute."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from adapters.campaign_artifact_renderer import (
    CampaignArtifactRenderer,
    CampaignArtifactRenderInput,
    retained_campaign_selections,
    retained_capabilities,
)
from adapters.campaign_insight_requests import portfolio_insight_request_derivation
from adapters.campaign_measurement import CampaignArtifactMeasurementService
from adapters.inbound.presentation_handlers.insights import InsightAssemblyRequest
from adapters.inbound.tool_handlers.measurement import MeasurementTooling
from adapters.jobs.insight_precompute.planning import RetainedPublicationPlanInput
from adapters.jobs.insight_precompute.requests import (
    RetainedCampaignDetailArtifact,
    RetainedCampaignInsightArtifacts,
)
from adapters.retail_insight_requests import (
    RetailInsightRequestDerivationInput,
    retail_insight_request_derivation,
)
from domain.catalog.capability import Catalog
from domain.operations.execution_graph_release import (
    DETERMINISTIC_INSIGHT_PAGES,
    BakeEffectKind,
    PrecomputeInputEntry,
    PrecomputeInputV1,
)
from domain.presentation.analytical_pages import PresentationDimensionLabel
from domain.presentation.campaign_custody import campaign_custody_manifest_from_payload
from domain.presentation.campaign_portfolio import (
    CAMPAIGN_PORTFOLIO_MAX_ROWS,
    CampaignPortfolioSort,
    CampaignPortfolioSortField,
    campaign_portfolio_artifact_from_payload,
)
from domain.presentation.insights import InsightContract, InsightSlotId
from domain.presentation.preset_artifacts import (
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactRevision,
    PresetArtifactVariant,
)
from domain.presentation.retail_health import retail_health_artifact_from_payload
from domain.presentation.retail_health_custody import retail_health_custody_manifest_from_payload
from domain.semantics.analytical_resolution import (
    AuthorizedAnalyticalScope,
    EntityContext,
    EntityKind,
)
from ports.outbound.entity_catalog import (
    AnalyticalEntityCatalogPort,
    EntityCatalogBrowseRequest,
)
from ports.outbound.preset_artifacts import (
    PresetArtifactBodyStorePort,
    PresetArtifactCustodyStorePort,
)
from ports.outbound.publication_selection import PresentationEvidenceInspectionPort


@dataclass(frozen=True, slots=True)
class RetainedCampaignArtifactMaterializer:
    """Re-attest and measure a retained cohort for the non-GRAPH stream path."""

    catalog: AnalyticalEntityCatalogPort
    scope: AuthorizedAnalyticalScope
    measurements: CampaignArtifactMeasurementService
    evidence: PresentationEvidenceInspectionPort
    tooling: MeasurementTooling
    labels: tuple[PresentationDimensionLabel, ...]

    def artifacts_for(
        self, request: RetainedPublicationPlanInput
    ) -> RetainedCampaignInsightArtifacts:
        """Measure a server-owned, re-attested cohort; never read browser custody state."""
        resolved = self.catalog.browse(
            EntityCatalogBrowseRequest(
                scope=self.scope,
                kinds=frozenset({EntityKind.CAMPAIGN}),
                window=request.window,
                current_on=None,
                context=EntityContext(),
                max_candidates=CAMPAIGN_PORTFOLIO_MAX_ROWS,
            )
        )
        if (
            request.prior_window is None
            or resolved.truncated
            or tuple(resolved.aliases) != request.campaign_aliases
            or len({alias.key.value for alias in request.campaign_aliases})
            > CAMPAIGN_PORTFOLIO_MAX_ROWS
        ):
            return RetainedCampaignInsightArtifacts(None, ())
        try:
            selections = retained_campaign_selections(
                request.campaign_aliases, request.window, self.labels
            )
        except TypeError, ValueError:
            return RetainedCampaignInsightArtifacts(None, ())
        if not selections:
            return RetainedCampaignInsightArtifacts(None, ())
        rendered = CampaignArtifactRenderer(
            measurements=self.measurements,
            evidence=self.evidence,
            tooling=self.tooling,
        ).render(
            CampaignArtifactRenderInput(
                selections=selections,
                period=request.window,
                prior_period=request.prior_window,
                sort=CampaignPortfolioSort(CampaignPortfolioSortField.SPEND, "desc"),
                capabilities=retained_capabilities(selections),
            )
        )
        if rendered is None:
            return RetainedCampaignInsightArtifacts(None, ())
        return RetainedCampaignInsightArtifacts(
            rendered.portfolio,
            tuple(
                RetainedCampaignDetailArtifact(selection.campaign.value, detail, delivery)
                for selection, detail, delivery in zip(
                    selections, rendered.details, rendered.deliveries, strict=True
                )
                if detail is not None and delivery is not None
            ),
        )


@dataclass(frozen=True, slots=True)
class ArtifactPrecomputePlan:
    """The bounded request union and one disposition per readiness roster row."""

    requests: tuple[InsightAssemblyRequest, ...]
    request_roster: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class StagedArtifactInsightPlanner:
    """Derive GRAPH requests only from exact readiness-bound immutable artifacts."""

    bodies: PresetArtifactBodyStorePort
    custody: PresetArtifactCustodyStorePort
    scope: AuthorizedAnalyticalScope
    contracts: Mapping[InsightSlotId, InsightContract]
    catalog: Catalog

    def plan(self, inputs: PrecomputeInputV1) -> ArtifactPrecomputePlan:
        """Attest every available body/custody pair and preserve readiness roster order."""
        requests: list[InsightAssemblyRequest] = []
        roster: list[tuple[str, str]] = []
        identities: dict[object, InsightAssemblyRequest] = {}
        for entry in inputs.entries:
            admitted = (
                () if entry.effect_kind is BakeEffectKind.UNAVAILABLE else self._requests(entry)
            )
            roster.append((entry.key, "requested" if admitted else "no_request"))
            for request in admitted:
                prior = identities.get(request.key)
                if prior is None:
                    identities[request.key] = request
                    requests.append(request)
                elif prior != request:
                    raise ValueError("equal artifact request identities disagree.")
        return ArtifactPrecomputePlan(tuple(requests), tuple(roster))

    def _requests(self, entry: PrecomputeInputEntry) -> tuple[InsightAssemblyRequest, ...]:
        """Attest one available entry's immutable body and derive its model requests.

        A deterministic-insight page (O5) is attested and then explicitly has no request; any
        page outside the closed set is refused, never silently skipped.
        """
        if entry.artifact_revision is None or entry.body_digest is None or entry.body_key is None:
            raise ValueError("an available readiness entry has no immutable manifest.")
        pointer = PresetArtifactPointer(
            PresetArtifactPage(entry.page),
            PresetArtifactVariant(entry.variant),
            PresetArtifactRevision(entry.artifact_revision),
            entry.body_digest,
            1,
            custody_digest=entry.custody_digest,
        )
        if pointer.body_key != entry.body_key:
            raise ValueError("a readiness entry names another immutable body key.")
        body = self.bodies.read(pointer)
        if body.digest != pointer.body_digest:
            raise ValueError("a readiness body is not the attested immutable body.")
        if entry.page in DETERMINISTIC_INSIGHT_PAGES:
            if entry.custody_digest is not None and (
                self.custody.read(pointer).digest != entry.custody_digest
            ):
                raise ValueError("artifact custody does not bind the readiness body.")
            return ()
        if entry.custody_digest is None:
            raise ValueError("an available readiness entry has no immutable manifest.")
        manifest = self.custody.read(pointer)
        payload: object = json.loads(body.payload_json)
        private: object = json.loads(manifest.payload_json)
        if entry.page == "campaigns":
            attested = campaign_custody_manifest_from_payload(private)
            derived, _ = portfolio_insight_request_derivation(
                artifact=campaign_portfolio_artifact_from_payload(payload),
                scope=self.scope,
                contracts=self.contracts,
            )
        elif entry.page == "retail-health":
            attested = retail_health_custody_manifest_from_payload(private)
            derived = retail_insight_request_derivation(
                RetailInsightRequestDerivationInput(
                    artifact=retail_health_artifact_from_payload(payload),
                    scope=self.scope,
                    contracts=self.contracts,
                    catalog=self.catalog,
                )
            )
        else:
            raise ValueError("a readiness entry names an unsupported artifact page.")
        if attested.body_digest != pointer.body_digest:
            raise ValueError("artifact custody does not bind the readiness body.")
        return tuple(derived.requests)
