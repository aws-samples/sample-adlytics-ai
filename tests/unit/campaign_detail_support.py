# SPDX-License-Identifier: MIT-0
"""Builders for a complete selected campaign detail and delivery shared across tests."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import cast

from campaign_portfolio_support import campaign_display_signature
from change_support import SEMANTICS

from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.campaign_attribution import (
    PURCHASE_SERIES_GRAIN,
    SUMMARY_GRAIN,
    CampaignAttributionDetailArtifact,
    CampaignAttributionDetailRequest,
    CampaignDelivery,
    CampaignDeliverySlot,
    CampaignDeliveryValue,
    CampaignDspQuality,
    CampaignEvidenceCondition,
    CampaignEvidenceSlot,
    CampaignMetricValue,
    CampaignPurchasePoint,
    CampaignPurchaseSeries,
    CampaignRecoveryAction,
    CampaignSlotEvidence,
    CampaignSummary,
    build_campaign_attribution_detail,
    build_campaign_prior_comparison,
    campaign_delivery_accepted_statuses,
    campaign_delivery_comparison,
)
from domain.shared_kernel.provenance import MeasurementBasis, Suppression
from domain.shared_kernel.values import DateRange

PERIOD = DateRange(date(2026, 8, 1), date(2026, 8, 3))
PRIOR = DateRange(date(2026, 7, 29), date(2026, 7, 31))
COUNT = Units(UnitKind.COUNT)
RATIO = Units(UnitKind.RATIO)
USD = Units(UnitKind.CURRENCY, "USD")


def metric_value(
    value: str | int, kind: UnitKind, period: DateRange = PERIOD
) -> CampaignMetricValue:
    """One ad-attributed metric value in the fixture's currency conventions."""
    units = USD if kind is UnitKind.CURRENCY else Units(kind)
    number = value if isinstance(value, int) else Decimal(value)
    return CampaignMetricValue(number, units, MeasurementBasis.AD_ATTRIBUTED, period)


def _slot_units(slot: CampaignEvidenceSlot) -> Units:
    if slot in {
        CampaignEvidenceSlot.PURCHASES,
        CampaignEvidenceSlot.PRIOR_PURCHASES,
        CampaignEvidenceSlot.PURCHASE_SERIES,
    }:
        return COUNT
    if slot in {
        CampaignEvidenceSlot.ATTRIBUTED_ROAS,
        CampaignEvidenceSlot.PRIOR_ATTRIBUTED_ROAS,
    }:
        return RATIO
    return USD


def evidence_slots() -> tuple[CampaignSlotEvidence, ...]:
    """Every evidence slot available: the shape a SELECTED detail requires."""
    slots = tuple(CampaignEvidenceSlot)
    return tuple(
        CampaignSlotEvidence(
            slot=slot,
            condition=CampaignEvidenceCondition.AVAILABLE,
            requested_period=PRIOR if slot.value.startswith("prior_") else PERIOD,
            actual_coverage=(PRIOR if slot.value.startswith("prior_") else PERIOD,),
            coverage_holes=(),
            units=_slot_units(slot),
            grain=PURCHASE_SERIES_GRAIN
            if slot is CampaignEvidenceSlot.PURCHASE_SERIES
            else SUMMARY_GRAIN,
            basis=MeasurementBasis.AD_ATTRIBUTED,
            suppression=Suppression(False),
            row_count=3 if slot is CampaignEvidenceSlot.PURCHASE_SERIES else 1,
            truncated=False,
        )
        for slot in slots
    )


def detail_fixture() -> tuple[CampaignAttributionDetailArtifact, CampaignDelivery]:
    """One valid SELECTED detail and complete delivery over the fixture windows."""
    summary = CampaignSummary(
        PERIOD,
        metric_value(3, UnitKind.COUNT),
        metric_value("2.5000", UnitKind.RATIO),
        metric_value("12.0000", UnitKind.CURRENCY),
    )
    prior_summary = CampaignSummary(
        PRIOR,
        metric_value(1, UnitKind.COUNT, PRIOR),
        metric_value("2.0000", UnitKind.RATIO, PRIOR),
        metric_value("10.0000", UnitKind.CURRENCY, PRIOR),
    )
    series = CampaignPurchaseSeries(
        PERIOD,
        tuple(
            CampaignPurchasePoint(PERIOD.start + timedelta(days=offset), 1)
            for offset in range(PERIOD.days)
        ),
        COUNT,
        MeasurementBasis.AD_ATTRIBUTED,
    )
    detail = build_campaign_attribution_detail(
        CampaignAttributionDetailRequest(
            campaign_display_signature("Fictional summer campaign"),
            PERIOD,
            summary,
            series,
            evidence_slots(),
            PRIOR,
            build_campaign_prior_comparison(summary, prior_summary, SEMANTICS),
        )
    )
    delivery_summary = tuple(
        CampaignDeliveryValue(
            slot,
            metric_value(
                30
                if slot in {CampaignDeliverySlot.IMPRESSIONS, CampaignDeliverySlot.CLICKS}
                else "0.5000"
                if slot is CampaignDeliverySlot.CTR
                else "2.5000",
                UnitKind.COUNT
                if slot in {CampaignDeliverySlot.IMPRESSIONS, CampaignDeliverySlot.CLICKS}
                else UnitKind.RATIO
                if slot is CampaignDeliverySlot.CTR
                else UnitKind.CURRENCY,
            ),
        )
        for slot in CampaignDeliverySlot
    )
    delivery_comparison = campaign_delivery_comparison(
        delivery_summary,
        tuple(
            metric_value(
                20
                if slot in {CampaignDeliverySlot.IMPRESSIONS, CampaignDeliverySlot.CLICKS}
                else "0.2500"
                if slot is CampaignDeliverySlot.CTR
                else "1.5000",
                UnitKind.COUNT
                if slot in {CampaignDeliverySlot.IMPRESSIONS, CampaignDeliverySlot.CLICKS}
                else UnitKind.RATIO
                if slot is CampaignDeliverySlot.CTR
                else UnitKind.CURRENCY,
                PRIOR,
            )
            for slot in CampaignDeliverySlot
        ),
        SEMANTICS,
    )
    rows = cast(
        "tuple[dict[str, object], ...]",
        tuple(
            {
                "current": {
                    slot.value: (
                        10 + offset
                        if slot in {CampaignDeliverySlot.IMPRESSIONS, CampaignDeliverySlot.CLICKS}
                        else Decimal("0.5000")
                    )
                    for slot in CampaignDeliverySlot
                },
                "date": (PERIOD.start + timedelta(days=offset)).isoformat(),
                "prior": {
                    slot.value: (
                        5
                        if slot in {CampaignDeliverySlot.IMPRESSIONS, CampaignDeliverySlot.CLICKS}
                        else Decimal("0.2500")
                    )
                    for slot in CampaignDeliverySlot
                },
            }
            for offset in range(PERIOD.days)
        ),
    )
    delivery = CampaignDelivery(
        "complete",
        delivery_summary,
        PRIOR,
        delivery_comparison,
        rows,
        rows,
        CampaignDspQuality(
            "available",
            metric_value(20, UnitKind.COUNT),
            metric_value(10, UnitKind.COUNT),
            metric_value(12, UnitKind.COUNT),
            metric_value("0.5000", UnitKind.RATIO),
            None,
        ),
        campaign_delivery_accepted_statuses(),
        CampaignRecoveryAction.CHANGE_PERIOD.value,
    )
    return detail, delivery
