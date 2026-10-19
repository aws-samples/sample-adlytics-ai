# SPDX-License-Identifier: MIT-0
"""Execution-graph protocol gauges: the stuck-flow sweep and the `PROMOTE#` age (S5).

One namespace (`Adlytics`), closed dimensions only: `Protocol` (the one active protocol version)
and `AuthorityScope` (`live` for the live graph table, `probe` for the dev probe table, so a probe
control can never move a live alarm). No identifier is ever a dimension; the per-shard evidence
naming each counted flow goes to the detector's terminal log instead.

`stuck_flow_datapoints` is the pure rendering both emitters share: EMF from the deployed detector
(`emit_stuck_flow_reading`), and `PutMetricData` from the probe runner, which has no Lambda log
stream. Each datapoint is a gauge read with `Maximum`; the four bake gauges (plan 6.1) ride the same
sweep and the same closed dimensions.

Addendum A2 signals (`emit_bake_trace_signal`): `GraphBakeTelemetryLoss` counts a bake telemetry
emission that failed (the business result is unchanged, A2.2) and `GraphBakeTraceIncomplete`
counts a succeeded Athena execution whose `athena.query` span carries `statistics_unavailable`
(A2.4). Both are event counts with the same closed dimensions, emitted only when non-zero.

`emit_serving_withheld` (ADR-139) is a 0/1 gauge with the same closed dimensions: `1` for a
finalized cohort whose binding is withheld (a failure kept its pages on last-good), `0` only on a
fresh non-withheld commit, which is what clears `GraphServingWithheld`.
"""

from __future__ import annotations

import resource
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from aws_lambda_powertools.metrics import MetricUnit, single_metric

from domain.operations.generated_execution_graphs import PROTOCOL_VERSION
from domain.operations.stuck_flow import SweepReading

if TYPE_CHECKING:
    from types_boto3_cloudwatch.client import CloudWatchClient

__all__ = [
    "BAKE_FINALIZER_BLOCKED_METRIC",
    "BAKE_PRECOMPUTE_BLOCKED_METRIC",
    "BAKE_QUERY_COUNT_METRIC",
    "BAKE_RETRY_EXHAUSTED_METRIC",
    "BAKE_TASK_FAILED_METRIC",
    "BAKE_TASK_MEMORY_METRIC",
    "BAKE_TASK_SECONDS_METRIC",
    "BAKE_TELEMETRY_LOSS_METRIC",
    "BAKE_TRACE_INCOMPLETE_METRIC",
    "BAKE_WRITE_PAIRS_METRIC",
    "CUTOVER_GATE_METRIC",
    "NAMESPACE",
    "POST_GRANT_INVARIANT_METRIC",
    "PROMOTE_AGE_METRIC",
    "SERVING_WITHHELD_METRIC",
    "STUCK_FLOW_METRIC",
    "UNCLOCKED_METRIC",
    "AuthorityScope",
    "BakeTaskReading",
    "Datapoint",
    "bake_task_datapoints",
    "emit_bake_task",
    "emit_bake_trace_signal",
    "emit_cutover_drop",
    "emit_post_grant_invariant",
    "emit_serving_withheld",
    "emit_stuck_flow_reading",
    "process_memory_peak_mb",
    "put_probe_stuck_flow_reading",
    "stuck_flow_datapoints",
]

