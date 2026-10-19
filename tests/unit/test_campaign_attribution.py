# SPDX-License-Identifier: MIT-0
"""Campaign-attribution list/detail state reduction and evidence invariants."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from change_support import SEMANTICS

from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.campaign_attribution import (
    CAMPAIGN_ATTRIBUTION_BOUNDARIES,
    CAMPAIGN_ATTRIBUTION_DETAIL_ARTIFACT_TYPE,
    CAMPAIGN_ATTRIBUTION_DETAIL_ARTIFACT_VERSION,
    CAMPAIGN_SUMMARY_GOVERNED_METRICS,
    PURCHASE_SERIES_GRAIN,
    SUMMARY_GRAIN,
    CampaignAttributionDetailArtifact,
    CampaignAttributionDetailRequest,
    CampaignAttributionDetailV2,
    CampaignComparisonReason,
    CampaignDelivery,
    CampaignDeliveryAnswer,
    CampaignDeliverySlot,
    CampaignDeliveryValue,
    CampaignDetailOutcome,
    CampaignDspQuality,
    CampaignEvidenceCondition,
    CampaignEvidenceSlot,
    CampaignEvidenceStatus,
    CampaignEvidenceStatusCondition,
    CampaignEvidenceStatusRequest,
    CampaignInsufficiencyReason,
    CampaignListEvidence,
    CampaignListOutcome,
    CampaignListReason,
    CampaignMetricComparison,
    CampaignMetricValue,
    CampaignPageKind,
    CampaignPriorComparison,
    CampaignPurchasePoint,
    CampaignPurchaseSeries,
    CampaignRecoveryAction,
    CampaignSelectionCapability,
    CampaignSelectionChoice,
    CampaignSelectionListArtifact,
    CampaignSelectionListRequest,
    CampaignSlotEvidence,
    CampaignSummary,
    CampaignSummarySlot,
    SafeCampaignDisplaySignature,
    build_campaign_attribution_detail,
    build_campaign_prior_comparison,
    build_campaign_selection_list,
    campaign_attribution_detail_payload,
    campaign_attribution_detail_v2_from_payload,
    campaign_attribution_detail_v2_payload,
    campaign_delivery_accepted_statuses,
    campaign_delivery_comparison,
    campaign_delivery_unavailable,
    campaign_detail_evidence_status,
    campaign_evidence_status,
    campaign_page_artifact_from_payload,
    campaign_selection_list_payload,
    derive_campaign_cpc,
)
from domain.presentation.campaigns import EffectiveNameStatus
from domain.presentation.change import ChangeOperands, build_change
from domain.presentation.currency_display import without_display_values
from domain.shared_kernel.errors import InvariantError
from domain.shared_kernel.provenance import MeasurementBasis, Suppression
from domain.shared_kernel.values import DateRange

pytestmark = pytest.mark.unit

PERIOD = DateRange(date(2026, 8, 1), date(2026, 8, 3))
PRIOR = DateRange(date(2026, 7, 29), date(2026, 7, 31))
COUNT = Units(UnitKind.COUNT)
RATIO = Units(UnitKind.RATIO)
USD = Units(UnitKind.CURRENCY, "USD")
# Fictional display labels for the detail's dictionary ids; the handler reads the dictionary.
DETAIL_LABELS = {
    metric: metric.replace("_", " ").capitalize()
    for metric in (
        "purchases",
        "roas_attributed_14d",
        "ad_spend",
        "impressions",
        "clicks",
        "ctr",
        "cpc",
        "cpm",
    )
}


def _v2(
    detail: CampaignAttributionDetailArtifact, delivery: CampaignDelivery
) -> CampaignAttributionDetailV2:
    """Serve a detail with the coverage block written from its delivery's statuses (ADR-119)."""
    return CampaignAttributionDetailV2(
        detail,
        delivery,
        evidence_status=campaign_detail_evidence_status(
            detail.period, delivery.evidence, DETAIL_LABELS
        ),
    )


def _signature(
    name: str | None = "Fictional summer campaign",
    *,
    status: EffectiveNameStatus = EffectiveNameStatus.AVAILABLE,
) -> SafeCampaignDisplaySignature:
    valid_from = date(2026, 7, 1) if name is not None else None
    return SafeCampaignDisplaySignature(
        effective_name=name,
        channel="Sponsored Products",
        marketplace="United States",
        platform="Amazon Ads",
        effective_name_status=status,
        effective_name_valid_from=valid_from,
    )


def _choice(name: str, token: str) -> CampaignSelectionChoice:
    return CampaignSelectionChoice(
        _signature(name),
        CampaignSelectionCapability(token, datetime(2026, 9, 1, tzinfo=UTC)),
    )


def _list_evidence() -> tuple[CampaignListEvidence, ...]:
    return (CampaignListEvidence("Governed campaign catalog", PERIOD, (PERIOD,)),)


def _metric(value: str | int, kind: UnitKind, period: DateRange = PERIOD) -> CampaignMetricValue:
    units = USD if kind is UnitKind.CURRENCY else Units(kind)
    number = value if isinstance(value, int) else Decimal(value)
    return CampaignMetricValue(number, units, MeasurementBasis.AD_ATTRIBUTED, period)


def _summary(
    purchases: CampaignMetricValue | None = None,
    roas: CampaignMetricValue | None = None,
    spend: CampaignMetricValue | None = None,
) -> CampaignSummary:
    return CampaignSummary(
        PERIOD,
        _metric(3, UnitKind.COUNT) if purchases is None else purchases,
        _metric("2.5000", UnitKind.RATIO) if roas is None else roas,
        _metric("12.0000", UnitKind.CURRENCY) if spend is None else spend,
    )


def _series(*values: int, period: DateRange = PERIOD) -> CampaignPurchaseSeries:
    points = tuple(
        CampaignPurchasePoint(date(2026, 8, index + 1), value) for index, value in enumerate(values)
    )
    return CampaignPurchaseSeries(
        period,
        points,
        COUNT,
        MeasurementBasis.AD_ATTRIBUTED,
    )


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


