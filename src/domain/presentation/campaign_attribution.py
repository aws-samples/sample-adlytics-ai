# SPDX-License-Identifier: MIT-0
"""Pure campaign-attribution list and detail presentation artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from itertools import pairwise
from typing import cast

from domain.measurement.comparison import absolute_change, percentage_change
from domain.measurement.figures import RATIO_SCALE
from domain.narrative.shaped_response import UnitKind, Units
from domain.presentation.analytical_pages import PresentationDimensionLabel
from domain.presentation.campaigns import CampaignPresentation, EffectiveNameStatus
from domain.presentation.change import (
    CHANGE_KEY,
    PERCENTAGE_KEY,
    Change,
    ChangeOperands,
    ChangeSemantics,
    build_change,
    change_from_payload,
    change_payload,
    check_change,
    comparison_change_payload,
    comparison_fields,
    require_change_blocks,
)
from domain.presentation.currency_display import (
    DISPLAY_VALUE_KEY,
    canonical_matches,
    carries_display_value,
    display_value_mode,
    metric_display_value,
)
from domain.presentation.evidence_status import (
    EVIDENCE_STATUS_KEY,
    CoverageFact,
    CoverageKind,
    CoverageState,
    EvidenceStatus,
    evidence_status_from_payload,
    evidence_status_payload,
    labels_for,
    require_entries,
    write_evidence_status,
)
from domain.presentation.insights import (
    InsightSlot,
    InsightSlotId,
    default_detail_insights,
    insight_slots_from_payload,
    insight_slots_payload,
    require_insight_roster,
)
from domain.shared_kernel.errors import ErrorCode, InvariantError
from domain.shared_kernel.provenance import MeasurementBasis, Suppression
from domain.shared_kernel.values import DateRange, Grain

SUMMARY_GRAIN = Grain(("period", "campaign", "marketplace"))
PURCHASE_SERIES_GRAIN = Grain(("date", "campaign", "marketplace"))
CAMPAIGN_ATTRIBUTION_ARTIFACT_TYPE = "CampaignAttribution"
CAMPAIGN_ATTRIBUTION_ARTIFACT_VERSION = 1
CAMPAIGN_ATTRIBUTION_BOUNDARIES = (
    (
        "ROAS is the governed ad-attributed metric for advertised products over a 14-day "
        "attribution window."
    ),
    (
        "Campaign-attributed purchases and ROAS do not establish Vendor retail impact or "
        "product-level ROAS."
    ),
    (
        "Campaign-attributed evidence is descriptive and does not establish incrementality "
        "or causality."
    ),
)
MIN_PURCHASE_SERIES_POINTS = 2
_CAMPAIGN_RECONCILIATION = ErrorCode("CAMPAIGN", 1)


type CampaignNumber = int | Decimal


class CampaignPageKind(StrEnum):
    """Discriminant for the two campaign page artifacts."""

    CAMPAIGN_LIST = "campaign_list"
    CAMPAIGN_DETAIL = "campaign_detail"


class CampaignListOutcome(StrEnum):
    """Closed campaign-list outcomes."""

    CHOICES = "choices"
    EMPTY = "empty"
    AMBIGUOUS = "ambiguous"
    INSUFFICIENT = "insufficient"


class CampaignDetailOutcome(StrEnum):
    """Closed selected-campaign outcomes."""

    SELECTED = "selected"
    INSUFFICIENT = "insufficient"


class CampaignListReason(StrEnum):
    """Closed reasons carried by non-choice campaign lists."""

    EMPTY_COVERED = "empty_covered"
    SAFE_SIGNATURE_COLLISION = "safe_signature_collision"
    MISSING_CATALOG = "missing_catalog"
    MISSING_CURRENT_PUBLICATION = "missing_current_publication"
    COVERAGE_HOLE = "coverage_hole"
    SUPPRESSED = "suppressed"
    TRUNCATED = "truncated"
    INCOMPATIBLE_SCOPE = "incompatible_scope"


class CampaignRecoveryAction(StrEnum):
    """Declared deterministic controls that campaign artifacts may expose."""

    CHANGE_PERIOD = "change_period"
    CHANGE_CAMPAIGN = "change_campaign"
    COMPARE_PREVIOUS_PERIOD = "compare_previous_period"
    INSPECT_CHANNEL_CONTEXT = "inspect_channel_context"
    CONTACT_WORKSPACE_DATA_OWNER = "contact_workspace_data_owner"


class CampaignSummarySlot(StrEnum):
    """Stable summary order for values and comparisons."""

    PURCHASES = "purchases"
    ATTRIBUTED_ROAS = "attributed_roas"
    SPEND = "spend"


class CampaignEvidenceSlot(StrEnum):
    """Every independently inspected current and prior evidence slot."""

    PURCHASES = "purchases"
    ATTRIBUTED_ROAS = "attributed_roas"
    SPEND = "spend"
    PURCHASE_SERIES = "purchase_series"
    PRIOR_PURCHASES = "prior_purchases"
    PRIOR_ATTRIBUTED_ROAS = "prior_attributed_roas"
    PRIOR_SPEND = "prior_spend"


class CampaignEvidenceCondition(StrEnum):
    """Closed preflight or shaped-answer condition for one evidence slot."""

    AVAILABLE = "available"
    EMPTY_COVERED = "empty_covered"
    MISSING_CURRENT_PUBLICATION = "missing_current_publication"
    MISSING_COMPARATOR_PUBLICATION = "missing_comparator_publication"
    SUPPRESSED = "suppressed"
    TRUNCATED = "truncated"
    COVERAGE_HOLE = "coverage_hole"
    INCOMPATIBLE_SCOPE = "incompatible_scope"


class CampaignInsufficiencyReason(StrEnum):
    """Closed evidence failures that prevent a selected detail outcome."""

    MISSING_CURRENT_PUBLICATION = "missing_current_publication"
    MISSING_COMPARATOR_PUBLICATION = "missing_comparator_publication"
    EMPTY_COVERED = "empty_covered"
    SUPPRESSED = "suppressed"
    TRUNCATED = "truncated"
    COVERAGE_HOLE = "coverage_hole"
    INCOMPATIBLE_SCOPE = "incompatible_scope"
    TOO_SHORT_SERIES = "too_short_series"
    ONE_SIDED_COMPARISON = "one_sided_comparison"


class CampaignComparisonReason(StrEnum):
    """Why one comparison metric has no percentage or no change."""

    ONE_SIDED = "one_sided"
    PRIOR_ZERO_PERCENTAGE_UNDEFINED = "prior_zero_percentage_undefined"


_CURRENT_SLOTS = (
    CampaignEvidenceSlot.PURCHASES,
    CampaignEvidenceSlot.ATTRIBUTED_ROAS,
    CampaignEvidenceSlot.SPEND,
    CampaignEvidenceSlot.PURCHASE_SERIES,
)
_PRIOR_SLOTS = (
    CampaignEvidenceSlot.PRIOR_PURCHASES,
    CampaignEvidenceSlot.PRIOR_ATTRIBUTED_ROAS,
    CampaignEvidenceSlot.PRIOR_SPEND,
)
_SUMMARY_SLOTS = (
    CampaignSummarySlot.PURCHASES,
    CampaignSummarySlot.ATTRIBUTED_ROAS,
    CampaignSummarySlot.SPEND,
)
_LIST_INSUFFICIENCY_REASONS = frozenset(
    {
        CampaignListReason.MISSING_CATALOG,
        CampaignListReason.MISSING_CURRENT_PUBLICATION,
        CampaignListReason.COVERAGE_HOLE,
        CampaignListReason.SUPPRESSED,
        CampaignListReason.TRUNCATED,
        CampaignListReason.INCOMPATIBLE_SCOPE,
    }
)
_REASON_BY_CONDITION = {
    CampaignEvidenceCondition.EMPTY_COVERED: CampaignInsufficiencyReason.EMPTY_COVERED,
    CampaignEvidenceCondition.MISSING_CURRENT_PUBLICATION: (
        CampaignInsufficiencyReason.MISSING_CURRENT_PUBLICATION
    ),
    CampaignEvidenceCondition.MISSING_COMPARATOR_PUBLICATION: (
        CampaignInsufficiencyReason.MISSING_COMPARATOR_PUBLICATION
    ),
    CampaignEvidenceCondition.SUPPRESSED: CampaignInsufficiencyReason.SUPPRESSED,
    CampaignEvidenceCondition.TRUNCATED: CampaignInsufficiencyReason.TRUNCATED,
    CampaignEvidenceCondition.COVERAGE_HOLE: CampaignInsufficiencyReason.COVERAGE_HOLE,
    CampaignEvidenceCondition.INCOMPATIBLE_SCOPE: (CampaignInsufficiencyReason.INCOMPATIBLE_SCOPE),
}


def _nonblank(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} cannot be blank.")


def _number(value: object, field_name: str) -> CampaignNumber:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise TypeError(f"{field_name} must be an integer or Decimal.")
    if value < 0:
        raise ValueError(f"{field_name} cannot be negative.")
    return value


def _range_ordered(values: tuple[DateRange, ...], field_name: str) -> None:
    if tuple(sorted(values, key=lambda item: (item.start, item.end))) != values:
        raise ValueError(f"{field_name} must be chronologically ordered.")
    if any(left.overlaps(right) for left, right in pairwise(values)):
        raise ValueError(f"{field_name} cannot overlap.")


def _previous_period(period: DateRange) -> DateRange:
    prior_end = period.start - timedelta(days=1)
    return DateRange(prior_end - timedelta(days=period.days - 1), prior_end)


def _ordered_reasons(
    reasons: set[CampaignInsufficiencyReason],
) -> tuple[CampaignInsufficiencyReason, ...]:
    return tuple(reason for reason in CampaignInsufficiencyReason if reason in reasons)


@dataclass(frozen=True, slots=True)
class SafeCampaignDisplaySignature:
    """Picker identity composed only from approved customer-facing fields."""

    effective_name: str | None
    channel: str
    marketplace: str
    platform: str
    effective_name_status: EffectiveNameStatus
    effective_name_valid_from: date | None = None
    effective_name_valid_to: date | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("campaign channel", self.channel),
            ("campaign marketplace", self.marketplace),
            ("campaign platform", self.platform),
        ):
            _nonblank(value, field_name)
        if self.effective_name is not None:
            _nonblank(self.effective_name, "campaign effective name")
            if self.effective_name_valid_from is None:
                raise ValueError("a displayed effective name requires its validity interval.")
        elif self.effective_name_valid_from is not None or self.effective_name_valid_to is not None:
            raise ValueError("an absent effective name cannot carry a validity interval.")
        if (
            self.effective_name_valid_from is not None
            and self.effective_name_valid_to is not None
            and self.effective_name_valid_to <= self.effective_name_valid_from
        ):
            raise ValueError("campaign effective-name intervals are half-open and nonempty.")
        if (
            self.effective_name_status is EffectiveNameStatus.AVAILABLE
            and self.effective_name is None
        ):
            raise ValueError("an available campaign signature requires an effective name.")
        if self.effective_name_status is EffectiveNameStatus.MISSING and (
            self.effective_name is not None
            or self.effective_name_valid_from is not None
            or self.effective_name_valid_to is not None
        ):
            raise ValueError("a missing campaign name cannot expose a fallback label or interval.")

    @property
    def sort_key(self) -> tuple[str, ...]:
        """Return a deterministic key containing no canonical selector."""
        return (
            self.platform,
            self.marketplace,
            self.channel,
            self.effective_name or "",
            self.effective_name_status.value,
            self.effective_name_valid_from.isoformat()
            if self.effective_name_valid_from is not None
            else "",
            self.effective_name_valid_to.isoformat()
            if self.effective_name_valid_to is not None
            else "",
        )


def safe_dimension_label(
    labels: tuple[PresentationDimensionLabel, ...],
    field: str,
    value: str,
) -> str:
    """Rewrite one internal selector value into its single governed public label."""
    matches = tuple(
        label.display_value
        for label in labels
        if label.selector_field == field and label.selector_value == value
    )
    if len(matches) != 1:
        raise ValueError("one governed public dimension label is required.")
    return matches[0]


def safe_campaign_signature(
    campaign: CampaignPresentation,
    period: DateRange,
    labels: tuple[PresentationDimensionLabel, ...],
) -> SafeCampaignDisplaySignature:
    """Derive the one display signature shared by baked custody and request-time redemption.

    Bake and redemption must produce byte-identical signatures for the same catalog history,
    so both paths call this derivation; a second derivation is the defect ADR-070's
    2026-09-16 signature amendment retired.
    """
    if campaign.channel is None or campaign.marketplace is None:
        raise ValueError("campaign presentation context is incomplete.")
    active_names = tuple(
        item
        for item in campaign.names
        if campaign.effective_name is not None
        and item.value == campaign.effective_name
        and item.valid_from <= period.end
        and (item.valid_to is None or period.end < item.valid_to)
    )
    if campaign.effective_name is not None and len(active_names) != 1:
        raise ValueError("campaign effective-name interval is not unique.")
    active = active_names[0] if active_names else None
    return SafeCampaignDisplaySignature(
        effective_name=campaign.effective_name,
        channel=safe_dimension_label(labels, "channel_key", campaign.channel.value),
        marketplace=safe_dimension_label(labels, "marketplace_key", campaign.marketplace.value),
        platform=safe_dimension_label(labels, "platform_key", campaign.platform.value),
        effective_name_status=campaign.effective_name_status,
        effective_name_valid_from=None if active is None else active.valid_from,
        effective_name_valid_to=None if active is None else active.valid_to,
    )


@dataclass(frozen=True, slots=True)
class CampaignSelectionCapability:
    """Opaque, expiry-aware capability issued by the server-owned selection store."""

    value: str
    expires_at: datetime

    def __post_init__(self) -> None:
        _nonblank(self.value, "campaign selection capability")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("campaign selection capability expiry must be timezone-aware.")


@dataclass(frozen=True, slots=True)
class CampaignSelectionChoice:
    """One safe signature and the opaque capability that redeems it."""

    signature: SafeCampaignDisplaySignature
    capability: CampaignSelectionCapability


@dataclass(frozen=True, slots=True)
class CampaignListEvidence:
    """Safe list-scoped catalog/publication evidence."""

    source: str
    requested_period: DateRange
    actual_coverage: tuple[DateRange, ...]
    coverage_holes: tuple[DateRange, ...] = ()
    freshness_qualifiers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _nonblank(self.source, "campaign list evidence source")
        _range_ordered(self.actual_coverage, "campaign list actual coverage")
        _range_ordered(self.coverage_holes, "campaign list coverage holes")
        if any(not self.requested_period.covers(item) for item in self.coverage_holes):
            raise ValueError("campaign list coverage holes must fall inside the requested period.")
        if len(set(self.freshness_qualifiers)) != len(self.freshness_qualifiers) or any(
            not value.strip() for value in self.freshness_qualifiers
        ):
            raise ValueError("campaign list freshness qualifiers must be unique and nonblank.")


@dataclass(frozen=True, slots=True)
class CampaignSelectionListRequest:
    """Authorized safe choices and list evidence used to derive one list outcome."""

    period: DateRange
    choices: tuple[CampaignSelectionChoice, ...]
    evidence: tuple[CampaignListEvidence, ...]
    truncated: bool = False
    blockers: tuple[CampaignListReason, ...] = ()
    recovery_actions: tuple[CampaignRecoveryAction, ...] = ()

    def __post_init__(self) -> None:
        if not self.evidence or any(item.requested_period != self.period for item in self.evidence):
            raise ValueError("campaign list requires period-matched list evidence.")
        if len(set(self.blockers)) != len(self.blockers) or any(
            reason not in _LIST_INSUFFICIENCY_REASONS for reason in self.blockers
        ):
            raise ValueError("campaign list blockers must be unique insufficiency reasons.")
        if len(set(self.recovery_actions)) != len(self.recovery_actions):
            raise ValueError("campaign list recovery actions must be unique.")
        capabilities = tuple(choice.capability.value for choice in self.choices)
        if len(set(capabilities)) != len(capabilities):
            raise ValueError("campaign choices cannot repeat an opaque capability.")


@dataclass(frozen=True, slots=True)
class CampaignSelectionListArtifact:
    """One exact list state; collisions never leak a guessed selection."""

    outcome: CampaignListOutcome
    period: DateRange
    evidence: tuple[CampaignListEvidence, ...]
    truncated: bool
    choices: tuple[CampaignSelectionChoice, ...]
    reason_codes: tuple[CampaignListReason, ...]
    recovery_actions: tuple[CampaignRecoveryAction, ...]
    kind: CampaignPageKind = field(init=False, default=CampaignPageKind.CAMPAIGN_LIST)

    def __post_init__(self) -> None:
        if not self.evidence or any(item.requested_period != self.period for item in self.evidence):
            raise ValueError("campaign list artifact requires period-matched evidence.")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("campaign list reason codes must be unique.")
        if len(set(self.recovery_actions)) != len(self.recovery_actions):
            raise ValueError("campaign list recovery actions must be unique.")
        signatures = tuple(choice.signature for choice in self.choices)
        sorted_choices = tuple(sorted(self.choices, key=lambda item: item.signature.sort_key))
        if self.choices != sorted_choices or len(set(signatures)) != len(signatures):
            raise ValueError("campaign choices must be uniquely and safely ordered.")

        if self.outcome is CampaignListOutcome.CHOICES:
            valid = bool(self.choices) and not self.reason_codes and not self.recovery_actions
        elif self.outcome is CampaignListOutcome.EMPTY:
            valid = not self.choices and self.reason_codes == (CampaignListReason.EMPTY_COVERED,)
        elif self.outcome is CampaignListOutcome.AMBIGUOUS:
            valid = (
                not self.choices
                and self.reason_codes == (CampaignListReason.SAFE_SIGNATURE_COLLISION,)
                and not self.recovery_actions
            )
        else:
            valid = (
                not self.choices
                and bool(self.reason_codes)
                and all(reason in _LIST_INSUFFICIENCY_REASONS for reason in self.reason_codes)
            )
        if not valid:
            raise ValueError("campaign list fields contradict its exact outcome.")


def build_campaign_selection_list(
    request: CampaignSelectionListRequest,
) -> CampaignSelectionListArtifact:
    """Derive choices, empty, ambiguous, or insufficient without exposing identity keys."""
    ordered_choices = tuple(sorted(request.choices, key=lambda item: item.signature.sort_key))
    if request.blockers:
        ordered_blockers = tuple(
            reason for reason in CampaignListReason if reason in request.blockers
        )
        return CampaignSelectionListArtifact(
            CampaignListOutcome.INSUFFICIENT,
            request.period,
            request.evidence,
            request.truncated,
            (),
            ordered_blockers,
            request.recovery_actions,
        )
    signatures = tuple(choice.signature for choice in ordered_choices)
    if len(set(signatures)) != len(signatures):
        return CampaignSelectionListArtifact(
            CampaignListOutcome.AMBIGUOUS,
            request.period,
            request.evidence,
            request.truncated,
            (),
            (CampaignListReason.SAFE_SIGNATURE_COLLISION,),
            (),
        )
    if ordered_choices:
        return CampaignSelectionListArtifact(
            CampaignListOutcome.CHOICES,
            request.period,
            request.evidence,
            request.truncated,
            ordered_choices,
            (),
            (),
        )
    return CampaignSelectionListArtifact(
        CampaignListOutcome.EMPTY,
        request.period,
        request.evidence,
        request.truncated,
        (),
        (CampaignListReason.EMPTY_COVERED,),
        request.recovery_actions,
    )


@dataclass(frozen=True, slots=True)
class CampaignMetricValue:
    """One represented summary value with exact units, basis, period, and grain."""

    value: CampaignNumber
    units: Units
    basis: MeasurementBasis
    period: DateRange
    grain: Grain = SUMMARY_GRAIN

    def __post_init__(self) -> None:
        _number(self.value, "campaign metric value")


@dataclass(frozen=True, slots=True)
class CampaignSummary:
    """The three ordered summary slots; unavailable slots remain absent, never zero."""

    period: DateRange
    purchases: CampaignMetricValue | None
    attributed_roas: CampaignMetricValue | None
    spend: CampaignMetricValue | None

    def __post_init__(self) -> None:
        for slot, value, expected_kind in (
            (CampaignSummarySlot.PURCHASES, self.purchases, UnitKind.COUNT),
            (CampaignSummarySlot.ATTRIBUTED_ROAS, self.attributed_roas, UnitKind.RATIO),
            (CampaignSummarySlot.SPEND, self.spend, UnitKind.CURRENCY),
        ):
            if value is None:
                continue
            if (
                value.period != self.period
                or value.grain != SUMMARY_GRAIN
                or value.units.kind is not expected_kind
                or value.basis is not MeasurementBasis.AD_ATTRIBUTED
            ):
                raise ValueError(
                    f"campaign {slot.value} has incompatible period, grain, units, or basis."
                )

    @property
    def complete(self) -> bool:
        """Whether all three summary slots carry represented values."""
        return (
            self.purchases is not None
            and self.attributed_roas is not None
            and self.spend is not None
        )

    def value_for(self, slot: CampaignSummarySlot) -> CampaignMetricValue | None:
        """Read one typed summary slot without string-based attribute lookup."""
        if slot is CampaignSummarySlot.PURCHASES:
            return self.purchases
        if slot is CampaignSummarySlot.ATTRIBUTED_ROAS:
            return self.attributed_roas
        return self.spend


@dataclass(frozen=True, slots=True)
class CampaignPurchasePoint:
    """One represented daily purchase value; numerical zero remains a row."""

    day: date
    purchases: CampaignNumber

    def __post_init__(self) -> None:
        _number(self.purchases, "daily campaign purchases")


@dataclass(frozen=True, slots=True)
class CampaignPurchaseSeries:
    """A true dated series at date/campaign/marketplace grain."""

    period: DateRange
    points: tuple[CampaignPurchasePoint, ...]
    units: Units
    basis: MeasurementBasis
    grain: Grain = PURCHASE_SERIES_GRAIN

    def __post_init__(self) -> None:
        days = tuple(point.day for point in self.points)
        if days != tuple(sorted(days)) or len(set(days)) != len(days):
            raise ValueError("campaign purchase points must be strictly ordered by date.")
        if any(day < self.period.start or day > self.period.end for day in days):
            raise ValueError("campaign purchase points must fall inside the selected period.")
        if (
            self.units.kind is not UnitKind.COUNT
            or self.basis is not MeasurementBasis.AD_ATTRIBUTED
            or self.grain != PURCHASE_SERIES_GRAIN
        ):
            raise ValueError("campaign purchase series has incompatible grain, units, or basis.")


@dataclass(frozen=True, slots=True)
class CampaignSlotEvidence:
    """Browser-safe evidence disclosure for one independently inspected slot."""

    slot: CampaignEvidenceSlot
    condition: CampaignEvidenceCondition
    requested_period: DateRange
    actual_coverage: tuple[DateRange, ...]
    coverage_holes: tuple[DateRange, ...]
    units: Units | None
    grain: Grain
    basis: MeasurementBasis | None
    suppression: Suppression
    row_count: int
    truncated: bool
    freshness_qualifiers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.row_count < 0:
            raise ValueError("campaign slot row count cannot be negative.")
        _range_ordered(self.actual_coverage, "campaign slot actual coverage")
        _range_ordered(self.coverage_holes, "campaign slot coverage holes")
        if any(not self.requested_period.covers(item) for item in self.coverage_holes):
            raise ValueError("campaign slot coverage holes must fall inside the requested period.")
        if len(set(self.freshness_qualifiers)) != len(self.freshness_qualifiers) or any(
            not value.strip() for value in self.freshness_qualifiers
        ):
            raise ValueError("campaign slot freshness qualifiers must be unique and nonblank.")
        if self.condition is CampaignEvidenceCondition.AVAILABLE and (
            self.actual_coverage != (self.requested_period,)
            or self.coverage_holes
            or self.suppression.rows_suppressed
            or self.truncated
            or self.row_count == 0
            or self.units is None
            or self.basis is None
        ):
            raise ValueError(
                "available campaign evidence must be complete, represented, and typed."
            )
        if self.condition is CampaignEvidenceCondition.EMPTY_COVERED and (
            self.actual_coverage != (self.requested_period,)
            or self.coverage_holes
            or self.suppression.rows_suppressed
            or self.truncated
            or self.row_count != 0
        ):
            raise ValueError("covered-empty campaign evidence has complete coverage and no rows.")
        if self.condition is CampaignEvidenceCondition.SUPPRESSED and not (
            self.suppression.rows_suppressed
        ):
            raise ValueError("suppressed campaign evidence requires typed suppression.")
        if self.condition is CampaignEvidenceCondition.TRUNCATED and not self.truncated:
            raise ValueError("truncated campaign evidence requires its truncation flag.")
        if self.condition is CampaignEvidenceCondition.COVERAGE_HOLE and not self.coverage_holes:
            raise ValueError("campaign coverage-hole evidence requires at least one hole.")
        if self.condition in {
            CampaignEvidenceCondition.MISSING_CURRENT_PUBLICATION,
            CampaignEvidenceCondition.MISSING_COMPARATOR_PUBLICATION,
        } and (self.actual_coverage or self.row_count != 0):
            raise ValueError("missing campaign publication evidence cannot claim coverage or rows.")
        prior_slot = self.slot in _PRIOR_SLOTS
        comparator_missing = (
            self.condition is CampaignEvidenceCondition.MISSING_COMPARATOR_PUBLICATION
        )
        if comparator_missing != prior_slot and comparator_missing:
            raise ValueError("only prior campaign slots can miss comparator publication.")
        if prior_slot and self.condition is CampaignEvidenceCondition.MISSING_CURRENT_PUBLICATION:
            raise ValueError("prior campaign slots cannot report a missing current publication.")


@dataclass(frozen=True, slots=True)
class CampaignMetricComparison:
    """Backend-computed current/prior/change values for one summary metric."""

    slot: CampaignSummarySlot
    current: CampaignMetricValue | None
    prior: CampaignMetricValue | None
    absolute_change: CampaignNumber | None
    percentage_change: str | None
    reason: CampaignComparisonReason | None
    change: Change | None = None
    """The server-declared change (ADR-121); `None` only when one-sided."""

    def __post_init__(self) -> None:
        if self.current is None and self.prior is None:
            raise ValueError("campaign comparison cannot contain two absent sides.")
        if self.current is None or self.prior is None:
            if (
                self.absolute_change is not None
                or self.percentage_change is not None
                or self.change is not None
                or self.reason is not CampaignComparisonReason.ONE_SIDED
            ):
                raise ValueError("one-sided campaign comparison cannot fabricate change.")
            return
        if self.change is not None:
            check_change(
                self.change,
                ChangeOperands(self.current.value, self.prior.value, self.current.units),
            )
        if (
            self.current.units != self.prior.units
            or self.current.basis is not self.prior.basis
            or self.current.grain != self.prior.grain
        ):
            raise ValueError(
                "campaign comparison sides require compatible units, basis, and grain."
            )
        expected_change = Decimal(self.current.value) - Decimal(self.prior.value)
        if self.absolute_change is None or Decimal(self.absolute_change) != expected_change:
            raise ValueError("campaign comparison absolute change must be backend-exact.")
        if self.prior.value == 0:
            if (
                self.percentage_change is not None
                or self.reason is not CampaignComparisonReason.PRIOR_ZERO_PERCENTAGE_UNDEFINED
            ):
                raise ValueError("a zero prior campaign value has no percentage change.")
        elif (
            self.percentage_change is None
            or not self.percentage_change.strip()
            or self.reason is not None
        ):
            raise ValueError("two comparable campaign values require a backend percentage.")


@dataclass(frozen=True, slots=True)
class CampaignPriorComparison:
    """The only V1 comparator: the immediately preceding equal-length period."""

    current_period: DateRange
    prior_period: DateRange
    metrics: tuple[CampaignMetricComparison, ...]

    def __post_init__(self) -> None:
        if self.prior_period != _previous_period(self.current_period):
            raise ValueError("campaign comparison requires the previous equal-length period.")
        if tuple(item.slot for item in self.metrics) != _SUMMARY_SLOTS:
            raise ValueError("campaign comparison must preserve all three summary slots in order.")
        require_change_blocks(
            (
                metric.change
                for metric in self.metrics
                if metric.current is not None and metric.prior is not None
            ),
            "campaign comparison",
        )
        for metric in self.metrics:
            if metric.current is not None and metric.current.period != self.current_period:
                raise ValueError("campaign comparison current value has the wrong period.")
            if metric.prior is not None and metric.prior.period != self.prior_period:
                raise ValueError("campaign comparison prior value has the wrong period.")


@dataclass(frozen=True, slots=True)
class CampaignSummaryBuildRequest:
    """Typed values used to construct one exact ordered campaign summary."""

    period: DateRange
    purchases: CampaignMetricValue | None
    attributed_roas: CampaignMetricValue | None
    spend: CampaignMetricValue | None


def build_campaign_summary(request: CampaignSummaryBuildRequest) -> CampaignSummary:
    """Build the exact ordered campaign summary without filling absent values."""
    return CampaignSummary(
        request.period,
        request.purchases,
        request.attributed_roas,
        request.spend,
    )


@dataclass(frozen=True, slots=True)
class CampaignPurchaseSeriesBuildRequest:
    """Typed rows and semantics for one exact campaign purchase series."""

    period: DateRange
    points: tuple[CampaignPurchasePoint, ...]
    units: Units
    basis: MeasurementBasis


def build_campaign_purchase_series(
    request: CampaignPurchaseSeriesBuildRequest,
) -> CampaignPurchaseSeries:
    """Build a true dated purchase series while preserving represented zeroes."""
    return CampaignPurchaseSeries(
        request.period,
        request.points,
        request.units,
        request.basis,
    )


CAMPAIGN_SUMMARY_GOVERNED_METRICS: dict[CampaignSummarySlot, str] = {
    CampaignSummarySlot.PURCHASES: "purchases",
    CampaignSummarySlot.ATTRIBUTED_ROAS: "roas_attributed_14d",
    CampaignSummarySlot.SPEND: "ad_spend",
}
"""The metric-dictionary entry each summary slot measures, for its change semantics."""


def build_campaign_prior_comparison(
    current: CampaignSummary,
    prior: CampaignSummary,
    semantics: ChangeSemantics,
) -> CampaignPriorComparison | None:
    """Compute exact current/prior changes in domain code for the fixed three slots."""
    if prior.period != _previous_period(current.period):
        raise ValueError("campaign comparison requires the previous equal-length period.")
    metrics: list[CampaignMetricComparison] = []
    for slot in _SUMMARY_SLOTS:
        current_value = current.value_for(slot)
        prior_value = prior.value_for(slot)
        if current_value is None and prior_value is None:
            return None
        if current_value is None or prior_value is None:
            metrics.append(
                CampaignMetricComparison(
                    slot,
                    current_value,
                    prior_value,
                    None,
                    None,
                    CampaignComparisonReason.ONE_SIDED,
                    None,
                )
            )
            continue
        percent = percentage_change(current_value.value, prior_value.value)
        metrics.append(
            CampaignMetricComparison(
                slot,
                current_value,
                prior_value,
                absolute_change(current_value.value, prior_value.value),
                percent,
                (
                    CampaignComparisonReason.PRIOR_ZERO_PERCENTAGE_UNDEFINED
                    if percent is None
                    else None
                ),
                build_change(
                    ChangeOperands(current_value.value, prior_value.value, current_value.units),
                    semantics.for_metric(CAMPAIGN_SUMMARY_GOVERNED_METRICS[slot]),
                ),
            )
        )
    return CampaignPriorComparison(current.period, prior.period, tuple(metrics))


@dataclass(frozen=True, slots=True)
class CampaignAttributionDetailRequest:
    """Revalidated safe selection and shaped exact evidence for detail reduction."""

    selection: SafeCampaignDisplaySignature
    period: DateRange
    summary: CampaignSummary
    purchase_series: CampaignPurchaseSeries | None
    evidence_by_slot: tuple[CampaignSlotEvidence, ...]
    prior_period: DateRange | None = None
    comparison: CampaignPriorComparison | None = None

    def __post_init__(self) -> None:
        if self.summary.period != self.period:
            raise ValueError("campaign detail summary must use the selected period.")
        if self.purchase_series is not None and self.purchase_series.period != self.period:
            raise ValueError("campaign detail series must use the selected period.")
        if self.prior_period is not None and self.prior_period != _previous_period(self.period):
            raise ValueError("campaign detail accepts only the previous equal-length period.")
        if self.comparison is not None and (
            self.prior_period is None
            or self.comparison.current_period != self.period
            or self.comparison.prior_period != self.prior_period
        ):
            raise ValueError("campaign detail comparison contradicts its exact periods.")
        slots = tuple(item.slot for item in self.evidence_by_slot)
        expected = (*_CURRENT_SLOTS, *(_PRIOR_SLOTS if self.prior_period is not None else ()))
        if len(set(slots)) != len(slots) or set(slots) != set(expected):
            raise ValueError(
                "campaign detail requires exactly one evidence record per enabled slot."
            )
        evidence = {item.slot: item for item in self.evidence_by_slot}
        for slot in _CURRENT_SLOTS:
            if evidence[slot].requested_period != self.period:
                raise ValueError("current campaign evidence must use the selected period.")
        if self.prior_period is not None and any(
            evidence[slot].requested_period != self.prior_period for slot in _PRIOR_SLOTS
        ):
            raise ValueError("prior campaign evidence must use the exact comparator period.")


@dataclass(frozen=True, slots=True)
class CampaignAttributionDetailArtifact:
    """One immutable selected or evidence-insufficient campaign detail."""

    outcome: CampaignDetailOutcome
    selection: SafeCampaignDisplaySignature
    period: DateRange
    prior_period: DateRange | None
    summary: CampaignSummary
    purchase_series: CampaignPurchaseSeries | None
    comparison: CampaignPriorComparison | None
    evidence_by_slot: tuple[CampaignSlotEvidence, ...]
    boundaries: tuple[str, ...]
    insufficiency_reasons: tuple[CampaignInsufficiencyReason, ...]
    kind: CampaignPageKind = field(init=False, default=CampaignPageKind.CAMPAIGN_DETAIL)

    def __post_init__(self) -> None:
        if self.boundaries != CAMPAIGN_ATTRIBUTION_BOUNDARIES:
            raise ValueError("campaign attribution boundaries are fixed domain policy.")
        if self.summary.period != self.period:
            raise ValueError("campaign detail artifact summary contradicts its period.")
        if self.purchase_series is not None and self.purchase_series.period != self.period:
            raise ValueError("campaign detail artifact series contradicts its period.")
        if self.prior_period is None and self.comparison is not None:
            raise ValueError("campaign detail cannot compare without a prior period.")
        slots = tuple(item.slot for item in self.evidence_by_slot)
        expected = (*_CURRENT_SLOTS, *(_PRIOR_SLOTS if self.prior_period is not None else ()))
        if slots != expected:
            raise ValueError("campaign detail evidence must preserve canonical slot order.")
        if len(set(self.insufficiency_reasons)) != len(self.insufficiency_reasons):
            raise ValueError("campaign detail insufficiency reasons must be unique.")
        if self.insufficiency_reasons != tuple(
            reason for reason in CampaignInsufficiencyReason if reason in self.insufficiency_reasons
        ):
            raise ValueError("campaign detail insufficiency reasons must preserve canonical order.")
        if self.outcome is CampaignDetailOutcome.SELECTED:
            if (
                self.insufficiency_reasons
                or not self.summary.complete
                or self.purchase_series is None
                or len(self.purchase_series.points) < MIN_PURCHASE_SERIES_POINTS
                or any(
                    item.condition is not CampaignEvidenceCondition.AVAILABLE
                    for item in self.evidence_by_slot
                )
                or (self.prior_period is not None and self.comparison is None)
            ):
                raise ValueError("selected campaign detail requires complete compatible evidence.")
        elif not self.insufficiency_reasons:
            raise ValueError("insufficient campaign detail requires at least one exact reason.")


def _expected_shape(evidence: CampaignSlotEvidence) -> bool:
    expected_kind = {
        CampaignEvidenceSlot.PURCHASES: UnitKind.COUNT,
        CampaignEvidenceSlot.ATTRIBUTED_ROAS: UnitKind.RATIO,
        CampaignEvidenceSlot.SPEND: UnitKind.CURRENCY,
        CampaignEvidenceSlot.PURCHASE_SERIES: UnitKind.COUNT,
        CampaignEvidenceSlot.PRIOR_PURCHASES: UnitKind.COUNT,
        CampaignEvidenceSlot.PRIOR_ATTRIBUTED_ROAS: UnitKind.RATIO,
        CampaignEvidenceSlot.PRIOR_SPEND: UnitKind.CURRENCY,
    }[evidence.slot]
    expected_grain = (
        PURCHASE_SERIES_GRAIN
        if evidence.slot is CampaignEvidenceSlot.PURCHASE_SERIES
        else SUMMARY_GRAIN
    )
    return evidence.condition is not CampaignEvidenceCondition.AVAILABLE or (
        evidence.units is not None
        and evidence.units.kind is expected_kind
        and evidence.grain == expected_grain
        and evidence.basis is MeasurementBasis.AD_ATTRIBUTED
    )


def _summary_evidence_pairs(
    summary: CampaignSummary,
    evidence: dict[CampaignEvidenceSlot, CampaignSlotEvidence],
) -> tuple[tuple[CampaignMetricValue | None, CampaignSlotEvidence], ...]:
    return (
        (summary.purchases, evidence[CampaignEvidenceSlot.PURCHASES]),
        (summary.attributed_roas, evidence[CampaignEvidenceSlot.ATTRIBUTED_ROAS]),
        (summary.spend, evidence[CampaignEvidenceSlot.SPEND]),
    )


def _current_detail_reasons(
    request: CampaignAttributionDetailRequest,
    evidence: dict[CampaignEvidenceSlot, CampaignSlotEvidence],
) -> set[CampaignInsufficiencyReason]:
    reasons = {
        _REASON_BY_CONDITION[item.condition]
        for item in request.evidence_by_slot
        if item.condition is not CampaignEvidenceCondition.AVAILABLE
    }
    if any(not _expected_shape(item) for item in request.evidence_by_slot):
        reasons.add(CampaignInsufficiencyReason.INCOMPATIBLE_SCOPE)
    for value, slot_evidence in _summary_evidence_pairs(request.summary, evidence):
        available = slot_evidence.condition is CampaignEvidenceCondition.AVAILABLE
        if available != (value is not None) or (available and slot_evidence.row_count != 1):
            reasons.add(CampaignInsufficiencyReason.INCOMPATIBLE_SCOPE)

    series_evidence = evidence[CampaignEvidenceSlot.PURCHASE_SERIES]
    series_available = series_evidence.condition is CampaignEvidenceCondition.AVAILABLE
    if series_available != (request.purchase_series is not None):
        reasons.add(CampaignInsufficiencyReason.INCOMPATIBLE_SCOPE)
    if request.purchase_series is not None:
        if series_evidence.row_count != len(request.purchase_series.points):
            reasons.add(CampaignInsufficiencyReason.INCOMPATIBLE_SCOPE)
        if len(request.purchase_series.points) < MIN_PURCHASE_SERIES_POINTS:
            reasons.add(CampaignInsufficiencyReason.TOO_SHORT_SERIES)
    return reasons


def _require_purchase_reconciliation(
    request: CampaignAttributionDetailRequest,
    evidence: dict[CampaignEvidenceSlot, CampaignSlotEvidence],
) -> None:
    purchases_evidence = evidence[CampaignEvidenceSlot.PURCHASES]
    series_evidence = evidence[CampaignEvidenceSlot.PURCHASE_SERIES]
    if (
        request.summary.purchases is None
        or request.purchase_series is None
        or purchases_evidence.condition is not CampaignEvidenceCondition.AVAILABLE
        or series_evidence.condition is not CampaignEvidenceCondition.AVAILABLE
    ):
        return
    series_total = sum(
        (Decimal(point.purchases) for point in request.purchase_series.points),
        start=Decimal(0),
    )
    if series_total != Decimal(request.summary.purchases.value):
        raise InvariantError(
            _CAMPAIGN_RECONCILIATION,
            "Complete daily campaign purchases do not reconcile to period purchases.",
        )


def _comparison_reasons(
    request: CampaignAttributionDetailRequest,
    evidence: dict[CampaignEvidenceSlot, CampaignSlotEvidence],
) -> set[CampaignInsufficiencyReason]:
    reasons: set[CampaignInsufficiencyReason] = set()
    if request.prior_period is None:
        return reasons
    if request.comparison is None:
        if all(
            evidence[slot].condition is CampaignEvidenceCondition.AVAILABLE for slot in _PRIOR_SLOTS
        ):
            reasons.add(CampaignInsufficiencyReason.INCOMPATIBLE_SCOPE)
        return reasons
    if any(
        item.reason is CampaignComparisonReason.ONE_SIDED for item in request.comparison.metrics
    ):
        reasons.add(CampaignInsufficiencyReason.ONE_SIDED_COMPARISON)
    if any(
        metric.current != request.summary.value_for(metric.slot)
        for metric in request.comparison.metrics
    ):
        reasons.add(CampaignInsufficiencyReason.INCOMPATIBLE_SCOPE)
    return reasons


def build_campaign_attribution_detail(
    request: CampaignAttributionDetailRequest,
) -> CampaignAttributionDetailArtifact:
    """Reduce exact slot evidence into selected or typed insufficiency."""
    evidence = {item.slot: item for item in request.evidence_by_slot}
    reasons = _current_detail_reasons(request, evidence)
    reasons.update(_comparison_reasons(request, evidence))
    _require_purchase_reconciliation(request, evidence)

    ordered_evidence = tuple(
        evidence[slot]
        for slot in (*_CURRENT_SLOTS, *(_PRIOR_SLOTS if request.prior_period is not None else ()))
    )
    ordered_reasons = _ordered_reasons(reasons)
    outcome = (
        CampaignDetailOutcome.INSUFFICIENT if ordered_reasons else CampaignDetailOutcome.SELECTED
    )
    return CampaignAttributionDetailArtifact(
        outcome=outcome,
        selection=request.selection,
        period=request.period,
        prior_period=request.prior_period,
        summary=request.summary,
        purchase_series=request.purchase_series,
        comparison=request.comparison,
        evidence_by_slot=ordered_evidence,
        boundaries=CAMPAIGN_ATTRIBUTION_BOUNDARIES,
        insufficiency_reasons=ordered_reasons,
    )


type CampaignPageArtifact = CampaignSelectionListArtifact | CampaignAttributionDetailArtifact


_SIGNATURE_FIELDS = frozenset(
    {
        "channel",
        "effective_name",
        "effective_name_status",
        "effective_name_valid_from",
        "effective_name_valid_to",
        "marketplace",
        "platform",
    }
)
_PERIOD_FIELDS = frozenset({"end", "start"})
_UNITS_FIELDS = frozenset({"currency_code", "kind"})
_SUPPRESSION_FIELDS = frozenset({"reason", "rows_suppressed"})
_LIST_EVIDENCE_FIELDS = frozenset(
    {
        "actual_coverage",
        "coverage_holes",
        "freshness_qualifiers",
        "requested_period",
        "source",
    }
)
_CAPABILITY_FIELDS = frozenset({"expires_at", "selection_reference"})
_CHOICE_FIELDS = frozenset({"capability", "signature"})
_LIST_ARTIFACT_FIELDS = frozenset(
    {
        "choices",
        "evidence",
        "kind",
        "outcome",
        "period",
        "reason_codes",
        "recovery_actions",
        "truncated",
    }
)
_METRIC_VALUE_FIELDS = frozenset({"basis", "grain", "period", "units", "value"})
_SUMMARY_FIELDS = frozenset({"attributed_roas", "period", "purchases", "spend"})
_PURCHASE_POINT_FIELDS = frozenset({"date", "purchases"})
_PURCHASE_SERIES_FIELDS = frozenset({"basis", "grain", "period", "points", "units"})
_SLOT_EVIDENCE_FIELDS = frozenset(
    {
        "actual_coverage",
        "basis",
        "condition",
        "coverage_holes",
        "freshness_qualifiers",
        "grain",
        "requested_period",
        "row_count",
        "slot",
        "suppression",
        "truncated",
        "units",
    }
)
_METRIC_COMPARISON_FIELDS = frozenset(
    {
        "absolute_change",
        "current",
        "prior",
        "reason",
        "slot",
    }
)
_PRIOR_COMPARISON_FIELDS = frozenset({"current_period", "metrics", "prior_period"})
_DETAIL_ARTIFACT_FIELDS = frozenset(
    {
        "boundaries",
        "comparison",
        "evidence_by_slot",
        "insufficiency_reasons",
        "kind",
        "outcome",
        "period",
        "prior_period",
        "purchase_series",
        "selection",
        "summary",
    }
)


def _campaign_period_payload(period: DateRange) -> dict[str, str]:
    return {"end": period.end.isoformat(), "start": period.start.isoformat()}


def _campaign_number_payload(value: CampaignNumber) -> int | str:
    return value if isinstance(value, int) else str(value)


def _campaign_units_payload(units: Units) -> dict[str, str | None]:
    return {"currency_code": units.currency_code, "kind": units.kind.value}


def safe_campaign_display_signature_payload(
    signature: SafeCampaignDisplaySignature,
) -> dict[str, object]:
    """Serialize one selector-free campaign display identity."""
    return {
        "channel": signature.channel,
        "effective_name": signature.effective_name,
        "effective_name_status": signature.effective_name_status.value,
        "effective_name_valid_from": (
            None
            if signature.effective_name_valid_from is None
            else signature.effective_name_valid_from.isoformat()
        ),
        "effective_name_valid_to": (
            None
            if signature.effective_name_valid_to is None
            else signature.effective_name_valid_to.isoformat()
        ),
        "marketplace": signature.marketplace,
        "platform": signature.platform,
    }


def _campaign_list_evidence_payload(evidence: CampaignListEvidence) -> dict[str, object]:
    return {
        "actual_coverage": [
            _campaign_period_payload(period) for period in evidence.actual_coverage
        ],
        "coverage_holes": [_campaign_period_payload(period) for period in evidence.coverage_holes],
        "freshness_qualifiers": list(evidence.freshness_qualifiers),
        "requested_period": _campaign_period_payload(evidence.requested_period),
        "source": evidence.source,
    }


def _campaign_choice_payload(choice: CampaignSelectionChoice) -> dict[str, object]:
    return {
        "capability": {
            "expires_at": choice.capability.expires_at.astimezone(UTC).isoformat(),
            "selection_reference": choice.capability.value,
        },
        "signature": safe_campaign_display_signature_payload(choice.signature),
    }


def campaign_selection_list_payload(
    artifact: CampaignSelectionListArtifact,
) -> dict[str, object]:
    """Project one campaign list into its canonical browser-safe v1 payload."""
    return {
        "choices": [_campaign_choice_payload(choice) for choice in artifact.choices],
        "evidence": [_campaign_list_evidence_payload(item) for item in artifact.evidence],
        "kind": artifact.kind.value,
        "outcome": artifact.outcome.value,
        "period": _campaign_period_payload(artifact.period),
        "reason_codes": [reason.value for reason in artifact.reason_codes],
        "recovery_actions": [action.value for action in artifact.recovery_actions],
        "truncated": artifact.truncated,
    }


def _campaign_metric_value_payload(value: CampaignMetricValue | None) -> object:
    if value is None:
        return None
    return {
        "basis": value.basis.value,
        "display_value": metric_display_value(value.units, value.value),
        "grain": list(value.grain.dimensions),
        "period": _campaign_period_payload(value.period),
        "units": _campaign_units_payload(value.units),
        "value": _campaign_number_payload(value.value),
    }


def _campaign_summary_payload(summary: CampaignSummary) -> dict[str, object]:
    return {
        "attributed_roas": _campaign_metric_value_payload(summary.attributed_roas),
        "period": _campaign_period_payload(summary.period),
        "purchases": _campaign_metric_value_payload(summary.purchases),
        "spend": _campaign_metric_value_payload(summary.spend),
    }


def _campaign_purchase_series_payload(
    series: CampaignPurchaseSeries | None,
) -> object:
    if series is None:
        return None
    return {
        "basis": series.basis.value,
        "grain": list(series.grain.dimensions),
        "period": _campaign_period_payload(series.period),
        "points": [
            {
                "date": point.day.isoformat(),
                "purchases": _campaign_number_payload(point.purchases),
            }
            for point in series.points
        ],
        "units": _campaign_units_payload(series.units),
    }


def _campaign_slot_evidence_payload(evidence: CampaignSlotEvidence) -> dict[str, object]:
    return {
        "actual_coverage": [
            _campaign_period_payload(period) for period in evidence.actual_coverage
        ],
        "basis": None if evidence.basis is None else evidence.basis.value,
        "condition": evidence.condition.value,
        "coverage_holes": [_campaign_period_payload(period) for period in evidence.coverage_holes],
        "freshness_qualifiers": list(evidence.freshness_qualifiers),
        "grain": list(evidence.grain.dimensions),
        "requested_period": _campaign_period_payload(evidence.requested_period),
        "row_count": evidence.row_count,
        "slot": evidence.slot.value,
        "suppression": {
            "reason": evidence.suppression.reason,
            "rows_suppressed": evidence.suppression.rows_suppressed,
        },
        "truncated": evidence.truncated,
        "units": None if evidence.units is None else _campaign_units_payload(evidence.units),
    }


def _campaign_metric_comparison_payload(
    comparison: CampaignMetricComparison,
) -> dict[str, object]:
    return {
        "absolute_change": (
            None
            if comparison.absolute_change is None
            else _campaign_number_payload(comparison.absolute_change)
        ),
        "current": _campaign_metric_value_payload(comparison.current),
        "prior": _campaign_metric_value_payload(comparison.prior),
        "reason": None if comparison.reason is None else comparison.reason.value,
        "slot": comparison.slot.value,
        **comparison_change_payload(comparison.percentage_change, comparison.change),
    }


def _campaign_prior_comparison_payload(
    comparison: CampaignPriorComparison | None,
) -> object:
    if comparison is None:
        return None
    return {
        "current_period": _campaign_period_payload(comparison.current_period),
        "metrics": [_campaign_metric_comparison_payload(metric) for metric in comparison.metrics],
        "prior_period": _campaign_period_payload(comparison.prior_period),
    }


def campaign_attribution_detail_payload(
    artifact: CampaignAttributionDetailArtifact,
) -> dict[str, object]:
    """Project one campaign detail into its canonical browser-safe v1 payload."""
    return {
        "boundaries": list(artifact.boundaries),
        "comparison": _campaign_prior_comparison_payload(artifact.comparison),
        "evidence_by_slot": [
            _campaign_slot_evidence_payload(item) for item in artifact.evidence_by_slot
        ],
        "insufficiency_reasons": [reason.value for reason in artifact.insufficiency_reasons],
        "kind": artifact.kind.value,
        "outcome": artifact.outcome.value,
        "period": _campaign_period_payload(artifact.period),
        "prior_period": (
            None
            if artifact.prior_period is None
            else _campaign_period_payload(artifact.prior_period)
        ),
        "purchase_series": _campaign_purchase_series_payload(artifact.purchase_series),
        "selection": safe_campaign_display_signature_payload(artifact.selection),
        "summary": _campaign_summary_payload(artifact.summary),
    }


def campaign_page_artifact_payload(artifact: CampaignPageArtifact) -> dict[str, object]:
    """Serialize either campaign kind under the shared CampaignAttribution v1 identity."""
    if isinstance(artifact, CampaignSelectionListArtifact):
        return campaign_selection_list_payload(artifact)
    return campaign_attribution_detail_payload(artifact)


def _campaign_object(
    value: object,
    fields: frozenset[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} fields are not exact.")
    item = cast("dict[str, object]", value)
    if frozenset(item) != fields:
        raise ValueError(f"{label} fields are not exact.")
    return item


def _campaign_items(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list.")
    return cast("list[object]", value)


def _campaign_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonblank text.")
    return value


def _campaign_optional_text(value: object, label: str) -> str | None:
    return None if value is None else _campaign_text(value, label)


def _campaign_boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean.")
    return value


def _campaign_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer.")
    return value


def _campaign_date(value: object, label: str) -> date:
    text = _campaign_text(value, label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{label} must be an ISO date.") from None
    if parsed.isoformat() != text:
        raise ValueError(f"{label} must be a canonical ISO date.")
    return parsed


def _campaign_optional_date(value: object, label: str) -> date | None:
    return None if value is None else _campaign_date(value, label)


def _campaign_instant(value: object, label: str) -> datetime:
    text = _campaign_text(value, label)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{label} must be an ISO instant.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include an offset.")
    if parsed.astimezone(UTC).isoformat() != text:
        raise ValueError(f"{label} must be a canonical UTC instant.")
    return parsed


def _campaign_period(value: object, label: str) -> DateRange:
    item = _campaign_object(value, _PERIOD_FIELDS, label)
    return DateRange(
        _campaign_date(item["start"], f"{label} start"),
        _campaign_date(item["end"], f"{label} end"),
    )


def _campaign_periods(value: object, label: str) -> tuple[DateRange, ...]:
    return tuple(_campaign_period(item, label) for item in _campaign_items(value, label))


def _campaign_texts(value: object, label: str) -> tuple[str, ...]:
    return tuple(_campaign_text(item, label) for item in _campaign_items(value, label))


def _campaign_number(value: object, label: str) -> CampaignNumber:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer or canonical Decimal text.")
    if isinstance(value, int):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be an integer or canonical Decimal text.")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{label} Decimal text is invalid.") from None
    if not number.is_finite() or str(number) != value:
        raise ValueError(f"{label} Decimal text is not canonical.")
    return number


def _campaign_units(value: object, label: str) -> Units:
    item = _campaign_object(value, _UNITS_FIELDS, label)
    try:
        kind = UnitKind(_campaign_text(item["kind"], f"{label} kind"))
    except ValueError:
        raise ValueError(f"{label} kind is unsupported.") from None
    return Units(kind, _campaign_optional_text(item["currency_code"], f"{label} currency"))


def _campaign_grain(value: object, label: str) -> Grain:
    dimensions = _campaign_texts(value, label)
    return Grain(dimensions)


def safe_campaign_display_signature_from_payload(
    value: object,
) -> SafeCampaignDisplaySignature:
    """Decode only the exact selector-free campaign signature shape."""
    item = _campaign_object(value, _SIGNATURE_FIELDS, "campaign signature")
    try:
        status = EffectiveNameStatus(
            _campaign_text(item["effective_name_status"], "campaign effective-name status")
        )
    except ValueError:
        raise ValueError("campaign effective-name status is unsupported.") from None
    signature = SafeCampaignDisplaySignature(
        effective_name=_campaign_optional_text(item["effective_name"], "campaign effective name"),
        channel=_campaign_text(item["channel"], "campaign channel"),
        marketplace=_campaign_text(item["marketplace"], "campaign marketplace"),
        platform=_campaign_text(item["platform"], "campaign platform"),
        effective_name_status=status,
        effective_name_valid_from=_campaign_optional_date(
            item["effective_name_valid_from"], "campaign effective-name valid-from"
        ),
        effective_name_valid_to=_campaign_optional_date(
            item["effective_name_valid_to"], "campaign effective-name valid-to"
        ),
    )
    if safe_campaign_display_signature_payload(signature) != item:
        raise ValueError("campaign signature is not canonical.")
    return signature


def _campaign_list_evidence(value: object) -> CampaignListEvidence:
    item = _campaign_object(value, _LIST_EVIDENCE_FIELDS, "campaign list evidence")
    return CampaignListEvidence(
        source=_campaign_text(item["source"], "campaign list evidence source"),
        requested_period=_campaign_period(
            item["requested_period"], "campaign list requested period"
        ),
        actual_coverage=_campaign_periods(item["actual_coverage"], "campaign list actual coverage"),
        coverage_holes=_campaign_periods(item["coverage_holes"], "campaign list coverage holes"),
        freshness_qualifiers=_campaign_texts(
            item["freshness_qualifiers"], "campaign list freshness qualifiers"
        ),
    )


def _campaign_choice(value: object) -> CampaignSelectionChoice:
    item = _campaign_object(value, _CHOICE_FIELDS, "campaign choice")
    capability_item = _campaign_object(
        item["capability"], _CAPABILITY_FIELDS, "campaign choice capability"
    )
    return CampaignSelectionChoice(
        signature=safe_campaign_display_signature_from_payload(item["signature"]),
        capability=CampaignSelectionCapability(
            _campaign_text(
                capability_item["selection_reference"],
                "campaign choice selection reference",
            ),
            _campaign_instant(capability_item["expires_at"], "campaign choice expiry"),
        ),
    )


def _campaign_selection_list_from_payload(
    value: object,
) -> CampaignSelectionListArtifact:
    item = _campaign_object(value, _LIST_ARTIFACT_FIELDS, "campaign list artifact")
    if _campaign_text(item["kind"], "campaign list kind") != CampaignPageKind.CAMPAIGN_LIST:
        raise ValueError("campaign list kind is unsupported.")
    try:
        outcome = CampaignListOutcome(_campaign_text(item["outcome"], "campaign list outcome"))
        reasons = tuple(
            CampaignListReason(_campaign_text(reason, "campaign list reason"))
            for reason in _campaign_items(item["reason_codes"], "campaign list reasons")
        )
        actions = tuple(
            CampaignRecoveryAction(_campaign_text(action, "campaign recovery action"))
            for action in _campaign_items(item["recovery_actions"], "campaign recovery actions")
        )
    except ValueError:
        raise ValueError("campaign list enum value is unsupported.") from None
    artifact = CampaignSelectionListArtifact(
        outcome=outcome,
        period=_campaign_period(item["period"], "campaign list period"),
        evidence=tuple(
            _campaign_list_evidence(evidence)
            for evidence in _campaign_items(item["evidence"], "campaign list evidence")
        ),
        truncated=_campaign_boolean(item["truncated"], "campaign list truncation"),
        choices=tuple(
            _campaign_choice(choice)
            for choice in _campaign_items(item["choices"], "campaign choices")
        ),
        reason_codes=reasons,
        recovery_actions=actions,
    )
    if campaign_selection_list_payload(artifact) != item:
        raise ValueError("campaign list payload is not canonical.")
    return artifact


def _campaign_metric_value(value: object, label: str) -> CampaignMetricValue | None:
    if value is None:
        return None
    # A retained pre-display body carries the five fields; a current one adds display_value.
    # The body-level canonical check proves a present display_value is the derived one.
    fields = (
        _METRIC_VALUE_FIELDS | {DISPLAY_VALUE_KEY}
        if carries_display_value(value)
        else _METRIC_VALUE_FIELDS
    )
    item = _campaign_object(value, fields, label)
    try:
        basis = MeasurementBasis(_campaign_text(item["basis"], f"{label} basis"))
    except ValueError:
        raise ValueError(f"{label} basis is unsupported.") from None
    return CampaignMetricValue(
        value=_campaign_number(item["value"], f"{label} value"),
        units=_campaign_units(item["units"], f"{label} units"),
        basis=basis,
        period=_campaign_period(item["period"], f"{label} period"),
        grain=_campaign_grain(item["grain"], f"{label} grain"),
    )


def _campaign_summary(value: object) -> CampaignSummary:
    item = _campaign_object(value, _SUMMARY_FIELDS, "campaign summary")
    return CampaignSummary(
        period=_campaign_period(item["period"], "campaign summary period"),
        purchases=_campaign_metric_value(item["purchases"], "campaign purchases"),
        attributed_roas=_campaign_metric_value(item["attributed_roas"], "campaign attributed ROAS"),
        spend=_campaign_metric_value(item["spend"], "campaign spend"),
    )


def _campaign_purchase_series(value: object) -> CampaignPurchaseSeries | None:
    if value is None:
        return None
    item = _campaign_object(value, _PURCHASE_SERIES_FIELDS, "campaign purchase series")
    try:
        basis = MeasurementBasis(_campaign_text(item["basis"], "campaign purchase-series basis"))
    except ValueError:
        raise ValueError("campaign purchase-series basis is unsupported.") from None
    points: list[CampaignPurchasePoint] = []
    for raw_point in _campaign_items(item["points"], "campaign purchase points"):
        point = _campaign_object(raw_point, _PURCHASE_POINT_FIELDS, "campaign purchase point")
        points.append(
            CampaignPurchasePoint(
                _campaign_date(point["date"], "campaign purchase date"),
                _campaign_number(point["purchases"], "campaign daily purchases"),
            )
        )
    return CampaignPurchaseSeries(
        period=_campaign_period(item["period"], "campaign purchase-series period"),
        points=tuple(points),
        units=_campaign_units(item["units"], "campaign purchase-series units"),
        basis=basis,
        grain=_campaign_grain(item["grain"], "campaign purchase-series grain"),
    )


def _campaign_slot_evidence(value: object) -> CampaignSlotEvidence:
    item = _campaign_object(value, _SLOT_EVIDENCE_FIELDS, "campaign slot evidence")
    suppression_item = _campaign_object(
        item["suppression"], _SUPPRESSION_FIELDS, "campaign evidence suppression"
    )
    basis_value = item["basis"]
    try:
        slot = CampaignEvidenceSlot(_campaign_text(item["slot"], "campaign evidence slot"))
        condition = CampaignEvidenceCondition(
            _campaign_text(item["condition"], "campaign evidence condition")
        )
        basis = (
            None
            if basis_value is None
            else MeasurementBasis(_campaign_text(basis_value, "campaign evidence basis"))
        )
    except ValueError:
        raise ValueError("campaign evidence enum value is unsupported.") from None
    units_value = item["units"]
    return CampaignSlotEvidence(
        slot=slot,
        condition=condition,
        requested_period=_campaign_period(
            item["requested_period"], "campaign evidence requested period"
        ),
        actual_coverage=_campaign_periods(
            item["actual_coverage"], "campaign evidence actual coverage"
        ),
        coverage_holes=_campaign_periods(
            item["coverage_holes"], "campaign evidence coverage holes"
        ),
        units=(
            None if units_value is None else _campaign_units(units_value, "campaign evidence units")
        ),
        grain=_campaign_grain(item["grain"], "campaign evidence grain"),
        basis=basis,
        suppression=Suppression(
            _campaign_boolean(
                suppression_item["rows_suppressed"],
                "campaign evidence rows-suppressed",
            ),
            _campaign_optional_text(
                suppression_item["reason"], "campaign evidence suppression reason"
            ),
        ),
        row_count=_campaign_integer(item["row_count"], "campaign evidence row count"),
        truncated=_campaign_boolean(item["truncated"], "campaign evidence truncation"),
        freshness_qualifiers=_campaign_texts(
            item["freshness_qualifiers"], "campaign evidence freshness qualifiers"
        ),
    )


def _campaign_metric_comparison(value: object) -> CampaignMetricComparison:
    # Every comparison carries the change key; the retained form without one is refused.
    item = _campaign_object(
        value,
        comparison_fields(value, _METRIC_COMPARISON_FIELDS),
        "campaign metric comparison",
    )
    reason_value = item["reason"]
    change_value = item["absolute_change"]
    try:
        slot = CampaignSummarySlot(_campaign_text(item["slot"], "campaign comparison slot"))
        reason = (
            None
            if reason_value is None
            else CampaignComparisonReason(
                _campaign_text(reason_value, "campaign comparison reason")
            )
        )
    except ValueError:
        raise ValueError("campaign comparison enum value is unsupported.") from None
    current = _campaign_metric_value(item["current"], "campaign comparison current")
    prior = _campaign_metric_value(item["prior"], "campaign comparison prior")
    block = item.get(CHANGE_KEY)
    change = (
        None
        if current is None or prior is None
        else change_from_payload(
            block,
            ChangeOperands(current.value, prior.value, current.units),
            "campaign comparison",
        )
    )
    if change is None and block is not None:
        raise ValueError("a one-sided campaign comparison carries no change.")
    return CampaignMetricComparison(
        slot=slot,
        current=current,
        prior=prior,
        absolute_change=(
            None
            if change_value is None
            else _campaign_number(change_value, "campaign comparison absolute change")
        ),
        percentage_change=(
            _campaign_optional_text(item[PERCENTAGE_KEY], "campaign comparison percentage")
            if PERCENTAGE_KEY in item
            else None
            if current is None or prior is None
            else percentage_change(current.value, prior.value)
        ),
        reason=reason,
        change=change,
    )


def _campaign_prior_comparison(value: object) -> CampaignPriorComparison | None:
    if value is None:
        return None
    item = _campaign_object(value, _PRIOR_COMPARISON_FIELDS, "campaign prior comparison")
    return CampaignPriorComparison(
        current_period=_campaign_period(
            item["current_period"], "campaign comparison current period"
        ),
        prior_period=_campaign_period(item["prior_period"], "campaign comparison prior period"),
        metrics=tuple(
            _campaign_metric_comparison(metric)
            for metric in _campaign_items(item["metrics"], "campaign comparison metrics")
        ),
    )


def _campaign_detail_from_payload(value: object) -> CampaignAttributionDetailArtifact:
    item = _campaign_object(value, _DETAIL_ARTIFACT_FIELDS, "campaign detail artifact")
    # A body is wholly pre-display or wholly current; a mixed one is refused before decoding.
    display_value_mode(item)
    if _campaign_text(item["kind"], "campaign detail kind") != CampaignPageKind.CAMPAIGN_DETAIL:
        raise ValueError("campaign detail kind is unsupported.")
    prior_value = item["prior_period"]
    try:
        outcome = CampaignDetailOutcome(_campaign_text(item["outcome"], "campaign detail outcome"))
        reasons = tuple(
            CampaignInsufficiencyReason(_campaign_text(reason, "campaign insufficiency reason"))
            for reason in _campaign_items(
                item["insufficiency_reasons"], "campaign insufficiency reasons"
            )
        )
    except ValueError:
        raise ValueError("campaign detail enum value is unsupported.") from None
    artifact = CampaignAttributionDetailArtifact(
        outcome=outcome,
        selection=safe_campaign_display_signature_from_payload(item["selection"]),
        period=_campaign_period(item["period"], "campaign detail period"),
        prior_period=(
            None
            if prior_value is None
            else _campaign_period(prior_value, "campaign detail prior period")
        ),
        summary=_campaign_summary(item["summary"]),
        purchase_series=_campaign_purchase_series(item["purchase_series"]),
        comparison=_campaign_prior_comparison(item["comparison"]),
        evidence_by_slot=tuple(
            _campaign_slot_evidence(evidence)
            for evidence in _campaign_items(item["evidence_by_slot"], "campaign evidence slots")
        ),
        boundaries=_campaign_texts(item["boundaries"], "campaign boundaries"),
        insufficiency_reasons=reasons,
    )
    canonical = campaign_attribution_detail_payload(artifact)
    if not canonical_matches(canonical, item):
        raise ValueError("campaign detail payload is not canonical.")
    return artifact


def campaign_page_artifact_from_payload(value: object) -> CampaignPageArtifact:
    """Decode and re-attest either exact CampaignAttribution v1 payload kind."""
    if not isinstance(value, dict):
        raise ValueError("campaign artifact must be an object.")
    item = cast("dict[str, object]", value)
    kind = _campaign_text(item.get("kind"), "campaign artifact kind")
    if kind == CampaignPageKind.CAMPAIGN_LIST:
        return _campaign_selection_list_from_payload(item)
    if kind == CampaignPageKind.CAMPAIGN_DETAIL:
        return _campaign_detail_from_payload(item)
    raise ValueError("campaign artifact kind is unsupported.")


# Campaign Portfolio V2 is a closed extension of the frozen CampaignAttribution V1
# artifact. V1 fields and meanings remain unchanged; V1 metric values gained the server
# `display_value` on 2026-10-01 (ADR-084).
CAMPAIGN_ATTRIBUTION_DETAIL_ARTIFACT_TYPE = "CampaignAttributionDetail"
CAMPAIGN_ATTRIBUTION_DETAIL_ARTIFACT_VERSION = 2


class CampaignDeliverySlot(StrEnum):
    """Fixed server-owned delivery metric order for Campaign Portfolio V2."""

    AD_SPEND = "ad_spend"
    IMPRESSIONS = "impressions"
    CLICKS = "clicks"
    CTR = "ctr"
    CPC = "cpc"
    CPM = "cpm"


_DELIVERY_SLOTS = tuple(CampaignDeliverySlot)


class CampaignEvidenceStatusCondition(StrEnum):
    """Closed ADR-072 per-slot status conditions; codes stay on the wire and in logs."""

    AVAILABLE = "available"
    EMPTY_COVERED = "empty_covered"
    MISSING_CURRENT_PUBLICATION = "missing_current_publication"
    MISSING_COMPARATOR_PUBLICATION = "missing_comparator_publication"
    SUPPRESSED = "suppressed"
    TRUNCATED = "truncated"
    COVERAGE_HOLE = "coverage_hole"
    INCOMPLETE_DAILY_SERIES = "incomplete_daily_series"
    SERIES_TOTAL_DIVERGENCE = "series_total_divergence"
    INCOMPATIBLE_SCOPE = "incompatible_scope"


@dataclass(frozen=True, slots=True)
class CampaignDeliveryAnswer:
    """One delivery (slot, role): its accepted value, or its typed refusal with safe counts.

    A value counts only when the condition is `AVAILABLE`. An incomplete derived daily series
    keeps its defined days, so the bake can densify them as it does every daily answer.
    """

    value: CampaignMetricValue | tuple[tuple[date, CampaignNumber], ...] | None
    condition: CampaignEvidenceStatusCondition
    days_present: int | None = None
    days_expected: int | None = None
    missing_dates: tuple[date, ...] = ()
    """The days a coverage hole or an incomplete daily series lacks (ADR-119)."""


def derive_campaign_cpc(
    spend: CampaignDeliveryAnswer, clicks: CampaignDeliveryAnswer
) -> CampaignDeliveryAnswer:
    """Derive one role's cost per click from its accepted ad spend and clicks answers.

    Amazon DSP writes spend (with its currency) and clicks in separate publications, so a
    planned `cpc` quotient grouped by currency splits into two half-rows and yields nothing.
    Dividing the accepted operands gives every channel the same figure the planner gives a
    single-writer row: the same sums, rounded to the same `RATIO_SCALE`.

    The first refused operand is returned as it stands, condition and safe counts alike.
    Zero clicks has no cost per click: an aggregate is `EMPTY_COVERED`, and a daily series
    lacks that day, so it is an `INCOMPLETE_DAILY_SERIES` naming the missing days.
    """
    refused = next(
        (
            operand
            for operand in (spend, clicks)
            if operand.value is None
            or operand.condition is not CampaignEvidenceStatusCondition.AVAILABLE
        ),
        None,
    )
    if refused is not None:
        return refused
    spent, clicked = spend.value, clicks.value
    if isinstance(spent, CampaignMetricValue) and isinstance(clicked, CampaignMetricValue):
        if not clicked.value:
            return CampaignDeliveryAnswer(None, CampaignEvidenceStatusCondition.EMPTY_COVERED)
        return CampaignDeliveryAnswer(
            replace(spent, value=_cost_per_click(spent.value, clicked.value)),
            CampaignEvidenceStatusCondition.AVAILABLE,
        )
    if not isinstance(spent, tuple) or not isinstance(clicked, tuple):
        raise ValueError("cost per click operands must both be aggregates or daily series.")
    clicks_by_day = dict(clicked)
    points = tuple(
        (day, _cost_per_click(value, clicks_by_day[day]))
        for day, value in spent
        if clicks_by_day.get(day)
    )
    if len(points) == len(spent):
        return CampaignDeliveryAnswer(points, CampaignEvidenceStatusCondition.AVAILABLE)
    return CampaignDeliveryAnswer(
        points,
        CampaignEvidenceStatusCondition.INCOMPLETE_DAILY_SERIES,
        days_present=len(points),
        days_expected=len(spent),
        missing_dates=tuple(day for day, _ in spent if not clicks_by_day.get(day)),
    )


def _cost_per_click(spend: CampaignNumber, clicks: CampaignNumber) -> Decimal:
    return (Decimal(spend) / Decimal(clicks)).quantize(RATIO_SCALE)


_ATTRIBUTION_STATUS_ROSTER: tuple[tuple[str, str], ...] = (
    ("purchases", "current"),
    ("attributed_roas", "current"),
    ("spend", "current"),
    ("purchase_series", "daily"),
    ("prior_purchases", "prior"),
    ("prior_attributed_roas", "prior"),
    ("prior_spend", "prior"),
)
_DELIVERY_STATUS_ROLES = ("current", "prior", "daily", "prior_daily")
_DELIVERY_STATUS_ROSTER: tuple[tuple[str, str], ...] = tuple(
    (slot.value, role) for slot in _DELIVERY_SLOTS for role in _DELIVERY_STATUS_ROLES
)
_EVIDENCE_STATUS_ROSTER = _ATTRIBUTION_STATUS_ROSTER + _DELIVERY_STATUS_ROSTER
_EVIDENCE_STATUS_PAIRS = frozenset(_EVIDENCE_STATUS_ROSTER)
_STATUS_METRIC_WORDS = {
    "purchases": "purchases",
    "attributed_roas": "attributed ROAS",
    "spend": "ad spend",
    "purchase_series": "the daily purchases series",
    "prior_purchases": "prior-window purchases",
    "prior_attributed_roas": "prior-window attributed ROAS",
    "prior_spend": "prior-window ad spend",
    "ad_spend": "ad spend",
    "impressions": "impressions",
    "clicks": "clicks",
    "ctr": "click-through rate",
    "cpc": "cost per click",
    "cpm": "cost per thousand impressions",
}
_STATUS_ROLE_WORDS = {
    "current": "this window",
    "prior": "the prior window",
    "daily": "this window's daily series",
    "prior_daily": "the prior window's daily series",
}


@dataclass(frozen=True, slots=True)
class CampaignEvidenceStatus:
    """One (slot, role)'s served status: accepted, or a named refusal with served words."""

    slot: str
    role: str
    condition: CampaignEvidenceStatusCondition
    explanation: str
    recovery_reading: str | None = None
    days_present: int | None = None
    days_expected: int | None = None
    missing_dates: tuple[date, ...] = ()
    """The days a refused slot lacks, where the server knows them; the coverage block names
    them (ADR-119). Not part of this status's own wire form."""

    def __post_init__(self) -> None:
        if (self.slot, self.role) not in _EVIDENCE_STATUS_PAIRS:
            raise ValueError("campaign evidence status names an undeclared slot or role.")
        _nonblank(self.explanation, "campaign evidence status explanation")
        available = self.condition is CampaignEvidenceStatusCondition.AVAILABLE
        if available != (self.recovery_reading is None):
            raise ValueError(
                "a refused evidence status serves its recovery reading; accepted serves none."
            )
        if available and self.missing_dates:
            raise ValueError("an accepted evidence status has no missing dates.")
        if self.recovery_reading is not None:
            _nonblank(self.recovery_reading, "campaign evidence status recovery reading")
        counted = self.condition is CampaignEvidenceStatusCondition.INCOMPLETE_DAILY_SERIES
        if counted:
            if (
                self.days_present is None
                or self.days_expected is None
                or not 0 <= self.days_present < self.days_expected
            ):
                raise ValueError("an incomplete daily series states its day counts exactly.")
        elif self.days_present is not None or self.days_expected is not None:
            raise ValueError("day counts belong only to an incomplete daily series.")


