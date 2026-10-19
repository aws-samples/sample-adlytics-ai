# SPDX-License-Identifier: MIT-0
"""The measurement reconciler: accept once, publish snapshots, confirm from the sink (FR-3).

One pass, over the fixed `METRIC_DUE#<n>` shards:

1. **Accept.** For each pending contribution, strongly read its acceptance, aggregate revision
   `r`, membership and bound snapshot. Exact bound evidence proves an earlier acceptance
   committed (even after an ambiguous response and even if the aggregate advanced since), so it
   is never retried; the snapshot chain is validated from that revision. Otherwise one
   transaction commits member, aggregate `r -> r+1`, snapshot `r+1` and `ACCEPTED`. A condition
   conflict or an ambiguous response re-reads and decides again, within a bound.
2. **Publish.** For each aggregate with a snapshot to deliver, take the snapshot its strongly read
   current revision names (never wall time, never a sibling row). `PENDING` is emitted, then
   moved to `EMITTED_UNCONFIRMED`; local success is never delivery.
3. **Confirm.** `EMITTED_UNCONFIRMED` is read back with `GetMetricData`. Only when every expected
   gauge is present at least at its value does one transaction write the immutable
   `SINK_CONFIRMATION#` receipt and move the snapshot to `DELIVERED`. Otherwise the same
   deterministic snapshot is emitted again, and it stays gate-blocking until confirmed.

The deployed handler runs over the status table (`AuthorityScope=live`). The probe runner runs
`reconcile` itself over the probe table, with its own sink and fault injection (P5).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from typing import Any, Final

import boto3
from aws_lambda_powertools import Logger

from adapters.config import operational_projector_settings
from adapters.outbound.dynamodb.metric_store import (
    METRIC_DUE_SHARDS,
    AcceptanceConflictError,
    AmbiguousCommitError,
    DynamoMetricStore,
    SinkConfirmation,
    gauge_key,
)
from adapters.outbound.telemetry.measurement_sink import (
    PERIOD,
    CloudWatchMeasurementReader,
    EmfMeasurementSink,
    MeasurementReader,
    MeasurementSink,
)
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_operation,
)
from domain.operations.measurement import (
    MeasurementCorruptError,
    RecoveryVerdict,
    SnapshotState,
    confirmed,
    gauges,
    next_snapshot,
    recovery_verdict,
    validate_chain,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode

__all__ = [
    "MAX_ACCEPT_ATTEMPTS",
    "AcceptOutcome",
    "PassCounts",
    "PublishOutcome",
    "ReconcileContext",
    "accept_one",
    "handler",
    "publish_one",
    "reconcile",
]

OPERATION: Final = "operational_status.metric_reconciler"
MAX_ACCEPT_ATTEMPTS: Final = 8
_EXHAUSTED: Final = ErrorCode("MEASUREMENT", 22)


class AcceptOutcome(StrEnum):
    """What one acceptance attempt proved."""

    ACCEPTED = "accepted"
    ALREADY_ACCEPTED = "already_accepted"


class PublishOutcome(StrEnum):
    """Where one aggregate's latest snapshot stands after this pass."""

    EMITTED = "emitted"
    UNCONFIRMED = "unconfirmed"
    DELIVERED = "delivered"


@dataclass(frozen=True, slots=True)
class ReconcileContext:
    """The store, the sink and its reader, the clock and the failure report."""

    store: DynamoMetricStore
    sink: MeasurementSink
    reader: MeasurementReader
    now: Callable[[], datetime]
    on_failure: Callable[[BaseException], None]


@dataclass(slots=True)
class PassCounts:
    """One pass's bounded counts."""

    accepted: int = 0
    already_accepted: int = 0
    outcomes: dict[str, int] = field(default_factory=dict[str, int])
    failed: int = 0


def accept_one(store: DynamoMetricStore, measurement_id: str) -> AcceptOutcome:
    """Accept one contribution exactly once, or prove from evidence that it already was."""
    contribution = store.read_measurement(measurement_id)
    if contribution is None:
        raise MeasurementCorruptError("a pending acceptance names no contribution.")
    aggregate = contribution.aggregate_id
    for _attempt in range(MAX_ACCEPT_ATTEMPTS):
        acceptance = store.read_acceptance(measurement_id)
        if acceptance is None:
            raise MeasurementCorruptError("a contribution has no acceptance record.")
        current = store.read_aggregate(aggregate)
        member = store.member_present(aggregate, measurement_id)
        bound = (
            None
            if acceptance.revision is None
            else store.read_snapshot(aggregate, acceptance.revision)
        )
        verdict = recovery_verdict(contribution, acceptance, member, bound, current)
        if verdict is RecoveryVerdict.ACCEPTED:
            revision = acceptance.revision
            assert revision is not None  # noqa: S101 -- an ACCEPTED verdict proves it
            validate_chain(store.read_chain(aggregate, revision), revision, current)
            store.retire_marker(measurement_id)
            return AcceptOutcome.ALREADY_ACCEPTED
        try:
            store.accept(contribution, current, next_snapshot(current, contribution))
        except AcceptanceConflictError, AmbiguousCommitError:
            continue
        return AcceptOutcome.ACCEPTED
    raise DependencyError(_EXHAUSTED, "an acceptance did not settle within its bound.")


