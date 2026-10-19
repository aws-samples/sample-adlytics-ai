# SPDX-License-Identifier: MIT-0
"""Retail Health artifact codec invariants."""

from __future__ import annotations

from datetime import date

import pytest

from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.insights import InsightSlotId, unavailable_insight_slots
from domain.presentation.retail_health import (
    RETAIL_HEALTH_ROW_GRAIN,
    RetailComparison,
    RetailDistributorView,
    RetailHealthArtifact,
    RetailHealthOutcome,
    RetailMetricValue,
    retail_health_artifact_from_payload,
    retail_health_artifact_payload,
)
from domain.shared_kernel.values import DateRange

pytestmark = pytest.mark.unit

PERIOD = DateRange(date(2026, 9, 9), date(2026, 9, 15))
PRIOR = DateRange(date(2026, 9, 2), date(2026, 9, 8))
RETAIL_INSIGHTS = (
    InsightSlotId.DEMAND_SHAPE,
    InsightSlotId.RETURNS_PRESSURE,
    InsightSlotId.ORDERED_VS_SHIPPED,
    InsightSlotId.RETAIL_MOVERS,
)


def _empty_artifact() -> RetailHealthArtifact:
    return RetailHealthArtifact(
        RetailHealthOutcome.EMPTY,
        PERIOD,
        PRIOR,
        RetailDistributorView.MANUFACTURING,
        (),
        (),
        unavailable_insight_slots(RETAIL_INSIGHTS),
        (),
        (),
        0,
        None,
        None,
        "Retail actuals are not campaign outcomes.",
    )


def test_retail_health_codec_round_trips_the_exact_declared_insight_roster() -> None:
    artifact = _empty_artifact()

    payload = retail_health_artifact_payload(artifact)

    assert retail_health_artifact_from_payload(payload) == artifact


def test_retail_health_codec_rejects_reordered_or_extra_insight_slots() -> None:
    payload = retail_health_artifact_payload(_empty_artifact())
    insights = payload["insights"]
    assert isinstance(insights, list)
    insights.reverse()

    with pytest.raises(ValueError, match="roster and order"):
        retail_health_artifact_from_payload(payload)

    payload = retail_health_artifact_payload(_empty_artifact())
    payload["unexpected"] = True
    with pytest.raises(ValueError, match="fields are not exact"):
        retail_health_artifact_from_payload(payload)


def test_a_zero_prior_derived_row_decodes_without_a_percentage() -> None:
    """The deployed retail 1D defect: a product's prior-day rate of zero must decode.

    The wire carries canonical decimal text, so the exactness re-check has to test zero
    numerically; a truthy "0" string reaching the division 500'd the whole page.
    """
    period = DateRange(date(2026, 9, 15), date(2026, 9, 15))
    prior = DateRange(date(2026, 9, 14), date(2026, 9, 14))
    comparison = RetailComparison(
        "return_rate",
        RetailMetricValue("3.6000", Units(UnitKind.RATIO), period, RETAIL_HEALTH_ROW_GRAIN),
        RetailMetricValue("0", Units(UnitKind.RATIO), prior, RETAIL_HEALTH_ROW_GRAIN),
        "3.6000",
        None,
    )
    assert comparison.percentage_change is None
    assert comparison.current.units.kind is UnitKind.RATIO