@dataclass(frozen=True, slots=True)
class CampaignEvidenceStatusRequest:
    """Condition and safe counts for one (slot, role); the factory composes the served words."""

    slot: str
    role: str
    condition: CampaignEvidenceStatusCondition
    days_present: int | None = None
    days_expected: int | None = None
    missing_dates: tuple[date, ...] = ()


def campaign_evidence_status(request: CampaignEvidenceStatusRequest) -> CampaignEvidenceStatus:
    """Compose one status with governed served words; copy lives here, never in a browser."""
    status = _campaign_evidence_status(request)
    return replace(status, missing_dates=tuple(sorted(set(request.missing_dates))))


def _campaign_evidence_status(request: CampaignEvidenceStatusRequest) -> CampaignEvidenceStatus:
    """Compose one status with governed served words; copy lives here, never in a browser."""
    if (request.slot, request.role) not in _EVIDENCE_STATUS_PAIRS:
        raise ValueError("campaign evidence status names an undeclared slot or role.")
    metric = _STATUS_METRIC_WORDS[request.slot]
    scope = _STATUS_ROLE_WORDS[request.role]
    condition = request.condition
    if condition is CampaignEvidenceStatusCondition.AVAILABLE:
        return CampaignEvidenceStatus(
            request.slot,
            request.role,
            condition,
            f"Complete: {metric} is fully served for {scope}.",
        )
    if condition is CampaignEvidenceStatusCondition.INCOMPLETE_DAILY_SERIES:
        return CampaignEvidenceStatus(
            request.slot,
            request.role,
            condition,
            (
                f"{request.days_present} of {request.days_expected} days have a {metric} row in "
                f"{scope}, and a trend is served only when every day is present."
            ),
            (
                "This fills when the source delivers the missing days; a window whose days are "
                "fully covered can be opened now."
            ),
            days_present=request.days_present,
            days_expected=request.days_expected,
        )
    explanation, recovery = {
        CampaignEvidenceStatusCondition.EMPTY_COVERED: (
            f"The window is covered but carries no {metric} rows for this campaign.",
            (
                "This fills when the source reports activity for this campaign; if activity is "
                "expected here, contact the workspace data owner."
            ),
        ),
        CampaignEvidenceStatusCondition.MISSING_CURRENT_PUBLICATION: (
            f"No publication covering {scope} has arrived for {metric}.",
            "This fills when the source publishes the window; try again after the next delivery.",
        ),
        CampaignEvidenceStatusCondition.MISSING_COMPARATOR_PUBLICATION: (
            (
                f"No publication covering {scope} has arrived for {metric}, so the comparison "
                "cannot be drawn."
            ),
            "Choose a period whose prior window falls inside published coverage.",
        ),
        CampaignEvidenceStatusCondition.SUPPRESSED: (
            f"Some {metric} rows for {scope} were withheld by the source's privacy thresholds.",
            "A broader period can clear the threshold.",
        ),
        CampaignEvidenceStatusCondition.TRUNCATED: (
            f"The {metric} result for {scope} exceeded its row bound and was cut short.",
            "Narrow the period so the result fits its bound.",
        ),
        CampaignEvidenceStatusCondition.COVERAGE_HOLE: (
            f"Published coverage for {scope} has gaps, so {metric} cannot be read end to end.",
            "Choose a period that falls fully inside published coverage.",
        ),
        CampaignEvidenceStatusCondition.SERIES_TOTAL_DIVERGENCE: (
            (
                f"The daily {metric} values do not add up to the window total, so neither figure "
                "is served."
            ),
            "This resolves when the source restates the window; no action is needed.",
        ),
        CampaignEvidenceStatusCondition.INCOMPATIBLE_SCOPE: (
            f"The returned {metric} evidence did not match this campaign's declared scope.",
            "If this persists, contact the workspace data owner.",
        ),
    }[condition]
    return CampaignEvidenceStatus(request.slot, request.role, condition, explanation, recovery)


