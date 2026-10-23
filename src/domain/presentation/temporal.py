# SPDX-License-Identifier: MIT-0
"""Immutable V2 temporal presentation controls and result envelopes.

The browser may request only a declared preset or an inclusive Custom date range. This
module models the server-owned resolution and its closed availability, comparison, and
recovery states; it does not resolve source coverage or derive dates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import Literal

from domain.catalog.freshness import common_intervals
from domain.presentation.page_schemas import (
    ROLLING_PRESET_IDS,
    PageTime,
    PresentationTemporalPolicy,
    TemporalComparisonPolicy,
    TemporalPresetId,
    is_calendar_preset,
)
from domain.shared_kernel.values import DateRange

__all__ = [
    "VINTAGE_LABEL_TEXT",
    "ComparisonUnavailableReason",
    "PresentationPageResult",
    "PresentationResult",
    "PresentationWindowUnavailableResult",
    "TemporalAvailability",
    "TemporalComparison",
    "TemporalComparisonPolicy",
    "TemporalComparisonStatus",
    "TemporalControl",
    "TemporalCustomBounds",
    "TemporalCustomControl",
    "TemporalDisplayGrain",
    "TemporalMode",
    "TemporalNoSelfServiceReason",
    "TemporalPreset",
    "TemporalPresetId",
    "TemporalRecovery",
    "TemporalReportingWindowStatus",
    "TemporalResolved",
    "TemporalResolvedSelection",
    "TemporalResolvedSelectionKind",
    "TemporalSelection",
    "TemporalSelectionKind",
    "TemporalUnavailableReason",
    "VintageControl",
    "VintageResolved",
    "preset_days",
    "temporal_preset_label_v2",
]

_TEMPORAL_CONTROL_VERSION = 1
_V2_TEMPORAL_CONTROL_VERSION = 2
_PRESENTATION_RESULT_VERSION = 2
_MAX_CUSTOM_DURATION_DAYS = 90
_CANONICAL_PRESET_IDS = ROLLING_PRESET_IDS
"""The version-1 control's complete preset order: the rolling presets only (ADR-100)."""
_V2_CANONICAL_PRESET_IDS = tuple(TemporalPresetId)
"""Version 2: the rolling presets in today's order, then week, month, quarter."""
_PRESET_LABELS = {
    TemporalPresetId.ONE_DAY: "1D",
    TemporalPresetId.SEVEN_DAYS: "7D",
    TemporalPresetId.FOURTEEN_DAYS: "14D",
    TemporalPresetId.THIRTY_DAYS: "30D",
    TemporalPresetId.LAST_COMPLETE_WEEK: "Latest measured week",
    TemporalPresetId.LAST_COMPLETE_MONTH: "Latest measured month",
    TemporalPresetId.LAST_COMPLETE_QUARTER: "Latest measured quarter",
}
"""Server display labels. A calendar preset reads "Latest measured …", never "Last complete …",
because it resolves to the newest period that has landed (ADR-100 point 4)."""


_V2_PRESET_LABELS = {
    **_PRESET_LABELS,
    TemporalPresetId.ONE_DAY: "1 day",
    TemporalPresetId.SEVEN_DAYS: "7 days",
    TemporalPresetId.FOURTEEN_DAYS: "14 days",
    TemporalPresetId.THIRTY_DAYS: "30 days",
}
"""Version-2 control labels: rolling presets read in words beside the calendar presets (ia §8.1)."""


def temporal_preset_label(preset_id: TemporalPresetId) -> str:
    """Return the canonical customer-visible label for one declared preset."""
    return _PRESET_LABELS[preset_id]


def temporal_preset_label_v2(preset_id: TemporalPresetId) -> str:
    """Return the label a version-2 control shows for one declared preset."""
    return _V2_PRESET_LABELS[preset_id]


class TemporalMode(StrEnum):
    """Whether a returned reporting window can be changed by the browser."""

    FIXED = "fixed"
    SELECTABLE = "selectable"


