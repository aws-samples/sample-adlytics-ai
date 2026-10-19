# SPDX-License-Identifier: MIT-0
# ruff: noqa: D101, D103, PLR0913
"""Strict, source-faithful Media Efficiency presentation artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, cast

MEDIA_EFFICIENCY_SCOPE_LIST_TYPE = "MediaEfficiencyScopeList"
MEDIA_EFFICIENCY_ARTIFACT_TYPE = "MediaEfficiencyArtifact"
MEDIA_EFFICIENCY_VERSION = 1
DELIVERY_SLOTS = ("ad_spend", "impressions", "clicks", "ctr", "cpc", "cpm")
VIEWABILITY_SLOTS = (
    "measurable_impressions",
    "viewable_impressions",
    "viewable_impressions_estimated",
    "viewable_rate",
)
AUDIENCE_SLOTS = ("reach_users", "avg_frequency", "cost_per_reach")


class MediaOutcome(StrEnum):
    COMPLETE = "complete"
    QUALIFIED = "qualified"
    INSUFFICIENT = "insufficient"


class AudienceAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class MediaScopeChoice:
    """Browser-safe selection capability; canonical scope remains in custody storage."""

    safe_scope_signature: str
    scope_reference: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.safe_scope_signature.strip() or not self.scope_reference.strip():
            raise ValueError("media scope choice needs a safe signature and opaque reference.")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("media scope expiry must be timezone-aware.")


@dataclass(frozen=True, slots=True)
class MediaWindowCapability:
    window_reference: str
    expires_at: datetime
    parent_revision: str

    def __post_init__(self) -> None:
        if not self.window_reference.strip() or not self.parent_revision.strip():
            raise ValueError("media window capability is invalid.")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("media window expiry must be timezone-aware.")


@dataclass(frozen=True, slots=True)
class MediaEfficiencyScopeList:
    artifact_revision: str
    temporal: object
    outcome: Literal["choices", "empty"]
    scopes: tuple[MediaScopeChoice, ...]
    window_capability: MediaWindowCapability | None
    reason_codes: tuple[str, ...]
    recovery: str
    artifact_type: Literal["MediaEfficiencyScopeList"] = MEDIA_EFFICIENCY_SCOPE_LIST_TYPE
    artifact_version: Literal[1] = MEDIA_EFFICIENCY_VERSION

    def __post_init__(self) -> None:
        if not self.artifact_revision.strip() or not self.recovery.strip():
            raise ValueError("media scope list identity and recovery are required.")
        signatures = tuple(choice.safe_scope_signature for choice in self.scopes)
        references = tuple(choice.scope_reference for choice in self.scopes)
        if len(set(signatures)) != len(signatures) or len(set(references)) != len(references):
            raise ValueError("media scope choices must be unique.")
        if self.outcome == "choices":
            if not self.scopes or self.window_capability is None:
                raise ValueError("media choices require scopes and exactly one window capability.")
            if self.window_capability.parent_revision != self.artifact_revision:
                raise ValueError("window capability must bind its exact parent revision.")
        elif self.scopes or self.window_capability is not None:
            raise ValueError("empty media scope list cannot carry capabilities.")


@dataclass(frozen=True, slots=True)
class MediaMetric:
    slot: str
    value: Decimal
    unit: str
    basis: str
    grain: str
    source: str

    def __post_init__(self) -> None:
        if self.slot not in (*DELIVERY_SLOTS, *VIEWABILITY_SLOTS):
            raise ValueError("media metric slot is not governed for V1.")
        if not all(value.strip() for value in (self.unit, self.basis, self.grain, self.source)):
            raise ValueError("media metric metadata is required.")


@dataclass(frozen=True, slots=True)
class MediaDatedRow:
    day: date
    values: tuple[MediaMetric, ...]

    def __post_init__(self) -> None:
        slots = tuple(item.slot for item in self.values)
        if slots not in (DELIVERY_SLOTS, VIEWABILITY_SLOTS):
            raise ValueError("media dated rows must have one canonical cohort order.")
        if slots == DELIVERY_SLOTS:
            _validate_delivery_ratios(self.values)
        else:
            _validate_viewability_ratio(self.values)


@dataclass(frozen=True, slots=True)
class MediaAudienceValue:
    slot: Literal["reach_users", "avg_frequency", "cost_per_reach"]
    availability: AudienceAvailability
    value: Decimal | None
    unit: str | None
    reason_code: str | None
    recovery: str

    def __post_init__(self) -> None:
        if not self.recovery.strip():
            raise ValueError("media audience recovery is required.")
        if self.availability is AudienceAvailability.AVAILABLE:
            if (
                self.value is None
                or self.unit is None
                or not self.unit.strip()
                or self.reason_code is not None
            ):
                raise ValueError("available audience values need a numeric value and unit only.")
        elif (
            self.value is not None
            or self.unit is not None
            or self.reason_code is None
            or not self.reason_code.strip()
        ):
            raise ValueError("unavailable audience values need a reason and no numeric value.")


@dataclass(frozen=True, slots=True)
class MediaVisual:
    visual_id: Literal["media_delivery_daily", "media_dsp_viewability_daily"]
    exact_rows: tuple[MediaDatedRow, ...]
    narration_reference: None = None


@dataclass(frozen=True, slots=True)
class MediaEfficiencyArtifact:
    artifact_revision: str
    parent_list_revision: str
    safe_scope_signature: str
    temporal: object
    expires_at: datetime
    outcome: MediaOutcome
    delivery_summary: tuple[MediaMetric, ...]
    delivery_series: tuple[MediaDatedRow, ...]
    viewability_summary: tuple[MediaMetric, ...]
    viewability_series: tuple[MediaDatedRow, ...]
    audience: tuple[MediaAudienceValue, ...]
    reason_codes: tuple[str, ...]
    recovery: str
    boundaries: tuple[str, ...]
    visuals: tuple[MediaVisual, ...]
    artifact_type: Literal["MediaEfficiencyArtifact"] = MEDIA_EFFICIENCY_ARTIFACT_TYPE
    artifact_version: Literal[1] = MEDIA_EFFICIENCY_VERSION

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.artifact_revision,
                self.parent_list_revision,
                self.safe_scope_signature,
                self.recovery,
            )
        ):
            raise ValueError("media artifact identity is invalid.")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("media artifact expiry must be timezone-aware.")
        if tuple(item.slot for item in self.audience) != AUDIENCE_SLOTS:
            raise ValueError("media audience values must retain canonical slot order.")
        if self.outcome is MediaOutcome.INSUFFICIENT:
            if any(
                (
                    self.delivery_summary,
                    self.delivery_series,
                    self.viewability_summary,
                    self.viewability_series,
                    self.visuals,
                )
            ):
                raise ValueError(
                    "insufficient media artifact cannot fabricate evidence rows or summaries."
                )
            if not self.reason_codes:
                raise ValueError("insufficient media artifact needs an evidence reason.")
            return
        _ordered(self.delivery_summary, DELIVERY_SLOTS, "delivery summary")
        _ordered(self.viewability_summary, VIEWABILITY_SLOTS, "viewability summary")
        if not self.delivery_series or not self.viewability_series:
            raise ValueError("complete or qualified media artifact needs exact daily evidence.")
        if tuple(item.visual_id for item in self.visuals) != (
            "media_delivery_daily",
            "media_dsp_viewability_daily",
        ):
            raise ValueError("media visual order is fixed.")
        if (
            self.visuals[0].exact_rows != self.delivery_series
            or self.visuals[1].exact_rows != self.viewability_series
        ):
            raise ValueError("media visuals must retain their exact evidence rows.")
        if any(visual.narration_reference is not None for visual in self.visuals):
            raise ValueError("Media V1 narration references are unavailable.")
        _reconcile(
            self.delivery_summary, self.delivery_series, ("ad_spend", "impressions", "clicks")
        )
        _reconcile(
            self.viewability_summary,
            self.viewability_series,
            ("measurable_impressions", "viewable_impressions", "viewable_impressions_estimated"),
        )


def _ordered(values: tuple[MediaMetric, ...], slots: tuple[str, ...], label: str) -> None:
    if tuple(item.slot for item in values) != slots:
        raise ValueError(f"{label} must retain canonical slot order.")


def _reconcile(
    summary: tuple[MediaMetric, ...], series: tuple[MediaDatedRow, ...], additive: tuple[str, ...]
) -> None:
    expected = {item.slot: item for item in summary}
    for slot in additive:
        total = sum(
            (next(item.value for item in row.values if item.slot == slot) for row in series),
            Decimal(),
        )
        if total != expected[slot].value:
            raise ValueError("media series does not reconcile to summary.")


def _validate_delivery_ratios(values: tuple[MediaMetric, ...]) -> None:
    by_slot = {item.slot: item for item in values}
    impressions, clicks = by_slot["impressions"], by_slot["clicks"]
    if impressions.value <= 0 or clicks.value <= 0:
        raise ValueError("media daily ratios require positive governed denominators.")
    if by_slot["ctr"].value != clicks.value / impressions.value:
        raise ValueError("media daily CTR must be server-derived from exact operands.")
    if by_slot["cpc"].value != by_slot["ad_spend"].value / clicks.value:
        raise ValueError("media daily CPC must be server-derived from exact operands.")
    if by_slot["cpm"].value != Decimal(1000) * by_slot["ad_spend"].value / impressions.value:
        raise ValueError("media daily CPM must be server-derived from exact operands.")


def _validate_viewability_ratio(values: tuple[MediaMetric, ...]) -> None:
    by_slot = {item.slot: item for item in values}
    if by_slot["measurable_impressions"].value <= 0:
        raise ValueError("media daily viewability requires a positive measurable denominator.")
    if (
        by_slot["viewable_rate"].value
        != by_slot["viewable_impressions"].value / by_slot["measurable_impressions"].value
    ):
        raise ValueError("media daily viewable rate must be server-derived from exact operands.")


def media_delivery_row(
    day: date, operands: tuple[MediaMetric, MediaMetric, MediaMetric]
) -> MediaDatedRow:
    spend, impressions, clicks = operands
    return MediaDatedRow(
        day,
        (
            *operands,
            MediaMetric(
                "ctr", clicks.value / impressions.value, "ratio", clicks.basis, "day", clicks.source
            ),
            MediaMetric(
                "cpc", spend.value / clicks.value, spend.unit, spend.basis, "day", spend.source
            ),
            MediaMetric(
                "cpm",
                Decimal(1000) * spend.value / impressions.value,
                spend.unit,
                spend.basis,
                "day",
                spend.source,
            ),
        ),
    )


def media_viewability_row(
    day: date, operands: tuple[MediaMetric, MediaMetric, MediaMetric]
) -> MediaDatedRow:
    measurable, observed, _estimated = operands
    return MediaDatedRow(
        day,
        (
            *operands,
            MediaMetric(
                "viewable_rate",
                observed.value / measurable.value,
                "ratio",
                observed.basis,
                "day",
                observed.source,
            ),
        ),
    )


def _unavailable_audience() -> tuple[MediaAudienceValue, ...]:
    return tuple(
        MediaAudienceValue(
            slot,
            AudienceAvailability.UNAVAILABLE,
            None,
            None,
            "exact_source_audience_metric_unavailable",
            "contact_workspace_data_owner",
        )
        for slot in AUDIENCE_SLOTS
    )


def insufficient_media_efficiency(
    *,
    artifact_revision: str,
    parent_list_revision: str,
    safe_scope_signature: str,
    temporal: object,
    expires_at: datetime,
    reason_codes: tuple[str, ...],
    recovery: str = "change_period",
    boundaries: tuple[str, ...] = (),
) -> MediaEfficiencyArtifact:
    return MediaEfficiencyArtifact(
        artifact_revision,
        parent_list_revision,
        safe_scope_signature,
        temporal,
        expires_at,
        MediaOutcome.INSUFFICIENT,
        (),
        (),
        (),
        (),
        _unavailable_audience(),
        reason_codes,
        recovery,
        boundaries,
        (),
    )


def reduce_media_efficiency(
    *,
    artifact_revision: str,
    parent_list_revision: str,
    safe_scope_signature: str,
    temporal: object,
    expires_at: datetime,
    delivery_operands: tuple[MediaMetric, MediaMetric, MediaMetric],
    delivery_series: tuple[MediaDatedRow, ...],
    viewability_operands: tuple[MediaMetric, MediaMetric, MediaMetric],
    viewability_series: tuple[MediaDatedRow, ...],
    outcome: MediaOutcome = MediaOutcome.COMPLETE,
    reason_codes: tuple[str, ...] = (),
    recovery: str = "change_period",
    boundaries: tuple[str, ...] = (),
) -> MediaEfficiencyArtifact:
    """Build V1 from governed operands; all summary and row ratios are server-derived."""
    spend, impressions, clicks = delivery_operands
    measurable, observed, _estimated = viewability_operands
    if outcome is MediaOutcome.INSUFFICIENT:
        raise ValueError("use insufficient_media_efficiency for absent governed evidence.")
    if tuple(item.slot for item in delivery_operands) != ("ad_spend", "impressions", "clicks"):
        raise ValueError("delivery operands are invalid.")
    if tuple(item.slot for item in viewability_operands) != (
        "measurable_impressions",
        "viewable_impressions",
        "viewable_impressions_estimated",
    ):
        raise ValueError("viewability operands are invalid.")
    if impressions.value <= 0 or clicks.value <= 0 or measurable.value <= 0:
        raise ValueError("server ratios require positive governed denominators.")
    delivery = (
        *delivery_operands,
        MediaMetric(
            "ctr",
            clicks.value / impressions.value,
            "ratio",
            clicks.basis,
            clicks.grain,
            clicks.source,
        ),
        MediaMetric(
            "cpc", spend.value / clicks.value, spend.unit, spend.basis, spend.grain, spend.source
        ),
        MediaMetric(
            "cpm",
            Decimal(1000) * spend.value / impressions.value,
            spend.unit,
            spend.basis,
            spend.grain,
            spend.source,
        ),
    )
    viewability = (
        *viewability_operands,
        MediaMetric(
            "viewable_rate",
            observed.value / measurable.value,
            "ratio",
            observed.basis,
            observed.grain,
            observed.source,
        ),
    )
    return MediaEfficiencyArtifact(
        artifact_revision,
        parent_list_revision,
        safe_scope_signature,
        temporal,
        expires_at,
        outcome,
        delivery,
        delivery_series,
        viewability,
        viewability_series,
        _unavailable_audience(),
        reason_codes,
        recovery,
        boundaries,
        (
            MediaVisual("media_delivery_daily", delivery_series),
            MediaVisual("media_dsp_viewability_daily", viewability_series),
        ),
    )


def media_efficiency_payload(
    value: MediaEfficiencyScopeList | MediaEfficiencyArtifact,
) -> dict[str, object]:
    """Return canonical browser/storage JSON without canonical selectors or duplicate custody."""
    payload = cast("dict[str, object]", asdict(value))
    payload.pop("temporal")
    if isinstance(value, MediaEfficiencyArtifact):
        payload.pop("expires_at")
    return cast("dict[str, object]", _json_value(payload))


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        items = cast("dict[object, object]", value)
        return {str(key): _json_value(item) for key, item in items.items()}
    if isinstance(value, tuple | list):
        items = cast("tuple[object, ...] | list[object]", value)
        return [_json_value(item) for item in items]
    if isinstance(value, StrEnum):
        return value.value
    return value