def campaign_delivery_accepted_statuses() -> tuple[CampaignEvidenceStatus, ...]:
    """Return the canonical delivery-status roster with every slot accepted."""
    return tuple(
        campaign_evidence_status(
            CampaignEvidenceStatusRequest(slot, role, CampaignEvidenceStatusCondition.AVAILABLE)
        )
        for slot, role in _DELIVERY_STATUS_ROSTER
    )


def campaign_delivery_missing_statuses() -> tuple[CampaignEvidenceStatus, ...]:
    """Return the canonical roster with every delivery slot missing its publication."""
    return tuple(
        campaign_evidence_status(
            CampaignEvidenceStatusRequest(
                slot,
                role,
                (
                    CampaignEvidenceStatusCondition.MISSING_COMPARATOR_PUBLICATION
                    if role in {"prior", "prior_daily"}
                    else CampaignEvidenceStatusCondition.MISSING_CURRENT_PUBLICATION
                ),
            )
        )
        for slot, role in _DELIVERY_STATUS_ROSTER
    )


_STATUS_BY_EVIDENCE_CONDITION = {
    CampaignEvidenceCondition.AVAILABLE: CampaignEvidenceStatusCondition.AVAILABLE,
    CampaignEvidenceCondition.EMPTY_COVERED: CampaignEvidenceStatusCondition.EMPTY_COVERED,
    CampaignEvidenceCondition.MISSING_CURRENT_PUBLICATION: (
        CampaignEvidenceStatusCondition.MISSING_CURRENT_PUBLICATION
    ),
    CampaignEvidenceCondition.MISSING_COMPARATOR_PUBLICATION: (
        CampaignEvidenceStatusCondition.MISSING_COMPARATOR_PUBLICATION
    ),
    CampaignEvidenceCondition.SUPPRESSED: CampaignEvidenceStatusCondition.SUPPRESSED,
    CampaignEvidenceCondition.TRUNCATED: CampaignEvidenceStatusCondition.TRUNCATED,
    CampaignEvidenceCondition.COVERAGE_HOLE: CampaignEvidenceStatusCondition.COVERAGE_HOLE,
    CampaignEvidenceCondition.INCOMPATIBLE_SCOPE: (
        CampaignEvidenceStatusCondition.INCOMPATIBLE_SCOPE
    ),
}
_ATTRIBUTION_STATUS_ROLE_BY_SLOT: dict[str, str] = dict(_ATTRIBUTION_STATUS_ROSTER)