class TemporalAvailability(StrEnum):
    """Closed runtime availability for one declared temporal choice."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class TemporalSelectionKind(StrEnum):
    """The three request forms accepted for a parent or list operation."""

    OMITTED = "omitted"
    PRESET = "preset"
    CUSTOM = "custom"
    VINTAGE = "vintage"
    """Version 2 only: one `as_of_month` on a `vintage_policy` page (ADR-100 point 8)."""


class TemporalResolvedSelectionKind(StrEnum):
    """The selection provenance returned after server-side resolution."""

    DEFAULT = "default"
    PRESET = "preset"
    CUSTOM = "custom"


class TemporalReportingWindowStatus(StrEnum):
    """How the resolved reporting window was selected."""

    PREFERRED_DEFAULT = "preferred_default"
    PARTIAL_DEFAULT = "partial_default"
    USER_SELECTED = "user_selected"


class TemporalDisplayGrain(StrEnum):
    """The declared display grain carried by an artifact and its control."""

    DAY = "day"
    WEEK = "week"
    PERIOD = "period"
    MONTH = "month"
    """Version 2 only: a resolved calendar month (ADR-100 point 8)."""
    QUARTER = "quarter"
    """Version 2 only: a resolved calendar quarter (ADR-100 point 8)."""


_V2_DISPLAY_GRAINS = frozenset({TemporalDisplayGrain.MONTH, TemporalDisplayGrain.QUARTER})
_CALENDAR_DISPLAY_GRAINS = frozenset(
    {TemporalDisplayGrain.WEEK, TemporalDisplayGrain.MONTH, TemporalDisplayGrain.QUARTER}
)


class TemporalComparisonStatus(StrEnum):
    """Server-derived comparison availability for the page policy."""

    AVAILABLE = "available"
    NOT_REQUESTED = "not_requested"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"


class TemporalUnavailableReason(StrEnum):
    """Closed reasons why a requested selection cannot produce a page artifact."""

    INVALID_CUSTOM_INPUT = "invalid_custom_input"
    CUSTOM_DISABLED = "custom_disabled"
    CUSTOM_OUT_OF_BOUNDS = "custom_out_of_bounds"
    PRESET_UNAVAILABLE = "preset_unavailable"
    VINTAGE_UNAVAILABLE = "vintage_unavailable"
    NO_COMMON_INTERVAL = "no_common_interval"
    INCOMPATIBLE_SOURCE_COVERAGE = "incompatible_source_coverage"
    REQUIRED_COMPARISON_UNAVAILABLE = "required_comparison_unavailable"
    SINGLE_PERIOD_NON_TREND_DETAIL_UNAVAILABLE = "single_period_non_trend_detail_unavailable"
    STALE_TEMPORAL_DECLARATION = "stale_temporal_declaration"
    NO_COMPLETE_PERIOD_MEASURED = "no_complete_period_measured"
    """Version 2 only: no complete calendar period has landed yet (ADR-100 point 5)."""


class ComparisonUnavailableReason(StrEnum):
    """Closed reasons why a page policy cannot provide an equal prior period."""

    NO_VALID_EQUAL_PRIOR = "no_valid_equal_prior"
    INCOMPATIBLE_SOURCE_COVERAGE = "incompatible_source_coverage"
    STALE_TEMPORAL_DECLARATION = "stale_temporal_declaration"
    CALENDAR_PERIOD_SELECTED = "calendar_period_selected"
    """Version 2 only: a calendar preset on a mixed page shows no comparison (ADR-100 point 8).
    It is the one reason a `not_applicable` comparison carries."""


class TemporalNoSelfServiceReason(StrEnum):
    """Closed recovery dispositions that deliberately expose no browser action."""

    NO_SUPPORTED_WINDOW = "no_supported_window"
    WAIT_FOR_SOURCE_COVERAGE = "wait_for_source_coverage"


def _require_enum(value: object, enum_type: type[StrEnum], field_name: str) -> None:
    if type(value) is not enum_type:
        raise ValueError(f"{field_name} must be a {enum_type.__name__}.")


def _require_date(value: object, field_name: str) -> None:
    if type(value) is not date:
        raise ValueError(f"{field_name} must be a date without a time or timezone.")


def _require_range(value: object, field_name: str) -> None:
    if type(value) is not DateRange:
        raise ValueError(f"{field_name} must be a DateRange.")
    _require_date(value.start, f"{field_name}.start")
    _require_date(value.end, f"{field_name}.end")


def _is_month(value: object) -> bool:
    """Whether a value is a `YYYY-MM` month."""
    if type(value) is not str or len(value) != 7 or value[4] != "-":  # noqa: PLR2004
        return False
    year, month = value[:4], value[5:]
    return year.isdigit() and month.isdigit() and 1 <= int(month) <= 12  # noqa: PLR2004


def _require_nonblank(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a nonblank string.")


@dataclass(frozen=True, slots=True, kw_only=True)
class TemporalSelection:
    """The exact V1 parent/list temporal intent; child requests carry no such intent."""

    kind: TemporalSelectionKind
    preset_id: TemporalPresetId | None = None
    start: date | None = None
    end: date | None = None
    as_of_month: str | None = None
    version: Literal[1, 2] = 1
    """Version 1 accepts only rolling presets or Custom; version 2 adds calendar presets and a
    `vintage` selection (ADR-100 point 8). The browser speaks version 1 until its v2 decoder
    lands, so every wire decode builds version 1."""

    def __post_init__(self) -> None:
        _require_enum(self.kind, TemporalSelectionKind, "temporal selection kind")
        if self._is_vintage():
            return
        if self.kind is TemporalSelectionKind.OMITTED:
            if self.preset_id is not None or self.start is not None or self.end is not None:
                raise ValueError("omitted temporal selection cannot carry preset or Custom fields.")
            return
        if self.kind is TemporalSelectionKind.PRESET:
            _require_enum(self.preset_id, TemporalPresetId, "temporal preset id")
            if self.start is not None or self.end is not None:
                raise ValueError("preset temporal selection cannot carry Custom fields.")
            return
        if self.preset_id is not None or self.start is None or self.end is None:
            raise ValueError("Custom temporal selection requires only start and end dates.")
        _require_date(self.start, "Custom temporal selection start")
        _require_date(self.end, "Custom temporal selection end")
        if self.end < self.start:
            raise ValueError("Custom temporal selection end cannot precede start.")
        if (self.end - self.start).days + 1 > _MAX_CUSTOM_DURATION_DAYS:
            raise ValueError("Custom temporal selection cannot exceed 90 inclusive days.")

    def _is_vintage(self) -> bool:
        """Check the version grammar; return whether this is a complete vintage selection."""
        if self.version not in {1, 2}:
            raise ValueError("temporal selection version must be 1 or 2.")
        if self.kind is TemporalSelectionKind.VINTAGE:
            if self.version != _V2_TEMPORAL_CONTROL_VERSION:
                raise ValueError("a vintage selection needs temporal control version 2.")
            if (
                self.preset_id is not None
                or self.start is not None
                or self.end is not None
                or not _is_month(self.as_of_month)
            ):
                raise ValueError("a vintage selection carries only as_of_month (YYYY-MM).")
            return True
        if self.as_of_month is not None:
            raise ValueError("only a vintage selection carries as_of_month.")
        if (
            self.preset_id is not None
            and is_calendar_preset(self.preset_id)
            and self.version != _V2_TEMPORAL_CONTROL_VERSION
        ):
            raise ValueError("a calendar preset needs temporal control version 2.")
        return False

    @classmethod
    def omitted(cls) -> TemporalSelection:
        """Create the omitted/default request form without date-like sentinel values."""
        return cls(kind=TemporalSelectionKind.OMITTED)


@dataclass(frozen=True, slots=True, kw_only=True)
class TemporalResolvedSelection:
    """The selection kind and optional preset retained in a resolved control."""

    kind: TemporalResolvedSelectionKind
    preset_id: TemporalPresetId | None = None

    def __post_init__(self) -> None:
        _require_enum(self.kind, TemporalResolvedSelectionKind, "resolved temporal selection kind")
        if self.kind is TemporalResolvedSelectionKind.PRESET:
            _require_enum(self.preset_id, TemporalPresetId, "resolved temporal preset id")
        elif self.preset_id is not None:
            raise ValueError("default and Custom resolved selections cannot carry a preset id.")


@dataclass(frozen=True, slots=True, kw_only=True)
class TemporalResolved:
    """One exact server-resolved period and its selection/display provenance."""

    window: DateRange
    reporting_window_status: TemporalReportingWindowStatus
    selection: TemporalResolvedSelection
    display_grain: TemporalDisplayGrain
    period_label: str | None = None
    """Version 2 only: the server-built label of a resolved calendar period ("August 2026")."""

    def __post_init__(self) -> None:
        _require_range(self.window, "resolved temporal window")
        if self.period_label is not None:
            _require_nonblank(self.period_label, "resolved period label")
        _require_enum(
            self.reporting_window_status,
            TemporalReportingWindowStatus,
            "reporting window status",
        )
        if type(self.selection) is not TemporalResolvedSelection:
            raise ValueError("resolved temporal selection must be a TemporalResolvedSelection.")
        _require_enum(self.display_grain, TemporalDisplayGrain, "display grain")
        if (self.reporting_window_status is TemporalReportingWindowStatus.USER_SELECTED) != (
            self.selection.kind is not TemporalResolvedSelectionKind.DEFAULT
        ):
            raise ValueError("reporting window status and resolved selection provenance disagree.")


@dataclass(frozen=True, slots=True, kw_only=True)
class TemporalComparison:
    """A page comparison policy paired with its exact server-derived status."""

    policy: TemporalComparisonPolicy
    status: TemporalComparisonStatus
    reason_code: ComparisonUnavailableReason | None = None
    prior_period_label: str | None = None

    def __post_init__(self) -> None:
        _require_enum(self.policy, TemporalComparisonPolicy, "comparison policy")
        _require_enum(self.status, TemporalComparisonStatus, "comparison status")
        if self.reason_code is not None:
            _require_enum(self.reason_code, ComparisonUnavailableReason, "comparison reason")
        allowed_statuses = {
            TemporalComparisonPolicy.REQUIRED_EQUAL_PRIOR: {
                TemporalComparisonStatus.AVAILABLE,
                TemporalComparisonStatus.UNAVAILABLE,
            },
            TemporalComparisonPolicy.OPTIONAL_EQUAL_PRIOR: {
                TemporalComparisonStatus.AVAILABLE,
                TemporalComparisonStatus.NOT_REQUESTED,
                TemporalComparisonStatus.UNAVAILABLE,
            },
            TemporalComparisonPolicy.NONE: {TemporalComparisonStatus.NOT_APPLICABLE},
            TemporalComparisonPolicy.PRIOR_CALENDAR_PERIOD: {
                TemporalComparisonStatus.AVAILABLE,
                TemporalComparisonStatus.UNAVAILABLE,
            },
        }
        if self.policy is TemporalComparisonPolicy.PRIOR_CALENDAR_PERIOD:
            if self.reason_code is not None or self.prior_period_label is None:
                raise ValueError(
                    "a prior-calendar comparison requires its period label and no reason code."
                )
            _require_nonblank(self.prior_period_label, "prior calendar period label")
            if self.status not in allowed_statuses[self.policy]:
                raise ValueError("a prior-calendar comparison has an invalid status.")
            return
        if self.prior_period_label is not None:
            raise ValueError("only a prior-calendar comparison carries its period label.")
        if self.calendar_period_selected:
            # Version 2: a calendar preset on a mixed page compares nothing (ADR-100 point 8).
            if self.policy is not TemporalComparisonPolicy.OPTIONAL_EQUAL_PRIOR:
                raise ValueError("only an optional equal-prior page skips calendar comparison.")
            return
        if self.reason_code is ComparisonUnavailableReason.CALENDAR_PERIOD_SELECTED:
            raise ValueError("calendar_period_selected belongs to a not_applicable comparison.")
        if self.status not in allowed_statuses[self.policy]:
            raise ValueError("comparison status is invalid for the declared comparison policy.")
        if (self.status is TemporalComparisonStatus.UNAVAILABLE) != (self.reason_code is not None):
            raise ValueError("only an unavailable comparison may carry a comparison reason.")

    @property
    def calendar_period_selected(self) -> bool:
        """Whether this is the version-2 "no comparison for calendar periods" state."""
        return (
            self.status is TemporalComparisonStatus.NOT_APPLICABLE
            and self.reason_code is ComparisonUnavailableReason.CALENDAR_PERIOD_SELECTED
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TemporalPreset:
    """One canonical preset and its server-declared runtime availability."""

    preset_id: TemporalPresetId
    label: str
    availability: TemporalAvailability
    reason_code: TemporalUnavailableReason | None = None
    window: DateRange | None = None
    """Version 2 only: the window this preset serves now, from its pointer (ia §8.1)."""
    period_label: str | None = None
    """Version 2 only: a calendar preset's served period label ("August 2026")."""
    reason_text: str | None = None
    """Version 2 only: the server text an unavailable preset shows inside its button (ia §8.1)."""

    def __post_init__(self) -> None:
        _require_enum(self.preset_id, TemporalPresetId, "temporal preset id")
        _require_enum(self.availability, TemporalAvailability, "temporal preset availability")
        if self.label not in {_PRESET_LABELS[self.preset_id], _V2_PRESET_LABELS[self.preset_id]}:
            raise ValueError("temporal preset label must match its canonical preset id.")
        if self.window is not None:
            _require_range(self.window, "temporal preset window")
        if self.reason_text is not None:
            _require_nonblank(self.reason_text, "temporal preset reason text")
        if self.period_label is not None:
            _require_nonblank(self.period_label, "temporal preset period label")
            if self.window is None or not is_calendar_preset(self.preset_id):
                raise ValueError("only a calendar preset with a window carries a period label.")
        if (
            self.window is not None
            and is_calendar_preset(self.preset_id)
            and self.period_label is None
        ):
            raise ValueError("a calendar preset's window carries its period label.")
        if self.reason_code is not None:
            _require_enum(self.reason_code, TemporalUnavailableReason, "temporal preset reason")
        if (self.availability is TemporalAvailability.UNAVAILABLE) != (
            self.reason_code is not None
        ):
            raise ValueError("only an unavailable temporal preset may carry a reason.")