def _slot_evidence(
    slot: CampaignEvidenceSlot,
    condition: CampaignEvidenceCondition = CampaignEvidenceCondition.AVAILABLE,
    details: tuple[DateRange, int | None] = (PERIOD, None),
) -> CampaignSlotEvidence:
    period, row_count = details
    actual: tuple[DateRange, ...] = (period,)
    holes: tuple[DateRange, ...] = ()
    suppression = Suppression(False)
    truncated = False
    units: Units | None = _slot_units(slot)
    basis: MeasurementBasis | None = MeasurementBasis.AD_ATTRIBUTED
    if row_count is None:
        row_count = 3 if slot is CampaignEvidenceSlot.PURCHASE_SERIES else 1
    if condition is CampaignEvidenceCondition.EMPTY_COVERED:
        row_count = 0
        units = None
        basis = None
    elif condition in {
        CampaignEvidenceCondition.MISSING_CURRENT_PUBLICATION,
        CampaignEvidenceCondition.MISSING_COMPARATOR_PUBLICATION,
    }:
        actual = ()
        row_count = 0
        units = None
        basis = None
    elif condition is CampaignEvidenceCondition.SUPPRESSED:
        row_count = 0
        suppression = Suppression(True, "source threshold")
    elif condition is CampaignEvidenceCondition.TRUNCATED:
        truncated = True
    elif condition is CampaignEvidenceCondition.COVERAGE_HOLE:
        actual = ()
        holes = (DateRange(period.start, period.start),)
        row_count = 0
        units = None
        basis = None
    elif condition is CampaignEvidenceCondition.INCOMPATIBLE_SCOPE:
        row_count = 0
    grain = PURCHASE_SERIES_GRAIN if slot is CampaignEvidenceSlot.PURCHASE_SERIES else SUMMARY_GRAIN
    return CampaignSlotEvidence(
        slot=slot,
        condition=condition,
        requested_period=period,
        actual_coverage=actual,
        coverage_holes=holes,
        units=units,
        grain=grain,
        basis=basis,
        suppression=suppression,
        row_count=row_count,
        truncated=truncated,
    )


def _current_evidence(
    overrides: dict[CampaignEvidenceSlot, CampaignEvidenceCondition] | None = None,
    *,
    series_rows: int = 3,
) -> tuple[CampaignSlotEvidence, ...]:
    conditions = overrides or {}
    return tuple(
        _slot_evidence(
            slot,
            conditions.get(slot, CampaignEvidenceCondition.AVAILABLE),
            (PERIOD, series_rows if slot is CampaignEvidenceSlot.PURCHASE_SERIES else None),
        )
        for slot in (
            CampaignEvidenceSlot.PURCHASES,
            CampaignEvidenceSlot.ATTRIBUTED_ROAS,
            CampaignEvidenceSlot.SPEND,
            CampaignEvidenceSlot.PURCHASE_SERIES,
        )
    )


def _prior_evidence(
    overrides: dict[CampaignEvidenceSlot, CampaignEvidenceCondition] | None = None,
) -> tuple[CampaignSlotEvidence, ...]:
    conditions = overrides or {}
    return tuple(
        _slot_evidence(
            slot,
            conditions.get(slot, CampaignEvidenceCondition.AVAILABLE),
            (PRIOR, None),
        )
        for slot in (
            CampaignEvidenceSlot.PRIOR_PURCHASES,
            CampaignEvidenceSlot.PRIOR_ATTRIBUTED_ROAS,
            CampaignEvidenceSlot.PRIOR_SPEND,
        )
    )


def _valid_comparison(
    summary: CampaignSummary,
    *,
    purchase_prior: CampaignMetricValue | None = None,
    purchase_reason: CampaignComparisonReason | None = None,
) -> CampaignPriorComparison:
    purchase_prior = purchase_prior or _metric(1, UnitKind.COUNT, PRIOR)
    purchase_change: int | Decimal | None = 2
    purchase_percent: str | None = "+200.00%"
    if purchase_reason is CampaignComparisonReason.ONE_SIDED:
        purchase_prior = None
        purchase_change = None
        purchase_percent = None
    elif purchase_prior.value == 0:
        purchase_change = 3
        purchase_percent = None
        purchase_reason = CampaignComparisonReason.PRIOR_ZERO_PERCENTAGE_UNDEFINED
    return CampaignPriorComparison(
        PERIOD,
        PRIOR,
        tuple(
            _with_change(metric)
            for metric in (
                CampaignMetricComparison(
                    CampaignSummarySlot.PURCHASES,
                    summary.purchases,
                    purchase_prior,
                    purchase_change,
                    purchase_percent,
                    purchase_reason,
                ),
                CampaignMetricComparison(
                    CampaignSummarySlot.ATTRIBUTED_ROAS,
                    summary.attributed_roas,
                    _metric("2.0000", UnitKind.RATIO, PRIOR),
                    Decimal("0.5000"),
                    "+25.00%",
                    None,
                ),
                CampaignMetricComparison(
                    CampaignSummarySlot.SPEND,
                    summary.spend,
                    _metric("10.0000", UnitKind.CURRENCY, PRIOR),
                    Decimal("2.0000"),
                    "+20.00%",
                    None,
                ),
            )
        ),
    )


def _with_change(metric: CampaignMetricComparison) -> CampaignMetricComparison:
    """Attach the governed change block every two-sided comparison carries (ADR-121)."""
    if metric.current is None or metric.prior is None:
        return metric
    return replace(
        metric,
        change=build_change(
            ChangeOperands(metric.current.value, metric.prior.value, metric.current.units),
            SEMANTICS.for_metric(CAMPAIGN_SUMMARY_GOVERNED_METRICS[metric.slot]),
        ),
    )


