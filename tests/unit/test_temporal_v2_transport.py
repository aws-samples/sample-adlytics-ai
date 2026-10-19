# SPDX-License-Identifier: MIT-0
"""Temporal transport v2 on the wire (ADR-100 point 8; UI slice 9a item 0.2).

A page that declares a calendar preset serves a version-2 control built from its baked presets;
every other page keeps version 1 exactly. Each passing case sits beside the control that must
fail on the guarded defect (`.kiro/steering/validation-probes.md`).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
from test_presentation_handler import (
    _PresentationModule,  # pyright: ignore[reportPrivateUsage]
    presentation_module,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

from adapters.page_artifact_codec import (
    page_artifact_record_from_json,
    page_artifact_record_to_json,
)
from domain.presentation.absence_copy import AbsenceTrigger, absence_message
from domain.presentation.artifacts import (
    PageArtifact,
    PageArtifactRecord,
    PageContextReference,
    canonical_artifact_json,
)
from domain.presentation.page_schemas import (
    PageTime,
    PresentationTemporalPolicy,
    TemporalComparisonPolicy,
    TemporalPresetId,
)
from domain.presentation.temporal import (
    ComparisonUnavailableReason,
    PresentationWindowUnavailableResult,
    TemporalAvailability,
    TemporalComparisonStatus,
    TemporalControl,
    TemporalDisplayGrain,
    TemporalNoSelfServiceReason,
    TemporalPreset,
    TemporalRecovery,
    TemporalResolutionAvailable,
    TemporalResolutionRequest,
    TemporalResolved,
    TemporalSelection,
    TemporalSelectionKind,
    TemporalUnavailableReason,
    resolve_temporal,
)
from domain.presentation.temporal_v2 import (
    PRESET_NOT_MEASURED_TEXT,
    PRESET_NOT_PUBLISHED_TEXT,
    BakedControlRequest,
    ServedPeriod,
    ServedPresetWindow,
    baked_preset_control_v2,
)
from domain.shared_kernel.errors import ValidationError
from domain.shared_kernel.values import DateRange

pytestmark = pytest.mark.unit

SEVEN = TemporalPresetId.SEVEN_DAYS
THIRTY = TemporalPresetId.THIRTY_DAYS
WEEK = TemporalPresetId.LAST_COMPLETE_WEEK
MONTH = TemporalPresetId.LAST_COMPLETE_MONTH
POLICY = PresentationTemporalPolicy(
    candidate_presets=(SEVEN, THIRTY, WEEK, MONTH),
    default_preset=SEVEN,
    custom_enabled=False,
    custom_min_duration_days=1,
    custom_max_duration_days=90,
    comparison_policy=TemporalComparisonPolicy.OPTIONAL_EQUAL_PRIOR,
    display_grains=(PageTime.PERIOD,),
)
WINDOW = DateRange(date(2026, 9, 9), date(2026, 9, 15))
PRIOR = DateRange(date(2026, 9, 2), date(2026, 9, 8))
AUGUST = DateRange(date(2026, 8, 1), date(2026, 8, 31))
POINTERS = (
    ServedPresetWindow(SEVEN, WINDOW),
    ServedPresetWindow(THIRTY, DateRange(date(2026, 8, 17), date(2026, 9, 15))),
    ServedPresetWindow(WEEK, DateRange(date(2026, 9, 7), date(2026, 9, 13)), "Week of Sep 7, 2026"),
    ServedPresetWindow(MONTH, AUGUST, "August 2026"),
)


def _rolling(
    selection: TemporalSelection | None = None,
    pointers: tuple[ServedPresetWindow, ...] = POINTERS,
    prior: DateRange | None = PRIOR,
) -> TemporalControl:
    return baked_preset_control_v2(
        BakedControlRequest(
            POLICY,
            selection or TemporalSelection.omitted(),
            ServedPeriod(SEVEN, WINDOW, prior, None),
            pointers,
        )
    )


def _month() -> TemporalControl:
    return baked_preset_control_v2(
        BakedControlRequest(
            POLICY,
            TemporalSelection(kind=TemporalSelectionKind.PRESET, preset_id=MONTH, version=2),
            ServedPeriod(MONTH, AUGUST, None, "August 2026"),
            POINTERS,
        )
    )


def test_a_rolling_preset_lists_the_declared_candidates_with_the_windows_they_serve() -> None:
    control = _rolling()
    assert control.version == 2
    assert [(item.preset_id, item.label) for item in control.presets] == [
        (SEVEN, "7 days"),
        (THIRTY, "30 days"),
        (WEEK, "Latest measured week"),
        (MONTH, "Latest measured month"),
    ]
    assert [item.window for item in control.presets] == [item.window for item in POINTERS]
    assert [item.period_label for item in control.presets] == [
        None,
        None,
        "Week of Sep 7, 2026",
        "August 2026",
    ]
    assert control.resolved.window == WINDOW
    assert control.resolved.period_label is None
    assert control.resolved.display_grain is TemporalDisplayGrain.PERIOD
    assert control.comparison.status is TemporalComparisonStatus.AVAILABLE
    # Paired: the same rolling window with no prior compares nothing and says why.
    alone = _rolling(prior=None)
    assert alone.comparison.status is TemporalComparisonStatus.UNAVAILABLE
    assert alone.comparison.reason_code is ComparisonUnavailableReason.NO_VALID_EQUAL_PRIOR


def test_a_calendar_preset_names_its_period_and_shows_no_comparison() -> None:
    control = _month()
    assert control.resolved.period_label == "August 2026"
    assert control.resolved.display_grain is TemporalDisplayGrain.MONTH
    assert control.comparison.calendar_period_selected
    # Paired: a rolling resolution can't carry the calendar comparison state.
    with pytest.raises(ValueError, match="calendar period, and only it, shows no comparison"):
        TemporalControl(
            version=2,
            mode=control.mode,
            resolved=_rolling().resolved,
            comparison=control.comparison,
            presets=control.presets,
            custom=control.custom,
        )


def test_a_preset_with_no_pointer_is_unavailable_with_its_own_text() -> None:
    control = _rolling(pointers=POINTERS[:1])
    unavailable = [item for item in control.presets if item.preset_id is not SEVEN]
    assert {item.availability for item in unavailable} == {TemporalAvailability.UNAVAILABLE}
    assert {item.reason_text for item in unavailable} == {PRESET_NOT_PUBLISHED_TEXT}
    assert {item.window for item in unavailable} == {None}
    # Paired: an available preset carrying a reason text is refused.
    with pytest.raises(ValueError, match="unavailable version-2 preset, and only it"):
        TemporalControl(
            version=2,
            mode=control.mode,
            resolved=control.resolved,
            comparison=control.comparison,
            presets=(
                TemporalPreset(
                    preset_id=SEVEN,
                    label="7 days",
                    availability=TemporalAvailability.AVAILABLE,
                    reason_text=PRESET_NOT_PUBLISHED_TEXT,
                ),
                *control.presets[1:],
            ),
            custom=control.custom,
        )


def test_a_calendar_preset_with_no_complete_period_reads_not_measured_yet(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    handler = presentation_module.module
    control = baked_preset_control_v2(
        BakedControlRequest(
            POLICY,
            TemporalSelection(kind=TemporalSelectionKind.PRESET, preset_id=MONTH, version=2),
            ServedPeriod(
                MONTH,
                DateRange(date(2026, 9, 1), date(2026, 9, 30)),
                None,
                "September 2026",
                available=False,
                unavailable_reason=TemporalUnavailableReason.NO_COMPLETE_PERIOD_MEASURED,
            ),
            POINTERS[:2],
        )
    )
    month = control.presets[-1]
    assert month.availability is TemporalAvailability.UNAVAILABLE
    assert month.reason_text == PRESET_NOT_MEASURED_TEXT
    assert month.period_label == "September 2026"
    result = PresentationWindowUnavailableResult(
        session_id="session",
        temporal=control,
        reason_code=TemporalUnavailableReason.NO_COMPLETE_PERIOD_MEASURED,
        recovery=TemporalRecovery(
            no_self_service_reason=TemporalNoSelfServiceReason.WAIT_FOR_SOURCE_COVERAGE
        ),
        message="No complete month has been measured yet. The first is expected after Oct 2.",
        expected_after=date(2026, 10, 2),
    )
    body = handler._presentation_result_json(result)
    assert body["message"] == result.message
    assert body["expected_after"] == "2026-10-02"
    # Paired: the reason with no text is refused, and so is a date on another reason.
    with pytest.raises(ValueError, match="carries the server's text"):
        PresentationWindowUnavailableResult(
            session_id="session",
            temporal=control,
            reason_code=TemporalUnavailableReason.NO_COMPLETE_PERIOD_MEASURED,
            recovery=result.recovery,
            expected_after=date(2026, 10, 2),
        )
    with pytest.raises(ValueError, match="and only it, carries a date"):
        PresentationWindowUnavailableResult(
            session_id="session",
            temporal=control,
            reason_code=TemporalUnavailableReason.PRESET_UNAVAILABLE,
            recovery=result.recovery,
            message="text",
            expected_after=date(2026, 10, 2),
        )
    # Contract rule 3: a pending preset on version 2 carries the server's text too, never a
    # browser sentence; with no text it is refused (gate F7).
    pending = PresentationWindowUnavailableResult(
        session_id="session",
        temporal=control,
        reason_code=TemporalUnavailableReason.PRESET_UNAVAILABLE,
        recovery=result.recovery,
        message=absence_message(
            AbsenceTrigger.SCORECARD_WINDOW_NOT_PUBLISHED,
            period_label=None,
            other_window_ready=False,
        ),
    )
    assert handler._presentation_result_json(pending)["message"] == pending.message
    with pytest.raises(ValueError, match="carries the server's text"):
        PresentationWindowUnavailableResult(
            session_id="session",
            temporal=control,
            reason_code=TemporalUnavailableReason.PRESET_UNAVAILABLE,
            recovery=result.recovery,
        )


def _v1_control() -> TemporalControl:
    resolution = resolve_temporal(
        TemporalResolutionRequest(
            policy=PresentationTemporalPolicy(
                candidate_presets=(SEVEN,),
                default_preset=SEVEN,
                custom_enabled=False,
                custom_min_duration_days=1,
                custom_max_duration_days=90,
                comparison_policy=TemporalComparisonPolicy.REQUIRED_EQUAL_PRIOR,
                display_grains=(PageTime.DAY,),
            ),
            coverage_intervals=(DateRange(date(2026, 8, 1), WINDOW.end),),
        )
    )
    assert isinstance(resolution, TemporalResolutionAvailable)
    return resolution.control


def test_a_version_1_control_carries_none_of_the_version_2_additions(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    handler = presentation_module.module
    control = _v1_control()
    assert control.version == 1
    body = handler._temporal_control_json(control)
    assert set(body["resolved"]) == {  # type: ignore[arg-type]
        "window",
        "reporting_window_status",
        "selection",
        "display_grain",
    }
    assert {key for preset in body["presets"] for key in preset} == {  # type: ignore[union-attr]
        "id",
        "label",
        "availability",
        "reason_code",
    }
    # Paired: a period label on a version-1 control is refused.
    with pytest.raises(ValueError, match="version-1 control can't carry a calendar resolution"):
        TemporalControl(
            version=1,
            mode=control.mode,
            resolved=TemporalResolved(
                window=control.resolved.window,
                reporting_window_status=control.resolved.reporting_window_status,
                selection=control.resolved.selection,
                display_grain=TemporalDisplayGrain.PERIOD,
                period_label="August 2026",
            ),
            comparison=control.comparison,
            presets=control.presets,
            custom=control.custom,
        )


def test_the_wire_shape_of_a_version_2_control(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    handler = presentation_module.module
    body: dict[str, Any] = handler._temporal_control_json(_month())
    assert body["version"] == 2
    assert body["resolved"]["period_label"] == "August 2026"
    assert body["resolved"]["as_of_month"] is None
    assert body["comparison"] == {
        "policy": "optional_equal_prior",
        "status": "not_applicable",
        "reason_code": "calendar_period_selected",
    }
    assert body["presets"][-1] == {
        "id": "last_complete_month",
        "label": "Latest measured month",
        "availability": "available",
        "reason_code": None,
        "reason_text": None,
        "window": {"start": "2026-08-01", "end": "2026-08-31"},
        "period_label": "August 2026",
    }


def _record(control: TemporalControl) -> PageArtifactRecord:
    return PageArtifactRecord(
        reference=PageContextReference("A" * 43),
        artifact=PageArtifact("NetworkScorecardArtifact", 1, canonical_artifact_json({"k": 1})),
        verified_owner_id="owner",
        conversation_session_id="session",
        expires_at=datetime(2026, 9, 16, tzinfo=UTC),
        temporal_control=control,
    )


def test_a_page_record_round_trips_a_version_2_control_and_keeps_version_1_bytes() -> None:
    for control in (_rolling(), _month()):
        assert (
            page_artifact_record_from_json(
                page_artifact_record_to_json(_record(control))
            ).temporal_control
            == control
        )
    v1 = json.loads(page_artifact_record_to_json(_record(_v1_control())))["temporal_control"]
    assert "period_label" not in v1["resolved"]
    assert "window" not in v1["presets"][0]
    # Paired: a version-1 record with a version-2 field is refused by the codec.
    tampered = json.loads(page_artifact_record_to_json(_record(_v1_control())))
    tampered["temporal_control"]["resolved"]["period_label"] = None
    with pytest.raises(ValueError, match="temporal resolved"):
        page_artifact_record_from_json(json.dumps(tampered, sort_keys=True, separators=(",", ":")))


def test_a_version_1_page_still_refuses_a_calendar_preset(
    presentation_module: _PresentationModule,  # noqa: F811
) -> None:
    handler = presentation_module.module
    accepted = handler._page_temporal_selection("campaigns", {"kind": "preset", "preset_id": "7d"})
    assert accepted.preset_id is SEVEN
    assert accepted.version == 1
    with pytest.raises(ValidationError):
        handler._page_temporal_selection(
            "campaigns", {"kind": "preset", "preset_id": "last_complete_month"}
        )