@dataclass(frozen=True, slots=True, kw_only=True)
class TemporalCustomBounds:
    """Inclusive Custom date limits, never wider than the universal 90-day ceiling."""

    earliest_start: date
    latest_end: date
    min_duration_days: int
    max_duration_days: int

    def __post_init__(self) -> None:
        _require_date(self.earliest_start, "Custom earliest start")
        _require_date(self.latest_end, "Custom latest end")
        if self.latest_end < self.earliest_start:
            raise ValueError("Custom latest end cannot precede earliest start.")
        for value, name in (
            (self.min_duration_days, "Custom minimum duration"),
            (self.max_duration_days, "Custom maximum duration"),
        ):
            if isinstance(value, bool) or type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer.")
        if self.max_duration_days < self.min_duration_days:
            raise ValueError("Custom maximum duration cannot be less than minimum duration.")
        if self.max_duration_days > _MAX_CUSTOM_DURATION_DAYS:
            raise ValueError("Custom maximum duration cannot exceed 90 inclusive days.")
        if (self.latest_end - self.earliest_start).days + 1 < self.min_duration_days:
            raise ValueError("Custom bounds cannot contain the declared minimum duration.")


@dataclass(frozen=True, slots=True, kw_only=True)
class TemporalCustomControl:
    """The runtime Custom control state and, when available, its exact bounds."""

    availability: TemporalAvailability
    bounds: TemporalCustomBounds | None = None
    reason_code: TemporalUnavailableReason | None = None

    def __post_init__(self) -> None:
        _require_enum(self.availability, TemporalAvailability, "Custom availability")
        if self.bounds is not None and type(self.bounds) is not TemporalCustomBounds:
            raise ValueError("Custom bounds must be a TemporalCustomBounds.")
        if self.reason_code is not None:
            _require_enum(self.reason_code, TemporalUnavailableReason, "Custom reason")
        if self.availability is TemporalAvailability.AVAILABLE:
            if self.bounds is None or self.reason_code is not None:
                raise ValueError("an available Custom control requires bounds and no reason.")
        elif self.bounds is not None or self.reason_code is None:
            raise ValueError("an unavailable Custom control requires a reason and no bounds.")


