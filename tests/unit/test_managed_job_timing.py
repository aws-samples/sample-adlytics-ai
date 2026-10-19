# SPDX-License-Identifier: MIT-0
"""BF-11: `InJobTimingManifest.v1` binds to its exact provider job, then one span set."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domain.operations.managed_job_timing import (
    JOB_SPAN,
    STAGE_SPAN,
    ManagedJobTimingError,
    ProviderTerminal,
    SpanLink,
    TimingOutcome,
    validate_timing_manifest,
    verify_after_terminal,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074")]

STAGES = ("extract", "load")


def manifest(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "manifest_version": "InJobTimingManifest.v1",
        "provider_job_id": "job-fictional-1",
        "executor": "SQS_LAMBDA",
        "clock_source": "wall_clock",
        "stages": [
            {
                "stage": "extract",
                "started_at": "2026-10-07T09:00:00+00:00",
                "ended_at": "2026-10-07T09:00:05+00:00",
            },
            {
                "stage": "load",
                "started_at": "2026-10-07T09:00:05+00:00",
                "ended_at": "2026-10-07T09:00:09+00:00",
            },
        ],
    }
    base.update(overrides)
    return base


def test_bf11_an_exact_manifest_yields_one_span_set_and_a_replay_dedupes() -> None:
    first = validate_timing_manifest(manifest(), "job-fictional-1", STAGES)
    again = validate_timing_manifest(manifest(), "job-fictional-1", STAGES)
    assert [stage.stage for stage in first.stages] == ["extract", "load"]
    assert first.clock_source == "wall_clock"
    assert first.digest == again.digest
    other_clock = validate_timing_manifest(
        manifest(clock_source="monotonic"), "job-fictional-1", STAGES
    )
    assert other_clock.digest != first.digest


@pytest.mark.parametrize(
    ("overrides", "provider"),
    [
        ({}, "job-other"),
        ({"executor": "MANAGED_ECS"}, "job-fictional-1"),
        ({"manifest_version": "v0"}, "job-fictional-1"),
        ({"extra": "x"}, "job-fictional-1"),
        ({"clock_source": "provider_guess"}, "job-fictional-1"),
        (
            {
                "stages": [
                    {
                        "stage": "undeclared",
                        "started_at": "2026-10-07T09:00:00+00:00",
                        "ended_at": "2026-10-07T09:00:01+00:00",
                    }
                ]
            },
            "job-fictional-1",
        ),
        (
            {
                "stages": [
                    {
                        "stage": "load",
                        "started_at": "2026-10-07T09:00:05+00:00",
                        "ended_at": "2026-10-07T09:00:01+00:00",
                    }
                ]
            },
            "job-fictional-1",
        ),
        ({"stages": []}, "job-fictional-1"),
    ],
)
def test_bf11_a_manifest_for_another_job_or_an_undeclared_stage_is_refused(
    overrides: dict[str, object], provider: str
) -> None:
    with pytest.raises(ManagedJobTimingError) as raised:
        validate_timing_manifest(manifest(**overrides), provider, STAGES)
    assert raised.value.signal == "MANAGED_JOB_TIMING_INVALID"


# S5 half (BF-11): the verifier and span synthesizer after provider terminal.

ROOT = "root-fictional-1"


def terminal(**overrides: object) -> ProviderTerminal:
    values: dict[str, object] = {
        "provider_job_id": "job-fictional-1",
        "executor": "SQS_LAMBDA",
        "started_at": datetime(2026, 10, 7, 8, 59, 59, tzinfo=UTC),
        "terminal_at": datetime(2026, 10, 7, 9, 0, 10, tzinfo=UTC),
        "state": "SUCCEEDED",
    }
    values.update(overrides)
    return ProviderTerminal(**values)  # pyright: ignore[reportArgumentType]


def test_bf11_an_exact_terminal_and_manifest_emit_one_span_set_with_declared_links() -> None:
    result = verify_after_terminal(manifest(), terminal(), STAGES, ROOT, frozenset())
    assert result.outcome is TimingOutcome.SPANS
    assert result.gap is None
    job, *stages = result.spans
    assert job.name == JOB_SPAN
    assert job.links == (SpanLink("follows_from", ROOT),)
    assert (job.started_at, job.ended_at) == (terminal().started_at, terminal().terminal_at)
    assert [span.stage for span in stages] == ["extract", "load"]
    assert all(span.name == STAGE_SPAN for span in stages)
    assert all(span.links == (SpanLink("child_of", job.span_id),) for span in stages)
    assert len({span.span_id for span in result.spans}) == len(result.spans)
    again = verify_after_terminal(manifest(), terminal(), STAGES, ROOT, frozenset())
    assert [span.span_id for span in again.spans] == [span.span_id for span in result.spans]


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED", "CANCELLED"])
def test_bf11_every_provider_terminal_state_yields_spans(state: str) -> None:
    result = verify_after_terminal(manifest(), terminal(state=state), STAGES, ROOT, frozenset())
    assert result.outcome is TimingOutcome.SPANS


def test_bf11_a_replay_of_an_emitted_manifest_emits_no_duplicate() -> None:
    first = verify_after_terminal(manifest(), terminal(), STAGES, ROOT, frozenset())
    assert first.digest is not None
    replay = verify_after_terminal(manifest(), terminal(), STAGES, ROOT, frozenset({first.digest}))
    assert replay.outcome is TimingOutcome.ALREADY_EMITTED
    assert replay.spans == ()
    assert replay.digest == first.digest


@pytest.mark.parametrize(
    ("overrides", "provider"),
    [
        ({}, {"provider_job_id": "job-other"}),
        ({}, {"executor": "MANAGED_ECS"}),
        ({}, {"terminal_at": datetime(2026, 10, 7, 9, 0, 8, tzinfo=UTC)}),
        ({}, {"started_at": datetime(2026, 10, 7, 9, 0, 1, tzinfo=UTC)}),
        # The provider record is not terminal yet.
        ({}, {"state": "RUNNING"}),
        ({}, {"state": "succeeded"}),
        # A monotonic manifest cannot be placed in the provider's wall-clock interval.
        ({"clock_source": "monotonic"}, {}),
        (
            {
                "stages": [
                    {
                        "stage": "undeclared",
                        "started_at": "2026-10-07T09:00:00+00:00",
                        "ended_at": "2026-10-07T09:00:01+00:00",
                    }
                ]
            },
            {},
        ),
    ],
)
def test_bf11_a_manifest_bound_elsewhere_or_with_an_undeclared_stage_records_a_gap_and_no_span(
    overrides: dict[str, object], provider: dict[str, object]
) -> None:
    result = verify_after_terminal(
        manifest(**overrides), terminal(**provider), STAGES, ROOT, frozenset()
    )
    assert result.outcome is TimingOutcome.GAP
    assert result.spans == ()
    assert result.gap is not None
    assert result.gap.signal == "MANAGED_JOB_TIMING_INVALID"
    assert result.gap.provider_job_id == terminal(**provider).provider_job_id