def publish_one(context: ReconcileContext, aggregate_id: str) -> PublishOutcome:
    """Emit, confirm or re-emit the latest snapshot of one aggregate."""
    store = context.store
    current = store.read_aggregate(aggregate_id)
    snapshot = store.read_snapshot(aggregate_id, current.revision)
    if snapshot is None or snapshot.statistics != current.statistics:
        raise MeasurementCorruptError("the aggregate's current revision names no exact snapshot.")
    family, dims, window_start = store.read_family(aggregate_id)
    expected = gauges(family, dims, snapshot.statistics)
    if snapshot.state is SnapshotState.DELIVERED:
        return PublishOutcome.DELIVERED
    if snapshot.state is SnapshotState.PENDING:
        context.sink.emit(expected, window_start)
        store.mark_emitted(aggregate_id, snapshot.revision, context.now().isoformat())
        return PublishOutcome.EMITTED
    observed = context.reader.read(expected, window_start)
    if not confirmed(expected, observed):
        context.sink.emit(expected, window_start)
        return PublishOutcome.UNCONFIRMED
    start = window_start.replace(second=0, microsecond=0)
    store.confirm(
        SinkConfirmation(
            aggregate_id=aggregate_id,
            revision=snapshot.revision,
            query_start=start,
            query_end=start + PERIOD,
            values=tuple((gauge_key(gauge), float(observed[gauge] or 0)) for gauge in expected),
            confirmed_at=context.now().isoformat(),
        )
    )
    return PublishOutcome.DELIVERED


def reconcile(context: ReconcileContext) -> PassCounts:
    """Run one pass over every metric due shard: accept, then publish and confirm."""
    counts = PassCounts()
    shards = tuple(f"METRIC_DUE#{index}" for index in range(METRIC_DUE_SHARDS))
    for shard in shards:
        for measurement_id in context.store.pending(shard):
            try:
                outcome = accept_one(context.store, measurement_id)
            except Exception as error:
                context.on_failure(error)
                counts.failed += 1
                continue
            if outcome is AcceptOutcome.ACCEPTED:
                counts.accepted += 1
            else:
                counts.already_accepted += 1
    for shard in shards:
        for aggregate_id, _revision in context.store.snapshot_markers(shard):
            try:
                published = publish_one(context, aggregate_id)
            except Exception as error:
                context.on_failure(error)
                counts.failed += 1
                continue
            counts.outcomes[published.value] = counts.outcomes.get(published.value, 0) + 1
    return counts


_LOGGER: Final = Logger(service="adlytics-operational-status-metric-reconciler")


@lru_cache(maxsize=1)
def _live_context() -> ReconcileContext:
    """Resolve the live store, sink and reader once per execution environment."""
    settings = operational_projector_settings()
    return ReconcileContext(
        DynamoMetricStore(settings.status_table, boto3.client("dynamodb")),
        EmfMeasurementSink(),
        CloudWatchMeasurementReader(boto3.client("cloudwatch")),
        lambda: datetime.now(UTC),
        _report,
    )


def _report(error: BaseException) -> None:
    report_error(error, Report(operation=OPERATION, correlation_id=OPERATION, logger=_LOGGER))


@flushes_metrics
def handler(_event: dict[str, Any], _context: object) -> dict[str, object]:
    """Run one live pass and report its terminal outcome once."""
    try:
        counts = reconcile(_live_context())
    except Exception as error:
        _report(error)
        raise
    details: dict[str, object] = {
        "accepted": counts.accepted,
        "already_accepted": counts.already_accepted,
        **counts.outcomes,
        "failed": counts.failed,
    }
    report_operation(
        OperationLog(
            operation=OPERATION,
            component="operational_status_metric_reconciler",
            phase="terminal",
            outcome="completed_with_failures" if counts.failed else "completed",
            severity=LogSeverity.WARNING if counts.failed else LogSeverity.INFO,
            details=details,
        ),
        _LOGGER,
    )
    return details