@dataclass(frozen=True, slots=True, kw_only=True)
class TemporalRecovery:
    """One generated action or explicit no-self-service disposition for a refusal."""

    action_id: str | None = None
    no_self_service_reason: TemporalNoSelfServiceReason | None = None

    def __post_init__(self) -> None:
        if (self.action_id is None) == (self.no_self_service_reason is None):
            raise ValueError("temporal recovery requires exactly one tagged alternative.")
        if self.action_id is not None:
            _require_nonblank(self.action_id, "temporal recovery action id")
        if self.no_self_service_reason is not None:
            _require_enum(
                self.no_self_service_reason,
                TemporalNoSelfServiceReason,
                "temporal no-self-service reason",
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class TemporalControl:
    """The immutable server-declared temporal control carried by every V2 result.

    Version 1 is the rolling-preset grammar. Version 2 (ADR-100 point 8) lists the page's
    declared candidates, rolling then calendar, each with the window it serves now, and adds a
    resolved calendar period's label and the "no comparison for calendar periods" state.
    """

    version: Literal[1, 2]
    mode: TemporalMode
    resolved: TemporalResolved
    comparison: TemporalComparison
    presets: tuple[TemporalPreset, ...]
    custom: TemporalCustomControl

    def __post_init__(self) -> None:
        if self.version not in {_TEMPORAL_CONTROL_VERSION, _V2_TEMPORAL_CONTROL_VERSION}:
            raise ValueError("TemporalControl version must be 1 or 2.")
        _require_enum(self.mode, TemporalMode, "temporal mode")
        if type(self.resolved) is not TemporalResolved:
            raise ValueError("temporal resolved state must be a TemporalResolved.")
        if type(self.comparison) is not TemporalComparison:
            raise ValueError("temporal comparison must be a TemporalComparison.")
        if type(self.presets) is not tuple or any(
            type(preset) is not TemporalPreset for preset in self.presets
        ):
            raise ValueError("temporal presets must be a tuple of TemporalPreset values.")
        if self.version == _TEMPORAL_CONTROL_VERSION:
            self._check_version_1()
        else:
            self._check_version_2()
        if type(self.custom) is not TemporalCustomControl:
            raise ValueError("temporal Custom control must be a TemporalCustomControl.")
        if self.mode is TemporalMode.FIXED and (
            any(preset.availability is TemporalAvailability.AVAILABLE for preset in self.presets)
            or self.custom.availability is TemporalAvailability.AVAILABLE
        ):
            raise ValueError("fixed temporal controls cannot expose selectable temporal choices.")

    def _check_version_1(self) -> None:
        """Version 1: the complete rolling order and none of the version-2 additions."""
        if tuple(preset.preset_id for preset in self.presets) != _CANONICAL_PRESET_IDS:
            raise ValueError("temporal presets must use the complete canonical preset order.")
        if any(
            preset.label != _PRESET_LABELS[preset.preset_id]
            or preset.window is not None
            or preset.period_label is not None
            or preset.reason_text is not None
            for preset in self.presets
        ):
            raise ValueError("a version-1 preset carries its version-1 label only.")
        if (
            self.resolved.period_label is not None
            or self.resolved.display_grain in _V2_DISPLAY_GRAINS
            or self.comparison.calendar_period_selected
        ):
            raise ValueError("a version-1 control can't carry a calendar resolution.")

    def _check_version_2(self) -> None:
        """Version 2: the declared candidates in canonical order, calendar periods labelled."""
        order = tuple(preset.preset_id for preset in self.presets)
        if not order or order != tuple(
            preset for preset in _V2_CANONICAL_PRESET_IDS if preset in order
        ):
            raise ValueError("version-2 presets are the declared candidates in canonical order.")
        if any(preset.label != _V2_PRESET_LABELS[preset.preset_id] for preset in self.presets):
            raise ValueError("a version-2 preset carries its version-2 label.")
        if any(
            (preset.availability is TemporalAvailability.UNAVAILABLE)
            != (preset.reason_text is not None)
            for preset in self.presets
        ):
            raise ValueError("an unavailable version-2 preset, and only it, carries its text.")
        calendar = self.resolved.display_grain in _CALENDAR_DISPLAY_GRAINS
        preset_id = self.resolved.selection.preset_id
        if preset_id is not None and is_calendar_preset(preset_id) != calendar:
            raise ValueError("a calendar preset resolves at its own calendar grain.")
        if calendar != (self.resolved.period_label is not None):
            raise ValueError("a resolved calendar period, and only it, carries its period label.")
        calendar_comparison = (
            self.comparison.calendar_period_selected
            or self.comparison.policy is TemporalComparisonPolicy.PRIOR_CALENDAR_PERIOD
        )
        if calendar != calendar_comparison:
            raise ValueError(
                "a calendar period, and only it, shows no comparison or its prior calendar period."
            )


VINTAGE_LABEL_TEXT = "Cohorts through {month}"
"""The server's static Retention label: the one vintage the page serves (latest only)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class VintageResolved:
    """The exact latest vintage, its label and window the server resolved."""

    as_of_month: str
    period_label: str
    window: DateRange

    def __post_init__(self) -> None:
        if not _is_month(self.as_of_month):
            raise ValueError("resolved vintage month must be YYYY-MM.")
        _require_nonblank(self.period_label, "resolved vintage label")
        _require_range(self.window, "resolved vintage window")
        if self.window.end.isoformat()[:7] != self.as_of_month:
            raise ValueError("resolved vintage window must end in its as-of month.")

    @property
    def label_text(self) -> str:
        """The static label the page shows: "Cohorts through {period_label}"."""
        return VINTAGE_LABEL_TEXT.format(month=self.period_label)


@dataclass(frozen=True, slots=True, kw_only=True)
class VintageControl:
    """The version-2 Retention control: the latest vintage, fixed, or explicitly unavailable.

    No roster and no selection: the page serves only its latest vintage (owner decision
    2026-10-14); a bounded historical roster is deferred work.
    """

    resolved: VintageResolved | None
    version: Literal[2] = 2
    mode: Literal["fixed"] = "fixed"

    def __post_init__(self) -> None:
        if self.version != 2 or self.mode != "fixed":  # noqa: PLR2004 -- wire v2
            raise ValueError("VintageControl must be fixed version 2.")
        if self.resolved is not None and type(self.resolved) is not VintageResolved:
            raise ValueError("vintage control requires a VintageResolved value or None.")


AnyTemporalControl = TemporalControl | VintageControl


@dataclass(frozen=True, slots=True, kw_only=True)
class PresentationPageResult:
    """A V2 result containing a page-owned artifact and its temporal control."""

    session_id: str
    temporal: AnyTemporalControl
    page: object
    envelope_version: Literal[2] = _PRESENTATION_RESULT_VERSION
    kind: Literal["page"] = "page"

    def __post_init__(self) -> None:
        if self.envelope_version != _PRESENTATION_RESULT_VERSION or self.kind != "page":
            raise ValueError("page presentation results must use envelope version 2 and kind page.")
        _require_nonblank(self.session_id, "presentation session id")
        if type(self.temporal) not in {TemporalControl, VintageControl}:
            raise ValueError("page presentation result requires a temporal control.")
        if isinstance(self.temporal, VintageControl) and self.temporal.resolved is None:
            raise ValueError("a page presentation result requires a resolved vintage control.")
        if self.page is None:
            raise ValueError("page presentation result requires a page artifact.")


@dataclass(frozen=True, slots=True, kw_only=True)
class PresentationWindowUnavailableResult:
    """A V2 result that refuses temporal resolution and deliberately carries no artifact."""

    session_id: str
    temporal: AnyTemporalControl
    reason_code: TemporalUnavailableReason
    recovery: TemporalRecovery
    envelope_version: Literal[2] = _PRESENTATION_RESULT_VERSION
    kind: Literal["window_unavailable"] = "window_unavailable"
    message: str | None = None
    """Version 2 only, on every reason: the server text the page shows (contract rule 3)."""
    expected_after: date | None = None
    """Version 2, `no_complete_period_measured` only: when the first period is expected."""

    def __post_init__(self) -> None:
        if (
            self.envelope_version != _PRESENTATION_RESULT_VERSION
            or self.kind != "window_unavailable"
        ):
            raise ValueError(
                "window-unavailable results must use envelope version 2 and "
                "kind window_unavailable."
            )
        _require_nonblank(self.session_id, "presentation session id")
        if type(self.temporal) not in {TemporalControl, VintageControl}:
            raise ValueError("window-unavailable presentation result requires a temporal control.")
        _require_enum(self.reason_code, TemporalUnavailableReason, "window-unavailable reason")
        if type(self.recovery) is not TemporalRecovery:
            raise ValueError("window-unavailable presentation result requires a TemporalRecovery.")
        no_period = self.reason_code is TemporalUnavailableReason.NO_COMPLETE_PERIOD_MEASURED
        version_2 = self.temporal.version == _V2_TEMPORAL_CONTROL_VERSION
        if no_period and not version_2:
            raise ValueError("no_complete_period_measured needs temporal control version 2.")
        if no_period != (self.expected_after is not None):
            raise ValueError("no_complete_period_measured, and only it, carries a date.")
        if version_2 != (self.message is not None):
            raise ValueError(
                "a version-2 window-unavailable result carries the server's text; "
                "a version-1 result carries none."
            )
        if version_2:
            _require_nonblank(self.message, "window-unavailable text")
        if no_period:
            _require_date(self.expected_after, "no-complete-period expected date")


PresentationResult = PresentationPageResult | PresentationWindowUnavailableResult


@dataclass(frozen=True, slots=True, kw_only=True)
class TemporalResolutionRequest:
    """Pure resolver input: declared policy, exact common coverage, and browser intent."""

    policy: PresentationTemporalPolicy
    coverage_intervals: tuple[DateRange, ...]
    selection: TemporalSelection = field(default_factory=TemporalSelection.omitted)
    comparison_requested: bool = False

    def __post_init__(self) -> None:
        if type(self.policy) is not PresentationTemporalPolicy:
            raise ValueError("temporal resolution requires a PresentationTemporalPolicy.")
        if type(self.coverage_intervals) is not tuple or any(
            type(interval) is not DateRange for interval in self.coverage_intervals
        ):
            raise ValueError("temporal resolution coverage must be a tuple of DateRange values.")
        if common_intervals((self.coverage_intervals, self.coverage_intervals)) != (
            self.coverage_intervals
        ):
            raise ValueError(
                "temporal resolution coverage must be sorted, disjoint, and coalesced."
            )
        if type(self.selection) is not TemporalSelection:
            raise ValueError("temporal resolution requires a TemporalSelection.")
        if type(self.comparison_requested) is not bool:
            raise ValueError("temporal comparison intent must be Boolean.")
        if self.comparison_requested and (
            self.policy.comparison_policy is not TemporalComparisonPolicy.OPTIONAL_EQUAL_PRIOR
        ):
            raise ValueError("comparison intent is valid only for optional equal-prior policy.")


@dataclass(frozen=True, slots=True, kw_only=True)
class TemporalResolutionAvailable:
    """A resolved exact window that may proceed to the page artifact reducer."""

    control: TemporalControl

    def __post_init__(self) -> None:
        if type(self.control) is not TemporalControl:
            raise ValueError("available temporal resolution requires a TemporalControl.")


@dataclass(frozen=True, slots=True, kw_only=True)
class TemporalResolutionUnavailable:
    """A temporal refusal with refreshed controls when a source window exists."""

    reason_code: TemporalUnavailableReason
    recovery: TemporalRecovery
    control: TemporalControl | None = None

    def __post_init__(self) -> None:
        _require_enum(self.reason_code, TemporalUnavailableReason, "temporal resolution reason")
        if type(self.recovery) is not TemporalRecovery:
            raise ValueError("unavailable temporal resolution requires a TemporalRecovery.")
        if self.control is not None and type(self.control) is not TemporalControl:
            raise ValueError("unavailable temporal resolution control must be a TemporalControl.")


TemporalResolution = TemporalResolutionAvailable | TemporalResolutionUnavailable


def preset_days(preset_id: TemporalPresetId) -> int:
    """Return the declared inclusive day count one rolling preset resolves to.

    A calendar preset has no day count: it resolves through `resolve_calendar_preset`.
    """
    if is_calendar_preset(preset_id):
        raise ValueError(f"{preset_id.value} resolves to a calendar period, not a day count.")
    return {
        TemporalPresetId.ONE_DAY: 1,
        TemporalPresetId.SEVEN_DAYS: 7,
        TemporalPresetId.FOURTEEN_DAYS: 14,
        TemporalPresetId.THIRTY_DAYS: 30,
    }[preset_id]


def _display_grain(policy: PresentationTemporalPolicy) -> TemporalDisplayGrain:
    """Select the declaration's first supported grain; re-graining stays artifact-owned."""
    primary = policy.display_grains[0]
    if primary is PageTime.DAY or primary is PageTime.EXACT_SOURCE:
        return TemporalDisplayGrain.DAY
    return TemporalDisplayGrain.PERIOD


def _latest_window(
    intervals: tuple[DateRange, ...],
    days: int,
    *,
    require_equal_prior: bool,
) -> DateRange | None:
    candidates: list[DateRange] = []
    for interval in intervals:
        if interval.days < days:
            continue
        current = DateRange(interval.end - timedelta(days=days - 1), interval.end)
        if require_equal_prior:
            prior = DateRange(
                current.start - timedelta(days=days),
                current.start - timedelta(days=1),
            )
            if not any(covered.covers(prior) for covered in intervals):
                continue
        candidates.append(current)
    return max(candidates, key=lambda item: (item.end, item.start), default=None)


def _has_equal_prior(intervals: tuple[DateRange, ...], window: DateRange) -> bool:
    prior = DateRange(
        window.start - timedelta(days=window.days),
        window.start - timedelta(days=1),
    )
    return any(interval.covers(prior) for interval in intervals)


def _covers(intervals: tuple[DateRange, ...], window: DateRange) -> bool:
    return any(interval.covers(window) for interval in intervals)


def _exception_reason(
    policy: PresentationTemporalPolicy, preset_id: TemporalPresetId
) -> TemporalUnavailableReason | None:
    exception = next(
        (item for item in policy.preset_exceptions if item.preset_id is preset_id), None
    )
    if exception is None:
        return None
    try:
        return TemporalUnavailableReason(exception.reason_code)
    except ValueError:
        return TemporalUnavailableReason.STALE_TEMPORAL_DECLARATION


def _comparison(
    context: _SelectionResolutionContext,
    window: DateRange,
    comparison_requested: bool,
) -> TemporalComparison:
    if context.policy.comparison_policy is TemporalComparisonPolicy.NONE:
        return TemporalComparison(
            policy=context.policy.comparison_policy,
            status=TemporalComparisonStatus.NOT_APPLICABLE,
        )
    if context.policy.comparison_policy is TemporalComparisonPolicy.OPTIONAL_EQUAL_PRIOR:
        if not comparison_requested:
            return TemporalComparison(
                policy=context.policy.comparison_policy,
                status=TemporalComparisonStatus.NOT_REQUESTED,
            )
        status = (
            TemporalComparisonStatus.AVAILABLE
            if _has_equal_prior(context.intervals, window)
            else TemporalComparisonStatus.UNAVAILABLE
        )
    else:
        status = (
            TemporalComparisonStatus.AVAILABLE
            if _has_equal_prior(context.intervals, window)
            else TemporalComparisonStatus.UNAVAILABLE
        )
    return TemporalComparison(
        policy=context.policy.comparison_policy,
        status=status,
        reason_code=(
            None
            if status is TemporalComparisonStatus.AVAILABLE
            else ComparisonUnavailableReason.NO_VALID_EQUAL_PRIOR
        ),
    )


def _preset_controls(
    policy: PresentationTemporalPolicy, intervals: tuple[DateRange, ...]
) -> tuple[TemporalPreset, ...]:
    controls: list[TemporalPreset] = []
    for preset_id in _CANONICAL_PRESET_IDS:
        reason = _exception_reason(policy, preset_id)
        if preset_id not in policy.candidate_presets:
            reason = TemporalUnavailableReason.PRESET_UNAVAILABLE
        elif reason is None:
            window = _latest_window(
                intervals,
                preset_days(preset_id),
                require_equal_prior=False,
            )
            if window is None:
                reason = TemporalUnavailableReason.PRESET_UNAVAILABLE
            elif (
                policy.comparison_policy is TemporalComparisonPolicy.REQUIRED_EQUAL_PRIOR
                and not _has_equal_prior(intervals, window)
            ):
                reason = TemporalUnavailableReason.REQUIRED_COMPARISON_UNAVAILABLE
        controls.append(
            TemporalPreset(
                preset_id=preset_id,
                label=_PRESET_LABELS[preset_id],
                availability=(
                    TemporalAvailability.UNAVAILABLE
                    if reason is not None
                    else TemporalAvailability.AVAILABLE
                ),
                reason_code=reason,
            )
        )
    return tuple(controls)


def _custom_control(
    policy: PresentationTemporalPolicy, intervals: tuple[DateRange, ...]
) -> TemporalCustomControl:
    if not policy.custom_enabled:
        return TemporalCustomControl(
            availability=TemporalAvailability.UNAVAILABLE,
            reason_code=TemporalUnavailableReason.CUSTOM_DISABLED,
        )
    required = policy.comparison_policy is TemporalComparisonPolicy.REQUIRED_EQUAL_PRIOR
    usable = _latest_window(
        intervals, policy.custom_min_duration_days, require_equal_prior=required
    )
    if usable is None:
        reason = (
            TemporalUnavailableReason.REQUIRED_COMPARISON_UNAVAILABLE
            if intervals and required
            else TemporalUnavailableReason.NO_COMMON_INTERVAL
        )
        return TemporalCustomControl(
            availability=TemporalAvailability.UNAVAILABLE,
            reason_code=reason,
        )
    max_duration = min(
        policy.custom_max_duration_days,
        max(interval.days // (2 if required else 1) for interval in intervals),
    )
    return TemporalCustomControl(
        availability=TemporalAvailability.AVAILABLE,
        bounds=TemporalCustomBounds(
            earliest_start=intervals[0].start,
            latest_end=intervals[-1].end,
            min_duration_days=policy.custom_min_duration_days,
            max_duration_days=max_duration,
        ),
    )


def _default_window(
    policy: PresentationTemporalPolicy, intervals: tuple[DateRange, ...]
) -> tuple[DateRange | None, TemporalReportingWindowStatus | None]:
    if is_calendar_preset(policy.default_preset):
        # A calendar default (New customers opens on Last full week, OD-UX-6) has no day count:
        # an explicit rolling preset still resolves on its own, and the calendar default itself
        # is resolved by the version-2 calendar-period path, never here.
        return None, None
    required = policy.comparison_policy is TemporalComparisonPolicy.REQUIRED_EQUAL_PRIOR
    preferred = _latest_window(
        intervals, preset_days(policy.default_preset), require_equal_prior=required
    )
    if preferred is not None:
        return preferred, TemporalReportingWindowStatus.PREFERRED_DEFAULT
    for days in range(
        preset_days(policy.default_preset) - 1,
        policy.custom_min_duration_days - 1,
        -1,
    ):
        partial = _latest_window(intervals, days, require_equal_prior=required)
        if partial is not None:
            return partial, TemporalReportingWindowStatus.PARTIAL_DEFAULT
    return None, None


def _recovery(reason: TemporalUnavailableReason) -> TemporalRecovery:
    return TemporalRecovery(
        no_self_service_reason=(
            TemporalNoSelfServiceReason.WAIT_FOR_SOURCE_COVERAGE
            if reason
            in {
                TemporalUnavailableReason.NO_COMMON_INTERVAL,
                TemporalUnavailableReason.INCOMPATIBLE_SOURCE_COVERAGE,
                TemporalUnavailableReason.REQUIRED_COMPARISON_UNAVAILABLE,
            }
            else TemporalNoSelfServiceReason.NO_SUPPORTED_WINDOW
        )
    )


@dataclass(frozen=True, slots=True)
class _SelectionResolutionContext:
    """Precomputed server state needed to resolve one browser temporal intent."""

    policy: PresentationTemporalPolicy
    intervals: tuple[DateRange, ...]
    preset_controls: tuple[TemporalPreset, ...]
    custom_control: TemporalCustomControl
    default_window: DateRange | None
    default_status: TemporalReportingWindowStatus | None


@dataclass(frozen=True, slots=True)
class _SelectionDecision:
    """Exact selected state retained even when the resolver must refuse it."""

    window: DateRange | None
    kind: TemporalResolvedSelectionKind
    reporting_status: TemporalReportingWindowStatus | None
    preset_id: TemporalPresetId | None
    refusal: TemporalUnavailableReason | None


def _custom_selection_window(selection: TemporalSelection) -> DateRange:
    """Return validated Custom dates without relying on a static type suppression."""
    if selection.start is None or selection.end is None:
        raise ValueError("Custom temporal selection requires start and end dates.")
    return DateRange(selection.start, selection.end)


def _resolve_selection(
    context: _SelectionResolutionContext,
    selection: TemporalSelection,
) -> _SelectionDecision:
    if selection.kind is TemporalSelectionKind.PRESET:
        preset_id = selection.preset_id
        if preset_id is None:
            raise ValueError("preset temporal selection requires a preset id.")
        preset = next(item for item in context.preset_controls if item.preset_id is preset_id)
        return _SelectionDecision(
            window=(
                None
                if preset.availability is TemporalAvailability.UNAVAILABLE
                else _latest_window(
                    context.intervals,
                    preset_days(preset_id),
                    require_equal_prior=False,
                )
            ),
            kind=TemporalResolvedSelectionKind.PRESET,
            reporting_status=TemporalReportingWindowStatus.USER_SELECTED,
            preset_id=preset_id,
            refusal=(
                preset.reason_code
                if preset.availability is TemporalAvailability.UNAVAILABLE
                else None
            ),
        )
    if selection.kind is TemporalSelectionKind.CUSTOM:
        window = _custom_selection_window(selection)
        bounds = context.custom_control.bounds
        if not context.policy.custom_enabled:
            refusal = TemporalUnavailableReason.CUSTOM_DISABLED
        elif bounds is None or (
            window.days < context.policy.custom_min_duration_days
            or window.days > bounds.max_duration_days
            or window.start < bounds.earliest_start
            or window.end > bounds.latest_end
        ):
            refusal = TemporalUnavailableReason.CUSTOM_OUT_OF_BOUNDS
        elif not _covers(context.intervals, window):
            refusal = TemporalUnavailableReason.INCOMPATIBLE_SOURCE_COVERAGE
        else:
            refusal = None
        return _SelectionDecision(
            window=window,
            kind=TemporalResolvedSelectionKind.CUSTOM,
            reporting_status=TemporalReportingWindowStatus.USER_SELECTED,
            preset_id=None,
            refusal=refusal,
        )
    return _SelectionDecision(
        window=context.default_window,
        kind=TemporalResolvedSelectionKind.DEFAULT,
        reporting_status=context.default_status,
        preset_id=None,
        refusal=(
            None
            if context.default_window is not None
            else TemporalUnavailableReason.NO_COMMON_INTERVAL
        ),
    )


def resolve_temporal(request: TemporalResolutionRequest) -> TemporalResolution:
    """Resolve exact coverage without source reads, clocks, gap filling, or date shifting."""
    policy = request.policy
    intervals = request.coverage_intervals
    default_window, default_status = _default_window(policy, intervals)
    context = _SelectionResolutionContext(
        policy=policy,
        intervals=intervals,
        preset_controls=_preset_controls(policy, intervals),
        custom_control=_custom_control(policy, intervals),
        default_window=default_window,
        default_status=default_status,
    )
    decision = _resolve_selection(context, request.selection)
    comparison = (
        None
        if decision.window is None
        else _comparison(context, decision.window, request.comparison_requested)
    )
    refusal = decision.refusal
    if (
        comparison is not None
        and comparison.status is TemporalComparisonStatus.UNAVAILABLE
        and policy.comparison_policy is TemporalComparisonPolicy.REQUIRED_EQUAL_PRIOR
    ):
        refusal = TemporalUnavailableReason.REQUIRED_COMPARISON_UNAVAILABLE

    control_window = decision.window or default_window
    control: TemporalControl | None = None
    if control_window is not None:
        uses_selection = decision.window is not None
        control = TemporalControl(
            version=1,
            mode=TemporalMode.SELECTABLE,
            resolved=TemporalResolved(
                window=control_window,
                reporting_window_status=(
                    decision.reporting_status
                    if uses_selection and decision.reporting_status is not None
                    else default_status or TemporalReportingWindowStatus.PREFERRED_DEFAULT
                ),
                selection=TemporalResolvedSelection(
                    kind=(
                        decision.kind if uses_selection else TemporalResolvedSelectionKind.DEFAULT
                    ),
                    preset_id=decision.preset_id if uses_selection else None,
                ),
                display_grain=_display_grain(policy),
            ),
            comparison=(
                comparison
                if comparison is not None
                else _comparison(context, control_window, request.comparison_requested)
            ),
            presets=context.preset_controls,
            custom=context.custom_control,
        )

    if refusal is not None:
        return TemporalResolutionUnavailable(
            reason_code=refusal,
            recovery=_recovery(refusal),
            control=control,
        )
    if control is None:
        return TemporalResolutionUnavailable(
            reason_code=TemporalUnavailableReason.NO_COMMON_INTERVAL,
            recovery=_recovery(TemporalUnavailableReason.NO_COMMON_INTERVAL),
        )
    return TemporalResolutionAvailable(control=control)


__all__ += [
    "TemporalResolution",
    "TemporalResolutionAvailable",
    "TemporalResolutionRequest",
    "TemporalResolutionUnavailable",
    "resolve_temporal",
]
