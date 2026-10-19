# SPDX-License-Identifier: MIT-0
"""Best-effort insight-fill child for an advanced Campaign preset revision.

Generation-materialization §5.3 (interim source): after the pointer advances, the
child derives the advanced revision's declared portfolio model requests and admits
them to the existing durable continuation queue, so the worker fills the
revision-keyed insight cache exactly as serve-time first-fill would. Until the
Stage-3 ``GenerationMaterialization.v1`` record exists, the findings source is the
just-baked parent artifact itself — the same bytes the serve path derives from, so
the warmed cache keys are byte-identical to the keys a serve would request.

Optional under ADR-074: any failure here is reported and swallowed; it never
reopens, fails, or gates the bake or the pointer.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from aws_lambda_powertools import Logger

from adapters.campaign_insight_requests import portfolio_insight_request_derivation
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    report_error,
    report_operation,
)
from adapters.retail_insight_requests import (
    RetailInsightRequestDerivationInput,
    retail_insight_request_derivation,
)
from domain.presentation.campaign_portfolio import campaign_portfolio_artifact_from_payload
from domain.presentation.insights import (
    InsightContinuationRequest,
    InsightGenerationRequest,
    InsightPromptTier,
)
from domain.presentation.retail_health import retail_health_artifact_from_payload

if TYPE_CHECKING:
    from adapters.inbound.presentation_handlers.insights import InsightAssemblyRequest
    from domain.catalog.capability import Catalog
    from domain.presentation.insights import InsightContract, InsightSlotId
    from domain.presentation.preset_artifacts import PresetArtifactPublicationRequest
    from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
    from ports.outbound.insights import InsightContinuationPort, InsightPromptActivationPort

LOGGER = Logger(service="adlytics-preset-artifact-publisher")
_OPERATION = "preset_artifact.insight_fill"
_COMPONENT = "preset_artifact_publisher"
_CAMPAIGNS_PAGE = "campaigns"
_RETAIL_PAGE = "retail-health"


@dataclass(frozen=True, slots=True)
class PresetInsightFillChild:
    """Admit the advanced revision's declared portfolio slots for durable cache fill."""

    prompts: InsightPromptActivationPort
    continuation: InsightContinuationPort
    scope: AuthorizedAnalyticalScope
    contracts: Mapping[InsightSlotId, InsightContract]
    catalog: Catalog

    def warm(self, request: PresetArtifactPublicationRequest) -> None:
        """Enqueue every admitted declared slot; report and swallow every failure."""
        if request.body.page.value not in {_CAMPAIGNS_PAGE, _RETAIL_PAGE}:
            return
        try:
            enqueued = self._enqueue(request)
        except Exception as error:  # optional child: never gates the pointer (ADR-074)
            report_error(
                error,
                Report(operation=_OPERATION, correlation_id="", logger=LOGGER),
            )
            return
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase="terminal",
                outcome="admitted",
                severity=LogSeverity.INFO,
                details={
                    "stage": "insight_fill_enqueue",
                    "page": request.body.page.value,
                    "variant": request.body.variant.value,
                    "enqueued_count": enqueued,
                },
            ),
            LOGGER,
        )

    def _enqueue(self, request: PresetArtifactPublicationRequest) -> int:
        enqueued = 0
        for assembly in self._declared_requests(request):
            # Bake-time warming fills the card tier only (the deep tier is on-request).
            prompt = self.prompts.active(assembly.key.slot_id, InsightPromptTier.CARD)
            if prompt is None:
                continue
            generation = InsightGenerationRequest(
                replace(assembly.key, prompt_version=prompt.version),
                assembly.output_class,
                prompt,
                assembly.purpose,
                assembly.findings,
                assembly.window_profile,
                assembly.context,
                assembly.next_step_options,
            )
            self.continuation.enqueue(InsightContinuationRequest(generation))
            enqueued += 1
        return enqueued

    def _declared_requests(
        self, request: PresetArtifactPublicationRequest
    ) -> tuple[InsightAssemblyRequest, ...]:
        """Derive the page's declared slot requests from the just-baked parent body.

        Both derivations read the same bytes the serve path decodes, so the warmed
        cache keys are byte-identical to the keys a serve of this revision requests.
        """
        payload = json.loads(request.body.payload_json)
        if request.body.page.value == _RETAIL_PAGE:
            return retail_insight_request_derivation(
                RetailInsightRequestDerivationInput(
                    artifact=retail_health_artifact_from_payload(payload),
                    scope=self.scope,
                    contracts=self.contracts,
                    catalog=self.catalog,
                )
            ).requests
        derivation, _ = portfolio_insight_request_derivation(
            artifact=campaign_portfolio_artifact_from_payload(payload),
            scope=self.scope,
            contracts=self.contracts,
        )
        return derivation.requests