def campaign_attribution_statuses(
    detail: CampaignAttributionDetailArtifact,
) -> tuple[CampaignEvidenceStatus, ...]:
    """Project the V1 per-slot conditions into served-words statuses for the sheet."""
    by_slot = {item.slot.value: item for item in detail.evidence_by_slot}
    return tuple(
        campaign_evidence_status(
            CampaignEvidenceStatusRequest(
                slot,
                _ATTRIBUTION_STATUS_ROLE_BY_SLOT[slot],
                _STATUS_BY_EVIDENCE_CONDITION[by_slot[slot].condition],
                missing_dates=()
                if by_slot[slot].condition is not CampaignEvidenceCondition.COVERAGE_HOLE
                else uncovered_days(
                    by_slot[slot].requested_period,
                    by_slot[slot].actual_coverage,
                    by_slot[slot].coverage_holes,
                ),
            )
        )
        for slot, _ in _ATTRIBUTION_STATUS_ROSTER
    )


def uncovered_days(
    period: DateRange, covered: tuple[DateRange, ...], holes: tuple[DateRange, ...]
) -> tuple[date, ...]:
    """Return the days of `period` that published coverage doesn't reach, in order."""
    days = (period.start + timedelta(days=offset) for offset in range(period.days))
    return tuple(
        day
        for day in days
        if not any(item.start <= day <= item.end for item in covered)
        or any(item.start <= day <= item.end for item in holes)
    )


