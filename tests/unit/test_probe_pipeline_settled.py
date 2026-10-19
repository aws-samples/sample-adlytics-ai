# SPDX-License-Identifier: MIT-0
"""The pipeline-settled decision over recorded reads, and its seal-guard report contract."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import probe_pipeline_settled as probe  # noqa: E402

from adapters.jobs.land_fixtures import job as land_job  # noqa: E402
from domain.curation.landing import LandingError  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-086")]

_EMPTY = probe.QueueDepth(0, 0, 0)
_DLQS = {f"{name}-dlq": 2 for name in probe.QUEUES}
_POINTERS = {"campaigns#7d": "a" * 64, "retail-health#7d": "b" * 64}


def _read(
    *,
    queues: dict[str, probe.QueueDepth] | None = None,
    dead_letters: dict[str, int] | None = None,
    pointers: dict[str, str] | None = None,
) -> probe.PipelineRead:
    depths: dict[str, probe.QueueDepth] = dict.fromkeys(probe.QUEUES, _EMPTY)
    depths.update(queues or {})
    return probe.PipelineRead(
        depths, {**_DLQS, **(dead_letters or {})}, {**_POINTERS, **(pointers or {})}
    )


def test_a_quiet_pipeline_is_settled() -> None:
    assert probe.settled_findings(_read(), _read(), _read()) == ()


def test_a_non_empty_bake_queue_is_not_settled() -> None:
    busy = _read(queues={"adlytics-dev-preset-bake.fifo": probe.QueueDepth(0, 3, 0)})
    assert probe.settled_findings(_read(), _read(), busy) == (
        "second read: adlytics-dev-preset-bake.fifo holds visible=0 in_flight=3 delayed=0",
    )


def test_a_dead_letter_rise_is_not_settled() -> None:
    risen = _read(dead_letters={"adlytics-dev-ingestion-dlq": 3})
    findings = probe.settled_findings(_read(), risen, risen)
    assert findings == (
        "first read: dead-letter queue adlytics-dev-ingestion-dlq moved 2 -> 3",
        "second read: dead-letter queue adlytics-dev-ingestion-dlq moved 2 -> 3",
    )


def test_a_recently_advanced_pointer_is_not_settled() -> None:
    advanced = _read(pointers={"campaigns#7d": "c" * 64})
    assert probe.settled_findings(_read(), advanced, advanced) == (
        "pointer campaigns#7d advanced during the observation window",
    )
    assert "a pointer advanced between the two end reads" in probe.settled_findings(
        _read(), _read(), advanced
    )


def test_the_report_is_the_shape_the_seal_guard_accepts_and_refuses() -> None:
    checked = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    settled = probe.settled_report(
        environment="dev", account="000000000000", checked_at=checked, findings=()
    )
    land_job.check_settled_report(
        settled, environment="dev", account="000000000000", now=checked + timedelta(minutes=5)
    )
    unsettled = probe.settled_report(
        environment="dev",
        account="000000000000",
        checked_at=checked,
        findings=("second read: adlytics-dev-preset-bake.fifo holds visible=1",),
    )
    with pytest.raises(LandingError, match="not settled"):
        land_job.check_settled_report(
            unsettled, environment="dev", account="000000000000", now=checked
        )


_QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/111122223333/"


def test_an_unprovisioned_registration_queue_is_omitted_only_on_stack_evidence() -> None:
    without = frozenset(
        {f"{_QUEUE_URL}adlytics-dev-ingestion", f"{_QUEUE_URL}adlytics-dev-ingestion-dlq"}
    )
    queues = probe.provisioned_queues(without)
    assert "adlytics-dev-registration" not in queues
    assert queues == tuple(name for name in probe.QUEUES if name != "adlytics-dev-registration")
    # Positive control: a stack that owns the registration queue keeps all five.
    with_it = without | {f"{_QUEUE_URL}adlytics-dev-registration"}
    assert probe.provisioned_queues(with_it) == probe.QUEUES
    # A read of the wrong stack (no ingestion queue) is refused, never read as "not provisioned".
    with pytest.raises(probe.ProbeError, match="does not own the ingestion queue"):
        probe.provisioned_queues(frozenset({f"{_QUEUE_URL}adlytics-dev-registration"}))


def test_only_the_provisioned_queues_are_required_to_be_read() -> None:
    expected = probe.provisioned_queues(frozenset({f"{_QUEUE_URL}adlytics-dev-ingestion"}))
    reads = probe.PipelineRead(dict.fromkeys(expected, _EMPTY), {}, {})
    assert probe.settled_findings(reads, reads, reads, expected) == ()
    # The default still requires all five, so a missing registration read is named.
    assert any(
        "registration was not read" in item for item in probe.settled_findings(reads, reads, reads)
    )


_NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
_OLD = dict.fromkeys(_POINTERS, _NOW - timedelta(minutes=30))


def _observe(reads: list[probe.PipelineRead], *, max_seconds: float = 600) -> probe.Observation:
    clock = [0.0]
    queue = list(reads)

    def read() -> probe.PipelineRead:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    return probe.observe_until_settled(
        read=read,
        baseline=_read(),
        expected=probe.QUEUES,
        quiet_seconds=180,
        max_seconds=max_seconds,
        sleep=sleep,
        monotonic=lambda: clock[0],
        now=lambda: _NOW,
    )


def _dated(**changes: datetime | None) -> probe.PipelineRead:
    base = _read()
    return probe.PipelineRead(
        dict(base.queues), dict(base.dead_letters), dict(base.pointers), {**_OLD, **changes}
    )


def test_an_idle_pipeline_settles_on_the_first_pair_of_reads() -> None:
    observation = _observe([_dated(), _dated()])
    assert observation.findings == ()
    assert observation.elapsed_seconds == 60  # one read gap, not the 10-minute window


def test_a_busy_pipeline_does_not_settle_and_times_out_at_the_maximum() -> None:
    busy = _read(queues={"adlytics-dev-preset-bake.fifo": probe.QueueDepth(4, 1, 0)})
    busy = probe.PipelineRead(busy.queues, busy.dead_letters, busy.pointers, _OLD)
    observation = _observe([busy], max_seconds=600)
    assert observation.elapsed_seconds == 600
    assert any("preset-bake.fifo holds visible=4" in item for item in observation.findings)


def test_a_pointer_advancing_inside_the_quiet_period_blocks_settle() -> None:
    recent = _dated(**{"campaigns#7d": _NOW - timedelta(seconds=90)})
    assert probe.quiet_findings(recent, _NOW, 180) == (
        "pointer campaigns#7d advanced 90 s ago, inside the 180 s quiet period",
    )
    observation = _observe([recent], max_seconds=300)
    assert observation.findings and observation.elapsed_seconds == 300
    # Positive control: the same pointer outside the quiet period settles at once.
    assert _observe([_dated(**{"campaigns#7d": _NOW - timedelta(seconds=181)})]).findings == ()
    # A pointer that cannot be dated never counts as quiet.
    assert probe.quiet_findings(_dated(**{"campaigns#7d": None}), _NOW, 180) == (
        "pointer campaigns#7d has no measurable last advance",
    )


def test_a_pointer_change_between_the_two_reads_still_blocks_settle() -> None:
    moved = _dated()
    moved = probe.PipelineRead(
        moved.queues, moved.dead_letters, {**moved.pointers, "campaigns#7d": "c" * 64}, _OLD
    )
    observation = _observe([_dated(), moved], max_seconds=60)
    assert "a pointer advanced between the two end reads" in observation.findings