NAMESPACE: Final = "Adlytics"
STUCK_FLOW_METRIC: Final = "OpenFlowsPastProtocolCeiling"
UNCLOCKED_METRIC: Final = "OpenFlowsWithoutRootClock"
PROMOTE_AGE_METRIC: Final = "PromoteObligationAgeSeconds"
POST_GRANT_INVARIANT_METRIC: Final = "GraphPostGrantInvariant"
CUTOVER_GATE_METRIC: Final = "LegacyWorkAfterCutover"
BAKE_TASK_SECONDS_METRIC: Final = "GraphBakeTaskSeconds"
BAKE_TASK_MEMORY_METRIC: Final = "GraphBakeTaskMemoryPeakMb"
BAKE_QUERY_COUNT_METRIC: Final = "GraphBakeQueryCount"
BAKE_WRITE_PAIRS_METRIC: Final = "GraphBakeWritePairs"
BAKE_TASK_FAILED_METRIC: Final = "GraphBakeTaskFailed"
BAKE_RETRY_EXHAUSTED_METRIC: Final = "GraphBakeRetryExhausted"
BAKE_PRECOMPUTE_BLOCKED_METRIC: Final = "GraphBakePrecomputeBlocked"
BAKE_FINALIZER_BLOCKED_METRIC: Final = "GraphBakeFinalizerBlocked"
BAKE_TELEMETRY_LOSS_METRIC: Final = "GraphBakeTelemetryLoss"
BAKE_TRACE_INCOMPLETE_METRIC: Final = "GraphBakeTraceIncomplete"
_BAKE_TRACE_SIGNALS: Final = frozenset({BAKE_TELEMETRY_LOSS_METRIC, BAKE_TRACE_INCOMPLETE_METRIC})
SERVING_WITHHELD_METRIC: Final = "GraphServingWithheld"


class AuthorityScope(StrEnum):
    """Which graph table a reading came from (closed)."""

    LIVE = "live"
    PROBE = "probe"


@dataclass(frozen=True, slots=True)
class Datapoint:
    """One gauge value with its closed dimensions."""

    name: str
    value: float
    unit: str
    dimensions: tuple[tuple[str, str], ...]


def _dimensions(scope: AuthorityScope) -> tuple[tuple[str, str], ...]:
    return (("Protocol", str(PROTOCOL_VERSION)), ("AuthorityScope", scope.value))


def stuck_flow_datapoints(reading: SweepReading, scope: AuthorityScope) -> tuple[Datapoint, ...]:
    """Render one complete sweep as its three protocol gauges and its four bake gauges."""
    dimensions = _dimensions(scope)
    bake = (
        (BAKE_TASK_FAILED_METRIC, reading.bake_tasks_failed),
        (BAKE_RETRY_EXHAUSTED_METRIC, reading.bake_retry_exhausted),
        (BAKE_PRECOMPUTE_BLOCKED_METRIC, reading.bake_precompute_blocked),
        (BAKE_FINALIZER_BLOCKED_METRIC, reading.bake_finalizer_blocked),
    )
    return (
        Datapoint(STUCK_FLOW_METRIC, float(reading.past_ceiling_count), "Count", dimensions),
        Datapoint(UNCLOCKED_METRIC, float(reading.without_root_clock_count), "Count", dimensions),
        Datapoint(PROMOTE_AGE_METRIC, reading.promotion_age_seconds, "Seconds", dimensions),
        *(Datapoint(name, float(len(items)), "Count", dimensions) for name, items in bake),
    )


def emit_stuck_flow_reading(reading: SweepReading, scope: AuthorityScope) -> None:
    """Emit each gauge as its own isolated EMF record, flushed at emission."""
    for point in stuck_flow_datapoints(reading, scope):
        with single_metric(
            name=point.name,
            unit=MetricUnit(point.unit),
            value=point.value,
            namespace=NAMESPACE,
        ) as emitted:
            for name, value in point.dimensions:
                emitted.add_dimension(name=name, value=value)


def emit_post_grant_invariant(signal: str) -> None:
    """Count one POST_GRANT invariant (S6 §2.7, D23), as its own EMF record, flushed at emission.

    Dimensioned only on the protocol and the closed signal (`post_grant_oversize`), never on a
    run or node identifier; the dispatcher's terminal log names the node.
    """
    with single_metric(
        name=POST_GRANT_INVARIANT_METRIC, unit=MetricUnit.Count, value=1, namespace=NAMESPACE
    ) as emitted:
        emitted.add_dimension(name="Protocol", value=str(PROTOCOL_VERSION))
        emitted.add_dimension(name="Signal", value=signal)


@dataclass(frozen=True, slots=True)
class BakeTaskReading:
    """One finished roster task's measured cost (plan 4.7), dimensioned only on closed enums."""

    page: str
    variant: str
    effect_kind: str
    seconds: float
    memory_peak_mb: float
    query_count: int
    write_pairs: int