class TestCampaignSelectionList:
    def test_choices_are_safely_ordered_and_keep_opaque_capabilities(self) -> None:
        artifact = build_campaign_selection_list(
            CampaignSelectionListRequest(
                PERIOD,
                (_choice("Zulu", "opaque-z"), _choice("Alpha", "opaque-a")),
                _list_evidence(),
                truncated=True,
            )
        )

        assert artifact.kind is CampaignPageKind.CAMPAIGN_LIST
        assert artifact.outcome is CampaignListOutcome.CHOICES
        assert [choice.signature.effective_name for choice in artifact.choices] == ["Alpha", "Zulu"]
        assert [choice.capability.value for choice in artifact.choices] == ["opaque-a", "opaque-z"]
        assert artifact.truncated is True
        assert artifact.reason_codes == ()

    def test_no_choices_is_typed_covered_empty_with_a_condition_backed_action(self) -> None:
        artifact = build_campaign_selection_list(
            CampaignSelectionListRequest(
                PERIOD,
                (),
                _list_evidence(),
                recovery_actions=(CampaignRecoveryAction.CHANGE_PERIOD,),
            )
        )

        assert artifact.outcome is CampaignListOutcome.EMPTY
        assert artifact.choices == ()
        assert artifact.reason_codes == (CampaignListReason.EMPTY_COVERED,)
        assert artifact.recovery_actions == (CampaignRecoveryAction.CHANGE_PERIOD,)

    def test_residual_safe_signature_collision_refuses_every_choice(self) -> None:
        signature = _signature()
        artifact = build_campaign_selection_list(
            CampaignSelectionListRequest(
                PERIOD,
                (
                    CampaignSelectionChoice(
                        signature,
                        CampaignSelectionCapability(
                            "opaque-first", datetime(2026, 9, 1, tzinfo=UTC)
                        ),
                    ),
                    CampaignSelectionChoice(
                        signature,
                        CampaignSelectionCapability(
                            "opaque-second", datetime(2026, 9, 1, tzinfo=UTC)
                        ),
                    ),
                ),
                _list_evidence(),
            )
        )

        assert artifact.outcome is CampaignListOutcome.AMBIGUOUS
        assert artifact.choices == ()
        assert artifact.reason_codes == (CampaignListReason.SAFE_SIGNATURE_COLLISION,)
        assert artifact.recovery_actions == ()

    def test_list_blockers_produce_one_canonically_ordered_insufficient_state(self) -> None:
        artifact = build_campaign_selection_list(
            CampaignSelectionListRequest(
                PERIOD,
                (_choice("Ignored while blocked", "opaque"),),
                _list_evidence(),
                blockers=(CampaignListReason.TRUNCATED, CampaignListReason.MISSING_CATALOG),
                recovery_actions=(CampaignRecoveryAction.CONTACT_WORKSPACE_DATA_OWNER,),
            )
        )

        assert artifact.outcome is CampaignListOutcome.INSUFFICIENT
        assert artifact.choices == ()
        assert artifact.reason_codes == (
            CampaignListReason.MISSING_CATALOG,
            CampaignListReason.TRUNCATED,
        )

    @pytest.mark.parametrize(
        ("status", "name"),
        [
            (EffectiveNameStatus.AVAILABLE, "Fictional campaign"),
            (EffectiveNameStatus.UNAVAILABLE_FOR_WINDOW, "Prior fictional label"),
            (EffectiveNameStatus.MISSING, None),
        ],
    )
    def test_safe_signature_supports_every_effective_name_status(
        self,
        status: EffectiveNameStatus,
        name: str | None,
    ) -> None:
        signature = _signature(name, status=status)

        assert signature.effective_name_status is status
        assert signature.effective_name == name

    def test_safe_artifact_has_no_canonical_selector_fields(self) -> None:
        artifact = build_campaign_selection_list(
            CampaignSelectionListRequest(
                PERIOD,
                (_choice("Fictional campaign", "opaque-choice"),),
                _list_evidence(),
            )
        )
        payload = json.dumps(asdict(artifact), default=str)

        assert "campaign_key" not in payload
        assert "channel_key" not in payload
        assert "marketplace_key" not in payload
        assert "platform_key" not in payload

    def test_list_artifact_refuses_fields_from_a_different_outcome(self) -> None:
        with pytest.raises(ValueError, match="contradict"):
            CampaignSelectionListArtifact(
                CampaignListOutcome.CHOICES,
                PERIOD,
                _list_evidence(),
                False,
                (),
                (CampaignListReason.EMPTY_COVERED,),
                (),
            )

    def test_canonical_list_payload_round_trips_exact_safe_fields_and_iso_expiry(self) -> None:
        artifact = build_campaign_selection_list(
            CampaignSelectionListRequest(
                PERIOD,
                (_choice("Fictional campaign", "opaque-choice"),),
                _list_evidence(),
            )
        )

        payload = campaign_selection_list_payload(artifact)

        assert payload["kind"] == "campaign_list"
        choice = cast("list[dict[str, object]]", payload["choices"])[0]
        capability = cast("dict[str, object]", choice["capability"])
        assert capability == {
            "expires_at": "2026-09-01T00:00:00+00:00",
            "selection_reference": "opaque-choice",
        }
        assert campaign_page_artifact_from_payload(payload) == artifact
        with pytest.raises(ValueError, match="fields are not exact"):
            campaign_page_artifact_from_payload({**payload, "campaign_key": "forbidden"})

    def test_selection_capability_requires_timezone_aware_expiry(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            CampaignSelectionCapability(
                "opaque", datetime(2026, 9, 1, tzinfo=UTC).replace(tzinfo=None)
            )


class TestCampaignDetailReduction:
    def test_complete_current_evidence_produces_selected_detail(self) -> None:
        summary = _summary()
        series = _series(1, 0, 2)
        artifact = build_campaign_attribution_detail(
            CampaignAttributionDetailRequest(
                _signature(),
                PERIOD,
                summary,
                series,
                _current_evidence(series_rows=3),
            )
        )

        assert artifact.kind is CampaignPageKind.CAMPAIGN_DETAIL
        assert artifact.outcome is CampaignDetailOutcome.SELECTED
        assert artifact.summary.purchases is not None
        assert artifact.summary.purchases.units == COUNT
        assert artifact.summary.purchases.grain == SUMMARY_GRAIN
        assert artifact.purchase_series is not None
        assert artifact.purchase_series.units == COUNT
        assert artifact.purchase_series.grain == PURCHASE_SERIES_GRAIN
        assert artifact.purchase_series.points[1].purchases == 0
        assert artifact.boundaries == CAMPAIGN_ATTRIBUTION_BOUNDARIES
        assert artifact.insufficiency_reasons == ()

    def test_canonical_detail_payload_round_trips_decimal_text_units_and_boundaries(self) -> None:
        artifact = build_campaign_attribution_detail(
            CampaignAttributionDetailRequest(
                _signature(),
                PERIOD,
                _summary(),
                _series(1, 0, 2),
                _current_evidence(series_rows=3),
            )
        )

        payload = campaign_attribution_detail_payload(artifact)
        summary = cast("dict[str, object]", payload["summary"])
        roas = cast("dict[str, object]", summary["attributed_roas"])
        spend = cast("dict[str, object]", summary["spend"])

        assert roas["value"] == "2.5000"
        assert roas["units"] == {"currency_code": None, "kind": "ratio"}
        assert spend["value"] == "12.0000"
        assert spend["basis"] == "ad_attributed"
        assert payload["boundaries"] == list(CAMPAIGN_ATTRIBUTION_BOUNDARIES)
        assert campaign_page_artifact_from_payload(payload) == artifact

    def test_reducer_canonically_orders_slot_evidence(self) -> None:
        artifact = build_campaign_attribution_detail(
            CampaignAttributionDetailRequest(
                _signature(),
                PERIOD,
                _summary(),
                _series(1, 0, 2),
                tuple(reversed(_current_evidence())),
            )
        )

        assert tuple(item.slot for item in artifact.evidence_by_slot) == (
            CampaignEvidenceSlot.PURCHASES,
            CampaignEvidenceSlot.ATTRIBUTED_ROAS,
            CampaignEvidenceSlot.SPEND,
            CampaignEvidenceSlot.PURCHASE_SERIES,
        )

    @pytest.mark.parametrize(
        ("condition", "expected"),
        [
            (CampaignEvidenceCondition.EMPTY_COVERED, CampaignInsufficiencyReason.EMPTY_COVERED),
            (
                CampaignEvidenceCondition.MISSING_CURRENT_PUBLICATION,
                CampaignInsufficiencyReason.MISSING_CURRENT_PUBLICATION,
            ),
            (CampaignEvidenceCondition.SUPPRESSED, CampaignInsufficiencyReason.SUPPRESSED),
            (CampaignEvidenceCondition.TRUNCATED, CampaignInsufficiencyReason.TRUNCATED),
            (CampaignEvidenceCondition.COVERAGE_HOLE, CampaignInsufficiencyReason.COVERAGE_HOLE),
            (
                CampaignEvidenceCondition.INCOMPATIBLE_SCOPE,
                CampaignInsufficiencyReason.INCOMPATIBLE_SCOPE,
            ),
        ],
    )
    def test_every_current_evidence_failure_has_its_own_reason(
        self,
        condition: CampaignEvidenceCondition,
        expected: CampaignInsufficiencyReason,
    ) -> None:
        summary = CampaignSummary(
            PERIOD,
            None,
            _metric("2.5000", UnitKind.RATIO),
            _metric("12.0000", UnitKind.CURRENCY),
        )
        evidence = _current_evidence({CampaignEvidenceSlot.PURCHASES: condition})
        artifact = build_campaign_attribution_detail(
            CampaignAttributionDetailRequest(
                _signature(),
                PERIOD,
                summary,
                _series(1, 0, 2),
                evidence,
            )
        )

        assert artifact.outcome is CampaignDetailOutcome.INSUFFICIENT
        assert expected in artifact.insufficiency_reasons
        assert artifact.summary.purchases is None

    def test_missing_comparator_publication_is_not_current_missing(self) -> None:
        evidence = (
            *_current_evidence(),
            *_prior_evidence(
                {
                    CampaignEvidenceSlot.PRIOR_PURCHASES: (
                        CampaignEvidenceCondition.MISSING_COMPARATOR_PUBLICATION
                    )
                }
            ),
        )
        artifact = build_campaign_attribution_detail(
            CampaignAttributionDetailRequest(
                _signature(),
                PERIOD,
                _summary(),
                _series(1, 0, 2),
                evidence,
                prior_period=PRIOR,
            )
        )

        assert artifact.outcome is CampaignDetailOutcome.INSUFFICIENT
        assert artifact.insufficiency_reasons == (
            CampaignInsufficiencyReason.MISSING_COMPARATOR_PUBLICATION,
        )

    def test_available_one_point_series_is_typed_too_short(self) -> None:
        artifact = build_campaign_attribution_detail(
            CampaignAttributionDetailRequest(
                _signature(),
                PERIOD,
                CampaignSummary(
                    PERIOD,
                    _metric(1, UnitKind.COUNT),
                    _metric("2.5000", UnitKind.RATIO),
                    _metric("12.0000", UnitKind.CURRENCY),
                ),
                _series(1),
                _current_evidence(series_rows=1),
            )
        )

        assert artifact.outcome is CampaignDetailOutcome.INSUFFICIENT
        assert artifact.insufficiency_reasons == (CampaignInsufficiencyReason.TOO_SHORT_SERIES,)

    def test_unordered_purchase_points_are_refused(self) -> None:
        with pytest.raises(ValueError, match="strictly ordered"):
            CampaignPurchaseSeries(
                PERIOD,
                (
                    CampaignPurchasePoint(date(2026, 8, 2), 1),
                    CampaignPurchasePoint(date(2026, 8, 1), 2),
                ),
                COUNT,
                MeasurementBasis.AD_ATTRIBUTED,
            )

    def test_complete_but_unreconciled_purchases_raise_invariant_error(self) -> None:
        with pytest.raises(InvariantError, match="do not reconcile"):
            build_campaign_attribution_detail(
                CampaignAttributionDetailRequest(
                    _signature(),
                    PERIOD,
                    _summary(),
                    _series(1, 1, 2),
                    _current_evidence(),
                )
            )

    def test_represented_zero_is_selected_and_is_not_covered_empty(self) -> None:
        zero_summary = CampaignSummary(
            PERIOD,
            _metric(0, UnitKind.COUNT),
            _metric("0.0000", UnitKind.RATIO),
            _metric("0.0000", UnitKind.CURRENCY),
        )
        artifact = build_campaign_attribution_detail(
            CampaignAttributionDetailRequest(
                _signature(),
                PERIOD,
                zero_summary,
                _series(0, 0, 0),
                _current_evidence(),
            )
        )

        assert artifact.outcome is CampaignDetailOutcome.SELECTED
        assert artifact.summary.purchases is not None
        assert artifact.summary.purchases.value == 0
        assert artifact.summary.purchases.units == COUNT
        assert artifact.summary.purchases.grain == SUMMARY_GRAIN

    def test_purchase_count_and_roas_are_independent_governed_values(self) -> None:
        summary = CampaignSummary(
            PERIOD,
            _metric(3, UnitKind.COUNT),
            _metric("91.1250", UnitKind.RATIO),
            _metric("12.0000", UnitKind.CURRENCY),
        )
        artifact = build_campaign_attribution_detail(
            CampaignAttributionDetailRequest(
                _signature(),
                PERIOD,
                summary,
                _series(1, 0, 2),
                _current_evidence(),
            )
        )

        purchases = artifact.summary.purchases
        attributed_roas = artifact.summary.attributed_roas
        assert artifact.outcome is CampaignDetailOutcome.SELECTED
        assert purchases is not None
        assert purchases.units.kind is UnitKind.COUNT
        assert attributed_roas is not None
        assert attributed_roas.units.kind is UnitKind.RATIO

    def test_available_evidence_without_a_value_is_incompatible_scope(self) -> None:
        summary = CampaignSummary(
            PERIOD,
            None,
            _metric("2.5000", UnitKind.RATIO),
            _metric("12.0000", UnitKind.CURRENCY),
        )
        artifact = build_campaign_attribution_detail(
            CampaignAttributionDetailRequest(
                _signature(),
                PERIOD,
                summary,
                _series(1, 0, 2),
                _current_evidence(),
            )
        )

        assert artifact.outcome is CampaignDetailOutcome.INSUFFICIENT
        assert artifact.insufficiency_reasons == (CampaignInsufficiencyReason.INCOMPATIBLE_SCOPE,)

    def test_selected_artifact_refuses_insufficiency_reasons(self) -> None:
        with pytest.raises(ValueError, match="selected"):
            CampaignAttributionDetailArtifact(
                CampaignDetailOutcome.SELECTED,
                _signature(),
                PERIOD,
                None,
                _summary(),
                _series(1, 0, 2),
                None,
                _current_evidence(),
                CAMPAIGN_ATTRIBUTION_BOUNDARIES,
                (CampaignInsufficiencyReason.EMPTY_COVERED,),
            )


class TestCampaignPriorComparison:
    def test_domain_builder_computes_exact_changes_and_formatted_percentages(self) -> None:
        current = _summary()
        prior = CampaignSummary(
            PRIOR,
            _metric(1, UnitKind.COUNT, PRIOR),
            _metric("2.0000", UnitKind.RATIO, PRIOR),
            _metric("10.0000", UnitKind.CURRENCY, PRIOR),
        )

        comparison = build_campaign_prior_comparison(current, prior, SEMANTICS)

        assert comparison is not None
        assert tuple(metric.absolute_change for metric in comparison.metrics) == (
            2,
            Decimal("0.5000"),
            Decimal("2.0000"),
        )
        assert tuple(metric.percentage_change for metric in comparison.metrics) == (
            "+200.00%",
            "+25.00%",
            "+20.00%",
        )

    def test_previous_equal_period_comparison_is_selected(self) -> None:
        summary = _summary()
        comparison = _valid_comparison(summary)
        artifact = build_campaign_attribution_detail(
            CampaignAttributionDetailRequest(
                _signature(),
                PERIOD,
                summary,
                _series(1, 0, 2),
                (*_current_evidence(), *_prior_evidence()),
                prior_period=PRIOR,
                comparison=comparison,
            )
        )

        assert artifact.outcome is CampaignDetailOutcome.SELECTED
        assert artifact.prior_period == PRIOR
        assert artifact.comparison is not None
        assert tuple(item.slot for item in artifact.comparison.metrics) == (
            CampaignSummarySlot.PURCHASES,
            CampaignSummarySlot.ATTRIBUTED_ROAS,
            CampaignSummarySlot.SPEND,
        )
        assert artifact.comparison.metrics[0].absolute_change == 2
        assert artifact.comparison.metrics[0].percentage_change == "+200.00%"

    def test_unequal_or_nonadjacent_comparator_is_refused(self) -> None:
        wrong_prior = DateRange(date(2026, 7, 1), date(2026, 7, 3))
        summary = _summary()

        with pytest.raises(ValueError, match="previous equal-length"):
            CampaignPriorComparison(wrong_prior, PERIOD, _valid_comparison(summary).metrics)

    def test_prior_zero_keeps_change_and_has_no_percentage(self) -> None:
        summary = _summary()
        comparison = _valid_comparison(
            summary,
            purchase_prior=_metric(0, UnitKind.COUNT, PRIOR),
        )
        artifact = build_campaign_attribution_detail(
            CampaignAttributionDetailRequest(
                _signature(),
                PERIOD,
                summary,
                _series(1, 0, 2),
                (*_current_evidence(), *_prior_evidence()),
                prior_period=PRIOR,
                comparison=comparison,
            )
        )

        purchase = artifact.comparison.metrics[0] if artifact.comparison is not None else None
        assert artifact.outcome is CampaignDetailOutcome.SELECTED
        assert purchase is not None
        assert purchase.absolute_change == 3
        assert purchase.percentage_change is None
        assert purchase.reason is CampaignComparisonReason.PRIOR_ZERO_PERCENTAGE_UNDEFINED

    def test_one_sided_comparison_stays_absent_and_makes_detail_insufficient(self) -> None:
        summary = _summary()
        comparison = _valid_comparison(
            summary,
            purchase_reason=CampaignComparisonReason.ONE_SIDED,
        )
        artifact = build_campaign_attribution_detail(
            CampaignAttributionDetailRequest(
                _signature(),
                PERIOD,
                summary,
                _series(1, 0, 2),
                (*_current_evidence(), *_prior_evidence()),
                prior_period=PRIOR,
                comparison=comparison,
            )
        )

        purchase = artifact.comparison.metrics[0] if artifact.comparison is not None else None
        assert artifact.outcome is CampaignDetailOutcome.INSUFFICIENT
        assert artifact.insufficiency_reasons == (CampaignInsufficiencyReason.ONE_SIDED_COMPARISON,)
        assert purchase is not None
        assert purchase.prior is None
        assert purchase.absolute_change is None
        assert purchase.percentage_change is None

    def test_comparison_refuses_fabricated_change_for_one_sided_value(self) -> None:
        with pytest.raises(ValueError, match="cannot fabricate"):
            CampaignMetricComparison(
                CampaignSummarySlot.PURCHASES,
                _metric(3, UnitKind.COUNT),
                None,
                3,
                "+100.00%",
                CampaignComparisonReason.ONE_SIDED,
            )


class TestCampaignAttributionDetailV2:
    def test_v2_delivery_extension_round_trips_and_keeps_v1_detail_immutable(self) -> None:
        detail = build_campaign_attribution_detail(
            CampaignAttributionDetailRequest(
                _signature(),
                PERIOD,
                _summary(),
                _series(1, 0, 2),
                (*_current_evidence(series_rows=3), *_prior_evidence()),
                PRIOR,
                _valid_comparison(_summary()),
            )
        )
        artifact = _v2(detail, campaign_delivery_unavailable(detail))

        payload = campaign_attribution_detail_v2_payload(artifact)

        assert CAMPAIGN_ATTRIBUTION_DETAIL_ARTIFACT_TYPE == "CampaignAttributionDetail"
        assert CAMPAIGN_ATTRIBUTION_DETAIL_ARTIFACT_VERSION == 2
        assert payload["summary"] == campaign_attribution_detail_payload(detail)["summary"]
        delivery = cast("dict[str, object]", payload["delivery"])
        assert delivery["outcome"] == "insufficient"
        assert delivery["narration_reference"] is None
        assert campaign_attribution_detail_v2_from_payload(payload) == artifact

    def test_v2_delivery_codec_refuses_noncanonical_extra_field(self) -> None:
        detail = build_campaign_attribution_detail(
            CampaignAttributionDetailRequest(
                _signature(),
                PERIOD,
                _summary(),
                _series(1, 0, 2),
                (*_current_evidence(series_rows=3), *_prior_evidence()),
                PRIOR,
                _valid_comparison(_summary()),
            )
        )
        payload = campaign_attribution_detail_v2_payload(
            _v2(detail, campaign_delivery_unavailable(detail))
        )

        with pytest.raises(ValueError, match="fields are not exact"):
            campaign_attribution_detail_v2_from_payload({**payload, "campaign_key": "forbidden"})


def _delivery_metric(
    slot: CampaignDeliverySlot,
    value: int | str,
    period: DateRange = PERIOD,
) -> CampaignDeliveryValue:
    kind = (
        UnitKind.COUNT
        if slot in {CampaignDeliverySlot.IMPRESSIONS, CampaignDeliverySlot.CLICKS}
        else UnitKind.RATIO
        if slot is CampaignDeliverySlot.CTR
        else UnitKind.CURRENCY
    )
    return CampaignDeliveryValue(slot, _metric(value, kind, period))


def _complete_delivery() -> CampaignDelivery:
    summary = tuple(
        _delivery_metric(
            slot,
            (
                30
                if slot
                in {
                    CampaignDeliverySlot.IMPRESSIONS,
                    CampaignDeliverySlot.CLICKS,
                }
                else "2.5000"
            ),
        )
        for slot in CampaignDeliverySlot
    )
    # Each comparison entry carries its change from the summary's current value (ADR-121).
    comparison = campaign_delivery_comparison(
        summary,
        tuple(
            _delivery_metric(
                slot,
                (
                    20
                    if slot
                    in {
                        CampaignDeliverySlot.IMPRESSIONS,
                        CampaignDeliverySlot.CLICKS,
                    }
                    else "1.5000"
                ),
                PRIOR,
            ).value
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
                        10
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
    return CampaignDelivery(
        "complete",
        summary,
        PRIOR,
        comparison,
        rows,
        rows,
        CampaignDspQuality(
            "available",
            _metric(20, UnitKind.COUNT),
            _metric(10, UnitKind.COUNT),
            _metric(12, UnitKind.COUNT),
            _metric("0.5000", UnitKind.RATIO),
            None,
        ),
        campaign_delivery_accepted_statuses(),
        CampaignRecoveryAction.CHANGE_PERIOD.value,
    )


def test_v2_complete_delivery_codec_requires_exact_daily_coverage() -> None:
    detail = build_campaign_attribution_detail(
        CampaignAttributionDetailRequest(
            _signature(),
            PERIOD,
            _summary(),
            _series(1, 0, 2),
            (*_current_evidence(series_rows=3), *_prior_evidence()),
            PRIOR,
            _valid_comparison(_summary()),
        )
    )
    artifact = _v2(detail, _complete_delivery())

    payload = campaign_attribution_detail_v2_payload(artifact)

    delivery = cast("dict[str, object]", payload["delivery"])
    summary = cast("list[object]", delivery["summary"])
    first_summary = cast("dict[str, object]", summary[0])
    summary_value = cast("dict[str, object]", first_summary["value"])
    assert delivery["outcome"] == "complete"
    assert summary_value["units"] == {
        "currency_code": "USD",
        "kind": "currency",
    }
    assert campaign_attribution_detail_v2_from_payload(payload) == artifact
    series = cast("dict[str, object]", delivery["series"])
    assert series["available_grains"] == ["day"]
    assert series["metrics"] == [slot.value for slot in CampaignDeliverySlot]
    first_row = cast("dict[str, object]", cast("list[object]", series["points"])[0])
    assert first_row["date"] == PERIOD.start.isoformat()
    assert set(cast("dict[str, object]", first_row["current"])) == {
        slot.value for slot in CampaignDeliverySlot
    }
    assert set(cast("dict[str, object]", first_row["prior"])) == {
        slot.value for slot in CampaignDeliverySlot
    }
    malformed = {
        **payload,
        "delivery": {
            **delivery,
            "series": {
                **series,
                "points": cast("list[object]", series["points"])[:-1],
                "exact_rows": cast("list[object]", series["exact_rows"])[:-1],
            },
        },
    }
    with pytest.raises(ValueError, match="exactly cover"):
        campaign_attribution_detail_v2_from_payload(malformed)


def test_v2_qualified_delivery_keeps_core_values_when_dsp_quality_is_unavailable() -> None:
    detail = build_campaign_attribution_detail(
        CampaignAttributionDetailRequest(
            _signature(),
            PERIOD,
            _summary(),
            _series(1, 0, 2),
            (*_current_evidence(series_rows=3), *_prior_evidence()),
            PRIOR,
            _valid_comparison(_summary()),
        )
    )
    complete = _complete_delivery()
    qualified = CampaignDelivery(
        "qualified",
        complete.summary,
        PRIOR,
        complete.comparison,
        complete.series_points,
        complete.exact_rows,
        CampaignDspQuality(
            "unavailable",
            None,
            None,
            None,
            None,
            "dsp_quality_evidence_unavailable",
        ),
        campaign_delivery_accepted_statuses(),
        CampaignRecoveryAction.CHANGE_PERIOD.value,
    )

    assert _v2(detail, qualified).delivery.outcome == "qualified"


@pytest.mark.covers("ADR-068")
def test_detail_v2_codec_requires_the_declared_unavailable_insight_roster() -> None:
    detail = build_campaign_attribution_detail(
        CampaignAttributionDetailRequest(
            _signature(),
            PERIOD,
            _summary(),
            _series(1, 0, 2),
            (*_current_evidence(series_rows=3), *_prior_evidence()),
            PRIOR,
            _valid_comparison(_summary()),
        )
    )
    artifact = _v2(detail, campaign_delivery_unavailable(detail))

    payload = campaign_attribution_detail_v2_payload(artifact)
    insights = cast("list[object]", payload["insights"])
    assert tuple(slot["id"] for slot in cast("list[dict[str, object]]", insights)) == (
        "trend-shape",
        "efficiency-drift",
        "funnel",
        "next-check",
    )
    assert campaign_attribution_detail_v2_from_payload(payload) == artifact

    cast("list[dict[str, object]]", insights)[0]["state"] = "filled"
    with pytest.raises(ValueError, match="filled insight slots require"):
        campaign_attribution_detail_v2_from_payload(payload)


@pytest.mark.covers("ADR-068")
def test_detail_v2_codec_re_attests_a_retained_body_without_declared_absences() -> None:
    """Exact readback must accept retained bodies whose slots declare no absence.

    Pre-taxonomy persisted bodies carry base-shape slots and stay serveable forever;
    this is the artifact-level regression behind the 2026-09-23 campaigns 500.
    """
    detail = build_campaign_attribution_detail(
        CampaignAttributionDetailRequest(
            _signature(),
            PERIOD,
            _summary(),
            _series(1, 0, 2),
            (*_current_evidence(series_rows=3), *_prior_evidence()),
            PRIOR,
            _valid_comparison(_summary()),
        )
    )
    artifact = _v2(detail, campaign_delivery_unavailable(detail))
    payload = campaign_attribution_detail_v2_payload(artifact)
    retained = {
        **payload,
        "insights": [
            {
                key: value
                for key, value in cast("dict[str, object]", slot).items()
                if key not in {"absence_class", "absence_reason"}
            }
            for slot in cast("list[object]", payload["insights"])
        ],
    }

    decoded = campaign_attribution_detail_v2_from_payload(retained)

    assert all(slot.absence_class is None for slot in decoded.insights)
    assert campaign_attribution_detail_v2_payload(decoded) == retained


@pytest.mark.covers("ADR-072")
class TestCampaignEvidenceStatus:
    def test_served_words_are_composed_per_condition_with_units_of_meaning(self) -> None:
        accepted = campaign_evidence_status(
            CampaignEvidenceStatusRequest(
                "impressions", "current", CampaignEvidenceStatusCondition.AVAILABLE
            )
        )
        assert "impressions" in accepted.explanation
        assert accepted.recovery_reading is None

        refused = campaign_evidence_status(
            CampaignEvidenceStatusRequest(
                "clicks",
                "daily",
                CampaignEvidenceStatusCondition.INCOMPLETE_DAILY_SERIES,
                days_present=3,
                days_expected=7,
            )
        )
        assert "3 of 7 days" in refused.explanation
        assert refused.recovery_reading is not None
        assert refused.days_present == 3
        assert refused.days_expected == 7

    def test_a_refusal_requires_served_recovery_and_counts_stay_scoped(self) -> None:
        with pytest.raises(ValueError, match="recovery reading"):
            CampaignEvidenceStatus(
                slot="ctr",
                role="current",
                condition=CampaignEvidenceStatusCondition.SUPPRESSED,
                explanation="Withheld.",
                recovery_reading=None,
            )
        with pytest.raises(ValueError, match="day counts"):
            CampaignEvidenceStatus(
                slot="ctr",
                role="current",
                condition=CampaignEvidenceStatusCondition.AVAILABLE,
                explanation="Complete.",
                days_present=1,
                days_expected=7,
            )
        with pytest.raises(ValueError, match="undeclared slot or role"):
            campaign_evidence_status(
                CampaignEvidenceStatusRequest(
                    "reach", "current", CampaignEvidenceStatusCondition.AVAILABLE
                )
            )

    def test_insufficient_delivery_must_itemize_and_the_codec_round_trips_it(self) -> None:
        detail = build_campaign_attribution_detail(
            CampaignAttributionDetailRequest(
                _signature(),
                PERIOD,
                _summary(),
                _series(1, 0, 2),
                (*_current_evidence(series_rows=3), *_prior_evidence()),
                PRIOR,
                _valid_comparison(_summary()),
            )
        )
        unavailable = campaign_delivery_unavailable(detail)
        assert all(
            status.condition is not CampaignEvidenceStatusCondition.AVAILABLE
            for status in unavailable.evidence
        )
        artifact = _v2(detail, unavailable)
        payload = campaign_attribution_detail_v2_payload(artifact)
        delivery = cast("dict[str, object]", payload["delivery"])
        first = cast("dict[str, object]", cast("list[object]", delivery["evidence"])[0])
        assert set(first) == {
            "condition",
            "days_expected",
            "days_present",
            "explanation",
            "recovery_reading",
            "role",
            "slot",
        }
        assert campaign_attribution_detail_v2_from_payload(payload) == artifact

    def test_delivery_refuses_an_all_accepted_insufficient_itemization(self) -> None:
        detail = build_campaign_attribution_detail(
            CampaignAttributionDetailRequest(
                _signature(),
                PERIOD,
                _summary(),
                _series(1, 0, 2),
                (*_current_evidence(series_rows=3), *_prior_evidence()),
                PRIOR,
                _valid_comparison(_summary()),
            )
        )
        unavailable = campaign_delivery_unavailable(detail)
        with pytest.raises(ValueError, match="must name what refused"):
            CampaignDelivery(
                outcome=unavailable.outcome,
                summary=unavailable.summary,
                comparison_period=unavailable.comparison_period,
                comparison=unavailable.comparison,
                series_points=unavailable.series_points,
                exact_rows=unavailable.exact_rows,
                dsp_quality=unavailable.dsp_quality,
                evidence=campaign_delivery_accepted_statuses(),
                recovery=unavailable.recovery,
            )

    def test_delivery_refuses_a_noncanonical_status_roster(self) -> None:
        detail = build_campaign_attribution_detail(
            CampaignAttributionDetailRequest(
                _signature(),
                PERIOD,
                _summary(),
                _series(1, 0, 2),
                (*_current_evidence(series_rows=3), *_prior_evidence()),
                PRIOR,
                _valid_comparison(_summary()),
            )
        )
        unavailable = campaign_delivery_unavailable(detail)
        with pytest.raises(ValueError, match="canonical roster"):
            CampaignDelivery(
                outcome=unavailable.outcome,
                summary=unavailable.summary,
                comparison_period=unavailable.comparison_period,
                comparison=unavailable.comparison,
                series_points=unavailable.series_points,
                exact_rows=unavailable.exact_rows,
                dsp_quality=unavailable.dsp_quality,
                evidence=unavailable.evidence[:-1],
                recovery=unavailable.recovery,
            )


def _complete_v2() -> CampaignAttributionDetailV2:
    detail = build_campaign_attribution_detail(
        CampaignAttributionDetailRequest(
            _signature(),
            PERIOD,
            _summary(),
            _series(1, 0, 2),
            (*_current_evidence(series_rows=3), *_prior_evidence()),
            PRIOR,
            _valid_comparison(_summary()),
        )
    )
    return _v2(detail, _complete_delivery())


def _metric_values(node: object) -> list[dict[str, object]]:
    """Every metric-value object in a payload (the five keys, with or without display_value)."""
    if isinstance(node, list):
        return [found for item in cast("list[object]", node) for found in _metric_values(item)]
    if not isinstance(node, dict):
        return []
    item = cast("dict[str, object]", node)
    if set(item) - {"display_value"} == {"basis", "grain", "period", "units", "value"}:
        return [item]
    return [found for value in item.values() for found in _metric_values(value)]


@pytest.mark.covers("ADR-084")
def test_detail_metric_values_carry_a_two_place_display_value_for_currency_only() -> None:
    """USD amounts at four places in dollars display at two; counts and ratios carry null."""
    payload = campaign_attribution_detail_v2_payload(_complete_v2())

    values = _metric_values(payload)
    assert values
    for value in values:
        units = cast("dict[str, object]", value["units"])
        if units["kind"] == "currency":
            assert value["display_value"] == f"{Decimal(cast('str', value['value'])):.2f}"
        else:
            assert value["display_value"] is None
    delivery = cast("dict[str, object]", payload["delivery"])
    quality = cast("dict[str, object]", delivery["dsp_quality"])
    for name in ("measurable_impressions", "viewable_impressions", "viewable_rate"):
        assert cast("dict[str, object]", quality[name])["display_value"] is None


@pytest.mark.covers("ADR-084")
def test_a_retained_pre_display_v1_detail_decodes_through_the_tolerant_rule() -> None:
    artifact = _complete_v2().detail
    retained = without_display_values(campaign_attribution_detail_payload(artifact))

    assert _metric_values(retained)
    assert all("display_value" not in value for value in _metric_values(retained))
    assert campaign_page_artifact_from_payload(retained) == artifact


@pytest.mark.covers("ADR-084")
def test_a_retained_pre_display_v2_detail_decodes_and_re_encodes_with_display_values() -> None:
    artifact = _complete_v2()
    retained = without_display_values(campaign_attribution_detail_v2_payload(artifact))

    decoded = campaign_attribution_detail_v2_from_payload(retained)

    assert decoded == artifact
    assert all(
        "display_value" in value
        for value in _metric_values(campaign_attribution_detail_v2_payload(decoded))
    )


def _first_currency(payload: dict[str, object]) -> dict[str, object]:
    summary = cast("dict[str, object]", payload["summary"])
    return cast("dict[str, object]", summary["spend"])


def _first_count(payload: dict[str, object]) -> dict[str, object]:
    summary = cast("dict[str, object]", payload["summary"])
    return cast("dict[str, object]", summary["purchases"])


@pytest.mark.covers("ADR-084")
def test_detail_decoders_refuse_a_wrong_count_or_mixed_display_value() -> None:
    artifact = _complete_v2()
    v1 = campaign_attribution_detail_payload(artifact.detail)
    v2 = campaign_attribution_detail_v2_payload(artifact)
    wrong = json.loads(json.dumps(v1))
    _first_currency(wrong)["display_value"] = "12.01"
    on_count = json.loads(json.dumps(v1))
    _first_count(on_count)["display_value"] = "2.00"
    mixed = json.loads(json.dumps(v2))
    del _first_currency(mixed)["display_value"]

    with pytest.raises(ValueError, match="not canonical"):
        campaign_page_artifact_from_payload(wrong)
    with pytest.raises(ValueError, match="not canonical"):
        campaign_page_artifact_from_payload(on_count)
    with pytest.raises(ValueError, match="mixes display values"):
        campaign_attribution_detail_v2_from_payload(mixed)


# --- dsp-cpc-derive: cost per click from the accepted spend and clicks ------------------------

_USD = Units(kind=UnitKind.CURRENCY, currency_code="USD")
_COUNT = Units(kind=UnitKind.COUNT)
_AVAILABLE = CampaignEvidenceStatusCondition.AVAILABLE
_DAYS = tuple(PERIOD.start + timedelta(days=offset) for offset in range(PERIOD.days))


def _aggregate(value: Decimal | int, units: Units) -> CampaignDeliveryAnswer:
    return CampaignDeliveryAnswer(
        CampaignMetricValue(value, units, MeasurementBasis.AD_ATTRIBUTED, PERIOD), _AVAILABLE
    )


def test_cpc_derives_spend_over_clicks_at_the_planned_scale_with_spends_scope() -> None:
    """DSP Flight 11's prior window (Sep 23-29): 11771.3696 / 14127 = 0.8333, in spend's USD."""
    derived = derive_campaign_cpc(
        _aggregate(Decimal("11771.3696"), _USD), _aggregate(14127, _COUNT)
    )

    assert derived == _aggregate(Decimal("0.8333"), _USD)


def test_cpc_over_zero_clicks_has_no_value() -> None:
    """Zero clicks: an aggregate is empty-covered, and a daily series lacks that day."""
    aggregate = derive_campaign_cpc(_aggregate(Decimal(0), _USD), _aggregate(0, _COUNT))
    daily = derive_campaign_cpc(
        CampaignDeliveryAnswer(
            tuple(zip(_DAYS, (Decimal(3), Decimal(0), Decimal(5)), strict=True)), _AVAILABLE
        ),
        CampaignDeliveryAnswer(tuple(zip(_DAYS, (2, 0, 4), strict=True)), _AVAILABLE),
    )

    assert aggregate == CampaignDeliveryAnswer(None, CampaignEvidenceStatusCondition.EMPTY_COVERED)
    assert daily == CampaignDeliveryAnswer(
        ((_DAYS[0], Decimal("1.5000")), (_DAYS[2], Decimal("1.2500"))),
        CampaignEvidenceStatusCondition.INCOMPLETE_DAILY_SERIES,
        days_present=2,
        days_expected=3,
        missing_dates=(_DAYS[1],),
    )


def test_cpc_carries_the_first_refused_operands_condition() -> None:
    """A refused operand is returned as it stands; spend is checked before clicks."""
    hole = CampaignDeliveryAnswer(
        None, CampaignEvidenceStatusCondition.COVERAGE_HOLE, missing_dates=(_DAYS[2],)
    )
    suppressed = CampaignDeliveryAnswer(None, CampaignEvidenceStatusCondition.SUPPRESSED)
    missing = CampaignDeliveryAnswer(
        None, CampaignEvidenceStatusCondition.MISSING_CURRENT_PUBLICATION
    )

    assert derive_campaign_cpc(hole, suppressed) == hole
    assert derive_campaign_cpc(_aggregate(Decimal(5), _USD), missing) == missing
