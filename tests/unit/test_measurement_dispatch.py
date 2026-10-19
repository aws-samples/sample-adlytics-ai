# SPDX-License-Identifier: MIT-0
"""Local ownership for measurement Gateway dispatch before AWS adapter composition."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest

from adapters.inbound.tool_handlers import gateway, measurement_dispatch
from adapters.inbound.tool_handlers.measurement import (
    MeasurementTool,
    MeasurementTooling,
    MeasurementToolRequest,
)
from adapters.inbound.tool_handlers.resolved_measurement import SemanticMeasurementContext
from domain.catalog.capability import Catalog
from domain.data_contracts.contract import DataContract
from domain.narrative.publication import RenderingKind
from domain.semantics.analytical_resolution import AuthorizedAnalyticalScope
from domain.shared_kernel.errors import ErrorCode, ValidationError
from ports.outbound.entity_catalog import AnalyticalEntityCatalogPort

pytestmark = pytest.mark.unit


@pytest.mark.covers("ADR-006", "ADR-062")
def test_declared_measurement_tool_name_resolves_and_unknown_name_is_a_typed_refusal() -> None:
    assert (
        measurement_dispatch.MeasurementDispatch.tool("measurement_metric_by_grain")
        is MeasurementTool.METRIC_BY_GRAIN
    )
    with pytest.raises(ValidationError) as caught:
        measurement_dispatch.MeasurementDispatch.tool("measurement_invent_a_metric")

    assert caught.value.code == gateway.BAD_INVOCATION
    assert "measurement_metric_by_grain" in caught.value.user_visible_description


@pytest.mark.covers("ADR-062")
def test_typed_measurement_refusal_keeps_a_blocking_grounded_rendering() -> None:
    error = ValidationError(ErrorCode("TEST", 1), "Fictional governed limitation.")

    response = measurement_dispatch.MeasurementDispatch.refusal_response(
        error, "123e4567-e89b-42d3-a456-426614174000"
    )

    assert response["refused"] is True
    assert response["reason"] == "Fictional governed limitation."
    rendering = response["metadata"]["grounding"]["renderings"][0]
    assert rendering["kind"] == RenderingKind.REFUSAL.value
    assert rendering["text"] == response["reason"]


@pytest.mark.covers("ADR-006")
def test_dispatch_builds_semantic_context_from_injected_runtime_and_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    returned = cast("MeasurementToolRequest", object())
    computed_at = datetime(2026, 9, 11, 12, tzinfo=UTC)
    dispatch = measurement_dispatch.MeasurementDispatch(
        catalog=cast("Catalog", object()),
        contracts=cast("tuple[DataContract, ...]", ()),
        entity_catalog=cast("AnalyticalEntityCatalogPort", object()),
        scope=cast("AuthorizedAnalyticalScope", object()),
        tooling=cast("MeasurementTooling", object()),
        now=lambda: computed_at,
    )
    invocation = cast(
        "gateway.GatewayInvocation",
        SimpleNamespace(arguments={"metric_reference": "impressions", "grains": ["date"]}),
    )

    def capture(
        tool: MeasurementTool,
        arguments: dict[str, object],
        context: object,
    ) -> MeasurementToolRequest:
        observed["tool"] = tool
        observed["arguments"] = arguments
        observed["context"] = context
        return returned

    monkeypatch.setattr(measurement_dispatch, "request_from_semantic_intent", capture)

    result = dispatch.request_from(
        invocation,
        MeasurementTool.METRIC_BY_GRAIN,
        "123e4567-e89b-42d3-a456-426614174000",
    )

    context = cast("SemanticMeasurementContext", observed["context"])
    assert result is returned
    assert observed["tool"] is MeasurementTool.METRIC_BY_GRAIN
    assert observed["arguments"] == {"metric_reference": "impressions", "grains": ["date"]}
    assert context.computed_at == computed_at
    assert context.today == computed_at.date()
    assert context.correlation_id == "123e4567-e89b-42d3-a456-426614174000"
    assert context.catalog is dispatch.catalog
    assert context.contracts == dispatch.contracts
    assert context.entity_catalog is dispatch.entity_catalog
    assert context.scope is dispatch.scope
