# SPDX-License-Identifier: MIT-0
"""The server half of temporal transport version 2 (ADR-100 point 8; design §4.7).

Version 2 adds three calendar presets, a `vintage` selection, a server-built period label and a
`no_complete_period_measured` result. This module is the server's decoder of a version-2
selection and its encoder of the version-2 additions to a temporal control, both pure.

**On the wire for pages that use the version-2 grammar** (UI slice 9a). A page whose entry
declares a calendar preset decodes its selection with `decode_selection_v2` and serves the
control `baked_preset_control_v2` builds; a page that uses only the version-1 grammar keeps
its version-1 selection and control unchanged, so Campaigns and Retail health read as before.
Vintage selections and `prior_calendar_period` comparisons are decoded here but no page serves
them yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING

from domain.presentation.page_schemas import (
    PageTime,
    PresentationPageSchema,
    PresentationTemporalPolicy,
    TemporalComparisonPolicy,
    TemporalPresetId,
    calendar_preset_grain,
    is_calendar_preset,
)
from domain.presentation.temporal import (
    ComparisonUnavailableReason,
    TemporalAvailability,
    TemporalComparison,
    TemporalComparisonStatus,
    TemporalControl,
    TemporalCustomControl,
    TemporalDisplayGrain,
    TemporalMode,
    TemporalPreset,
    TemporalReportingWindowStatus,
    TemporalResolved,
    TemporalResolvedSelection,
    TemporalResolvedSelectionKind,
    TemporalSelection,
    TemporalSelectionKind,
    TemporalUnavailableReason,
    temporal_preset_label,
    temporal_preset_label_v2,
)
from domain.semantics.calendar_periods import CalendarPeriod
from domain.shared_kernel.values import DateRange

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Mapping

    from domain.presentation.calendar_presets import ResolveCalendarPresetResult

__all__ = [
    "MAX_VINTAGES",
    "PRESET_NOT_MEASURED_TEXT",
    "PRESET_NOT_PUBLISHED_TEXT",
    "BakedControlRequest",
    "CalendarResolution",
    "SelectionRefusalReason",
    "ServedPeriod",
    "ServedPresetWindow",
    "TemporalSelectionRefusalError",
    "VintageOption",
    "baked_preset_control_v2",
    "calendar_resolution_fields",
    "decode_selection_v2",
    "no_complete_period_result",
    "preset_entries_v2",
    "vintage_entries_v2",
]

MAX_VINTAGES = 13
_TRANSPORT_VERSION = 2


class SelectionRefusalReason(StrEnum):
    """Why the server refuses a version-2 selection before resolving it."""

    INVALID = "invalid_selection"
    UNDECLARED_CALENDAR_PRESET = "undeclared_calendar_preset"
    VINTAGE_ON_PRESET_PAGE = "vintage_on_preset_page"
    PRESET_ON_VINTAGE_PAGE = "preset_on_vintage_page"
    VINTAGE_NOT_OFFERED = "vintage_not_offered"


class TemporalSelectionRefusalError(ValueError):
    """A version-2 selection the page doesn't allow; `reason` is the closed signal."""

    def __init__(self, reason: SelectionRefusalReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


def _refuse(reason: SelectionRefusalReason) -> TemporalSelectionRefusalError:
    return TemporalSelectionRefusalError(reason)


def decode_selection_v2(
    raw: Mapping[str, object] | None,
    page: PresentationPageSchema,
    offered_vintages: tuple[str, ...] | None = (),
) -> TemporalSelection:
    """Decode one `TemporalSelection.v2` for `page`, refusing what the page doesn't declare.

    A calendar `preset_id` the page doesn't declare is refused, and so is a `vintage` on a
    `temporal_policy` page, a preset or Custom on a `vintage_policy` page, and a vintage the
    server didn't offer. Unknown fields are refused, as in version 1.
    """
    if raw is None:
        return TemporalSelection(kind=TemporalSelectionKind.OMITTED, version=_TRANSPORT_VERSION)
    kind = raw.get("kind")
    vintage_page = page.vintage_policy is not None
    if kind == TemporalSelectionKind.VINTAGE.value and set(raw) == {"kind", "as_of_month"}:
        if not vintage_page:
            raise _refuse(SelectionRefusalReason.VINTAGE_ON_PRESET_PAGE)
        month = raw["as_of_month"]
        if offered_vintages is not None and month not in offered_vintages:
            raise _refuse(SelectionRefusalReason.VINTAGE_NOT_OFFERED)
        return _selection(kind=TemporalSelectionKind.VINTAGE, as_of_month=str(month))
    if kind in {TemporalSelectionKind.PRESET.value, TemporalSelectionKind.CUSTOM.value}:
        if vintage_page:
            raise _refuse(SelectionRefusalReason.PRESET_ON_VINTAGE_PAGE)
        if kind == TemporalSelectionKind.PRESET.value and set(raw) == {"kind", "preset_id"}:
            preset = _preset(raw["preset_id"])
            if is_calendar_preset(preset) and preset not in page.temporal_policy.calendar_presets:
                raise _refuse(SelectionRefusalReason.UNDECLARED_CALENDAR_PRESET)
            return _selection(kind=TemporalSelectionKind.PRESET, preset_id=preset)
        if set(raw) == {"kind", "start", "end"}:
            return _selection(
                kind=TemporalSelectionKind.CUSTOM,
                start=_day(raw["start"]),
                end=_day(raw["end"]),
            )
    raise _refuse(SelectionRefusalReason.INVALID)


def _selection(**fields: object) -> TemporalSelection:
    try:
        return TemporalSelection(version=_TRANSPORT_VERSION, **fields)  # type: ignore[arg-type]
    except ValueError:
        raise _refuse(SelectionRefusalReason.INVALID) from None


def _preset(value: object) -> TemporalPresetId:
    try:
        return TemporalPresetId(str(value))
    except ValueError:
        raise _refuse(SelectionRefusalReason.INVALID) from None


def _day(value: object) -> date:
    if not isinstance(value, str):
        raise _refuse(SelectionRefusalReason.INVALID)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _refuse(SelectionRefusalReason.INVALID) from None


def preset_entries_v2(policy: PresentationTemporalPolicy) -> list[dict[str, object]]:
    """Build the version-2 preset list: canonical order, server labels, candidates only."""
    return [
        {"id": preset.value, "label": temporal_preset_label(preset)}
        for preset in TemporalPresetId
        if preset in policy.candidate_presets
    ]


@dataclass(frozen=True, slots=True)
class CalendarResolution:
    """A resolved calendar preset on one page, and whether its prior period is published."""

    preset: TemporalPresetId
    policy: PresentationTemporalPolicy
    period: CalendarPeriod
    prior_published: bool = False


def calendar_resolution_fields(resolution: CalendarResolution) -> dict[str, object]:
    """Encode the version-2 `resolved` and `comparison` fields for a resolved calendar preset.

    The browser never formats the period: `period_label` is built here. Under
    `optional_equal_prior` a calendar preset shows no comparison (`calendar_period_selected`);
    under `prior_calendar_period` the comparison is the previous period of the same grain.
    """
    preset, policy, period = resolution.preset, resolution.policy, resolution.period
    if calendar_preset_grain(preset) is not period.grain:
        raise ValueError("the resolved period's grain must match the preset.")
    if policy.comparison_policy is TemporalComparisonPolicy.PRIOR_CALENDAR_PERIOD:
        comparison: dict[str, object] = {
            "policy": policy.comparison_policy.value,
            "status": "available" if resolution.prior_published else "unavailable",
            "prior_period_label": period.previous().label,
        }
    else:
        comparison = {
            "policy": policy.comparison_policy.value,
            "status": "not_applicable",
            "reason_code": "calendar_period_selected",
        }
    return {
        "resolved": {
            "window": {"start": period.start.isoformat(), "end": period.end.isoformat()},
            "selection": {"kind": "preset", "preset_id": preset.value},
            "display_grain": period.grain.value,
            "period_label": period.label,
            "as_of_month": None,
        },
        "comparison": comparison,
    }


def no_complete_period_result(result: ResolveCalendarPresetResult) -> dict[str, object]:
    """Encode result `none` as `window_unavailable` with its reason and `expected_after`."""
    if result.period is not None:
        raise ValueError("a resolved period isn't a window-unavailable result.")
    return {
        "kind": "window_unavailable",
        "reason_code": "no_complete_period_measured",
        "expected_after": result.expected_after.isoformat(),
        "message": result.none_text,
    }


PRESET_NOT_PUBLISHED_TEXT = "Not available yet"
"""An unavailable preset's text when its bake hasn't published (inside the button, ia §8.1)."""
PRESET_NOT_MEASURED_TEXT = "Not measured yet"
"""A calendar preset's text when no complete period of its grain has landed yet."""


@dataclass(frozen=True, slots=True)
class ServedPresetWindow:
    """What one declared preset serves now, read from its `CURRENT` pointer (ADR-116 d4).

    `window` is None for a pointer written before pointers carried windows: the preset is
    available and its button names no dates.
    """

    preset: TemporalPresetId
    window: DateRange | None
    period_label: str | None = None


@dataclass(frozen=True, slots=True)
class ServedPeriod:
    """The period the selected preset resolves to, its equal prior, and a calendar label.

    `available` is False when the preset has no baked body to serve: `unavailable_reason`
    then names why, and the window is the one the preset resolves to on the sources now.
    """

    preset: TemporalPresetId
    window: DateRange
    prior_window: DateRange | None
    period_label: str | None
    available: bool = True
    unavailable_reason: TemporalUnavailableReason | None = None

    def __post_init__(self) -> None:
        if self.available != (self.unavailable_reason is None):
            raise ValueError("only an unavailable served period names its reason.")
        if is_calendar_preset(self.preset) != (self.period_label is not None):
            raise ValueError("a calendar period, and only it, carries its label.")
        if is_calendar_preset(self.preset) and self.prior_window is not None:
            raise ValueError("a calendar preset on this page has no prior window.")


@dataclass(frozen=True, slots=True)
class BakedControlRequest:
    """Inputs of one version-2 control for a page served from preset artifacts."""

    policy: PresentationTemporalPolicy
    selection: TemporalSelection
    served: ServedPeriod
    pointers: tuple[ServedPresetWindow, ...]


def _calendar_grain(preset: TemporalPresetId) -> TemporalDisplayGrain:
    return TemporalDisplayGrain(calendar_preset_grain(preset).value)


def _rolling_grain(policy: PresentationTemporalPolicy) -> TemporalDisplayGrain:
    primary = policy.display_grains[0]
    if primary is PageTime.DAY or primary is PageTime.EXACT_SOURCE:
        return TemporalDisplayGrain.DAY
    return TemporalDisplayGrain.PERIOD


def _comparison_v2(request: BakedControlRequest) -> TemporalComparison:
    policy = request.policy.comparison_policy
    served = request.served
    if is_calendar_preset(served.preset):
        if policy is TemporalComparisonPolicy.PRIOR_CALENDAR_PERIOD:
            period = CalendarPeriod(
                served.window.start,
                served.window.end,
                calendar_preset_grain(served.preset),
            )
            return TemporalComparison(
                policy=policy,
                status=(
                    TemporalComparisonStatus.AVAILABLE
                    if served.prior_window is not None
                    else TemporalComparisonStatus.UNAVAILABLE
                ),
                prior_period_label=period.previous().label,
            )
        if policy is not TemporalComparisonPolicy.OPTIONAL_EQUAL_PRIOR:
            raise ValueError("a calendar preset needs a calendar or mixed-page comparison policy.")
        return TemporalComparison(
            policy=policy,
            status=TemporalComparisonStatus.NOT_APPLICABLE,
            reason_code=ComparisonUnavailableReason.CALENDAR_PERIOD_SELECTED,
        )
    if policy is TemporalComparisonPolicy.NONE:
        return TemporalComparison(policy=policy, status=TemporalComparisonStatus.NOT_APPLICABLE)
    if served.prior_window is not None:
        return TemporalComparison(policy=policy, status=TemporalComparisonStatus.AVAILABLE)
    return TemporalComparison(
        policy=policy,
        status=TemporalComparisonStatus.UNAVAILABLE,
        reason_code=ComparisonUnavailableReason.NO_VALID_EQUAL_PRIOR,
    )


def _preset_text(reason: TemporalUnavailableReason) -> str:
    return (
        PRESET_NOT_MEASURED_TEXT
        if reason is TemporalUnavailableReason.NO_COMPLETE_PERIOD_MEASURED
        else PRESET_NOT_PUBLISHED_TEXT
    )


def baked_preset_control_v2(request: BakedControlRequest) -> TemporalControl:
    """Build the version-2 control of a page whose every preset is a baked artifact.

    Each declared candidate is listed in canonical order with the window its pointer serves
    now; a candidate with no pointer is unavailable with its server text. The selected preset
    shows the served period itself, so its button and the scope band always agree.
    """
    policy, served = request.policy, request.served
    if served.preset not in policy.candidate_presets:
        raise ValueError("the served preset is one the page declares.")
    by_preset = {item.preset: item for item in request.pointers}
    presets: list[TemporalPreset] = []
    for preset in TemporalPresetId:
        if preset not in policy.candidate_presets:
            continue
        if preset is served.preset:
            reason = served.unavailable_reason
            window: DateRange | None = served.window
            label = served.period_label
        else:
            pointer = by_preset.get(preset)
            reason = None if pointer is not None else TemporalUnavailableReason.PRESET_UNAVAILABLE
            window = None if pointer is None else pointer.window
            label = None if pointer is None or window is None else pointer.period_label
        presets.append(
            TemporalPreset(
                preset_id=preset,
                label=temporal_preset_label_v2(preset),
                availability=(
                    TemporalAvailability.AVAILABLE
                    if reason is None
                    else TemporalAvailability.UNAVAILABLE
                ),
                reason_code=reason,
                window=window,
                period_label=label,
                reason_text=None if reason is None else _preset_text(reason),
            )
        )
    omitted = request.selection.kind is TemporalSelectionKind.OMITTED
    if not omitted and request.selection.preset_id is not served.preset:
        raise ValueError("the served period answers the selected preset.")
    return TemporalControl(
        version=_TRANSPORT_VERSION,
        mode=TemporalMode.SELECTABLE,
        resolved=TemporalResolved(
            window=served.window,
            reporting_window_status=(
                TemporalReportingWindowStatus.PREFERRED_DEFAULT
                if omitted
                else TemporalReportingWindowStatus.USER_SELECTED
            ),
            selection=TemporalResolvedSelection(
                kind=(
                    TemporalResolvedSelectionKind.DEFAULT
                    if omitted
                    else TemporalResolvedSelectionKind.PRESET
                ),
                preset_id=None if omitted else served.preset,
            ),
            display_grain=(
                _calendar_grain(served.preset)
                if is_calendar_preset(served.preset)
                else _rolling_grain(policy)
            ),
            period_label=served.period_label,
        ),
        comparison=_comparison_v2(request),
        presets=tuple(presets),
        custom=TemporalCustomControl(
            availability=TemporalAvailability.UNAVAILABLE,
            reason_code=TemporalUnavailableReason.CUSTOM_DISABLED,
        ),
    )


@dataclass(frozen=True, slots=True)
class VintageOption:
    """One `as_of_month` the server offers, with its label and availability."""

    as_of_month: str
    label: str
    available: bool


def vintage_entries_v2(options: tuple[VintageOption, ...]) -> list[dict[str, object]]:
    """Newest first, at most 13: the only vintages the browser may select."""
    newest_first = sorted(options, key=lambda option: option.as_of_month, reverse=True)
    return [
        {
            "as_of_month": option.as_of_month,
            "label": option.label,
            "availability": "available" if option.available else "unavailable",
        }
        for option in newest_first[:MAX_VINTAGES]
    ]