def bake_task_datapoints(reading: BakeTaskReading) -> tuple[Datapoint, ...]:
    """Render one task as its four datapoints.

    The dimensions are the roster's page and variant (the contract-governed roster vocabulary)
    and the closed effect kind; never a run, task, grant or publication identifier.
    """
    dimensions = (
        ("Page", reading.page),
        ("Variant", reading.variant),
        ("EffectKind", reading.effect_kind),
    )
    return (
        Datapoint(BAKE_TASK_SECONDS_METRIC, reading.seconds, "Seconds", dimensions),
        Datapoint(BAKE_TASK_MEMORY_METRIC, reading.memory_peak_mb, "Megabytes", dimensions),
        Datapoint(BAKE_QUERY_COUNT_METRIC, float(reading.query_count), "Count", dimensions),
        Datapoint(BAKE_WRITE_PAIRS_METRIC, float(reading.write_pairs), "Count", dimensions),
    )


def emit_bake_task(reading: BakeTaskReading) -> None:
    """Emit one finished task's datapoints, each its own EMF record flushed at emission."""
    for point in bake_task_datapoints(reading):
        with single_metric(
            name=point.name,
            unit=MetricUnit(point.unit),
            value=point.value,
            namespace=NAMESPACE,
        ) as emitted:
            for name, value in point.dimensions:
                emitted.add_dimension(name=name, value=value)


def emit_bake_trace_signal(name: str, count: int) -> None:
    """Count one A2 signal (telemetry loss or trace incompleteness), flushed at emission.

    Dimensioned only on the protocol and `AuthorityScope=live` (the Baker bakes the live table
    only); never on a run, task or page identifier. The bake's summary line names the task.
    """
    if name not in _BAKE_TRACE_SIGNALS:
        raise ValueError("an A2 bake signal is one of the two closed names.")
    with single_metric(
        name=name, unit=MetricUnit.Count, value=count, namespace=NAMESPACE
    ) as emitted:
        for dimension, value in _dimensions(AuthorityScope.LIVE):
            emitted.add_dimension(name=dimension, value=value)


def emit_serving_withheld(count: Literal[0, 1]) -> None:
    """Record whether one finalized cohort's binding was withheld (1) or freshly served (0).

    Dimensioned only on the protocol and `AuthorityScope=live`; the finalizer's
    `finalize serving_withheld` line names the run and its failure reasons.
    """
    with single_metric(
        name=SERVING_WITHHELD_METRIC, unit=MetricUnit.Count, value=count, namespace=NAMESPACE
    ) as emitted:
        for dimension, value in _dimensions(AuthorityScope.LIVE):
            emitted.add_dimension(name=dimension, value=value)


def process_memory_peak_mb() -> float:
    """Return this process's peak resident memory in MB (`ru_maxrss`: KiB on Linux)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024  # macOS reports bytes
    return round(peak / divisor, 1)


def emit_cutover_drop(signal: str, count: int) -> None:
    """Count legacy work a GRAPH gate dropped with zero effect (S6 §2.9, D6), flushed at once.

    `Signal` is closed: `STREAM_TRIGGER_AFTER_CUTOVER`, `LEGACY_BAKE_AFTER_CUTOVER` or
    `BACKFILL_AFTER_CUTOVER`. The value is the number of records or intents dropped.
    """
    with single_metric(
        name=CUTOVER_GATE_METRIC, unit=MetricUnit.Count, value=count, namespace=NAMESPACE
    ) as emitted:
        emitted.add_dimension(name="Protocol", value=str(PROTOCOL_VERSION))
        emitted.add_dimension(name="Signal", value=signal)


def put_probe_stuck_flow_reading(client: CloudWatchClient, reading: SweepReading) -> None:
    """Put one sweep's gauges for the probe runner, always `AuthorityScope=probe`.

    It takes no scope. IAM conditions the probe role's `PutMetricData` on the namespace only,
    which live and probe share, so this is the one place a probe reading is rendered and it can
    only render probe scope: a probe-written live zero would turn a dead live detector `OK`.
    """
    data = [
        {
            "MetricName": point.name,
            "Dimensions": [{"Name": name, "Value": value} for name, value in point.dimensions],
            "Value": point.value,
            "Unit": point.unit,
        }
        for point in stuck_flow_datapoints(reading, AuthorityScope.PROBE)
    ]
    client.put_metric_data(Namespace=NAMESPACE, MetricData=cast("Any", data))