@dataclass(frozen=True, slots=True)
class CampaignDeliveryValue:
    """One delivery metric value or an explicit unavailable slot."""

    slot: CampaignDeliverySlot
    value: CampaignMetricValue | None
    change: Change | None = None
    """On a comparison entry only: the change from this prior value to the summary's current
    value (ADR-121). A summary entry and an absent side carry none."""

    def __post_init__(self) -> None:
        if self.value is None:
            if self.change is not None:
                raise ValueError("an unavailable delivery value carries no change.")
            return
        expected_kind = (
            UnitKind.COUNT
            if self.slot in {CampaignDeliverySlot.IMPRESSIONS, CampaignDeliverySlot.CLICKS}
            else UnitKind.RATIO
            if self.slot is CampaignDeliverySlot.CTR
            else UnitKind.CURRENCY
        )
        if (
            self.value.units.kind is not expected_kind
            or self.value.grain != SUMMARY_GRAIN
            or self.value.basis is not MeasurementBasis.AD_ATTRIBUTED
        ):
            raise ValueError("campaign delivery value has incompatible units, grain, or basis.")


@dataclass(frozen=True, slots=True)
class CampaignDspQuality:
    """DSP-quality disclosure; unavailable values are never represented as zero."""

    outcome: str
    measurable_impressions: CampaignMetricValue | None
    viewable_impressions: CampaignMetricValue | None
    viewable_impressions_estimated: CampaignMetricValue | None
    viewable_rate: CampaignMetricValue | None
    reason_code: str | None

    def __post_init__(self) -> None:
        if self.outcome not in {"available", "unavailable"}:
            raise ValueError("campaign DSP-quality outcome is unsupported.")
        values = (
            self.measurable_impressions,
            self.viewable_impressions,
            self.viewable_impressions_estimated,
            self.viewable_rate,
        )
        if self.outcome == "available":
            if any(value is None for value in values) or self.reason_code is not None:
                raise ValueError("available DSP quality requires all values without a reason.")
        elif any(value is not None for value in values) or self.reason_code is None:
            raise ValueError("unavailable DSP quality requires a reason and no numeric values.")


