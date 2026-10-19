# SPDX-License-Identifier: MIT-0
"""Unit-testable dispatch boundary for one AWS-composed measurement Gateway target."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from adapters.inbound.tool_handlers import gateway
from adapters.inbound.tool_handlers.measurement import (
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
)
from adapters.inbound.tool_handlers.resolved_measurement import (
    SemanticMeasurementContext,
    request_from_semantic_intent,
)
from adapters.outbound.catalog import MetricWindowDefaults
from domain.catalog.capability import Catalog
from domain.data_contracts.contract import DataContract
from domain.narrative.publication import grounded_refusal
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.shared_kernel.errors import AdlyticsError, ValidationError
from ports.outbound.entity_catalog import AnalyticalEntityCatalogPort

__all__ = ["MeasurementDispatch"]


@dataclass(frozen=True, slots=True)
class MeasurementDispatch:
    """Resolve one Gateway invocation without composing AWS adapters or issuing a receipt."""

    catalog: Catalog
    contracts: tuple[DataContract, ...]
    entity_catalog: AnalyticalEntityCatalogPort
    scope: AuthorizedAnalyticalScope
    tooling: MeasurementTooling
    now: Callable[[], datetime]
    window_defaults: MetricWindowDefaults | None = None
    """Coverage-derived governed window defaulting; None keeps the explicit-window refusal."""

    @staticmethod
    def tool(name: str) -> MeasurementTool:
        """Resolve the declared tool name or return a classified boundary refusal."""
        try:
            return MeasurementTool(name)
        except ValueError:
            raise ValidationError(
                code=gateway.BAD_INVOCATION,
                description=(
                    "The requested tool is not implemented by this measurement target. "
                    "Available tools: "
                    + ", ".join(sorted(member.value for member in MeasurementTool))
                    + "."
                ),
                context={"available": sorted(member.value for member in MeasurementTool)},
            ) from None

    def request_from(
        self,
        invocation: gateway.GatewayInvocation,
        tool: MeasurementTool,
        correlation_id: str,
    ) -> MeasurementToolRequest:
        """Build one authoritative semantic request with one injected timestamp."""
        computed_at = self.now()
        return request_from_semantic_intent(
            tool,
            invocation.arguments,
            SemanticMeasurementContext(
                catalog=self.catalog,
                contracts=self.contracts,
                entity_catalog=self.entity_catalog,
                scope=self.scope,
                today=computed_at.date(),
                computed_at=computed_at,
                correlation_id=correlation_id,
                default_window=(
                    None if self.window_defaults is None else self.window_defaults.default_window
                ),
            ),
        )

    @staticmethod
    def refusal_response(error: AdlyticsError, correlation_id: str) -> dict[str, Any]:
        """Return one safe, grounded refusal before the entrypoint issues no receipt."""
        grounding = grounded_refusal(
            error.user_visible_description,
            blocks_answer=True,
            identity_salt=correlation_id,
        )
        return gateway.to_json_safe(
            {
                "refused": True,
                "code": str(error.code),
                "reason": error.user_visible_description,
                "correlation_id": correlation_id,
                "metadata": {"grounding": grounding},
            }
        )
