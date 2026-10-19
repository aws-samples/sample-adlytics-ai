# SPDX-License-Identifier: MIT-0
"""S5 (FR-3): the measurement reconciler's fault controls, over the real adapter (fictional).

Every control drives `DynamoMetricStore` over `FakeDynamo`, which evaluates the adapter's own
condition and update expressions transactionally, and a spy sink whose read-back can be
suppressed. Fault points follow the design: after projection commit, before acceptance, an
ambiguous acceptance response (with a competing acceptance before recovery), condition
conflicts, after local emission before the state change, after `EMITTED_UNCONFIRMED`, and after
sink confirmation before `DELIVERED`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from execution_graph_support import FakeDynamo

from adapters.jobs.operational_status import metric_reconciler
from adapters.jobs.operational_status.metric_reconciler import (
    AcceptOutcome,
    PublishOutcome,
    ReconcileContext,
    accept_one,
    publish_one,
    reconcile,
)
from adapters.outbound.dynamodb.metric_store import AmbiguousCommitError, DynamoMetricStore
from adapters.outbound.telemetry.reporting import Report
from domain.operations.measurement import (
    Gauge,
    Measurement,
    MeasurementCorruptError,
    SnapshotState,
    measurement,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-074")]

TABLE = "execution-graph-probe-fixture"
AT = datetime(2026, 10, 8, 12, 10, tzinfo=UTC)
DIMS = {
    "Protocol": "v1",
    "Surface": "graph",
    "Stage": "run_request_decided",
    "Outcome": "ADMITTED",
    "AuthorityScope": "probe",
}


@dataclass
class SpySink:
    """Record every emission; read back the latest emitted values unless suppressed."""

    emitted: list[tuple[tuple[Gauge, ...], datetime]] = field(
        default_factory=list[tuple[tuple[Gauge, ...], datetime]]
    )
    suppressed: bool = False
    fail_emit: bool = False

    def emit(self, gauges: Sequence[Gauge], timestamp: datetime) -> None:
        if self.fail_emit:
            raise RuntimeError("injected emission failure")
        self.emitted.append((tuple(gauges), timestamp))

    def read(self, gauges: Sequence[Gauge], timestamp: datetime) -> Mapping[Gauge, float | None]:
        del timestamp
        if self.suppressed:
            return dict.fromkeys(gauges)
        latest: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        for batch, _at in self.emitted:
            for gauge in batch:
                key = (gauge.name, gauge.dimensions)
                latest[key] = max(latest.get(key, 0.0), float(gauge.value))
        return {gauge: latest.get((gauge.name, gauge.dimensions)) for gauge in gauges}


def _world() -> tuple[FakeDynamo, DynamoMetricStore, SpySink, list[BaseException]]:
    fake = FakeDynamo()
    store = DynamoMetricStore(TABLE, cast("Any", fake), partition_key="pk", sort_key="sk")
    return fake, store, SpySink(), []


def _context(store: DynamoMetricStore, sink: SpySink, failures: list[BaseException]) -> Any:
    return ReconcileContext(store, sink, sink, lambda: AT, failures.append)


def _contribute(  # noqa: PLR0913, PLR0917 -- one fixture writer
    fake: FakeDynamo, store: DynamoMetricStore, source: str, value: int
) -> Measurement:
    """Write what the projection transaction writes: the contribution, PENDING."""
    contribution = measurement(source, "FlowRootElapsed", DIMS, value, AT)
    fake.transact_write_items(TransactItems=store.contribution_members(contribution))
    return contribution


def _aggregate(store: DynamoMetricStore, contribution: Measurement) -> tuple[int, int, int]:
    current = store.read_aggregate(contribution.aggregate_id)
    return current.revision, current.statistics.count, current.statistics.sum_ms


def _members(fake: FakeDynamo) -> int:
    return sum(1 for pk, _sk in fake.items if pk.startswith("AGGREGATE_MEMBER#"))


def test_crash_after_projection_commit_leaves_recoverable_pending_work() -> None:
    fake, store, sink, failures = _world()
    first = _contribute(fake, store, "RECEIPT#a/RECEIPT", 1_000)
    counts = reconcile(_context(store, sink, failures))
    assert (counts.accepted, counts.failed) == (1, 0)
    assert _aggregate(store, first) == (1, 1, 1_000)
    assert counts.outcomes == {"emitted": 1}


def test_crash_before_acceptance_changes_nothing_and_the_next_pass_accepts_once() -> None:
    fake, store, sink, failures = _world()
    first = _contribute(fake, store, "RECEIPT#a/RECEIPT", 1_000)
    fake.fail_next = True  # the acceptance transaction fails before it commits
    assert accept_one(store, first.measurement_id) is AcceptOutcome.ACCEPTED
    assert accept_one(store, first.measurement_id) is AcceptOutcome.ALREADY_ACCEPTED
    assert _aggregate(store, first) == (1, 1, 1_000)
    assert _members(fake) == 1
    del sink, failures


def test_an_ambiguous_acceptance_with_a_competitor_first_is_proved_from_its_bound_revision() -> (
    None
):
    """A commits with a lost response; B advances the same aggregate before A recovers."""
    fake, store, _sink, _failures = _world()
    a = _contribute(fake, store, "RECEIPT#a/RECEIPT", 1_000)
    b = _contribute(fake, store, "RECEIPT#b/RECEIPT", 4_000)
    original = store.read_acceptance
    reads: list[str] = []

    def competing_read(measurement_id: str) -> Any:
        reads.append(measurement_id)
        if measurement_id == a.measurement_id and len(reads) == 2:
            # A's recovery read: B advances the same aggregate first.
            assert accept_one(store, b.measurement_id) is AcceptOutcome.ACCEPTED
        return original(measurement_id)

    store.read_acceptance = competing_read  # type: ignore[method-assign]
    fake.ambiguous_next = True  # A commits, then its response is lost
    assert accept_one(store, a.measurement_id) is AcceptOutcome.ALREADY_ACCEPTED
    store.read_acceptance = original  # type: ignore[method-assign]
    assert _aggregate(store, a) == (2, 2, 5_000)
    assert _members(fake) == 2
    acceptance = store.read_acceptance(a.measurement_id)
    assert acceptance is not None and acceptance.revision == 1


def test_forced_condition_conflicts_converge_to_every_contribution_exactly_once() -> None:
    fake, store, sink, failures = _world()
    contributions = [
        _contribute(fake, store, f"RECEIPT#c{index}/RECEIPT", 1_000 * (index + 1))
        for index in range(5)
    ]
    fake.conflicts_next = 3
    counts = reconcile(_context(store, sink, failures))
    assert counts.failed == 0
    assert counts.accepted == 5
    revision, count, total = _aggregate(store, contributions[0])
    assert (revision, count, total) == (5, 5, 15_000)
    assert _members(fake) == 5
    again = reconcile(_context(store, sink, failures))
    assert again.accepted == 0
    assert _aggregate(store, contributions[0]) == (5, 5, 15_000)


def test_emission_then_a_crash_before_the_state_change_re_emits_the_same_snapshot() -> None:
    fake, store, sink, failures = _world()
    first = _contribute(fake, store, "RECEIPT#a/RECEIPT", 1_000)
    accept_one(store, first.measurement_id)
    context = _context(store, sink, failures)
    fake.fail_next = True  # mark_emitted fails after the local emission succeeded
    with pytest.raises(AmbiguousCommitError):
        publish_one(context, first.aggregate_id)
    snapshot = store.read_snapshot(first.aggregate_id, 1)
    assert snapshot is not None and snapshot.state is SnapshotState.PENDING
    assert publish_one(context, first.aggregate_id) is PublishOutcome.EMITTED
    assert len(sink.emitted) == 2
    assert sink.emitted[0] == sink.emitted[1]  # byte-identical gauges at the same timestamp


def test_emitted_unconfirmed_is_confirmed_only_from_the_sink_read() -> None:
    fake, store, sink, failures = _world()
    first = _contribute(fake, store, "RECEIPT#a/RECEIPT", 1_000)
    context = _context(store, sink, failures)
    reconcile(context)
    snapshot = store.read_snapshot(first.aggregate_id, 1)
    assert snapshot is not None and snapshot.state is SnapshotState.EMITTED_UNCONFIRMED
    assert publish_one(context, first.aggregate_id) is PublishOutcome.DELIVERED
    delivered = store.read_snapshot(first.aggregate_id, 1)
    assert delivered is not None and delivered.state is SnapshotState.DELIVERED
    receipt = store.read_confirmation(first.aggregate_id, 1)
    assert receipt is not None
    assert store.snapshot_markers(f"METRIC_DUE#{int(first.aggregate_id[:8], 16) % 16}") == ()
    del fake


def test_suppressed_sink_reads_keep_replaying_and_never_deliver() -> None:
    fake, store, sink, failures = _world()
    first = _contribute(fake, store, "RECEIPT#a/RECEIPT", 1_000)
    sink.suppressed = True
    context = _context(store, sink, failures)
    for _ in range(4):
        reconcile(context)
    snapshot = store.read_snapshot(first.aggregate_id, 1)
    assert snapshot is not None and snapshot.state is SnapshotState.EMITTED_UNCONFIRMED
    assert store.read_confirmation(first.aggregate_id, 1) is None
    assert len(sink.emitted) == 4  # emitted, then replayed on every unconfirmed pass
    del fake


def test_a_crash_after_sink_confirmation_before_delivered_confirms_on_the_next_pass() -> None:
    fake, store, sink, failures = _world()
    first = _contribute(fake, store, "RECEIPT#a/RECEIPT", 1_000)
    context = _context(store, sink, failures)
    reconcile(context)  # accepted and emitted
    fake.fail_next = True  # the confirmation transaction fails before commit
    with pytest.raises(AmbiguousCommitError):
        publish_one(context, first.aggregate_id)
    assert store.read_confirmation(first.aggregate_id, 1) is None
    assert publish_one(context, first.aggregate_id) is PublishOutcome.DELIVERED
    assert store.read_confirmation(first.aggregate_id, 1) is not None


def test_the_latest_revision_needs_its_own_confirmation() -> None:
    fake, store, sink, failures = _world()
    first = _contribute(fake, store, "RECEIPT#a/RECEIPT", 1_000)
    context = _context(store, sink, failures)
    reconcile(context)
    reconcile(context)  # revision 1 delivered
    _contribute(fake, store, "RECEIPT#b/RECEIPT", 2_000)
    reconcile(context)  # revision 2 accepted and emitted
    latest = store.read_snapshot(first.aggregate_id, 2)
    assert latest is not None and latest.state is SnapshotState.EMITTED_UNCONFIRMED
    reconcile(context)
    latest = store.read_snapshot(first.aggregate_id, 2)
    assert latest is not None and latest.state is SnapshotState.DELIVERED


def test_partial_evidence_is_corruption_and_never_retried() -> None:
    fake, store, sink, failures = _world()
    first = _contribute(fake, store, "RECEIPT#a/RECEIPT", 1_000)
    accept_one(store, first.measurement_id)
    member = next(key for key in fake.items if key[0].startswith("AGGREGATE_MEMBER#"))
    del fake.items[member]
    with pytest.raises(MeasurementCorruptError):
        accept_one(store, first.measurement_id)
    _contribute(fake, store, "RECEIPT#orphan/RECEIPT", 1)
    orphan = next(
        key
        for key in fake.items
        if key[0].startswith("MEASUREMENT#")
        and key != (f"MEASUREMENT#{first.measurement_id}", "MEASUREMENT")
    )
    del fake.items[orphan]
    counts = reconcile(_context(store, sink, failures))
    assert counts.failed >= 1
    assert any(isinstance(error, MeasurementCorruptError) for error in failures)


def test_an_absorbed_failure_is_reported_on_the_operation_its_alarm_watches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported: list[tuple[BaseException, str]] = []

    def spy(error: BaseException, report: Report) -> None:
        reported.append((error, report.operation))

    monkeypatch.setattr(metric_reconciler, "report_error", spy)
    corrupt = MeasurementCorruptError("fixture")
    # The live context's failure callback.
    metric_reconciler._report(corrupt)  # pyright: ignore[reportPrivateUsage]
    assert reported == [(corrupt, "operational_status.metric_reconciler")]
    stack = (
        Path(__file__).resolve().parents[2]
        / "infra/lib/stacks/operational-status-processor-stack.ts"
    ).read_text(encoding="utf-8")
    assert f"METRIC_RECONCILER_OPERATION = '{metric_reconciler.OPERATION}'" in stack