def _validate_delivery_statuses(outcome: str, evidence: tuple[CampaignEvidenceStatus, ...]) -> None:
    """Require the canonical itemized roster and outcome-consistent conditions (ADR-072)."""
    if outcome not in {"complete", "qualified", "insufficient"}:
        raise ValueError("campaign delivery outcome is unsupported.")
    roster = tuple((status.slot, status.role) for status in evidence)
    if roster not in {_DELIVERY_STATUS_ROSTER, _EVIDENCE_STATUS_ROSTER}:
        raise ValueError("campaign delivery evidence must itemize the canonical roster.")
    delivery_statuses = evidence[-len(_DELIVERY_STATUS_ROSTER) :]
    delivery_accepted = all(
        status.condition is CampaignEvidenceStatusCondition.AVAILABLE
        for status in delivery_statuses
    )
    if outcome in {"complete", "qualified"} and not delivery_accepted:
        raise ValueError("served campaign delivery cannot carry refused evidence statuses.")
    if outcome == "insufficient" and delivery_accepted:
        raise ValueError("insufficient campaign delivery must name what refused (ADR-072).")


@dataclass(frozen=True, slots=True)
class CampaignDelivery:
    """Closed V2 delivery extension with exact server-returned, never-browser-derived values."""

    outcome: str
    summary: tuple[CampaignDeliveryValue, ...]
    comparison_period: DateRange
    comparison: tuple[CampaignDeliveryValue, ...]
    series_points: tuple[dict[str, object], ...]
    exact_rows: tuple[dict[str, object], ...]
    dsp_quality: CampaignDspQuality
    evidence: tuple[CampaignEvidenceStatus, ...]
    recovery: str
    narration_reference: None = None
    available_grains: tuple[str, ...] = ("day",)
    series_metrics: tuple[CampaignDeliverySlot, ...] = _DELIVERY_SLOTS

    def __post_init__(self) -> None:  # noqa: PLR0912, PLR0915 -- one closed contract
        _validate_delivery_statuses(self.outcome, self.evidence)
        if self.available_grains != ("day",) or self.series_metrics != _DELIVERY_SLOTS:
            raise ValueError("campaign delivery series declaration is unsupported.")
        if tuple(value.slot for value in self.summary) != _DELIVERY_SLOTS:
            raise ValueError("campaign delivery summary must retain canonical slot order.")
        if tuple(value.slot for value in self.comparison) != _DELIVERY_SLOTS:
            raise ValueError("campaign delivery comparison must retain canonical slot order.")
        if any(
            value.value is not None and value.value.period != self.comparison_period
            for value in self.comparison
        ):
            raise ValueError("campaign delivery comparison values have the wrong period.")
        if any(value.change is not None for value in self.summary):
            raise ValueError("a campaign delivery summary value carries no change.")
        computable: list[Change | None] = []
        for current, prior in zip(self.summary, self.comparison, strict=True):
            if current.value is None or prior.value is None:
                if prior.change is not None:
                    raise ValueError("a one-sided campaign delivery value carries no change.")
                continue
            if prior.change is not None:
                check_change(
                    prior.change,
                    ChangeOperands(current.value.value, prior.value.value, current.value.units),
                )
            computable.append(prior.change)
        require_change_blocks(computable, "campaign delivery comparison")
        summary_periods = {value.value.period for value in self.summary if value.value is not None}
        if len(summary_periods) > 1:
            raise ValueError("campaign delivery summary values must share one period.")
        summary_period = next(iter(summary_periods), None)
        expected_days = (
            ()
            if summary_period is None
            else tuple(
                summary_period.start + timedelta(days=offset)
                for offset in range(summary_period.days)
            )
        )
        rows = self.series_points
        if self.series_points != self.exact_rows:
            raise ValueError("campaign delivery points must equal exact rows.")
        if rows:
            parsed_days: list[date] = []
            for row in rows:
                if frozenset(row) != frozenset({"current", "date", "prior"}):
                    raise ValueError("campaign delivery daily rows must be exact.")
                raw_day = row["date"]
                try:
                    row_day = date.fromisoformat(raw_day) if isinstance(raw_day, str) else None
                except ValueError:
                    row_day = None
                if row_day is None:
                    raise ValueError("campaign delivery daily row is malformed.")
                for series_name in ("current", "prior"):
                    raw_values = row[series_name]
                    if not isinstance(raw_values, dict):
                        raise ValueError("campaign delivery daily row is malformed.")
                    typed_values = cast("dict[str, object]", raw_values)
                    if frozenset(typed_values) != frozenset(slot.value for slot in _DELIVERY_SLOTS):
                        raise ValueError("campaign delivery daily row is malformed.")
                    if any(
                        isinstance(number, bool)
                        or not isinstance(number, (int, Decimal))
                        or number < 0
                        for number in typed_values.values()
                    ):
                        raise ValueError(
                            "campaign delivery daily values must be non-negative numbers."
                        )
                parsed_days.append(row_day)
            if tuple(parsed_days) != tuple(sorted(parsed_days)) or len(set(parsed_days)) != len(
                parsed_days
            ):
                raise ValueError("campaign delivery daily rows must be ordered and unique.")
            if expected_days and tuple(parsed_days) != expected_days:
                raise ValueError(
                    "campaign delivery daily rows must exactly cover the selected period."
                )
        if self.outcome == "complete" and (
            any(value.value is None for value in (*self.summary, *self.comparison))
            or not rows
            or self.dsp_quality.outcome != "available"
        ):
            raise ValueError(
                "complete campaign delivery requires all values, daily rows, and DSP quality."
            )
        if self.outcome == "qualified" and (
            any(value.value is None for value in (*self.summary, *self.comparison)) or not rows
        ):
            raise ValueError(
                "qualified campaign delivery requires complete core delivery evidence."
            )
        if self.outcome == "insufficient" and any(
            value.value is not None for value in self.summary
        ):
            raise ValueError("insufficient campaign delivery cannot expose partial summary values.")
        if self.narration_reference is not None or not self.recovery.strip():
            raise ValueError("campaign delivery narration and recovery are invalid.")


