# SPDX-License-Identifier: MIT-0
"""V2 presentation temporal controls, request grammar, and result-envelope invariants."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta
from typing import Literal, cast

import pytest

from domain.catalog.freshness import common_intervals
from domain.presentation.page_schemas import (
    ROLLING_PRESET_IDS,
    PageTime,
    PresentationTemporalPolicy,
    TemporalComparisonPolicy,
    TemporalPresetException,
)
from domain.presentation.temporal import (
    ComparisonUnavailableReason,
    PresentationPageResult,
    PresentationWindowUnavailableResult,
    TemporalAvailability,
    TemporalComparison,
    TemporalComparisonStatus,
    TemporalControl,
    TemporalCustomBounds,
    TemporalCustomControl,
    TemporalDisplayGrain,
    TemporalMode,
    TemporalNoSelfServiceReason,
    TemporalPreset,
    TemporalPresetId,
    TemporalRecovery,
    TemporalReportingWindowStatus,
    TemporalResolutionAvailable,
    TemporalResolutionRequest,
    TemporalResolutionUnavailable,
    TemporalResolved,
    TemporalResolvedSelection,
    TemporalResolvedSelectionKind,
    TemporalSelection,
    TemporalSelectionKind,
    TemporalUnavailableReason,
    VintageControl,
    VintageResolved,
    resolve_temporal,
)
from domain.shared_kernel.values import DateRange

pytestmark = [pytest.mark.unit]


def _resolved(
    *, selection_kind: TemporalResolvedSelectionKind = TemporalResolvedSelectionKind.DEFAULT
) -> TemporalResolved:
    preset_id = (
        TemporalPresetId.SEVEN_DAYS
        if selection_kind is TemporalResolvedSelectionKind.PRESET
        else None
    )
    status = (
        TemporalReportingWindowStatus.USER_SELECTED
        if selection_kind is not TemporalResolvedSelectionKind.DEFAULT
        else TemporalReportingWindowStatus.PREFERRED_DEFAULT
    )
    return TemporalResolved(
        window=DateRange(date(2026, 1, 1), date(2026, 1, 7)),
        reporting_window_status=status,
        selection=TemporalResolvedSelection(kind=selection_kind, preset_id=preset_id),
        display_grain=TemporalDisplayGrain.DAY,
    )


def _preset(preset_id: TemporalPresetId, available: bool = True) -> TemporalPreset:
    return TemporalPreset(
        preset_id=preset_id,
        label={
            TemporalPresetId.ONE_DAY: "1D",
            TemporalPresetId.SEVEN_DAYS: "7D",
            TemporalPresetId.FOURTEEN_DAYS: "14D",
            TemporalPresetId.THIRTY_DAYS: "30D",
        }[preset_id],
        availability=(
            TemporalAvailability.AVAILABLE if available else TemporalAvailability.UNAVAILABLE
        ),
        reason_code=(None if available else TemporalUnavailableReason.PRESET_UNAVAILABLE),
    )


def _control(
    *,
    mode: TemporalMode = TemporalMode.SELECTABLE,
    comparison: TemporalComparison | None = None,
) -> TemporalControl:
    available = mode is TemporalMode.SELECTABLE
    return TemporalControl(
        version=1,
        mode=mode,
        resolved=_resolved(),
        comparison=comparison
        or TemporalComparison(
            policy=TemporalComparisonPolicy.NONE,
            status=TemporalComparisonStatus.NOT_APPLICABLE,
        ),
        presets=tuple(_preset(preset, available) for preset in ROLLING_PRESET_IDS),
        custom=TemporalCustomControl(
            availability=(
                TemporalAvailability.AVAILABLE if available else TemporalAvailability.UNAVAILABLE
            ),
            bounds=(
                TemporalCustomBounds(
                    earliest_start=date(2025, 10, 1),
                    latest_end=date(2026, 1, 7),
                    min_duration_days=1,
                    max_duration_days=90,
                )
                if available
                else None
            ),
            reason_code=(None if available else TemporalUnavailableReason.NO_COMMON_INTERVAL),
        ),
    )


def test_temporal_selection_accepts_only_omitted_preset_or_inclusive_custom_intent() -> None:
    assert TemporalSelection.omitted() == TemporalSelection(kind=TemporalSelectionKind.OMITTED)
    assert (
        TemporalSelection(
            kind=TemporalSelectionKind.PRESET,
            preset_id=TemporalPresetId.FOURTEEN_DAYS,
        ).preset_id
        is TemporalPresetId.FOURTEEN_DAYS
    )
    custom = TemporalSelection(
        kind=TemporalSelectionKind.CUSTOM,
        start=date(2026, 1, 1),
        end=date(2026, 3, 31),
    )
    assert custom.start == date(2026, 1, 1)
    assert custom.end == date(2026, 3, 31)

    with pytest.raises(ValueError, match="cannot exceed 90 inclusive days"):
        TemporalSelection(
            kind=TemporalSelectionKind.CUSTOM,
            start=date(2026, 1, 1),
            end=date(2026, 4, 1),
        )
    with pytest.raises(ValueError, match="preset temporal selection cannot carry Custom fields"):
        TemporalSelection(
            kind=TemporalSelectionKind.PRESET,
            preset_id=TemporalPresetId.ONE_DAY,
            start=date(2026, 1, 1),
        )
    with pytest.raises(ValueError, match="date without a time or timezone"):
        TemporalSelection(
            kind=TemporalSelectionKind.CUSTOM,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=date(2026, 1, 2),
        )


def test_temporal_control_requires_canonical_presets_and_policy_consistent_comparison() -> None:
    control = _control(
        comparison=TemporalComparison(
            policy=TemporalComparisonPolicy.REQUIRED_EQUAL_PRIOR,
            status=TemporalComparisonStatus.UNAVAILABLE,
            reason_code=ComparisonUnavailableReason.NO_VALID_EQUAL_PRIOR,
        )
    )
    assert tuple(preset.preset_id for preset in control.presets) == ROLLING_PRESET_IDS

    with pytest.raises(ValueError, match="complete canonical preset order"):
        TemporalControl(
            version=1,
            mode=TemporalMode.SELECTABLE,
            resolved=_resolved(),
            comparison=TemporalComparison(
                policy=TemporalComparisonPolicy.NONE,
                status=TemporalComparisonStatus.NOT_APPLICABLE,
            ),
            presets=control.presets[::-1],
            custom=control.custom,
        )
    with pytest.raises(ValueError, match="invalid for the declared comparison policy"):
        TemporalComparison(
            policy=TemporalComparisonPolicy.NONE,
            status=TemporalComparisonStatus.AVAILABLE,
        )
    with pytest.raises(ValueError, match="only an unavailable comparison may carry"):
        TemporalComparison(
            policy=TemporalComparisonPolicy.OPTIONAL_EQUAL_PRIOR,
            status=TemporalComparisonStatus.NOT_REQUESTED,
            reason_code=ComparisonUnavailableReason.NO_VALID_EQUAL_PRIOR,
        )


def test_temporal_control_enforces_custom_and_fixed_control_availability() -> None:
    with pytest.raises(ValueError, match="available Custom control requires bounds"):
        TemporalCustomControl(availability=TemporalAvailability.AVAILABLE)
    with pytest.raises(ValueError, match="fixed temporal controls cannot expose selectable"):
        TemporalControl(
            version=1,
            mode=TemporalMode.FIXED,
            resolved=_resolved(),
            comparison=TemporalComparison(
                policy=TemporalComparisonPolicy.NONE,
                status=TemporalComparisonStatus.NOT_APPLICABLE,
            ),
            presets=tuple(_preset(preset) for preset in ROLLING_PRESET_IDS),
            custom=TemporalCustomControl(
                availability=TemporalAvailability.UNAVAILABLE,
                reason_code=TemporalUnavailableReason.NO_COMMON_INTERVAL,
            ),
        )


def test_temporal_control_and_result_models_are_immutable_and_disjoint() -> None:
    control = _control()
    page = PresentationPageResult(session_id="session-1", temporal=control, page=object())
    unavailable = PresentationWindowUnavailableResult(
        session_id="session-1",
        temporal=control,
        reason_code=TemporalUnavailableReason.NO_COMMON_INTERVAL,
        recovery=TemporalRecovery(
            no_self_service_reason=TemporalNoSelfServiceReason.NO_SUPPORTED_WINDOW
        ),
    )

    assert page.kind == "page"
    assert unavailable.kind == "window_unavailable"
    with pytest.raises(FrozenInstanceError):
        control.mode = TemporalMode.FIXED  # type: ignore[misc]
    with pytest.raises(ValueError, match="exactly one tagged alternative"):
        TemporalRecovery()
    with pytest.raises(ValueError, match="requires a page artifact"):
        PresentationPageResult(session_id="session-1", temporal=control, page=None)


def _policy(
    *,
    comparison_policy: TemporalComparisonPolicy = TemporalComparisonPolicy.NONE,
    custom_min_duration_days: int = 1,
    preset_exceptions: tuple[TemporalPresetException, ...] = (),
) -> PresentationTemporalPolicy:
    return PresentationTemporalPolicy(
        candidate_presets=ROLLING_PRESET_IDS,
        default_preset=TemporalPresetId.SEVEN_DAYS,
        custom_enabled=True,
        custom_min_duration_days=custom_min_duration_days,
        custom_max_duration_days=90,
        comparison_policy=comparison_policy,
        display_grains=(PageTime.PERIOD, PageTime.DAY),
        preset_exceptions=preset_exceptions,
    )


def test_resolver_returns_canonical_controls_and_preferred_default_from_exact_coverage() -> None:
    result = resolve_temporal(
        TemporalResolutionRequest(
            policy=_policy(),
            coverage_intervals=(DateRange(date(2026, 1, 1), date(2026, 1, 31)),),
        )
    )

    assert isinstance(result, TemporalResolutionAvailable)
    assert result.control.resolved.window == DateRange(date(2026, 1, 25), date(2026, 1, 31))
    assert (
        result.control.resolved.reporting_window_status
        is TemporalReportingWindowStatus.PREFERRED_DEFAULT
    )
    assert result.control.resolved.display_grain is TemporalDisplayGrain.PERIOD
    assert tuple(preset.preset_id for preset in result.control.presets) == ROLLING_PRESET_IDS
    assert all(
        preset.availability is TemporalAvailability.AVAILABLE for preset in result.control.presets
    )


def test_resolver_preserves_a_short_omitted_window_as_partial_default() -> None:
    result = resolve_temporal(
        TemporalResolutionRequest(
            policy=_policy(),
            coverage_intervals=(DateRange(date(2026, 1, 1), date(2026, 1, 3)),),
        )
    )

    assert isinstance(result, TemporalResolutionAvailable)
    assert result.control.resolved.window == DateRange(date(2026, 1, 1), date(2026, 1, 3))
    assert (
        result.control.resolved.reporting_window_status
        is TemporalReportingWindowStatus.PARTIAL_DEFAULT
    )


def test_resolver_marks_disabled_custom_policy_unavailable_and_refuses_selection() -> None:
    requested = DateRange(date(2026, 1, 10), date(2026, 1, 12))
    result = resolve_temporal(
        TemporalResolutionRequest(
            policy=replace(_policy(), custom_enabled=False),
            coverage_intervals=(DateRange(date(2026, 1, 1), date(2026, 1, 31)),),
            selection=TemporalSelection(
                kind=TemporalSelectionKind.CUSTOM,
                start=requested.start,
                end=requested.end,
            ),
        )
    )

    assert isinstance(result, TemporalResolutionUnavailable)
    assert result.reason_code is TemporalUnavailableReason.CUSTOM_DISABLED
    assert result.control is not None
    assert result.control.custom.availability is TemporalAvailability.UNAVAILABLE
    assert result.control.custom.bounds is None
    assert result.control.custom.reason_code is TemporalUnavailableReason.CUSTOM_DISABLED


def test_resolver_keeps_a_covered_optional_comparison_request_and_derives_status() -> None:
    result = resolve_temporal(
        TemporalResolutionRequest(
            policy=_policy(comparison_policy=TemporalComparisonPolicy.OPTIONAL_EQUAL_PRIOR),
            coverage_intervals=(DateRange(date(2026, 1, 1), date(2026, 1, 31)),),
            selection=TemporalSelection(
                kind=TemporalSelectionKind.CUSTOM,
                start=date(2026, 1, 10),
                end=date(2026, 1, 12),
            ),
            comparison_requested=True,
        )
    )

    assert isinstance(result, TemporalResolutionAvailable)
    assert result.control.resolved.window == DateRange(date(2026, 1, 10), date(2026, 1, 12))
    assert result.control.comparison.status is TemporalComparisonStatus.AVAILABLE


def test_resolver_refuses_absent_common_coverage_without_fabricating_a_window() -> None:
    result = resolve_temporal(TemporalResolutionRequest(policy=_policy(), coverage_intervals=()))

    assert isinstance(result, TemporalResolutionUnavailable)
    assert result.reason_code is TemporalUnavailableReason.NO_COMMON_INTERVAL
    assert result.control is None


def test_campaign_one_day_refusal_returns_refreshed_default_controls() -> None:
    result = resolve_temporal(
        TemporalResolutionRequest(
            policy=_policy(
                comparison_policy=TemporalComparisonPolicy.REQUIRED_EQUAL_PRIOR,
                custom_min_duration_days=2,
                preset_exceptions=(
                    TemporalPresetException(
                        preset_id=TemporalPresetId.ONE_DAY,
                        availability="unavailable",
                        reason_code="single_period_non_trend_detail_unavailable",
                    ),
                ),
            ),
            coverage_intervals=(DateRange(date(2026, 1, 1), date(2026, 3, 31)),),
            selection=TemporalSelection(
                kind=TemporalSelectionKind.PRESET,
                preset_id=TemporalPresetId.ONE_DAY,
            ),
        )
    )

    assert isinstance(result, TemporalResolutionUnavailable)
    assert (
        result.reason_code is TemporalUnavailableReason.SINGLE_PERIOD_NON_TREND_DETAIL_UNAVAILABLE
    )
    assert result.control is not None
    assert result.control.resolved.selection.kind is TemporalResolvedSelectionKind.DEFAULT
    assert (
        result.control.resolved.reporting_window_status
        is TemporalReportingWindowStatus.PREFERRED_DEFAULT
    )
    assert result.control.comparison.status is TemporalComparisonStatus.AVAILABLE
    one_day = next(
        preset for preset in result.control.presets if preset.preset_id is TemporalPresetId.ONE_DAY
    )
    assert one_day.availability is TemporalAvailability.UNAVAILABLE
    assert (
        one_day.reason_code is TemporalUnavailableReason.SINGLE_PERIOD_NON_TREND_DETAIL_UNAVAILABLE
    )


@pytest.mark.parametrize(
    "custom_minimum_days",
    (2, 1, 1),
    ids=("Campaign", "Channel", "Marketplace"),
)
def test_required_equal_prior_pages_refuse_a_current_window_without_its_prior_period(
    custom_minimum_days: int,
) -> None:
    result = resolve_temporal(
        TemporalResolutionRequest(
            policy=_policy(
                comparison_policy=TemporalComparisonPolicy.REQUIRED_EQUAL_PRIOR,
                custom_min_duration_days=custom_minimum_days,
            ),
            coverage_intervals=(DateRange(date(2026, 1, 25), date(2026, 1, 31)),),
            selection=TemporalSelection(
                kind=TemporalSelectionKind.PRESET,
                preset_id=TemporalPresetId.SEVEN_DAYS,
            ),
        )
    )

    assert isinstance(result, TemporalResolutionUnavailable)
    assert result.reason_code is TemporalUnavailableReason.REQUIRED_COMPARISON_UNAVAILABLE
    assert result.control is not None
    assert result.control.resolved.selection.kind is TemporalResolvedSelectionKind.DEFAULT
    assert (
        result.control.resolved.reporting_window_status
        is TemporalReportingWindowStatus.PARTIAL_DEFAULT
    )
    assert result.control.comparison.status is TemporalComparisonStatus.AVAILABLE


@pytest.mark.parametrize("_page", ("Product", "Media"))
def test_optional_comparison_pages_distinguish_unrequested_and_available_prior(
    _page: str,
) -> None:
    policy = _policy(comparison_policy=TemporalComparisonPolicy.OPTIONAL_EQUAL_PRIOR)
    coverage = (DateRange(date(2026, 1, 1), date(2026, 1, 31)),)

    without_comparison = resolve_temporal(
        TemporalResolutionRequest(policy=policy, coverage_intervals=coverage)
    )
    with_comparison = resolve_temporal(
        TemporalResolutionRequest(
            policy=policy,
            coverage_intervals=coverage,
            comparison_requested=True,
        )
    )

    assert isinstance(without_comparison, TemporalResolutionAvailable)
    assert without_comparison.control.comparison.status is TemporalComparisonStatus.NOT_REQUESTED
    assert isinstance(with_comparison, TemporalResolutionAvailable)
    assert with_comparison.control.comparison.status is TemporalComparisonStatus.AVAILABLE


def test_evidence_health_policy_never_accepts_or_derives_a_comparison() -> None:
    policy = _policy(comparison_policy=TemporalComparisonPolicy.NONE)
    coverage = (DateRange(date(2026, 1, 1), date(2026, 1, 31)),)

    result = resolve_temporal(TemporalResolutionRequest(policy=policy, coverage_intervals=coverage))

    assert isinstance(result, TemporalResolutionAvailable)
    assert result.control.comparison.policy is TemporalComparisonPolicy.NONE
    assert result.control.comparison.status is TemporalComparisonStatus.NOT_APPLICABLE
    with pytest.raises(ValueError, match="only for optional equal-prior policy"):
        TemporalResolutionRequest(
            policy=policy,
            coverage_intervals=coverage,
            comparison_requested=True,
        )


@pytest.mark.parametrize(
    ("start", "end", "message"),
    (
        (
            datetime(2026, 1, 1, tzinfo=UTC),
            date(2026, 1, 2),
            "date without a time or timezone",
        ),
        (date(2026, 1, 2), date(2026, 1, 1), "end cannot precede start"),
    ),
)
def test_custom_selection_rejects_invalid_date_boundaries(
    start: date | datetime,
    end: date,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        TemporalSelection(kind=TemporalSelectionKind.CUSTOM, start=start, end=end)


@pytest.mark.parametrize("duration_days", (1, 2, 7, 14, 30, 90))
def test_custom_selection_preserves_exact_inclusive_duration_boundaries(
    duration_days: int,
) -> None:
    start = date(2026, 1, 1)
    end = start + timedelta(days=duration_days - 1)

    selection = TemporalSelection(kind=TemporalSelectionKind.CUSTOM, start=start, end=end)

    assert selection.start is not None
    assert selection.end is not None
    assert (selection.end - selection.start).days + 1 == duration_days


def test_resolver_refuses_an_undeclared_preset_with_refreshed_controls() -> None:
    result = resolve_temporal(
        TemporalResolutionRequest(
            policy=PresentationTemporalPolicy(
                candidate_presets=(TemporalPresetId.SEVEN_DAYS,),
                default_preset=TemporalPresetId.SEVEN_DAYS,
                custom_enabled=True,
                custom_min_duration_days=1,
                custom_max_duration_days=90,
                comparison_policy=TemporalComparisonPolicy.NONE,
                display_grains=(PageTime.DAY,),
            ),
            coverage_intervals=(DateRange(date(2026, 1, 1), date(2026, 1, 31)),),
            selection=TemporalSelection(
                kind=TemporalSelectionKind.PRESET,
                preset_id=TemporalPresetId.ONE_DAY,
            ),
        )
    )

    assert isinstance(result, TemporalResolutionUnavailable)
    assert result.reason_code is TemporalUnavailableReason.PRESET_UNAVAILABLE
    assert result.control is not None
    assert result.control.resolved.window == DateRange(date(2026, 1, 25), date(2026, 1, 31))
    one_day = next(
        preset for preset in result.control.presets if preset.preset_id is TemporalPresetId.ONE_DAY
    )
    assert one_day.availability is TemporalAvailability.UNAVAILABLE


@pytest.mark.parametrize(
    ("start", "end", "expected_reason"),
    (
        (
            date(2026, 1, 1),
            date(2026, 1, 1),
            TemporalUnavailableReason.CUSTOM_OUT_OF_BOUNDS,
        ),
        (
            date(2025, 12, 31),
            date(2026, 1, 1),
            TemporalUnavailableReason.CUSTOM_OUT_OF_BOUNDS,
        ),
        (
            date(2026, 1, 3),
            date(2026, 1, 4),
            TemporalUnavailableReason.INCOMPATIBLE_SOURCE_COVERAGE,
        ),
    ),
)
def test_resolver_enforces_custom_bounds_and_never_normalizes_requested_dates(
    start: date,
    end: date,
    expected_reason: TemporalUnavailableReason,
) -> None:
    requested = DateRange(start, end)
    result = resolve_temporal(
        TemporalResolutionRequest(
            policy=_policy(custom_min_duration_days=2),
            coverage_intervals=(
                DateRange(date(2026, 1, 1), date(2026, 1, 2)),
                DateRange(date(2026, 1, 5), date(2026, 4, 4)),
            ),
            selection=TemporalSelection(
                kind=TemporalSelectionKind.CUSTOM,
                start=requested.start,
                end=requested.end,
            ),
        )
    )

    assert isinstance(result, TemporalResolutionUnavailable)
    assert result.reason_code is expected_reason
    assert result.control is not None
    assert result.control.resolved.window == requested


def test_resolver_accepts_exact_minimum_and_maximum_custom_durations() -> None:
    coverage = (DateRange(date(2026, 1, 1), date(2026, 4, 30)),)
    policy = _policy(custom_min_duration_days=2)

    for requested in (
        DateRange(date(2026, 1, 1), date(2026, 1, 2)),
        DateRange(date(2026, 1, 1), date(2026, 3, 31)),
    ):
        result = resolve_temporal(
            TemporalResolutionRequest(
                policy=policy,
                coverage_intervals=coverage,
                selection=TemporalSelection(
                    kind=TemporalSelectionKind.CUSTOM,
                    start=requested.start,
                    end=requested.end,
                ),
            )
        )

        assert isinstance(result, TemporalResolutionAvailable)
        assert result.control.resolved.window == requested
        assert result.control.resolved.selection.kind is TemporalResolvedSelectionKind.CUSTOM


def test_resolver_fails_closed_for_a_stale_preset_declaration() -> None:
    result = resolve_temporal(
        TemporalResolutionRequest(
            policy=_policy(
                preset_exceptions=(
                    TemporalPresetException(
                        preset_id=TemporalPresetId.SEVEN_DAYS,
                        availability="unavailable",
                        reason_code="obsolete_declaration_reason",
                    ),
                ),
            ),
            coverage_intervals=(DateRange(date(2026, 1, 1), date(2026, 1, 31)),),
            selection=TemporalSelection(
                kind=TemporalSelectionKind.PRESET,
                preset_id=TemporalPresetId.SEVEN_DAYS,
            ),
        )
    )

    assert isinstance(result, TemporalResolutionUnavailable)
    assert result.reason_code is TemporalUnavailableReason.STALE_TEMPORAL_DECLARATION


def test_resolver_requires_preintersected_source_coverage_without_bridging_gaps() -> None:
    common_coverage = common_intervals(
        (
            (DateRange(date(2026, 1, 1), date(2026, 1, 20)),),
            (
                DateRange(date(2026, 1, 1), date(2026, 1, 2)),
                DateRange(date(2026, 1, 5), date(2026, 1, 20)),
            ),
        )
    )
    requested = DateRange(date(2026, 1, 2), date(2026, 1, 5))

    result = resolve_temporal(
        TemporalResolutionRequest(
            policy=_policy(),
            coverage_intervals=common_coverage,
            selection=TemporalSelection(
                kind=TemporalSelectionKind.CUSTOM,
                start=requested.start,
                end=requested.end,
            ),
        )
    )

    assert common_coverage == (
        DateRange(date(2026, 1, 1), date(2026, 1, 2)),
        DateRange(date(2026, 1, 5), date(2026, 1, 20)),
    )
    assert isinstance(result, TemporalResolutionUnavailable)
    assert result.reason_code is TemporalUnavailableReason.INCOMPATIBLE_SOURCE_COVERAGE
    assert result.control is not None
    assert result.control.resolved.window == requested


@pytest.mark.parametrize(
    "coverage_intervals",
    (
        (
            DateRange(date(2026, 1, 5), date(2026, 1, 20)),
            DateRange(date(2026, 1, 1), date(2026, 1, 2)),
        ),
        (
            DateRange(date(2026, 1, 1), date(2026, 1, 2)),
            DateRange(date(2026, 1, 3), date(2026, 1, 20)),
        ),
    ),
)
def test_temporal_resolution_request_rejects_noncanonical_source_intersections(
    coverage_intervals: tuple[DateRange, ...],
) -> None:
    with pytest.raises(ValueError, match="sorted, disjoint, and coalesced"):
        TemporalResolutionRequest(policy=_policy(), coverage_intervals=coverage_intervals)


def _vintage_control() -> VintageControl:
    return VintageControl(
        resolved=VintageResolved(
            as_of_month="2026-09",
            period_label="September 2026",
            window=DateRange(date(2025, 9, 1), date(2026, 9, 30)),
        )
    )


def test_vintage_control_has_one_explicit_unavailable_state_and_pages_refuse_it() -> None:
    unavailable = VintageControl(resolved=None)
    assert unavailable.resolved is None
    with pytest.raises(ValueError, match="resolved vintage"):
        PresentationPageResult(session_id="session", temporal=unavailable, page={})
    # Control: a resolved latest vintage is a page result.
    PresentationPageResult(session_id="session", temporal=_vintage_control(), page={})


def test_vintage_control_is_fixed_latest_only_with_the_servers_static_label() -> None:
    control = _vintage_control()
    assert control.resolved is not None
    assert (control.version, control.mode) == (2, "fixed")
    assert control.resolved.label_text == "Cohorts through September 2026"
    with pytest.raises(ValueError, match="fixed version 2"):
        replace(control, mode=cast("Literal['fixed']", "selectable"))
    with pytest.raises(ValueError, match="YYYY-MM"):
        replace(control.resolved, as_of_month="2026-13")
    with pytest.raises(ValueError, match="nonblank"):
        replace(control.resolved, period_label=" ")
    with pytest.raises(ValueError, match="DateRange"):
        replace(control.resolved, window=cast("DateRange", "2025-09..2026-09"))
    with pytest.raises(ValueError, match="as-of month"):
        replace(
            control.resolved,
            window=DateRange(date(2025, 8, 1), date(2026, 8, 31)),
        )
