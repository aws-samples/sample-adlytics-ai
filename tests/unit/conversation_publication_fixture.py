# SPDX-License-Identifier: MIT-0
"""Typed fictional managed-publication fixtures shared by focused unit tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from domain.conversation.tool_custody import (
    AgentToolCustodyV1,
    ToolCustodyDispositionV1,
    ValidatedToolResultV1,
)
from domain.narrative.shaped_response import (
    ClaimField,
    FreshnessStatus,
    Grain,
    MeasurementBasis,
    Metadata,
    ResponseData,
    ShapedResponse,
    Suppression,
    UnitKind,
    Units,
)

FIXTURE_TOOL_RESULT_REF = "3b2f7d0e-6c4a-4f9e-9b6d-2a1c8e7f5d40"
"""Fictional UUID4 standing in for one governed tool response correlation ID."""


def shaped_response() -> ShapedResponse:
    """Build one minimal fully grounded fictional tool response."""
    units = Units(kind=UnitKind.COUNT)
    return ShapedResponse(
        data=ResponseData(
            values=({"metric": "fictional_count", "count": Decimal("7")},),
            units=units,
            grain=Grain(dimensions=("date",)),
            claim_fields=(ClaimField("count", units, label="Fictional count"),),
        ),
        narrative=("Fictional result scaffolding.",),
        metadata=Metadata(
            facts_read=("fictional_fact",),
            measurement_basis=(MeasurementBasis.AD_ATTRIBUTED,),
            coverage_start=datetime(2026, 1, 1, tzinfo=UTC),
            coverage_end=datetime(2026, 1, 1, tzinfo=UTC),
            freshness_status=FreshnessStatus.FRESH,
            suppression=Suppression(rows_suppressed=False),
            row_count=1,
            computed_at=datetime(2026, 1, 1, tzinfo=UTC),
            correlation_id=FIXTURE_TOOL_RESULT_REF,
        ),
    )


def custody(tool_use_ref: str = "tool-use-1") -> AgentToolCustodyV1:
    """Bind the fictional response to one current-turn catalog tool result."""
    return AgentToolCustodyV1.from_results(
        (
            ValidatedToolResultV1(
                tool_use_ref=tool_use_ref,
                tool_name="catalog_list_metrics",
                ordinal=1,
                disposition=ToolCustodyDispositionV1.SUCCEEDED,
                tool_result_ref=FIXTURE_TOOL_RESULT_REF,
                grounding_set=shaped_response().metadata.grounding,
            ),
        )
    )