_DETAIL_DICTIONARY_IDS = {
    "purchases": "purchases",
    "attributed_roas": "roas_attributed_14d",
    "spend": "ad_spend",
    "purchase_series": "purchases",
    "prior_purchases": "purchases",
    "prior_attributed_roas": "roas_attributed_14d",
    "prior_spend": "ad_spend",
    **{slot.value: slot.value for slot in _DELIVERY_SLOTS},
}
_DETAIL_ROLE_WORDS = {
    "current": "this window",
    "prior": "prior window",
    "daily": "daily series, this window",
    "prior_daily": "daily series, prior window",
}
_ATTRIBUTION_STATUS_SLOTS = frozenset(slot for slot, _ in _ATTRIBUTION_STATUS_ROSTER)
"""Each role label names its source family, as the mockup's Role column does, so the
attributed and delivery ad spend slots never read as the same figure and role."""
_DETAIL_ROLE_LABELS = {
    (family, role): f"{family}, {words}"
    for family in ("Attributed outcomes", "Delivery")
    for role, words in _DETAIL_ROLE_WORDS.items()
}
_DETAIL_RATIO_SLOTS = frozenset({"attributed_roas", "prior_attributed_roas", "ctr", "cpc", "cpm"})
_UNSENT_CONDITIONS = frozenset(
    {
        CampaignEvidenceStatusCondition.MISSING_CURRENT_PUBLICATION,
        CampaignEvidenceStatusCondition.MISSING_COMPARATOR_PUBLICATION,
    }
)
"""Refusals whose source has sent nothing for the slot's period: its sheet row reads
"Not sent" (ADR-119). Never-loaded answers refuse with these conditions too."""


def campaign_detail_evidence_status(
    period: DateRange,
    statuses: tuple[CampaignEvidenceStatus, ...],
    labels: Mapping[str, str],
) -> EvidenceStatus:
    """Write campaign detail's coverage block from its served per-slot statuses (ADR-119).

    One entry per (slot, role), in the statuses' order. An accepted slot is complete; a refused
    one isn't shown, and its entry carries the status's served reason and recovery and the days
    it lacks, where the server knows them. `labels` maps dictionary ids to display labels.
    """
    named = labels_for(
        (_DETAIL_DICTIONARY_IDS[status.slot] for status in statuses), labels, "campaign detail"
    )
    return write_evidence_status(
        period,
        tuple(
            CoverageFact(
                status.slot,
                named[_DETAIL_DICTIONARY_IDS[status.slot]],
                _DETAIL_ROLE_LABELS[
                    (
                        "Attributed outcomes"
                        if status.slot in _ATTRIBUTION_STATUS_SLOTS
                        else "Delivery",
                        status.role,
                    )
                ],
                CoverageKind.RATIO if status.slot in _DETAIL_RATIO_SLOTS else CoverageKind.ADDITIVE,
                role=status.role,
                missing_dates=status.missing_dates,
                entity_word="days",
                unavailable=None
                if status.recovery_reading is None
                else (status.explanation, status.recovery_reading),
                unsent=status.condition in _UNSENT_CONDITIONS,
            )
            for status in statuses
        ),
    )


@dataclass(frozen=True, slots=True)
class CampaignAttributionDetailV2:
    """V2 selected detail that preserves V1 semantics and adds governed Slice-C slots."""

    detail: CampaignAttributionDetailArtifact
    delivery: CampaignDelivery
    insights: tuple[InsightSlot, ...] = field(default_factory=default_detail_insights)
    evidence_status: EvidenceStatus | None = None
    """The served coverage block (ADR-119), written from the served statuses; required on the
    wire. `None` only on the measured-bytes form insight fingerprints read."""

    def __post_init__(self) -> None:
        status = self.evidence_status
        if status is not None:
            require_entries(
                status,
                tuple((item.slot, item.role) for item in self.delivery.evidence),
                self.detail.period,
                label="campaign detail",
            )
            for entry, served in zip(status.entries, self.delivery.evidence, strict=True):
                accepted = served.condition is CampaignEvidenceStatusCondition.AVAILABLE
                if accepted != (entry.state is CoverageState.COMPLETE) or (
                    not accepted and entry.state is not CoverageState.UNAVAILABLE
                ):
                    raise ValueError("campaign detail coverage contradicts its served statuses.")
        require_insight_roster(
            self.insights,
            (
                InsightSlotId.TREND_SHAPE,
                InsightSlotId.EFFICIENCY_DRIFT,
                InsightSlotId.FUNNEL,
                InsightSlotId.NEXT_CHECK,
            ),
        )
        if (
            self.detail.prior_period is None
            or self.detail.prior_period != self.delivery.comparison_period
        ):
            raise ValueError("campaign V2 delivery comparison must match the V1 prior period.")


def campaign_delivery_comparison(
    summary: tuple[CampaignDeliveryValue, ...],
    prior: tuple[CampaignMetricValue | None, ...],
    semantics: ChangeSemantics,
) -> tuple[CampaignDeliveryValue, ...]:
    """Pair each prior delivery value with its change from the summary's current value."""
    entries: list[CampaignDeliveryValue] = []
    for current, prior_value in zip(summary, prior, strict=True):
        change = (
            None
            if current.value is None or prior_value is None
            else build_change(
                ChangeOperands(current.value.value, prior_value.value, current.value.units),
                semantics.for_metric(current.slot.value),
            )
        )
        entries.append(CampaignDeliveryValue(current.slot, prior_value, change))
    return tuple(entries)


def campaign_delivery_unavailable(detail: CampaignAttributionDetailArtifact) -> CampaignDelivery:
    """Return an explicit no-numeric delivery state until governed plans produce every slot."""
    if detail.prior_period is None:
        raise ValueError("campaign V2 delivery requires an equal prior period.")
    unavailable = tuple(CampaignDeliveryValue(slot, None) for slot in _DELIVERY_SLOTS)
    return CampaignDelivery(
        outcome="insufficient",
        summary=unavailable,
        comparison_period=detail.prior_period,
        comparison=unavailable,
        series_points=(),
        exact_rows=(),
        dsp_quality=CampaignDspQuality(
            outcome="unavailable",
            measurable_impressions=None,
            viewable_impressions=None,
            viewable_impressions_estimated=None,
            viewable_rate=None,
            reason_code="delivery_evidence_unavailable",
        ),
        evidence=campaign_delivery_missing_statuses(),
        recovery=CampaignRecoveryAction.CHANGE_PERIOD.value,
    )


def _campaign_evidence_status_payload(status: CampaignEvidenceStatus) -> dict[str, object]:
    return {
        "condition": status.condition.value,
        "days_expected": status.days_expected,
        "days_present": status.days_present,
        "explanation": status.explanation,
        "recovery_reading": status.recovery_reading,
        "role": status.role,
        "slot": status.slot,
    }


_EVIDENCE_STATUS_FIELDS = frozenset(
    {
        "condition",
        "days_expected",
        "days_present",
        "explanation",
        "recovery_reading",
        "role",
        "slot",
    }
)


def _campaign_evidence_status_from_payload(value: object) -> CampaignEvidenceStatus:
    item = _campaign_object(value, _EVIDENCE_STATUS_FIELDS, "campaign evidence status")
    try:
        condition = CampaignEvidenceStatusCondition(
            _campaign_text(item["condition"], "campaign evidence status condition")
        )
    except ValueError:
        raise ValueError("campaign evidence status condition is unsupported.") from None
    days_expected = item["days_expected"]
    days_present = item["days_present"]
    return CampaignEvidenceStatus(
        slot=_campaign_text(item["slot"], "campaign evidence status slot"),
        role=_campaign_text(item["role"], "campaign evidence status role"),
        condition=condition,
        explanation=_campaign_text(item["explanation"], "campaign evidence status explanation"),
        recovery_reading=_campaign_optional_text(
            item["recovery_reading"], "campaign evidence status recovery"
        ),
        days_present=None if days_present is None else _campaign_integer(days_present, "days"),
        days_expected=None if days_expected is None else _campaign_integer(days_expected, "days"),
    )


def _campaign_delivery_value_payload(value: CampaignDeliveryValue) -> dict[str, object]:
    return {"slot": value.slot.value, "value": _campaign_metric_value_payload(value.value)}


def _campaign_delivery_comparison_payload(value: CampaignDeliveryValue) -> dict[str, object]:
    return {**_campaign_delivery_value_payload(value), CHANGE_KEY: change_payload(value.change)}


def _campaign_delivery_row_payload(row: dict[str, object]) -> dict[str, object]:
    """Encode server-aligned current and prior daily values canonically."""
    return {
        "current": {
            slot.value: _campaign_number_payload(
                cast("dict[str, CampaignNumber]", row["current"])[slot.value]
            )
            for slot in _DELIVERY_SLOTS
        },
        "date": row["date"],
        "prior": {
            slot.value: _campaign_number_payload(
                cast("dict[str, CampaignNumber]", row["prior"])[slot.value]
            )
            for slot in _DELIVERY_SLOTS
        },
    }


def campaign_attribution_detail_v2_payload(
    artifact: CampaignAttributionDetailV2,
) -> dict[str, object]:
    """Serialize the V2 extension without changing any V1 field or meaning."""
    payload = campaign_attribution_detail_payload(artifact.detail)
    delivery = artifact.delivery
    return {
        **payload,
        "insights": insight_slots_payload(artifact.insights),
        "delivery": {
            "comparison": {
                "period": _campaign_period_payload(delivery.comparison_period),
                "slots": [
                    _campaign_delivery_comparison_payload(value) for value in delivery.comparison
                ],
            },
            "dsp_quality": {
                "measurable_impressions": _campaign_metric_value_payload(
                    delivery.dsp_quality.measurable_impressions
                ),
                "outcome": delivery.dsp_quality.outcome,
                "reason_code": delivery.dsp_quality.reason_code,
                "viewable_impressions": _campaign_metric_value_payload(
                    delivery.dsp_quality.viewable_impressions
                ),
                "viewable_impressions_estimated": _campaign_metric_value_payload(
                    delivery.dsp_quality.viewable_impressions_estimated
                ),
                "viewable_rate": _campaign_metric_value_payload(delivery.dsp_quality.viewable_rate),
            },
            "evidence": [_campaign_evidence_status_payload(value) for value in delivery.evidence],
            "narration_reference": None,
            "outcome": delivery.outcome,
            "recovery": delivery.recovery,
            "series": {
                "available_grains": list(delivery.available_grains),
                "exact_rows": [_campaign_delivery_row_payload(row) for row in delivery.exact_rows],
                "grain": delivery.available_grains[0],
                "metrics": [slot.value for slot in delivery.series_metrics],
                "points": [_campaign_delivery_row_payload(row) for row in delivery.series_points],
                "visual_id": "campaign_delivery_daily",
            },
            "summary": [_campaign_delivery_value_payload(value) for value in delivery.summary],
        },
        EVIDENCE_STATUS_KEY: evidence_status_payload(artifact.evidence_status),
    }


def _campaign_delivery_values(
    values: list[object],
    label: str,
    current: tuple[CampaignDeliveryValue, ...] | None = None,
) -> tuple[CampaignDeliveryValue, ...]:
    """Decode summary entries, or comparison entries with their change from `current`."""
    if len(values) != len(_DELIVERY_SLOTS):
        raise ValueError("campaign delivery slot count is invalid.")
    parsed: list[CampaignDeliveryValue] = []
    for index, (raw, slot) in enumerate(zip(values, _DELIVERY_SLOTS, strict=True)):
        # A comparison entry always carries its change key; the retained form is refused.
        fields = (
            frozenset({"slot", "value", CHANGE_KEY})
            if current is not None
            else frozenset({"slot", "value"})
        )
        entry = _campaign_object(raw, fields, label)
        try:
            parsed_slot = CampaignDeliverySlot(_campaign_text(entry["slot"], f"{label} slot"))
        except ValueError:
            raise ValueError("campaign delivery slot is unsupported.") from None
        if parsed_slot is not slot:
            raise ValueError("campaign delivery slots are not canonical.")
        value = _campaign_metric_value(entry["value"], label)
        change: Change | None = None
        now = None if current is None else current[index].value
        if now is not None and value is not None:
            change = change_from_payload(
                entry[CHANGE_KEY], ChangeOperands(now.value, value.value, now.units), label
            )
        elif entry.get(CHANGE_KEY) is not None:
            raise ValueError("a one-sided campaign delivery value carries no change.")
        parsed.append(CampaignDeliveryValue(parsed_slot, value, change))
    return tuple(parsed)


def campaign_attribution_detail_v2_from_payload(value: object) -> CampaignAttributionDetailV2:
    """Strictly decode and re-attest a closed CampaignAttributionDetail.v2 payload."""
    if not isinstance(value, dict):
        raise ValueError("campaign V2 detail must be an object.")
    item = cast("dict[str, object]", value)
    expected = _DETAIL_ARTIFACT_FIELDS | {"delivery", "insights", EVIDENCE_STATUS_KEY}
    if frozenset(item) != expected:
        raise ValueError("campaign V2 detail fields are not exact.")
    if item[EVIDENCE_STATUS_KEY] is None:
        raise ValueError("campaign V2 detail serves its coverage block.")
    display_value_mode(item)
    legacy = _campaign_detail_from_payload({key: item[key] for key in _DETAIL_ARTIFACT_FIELDS})
    raw_delivery = _campaign_object(
        item["delivery"],
        frozenset(
            {
                "comparison",
                "dsp_quality",
                "evidence",
                "narration_reference",
                "outcome",
                "recovery",
                "series",
                "summary",
            }
        ),
        "campaign delivery",
    )
    if raw_delivery["narration_reference"] is not None:
        raise ValueError("campaign delivery narration reference must be null.")
    raw_summary = _campaign_items(raw_delivery["summary"], "campaign delivery summary")
    raw_comparison = _campaign_object(
        raw_delivery["comparison"], frozenset({"period", "slots"}), "campaign delivery comparison"
    )
    raw_comparison_slots = _campaign_items(
        raw_comparison["slots"], "campaign delivery comparison slots"
    )

    raw_series = _campaign_object(
        raw_delivery["series"],
        frozenset({"available_grains", "exact_rows", "grain", "metrics", "points", "visual_id"}),
        "campaign delivery series",
    )
    raw_points = _campaign_items(raw_series["points"], "campaign delivery daily points")
    raw_exact_rows = _campaign_items(raw_series["exact_rows"], "campaign delivery daily rows")
    raw_grains = _campaign_items(raw_series["available_grains"], "campaign delivery display grains")
    raw_metrics = _campaign_items(raw_series["metrics"], "campaign delivery series metrics")
    if (
        raw_series["grain"] != "day"
        or raw_series["visual_id"] != "campaign_delivery_daily"
        or raw_grains != ["day"]
        or raw_metrics != [slot.value for slot in _DELIVERY_SLOTS]
        or raw_points != raw_exact_rows
    ):
        raise ValueError("campaign delivery series is invalid.")

    def delivery_rows(rows: list[object]) -> tuple[dict[str, object], ...]:
        parsed_rows: list[dict[str, object]] = []
        for raw in rows:
            row = _campaign_object(
                raw,
                frozenset({"current", "date", "prior"}),
                "campaign delivery daily row",
            )
            parsed: dict[str, dict[str, CampaignNumber]] = {}
            for series_name in ("current", "prior"):
                raw_values = _campaign_object(
                    row[series_name],
                    frozenset(slot.value for slot in _DELIVERY_SLOTS),
                    f"campaign delivery {series_name} daily values",
                )
                parsed[series_name] = {
                    slot.value: _campaign_number(raw_values[slot.value], slot.value)
                    for slot in _DELIVERY_SLOTS
                }
            parsed_rows.append(
                {
                    "current": parsed["current"],
                    "date": _campaign_text(row["date"], "campaign delivery daily date"),
                    "prior": parsed["prior"],
                }
            )
        return tuple(parsed_rows)

    quality = _campaign_object(
        raw_delivery["dsp_quality"],
        frozenset(
            {
                "measurable_impressions",
                "outcome",
                "reason_code",
                "viewable_impressions",
                "viewable_impressions_estimated",
                "viewable_rate",
            }
        ),
        "campaign DSP quality",
    )
    summary = _campaign_delivery_values(raw_summary, "campaign delivery summary")
    detail = CampaignAttributionDetailV2(
        detail=legacy,
        delivery=CampaignDelivery(
            outcome=_campaign_text(raw_delivery["outcome"], "campaign delivery outcome"),
            summary=summary,
            comparison_period=_campaign_period(
                raw_comparison["period"], "campaign delivery comparison period"
            ),
            comparison=_campaign_delivery_values(
                raw_comparison_slots, "campaign delivery comparison slots", summary
            ),
            series_points=delivery_rows(raw_points),
            exact_rows=delivery_rows(raw_exact_rows),
            dsp_quality=CampaignDspQuality(
                outcome=_campaign_text(quality["outcome"], "campaign DSP quality outcome"),
                measurable_impressions=_campaign_metric_value(
                    quality["measurable_impressions"], "measurable impressions"
                ),
                viewable_impressions=_campaign_metric_value(
                    quality["viewable_impressions"], "viewable impressions"
                ),
                viewable_impressions_estimated=_campaign_metric_value(
                    quality["viewable_impressions_estimated"],
                    "estimated viewable impressions",
                ),
                viewable_rate=_campaign_metric_value(quality["viewable_rate"], "viewable rate"),
                reason_code=_campaign_optional_text(
                    quality["reason_code"], "campaign DSP quality reason"
                ),
            ),
            evidence=tuple(
                _campaign_evidence_status_from_payload(item)
                for item in _campaign_items(raw_delivery["evidence"], "campaign delivery evidence")
            ),
            recovery=_campaign_text(raw_delivery["recovery"], "campaign delivery recovery"),
            narration_reference=None,
        ),
        insights=insight_slots_from_payload(
            item["insights"],
            expected=(
                InsightSlotId.TREND_SHAPE,
                InsightSlotId.EFFICIENCY_DRIFT,
                InsightSlotId.FUNNEL,
                InsightSlotId.NEXT_CHECK,
            ),
        ),
        evidence_status=evidence_status_from_payload(item[EVIDENCE_STATUS_KEY]),
    )
    canonical = campaign_attribution_detail_v2_payload(detail)
    if not canonical_matches(canonical, item):
        raise ValueError("campaign V2 detail payload is not canonical.")
    return detail
