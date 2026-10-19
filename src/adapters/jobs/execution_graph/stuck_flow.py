# SPDX-License-Identifier: MIT-0
"""The stuck-flow detector: one complete strongly consistent sweep, then its gauges (S5).

It reads every fixed due shard (`DUE#<n>`) and the hold, folds them with
`domain.operations.stuck_flow.sweep`, and emits `OpenFlowsPastProtocolCeiling`,
`OpenFlowsWithoutRootClock` and `PromoteObligationAgeSeconds` with `AuthorityScope`, and the four
bake gauges (plan 6.1): `GraphBakeTaskFailed`, `GraphBakeRetryExhausted`,
`GraphBakePrecomputeBlocked` and `GraphBakeFinalizerBlocked` (ADR-140 also anchors the blocked pair
on the stored bake contribution). A shard or contribution read that fails is reported once and
the sweep yields no value at all, never a zero, so the live stuck-flow alarm, which treats missing
data as breaching, fires on a detector that cannot read.

Unlike the five graph workers it runs in every mode: it only reads, and in `LEGACY` and `SHADOW`
the live table holds no flow obligation, so a healthy sweep reads zero there. Its role holds
`Query` on the `DUE#` partitions and `GetItem` on the mode item, nothing else.

The deployed handler sweeps the live table. The probe runner calls `detect` itself under the
probe role against the probe table, with `AuthorityScope=probe` and its own emitter (P5).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Final, Protocol

import boto3
from aws_lambda_powertools import Logger

from adapters.config import run_request_api_settings
from adapters.jobs.execution_graph.fence_settler import due_shards
from adapters.outbound.dynamodb.due_shards import DynamoDueShardReader
from adapters.outbound.telemetry.graph_metrics import AuthorityScope, emit_stuck_flow_reading
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_operation,
)
from domain.operations.execution_graph_release import CohortMember
from domain.operations.stuck_flow import (
    ContributionObservation,
    DueObservation,
    HoldState,
    SweepIncompleteError,
    SweepReading,
    bake_contribution_candidates,
    sweep,
    unarmed_contribution_candidates,
)
from ports.outbound.execution_graph_dispatch import GraphStoreUnavailableError

__all__ = ["DetectorContext", "DueShardReader", "detect", "handler"]

OPERATION: Final = "execution_graph.stuck_flow_detector"


class DueShardReader(Protocol):
    """Strong reads of one due shard and of the hold."""

    def read_shard(self, shard: str) -> tuple[DueObservation, ...]:
        """Return every item in one due shard."""
        ...

    def read_hold(self) -> HoldState:
        """Return the automatic-admission hold."""
        ...

    def read_contribution(
        self, grant_id: str, member: CohortMember
    ) -> ContributionObservation | None:
        """Return one grant's contribution of one member, or None when none is stored."""
        ...


@dataclass(frozen=True, slots=True)
class DetectorContext:
    """What one sweep reads, emits and reports through."""

    reader: DueShardReader
    emit: Callable[[SweepReading, AuthorityScope], None]
    now: Callable[[], datetime]
    on_failure: Callable[[BaseException], None]
    scope: AuthorityScope = AuthorityScope.LIVE


def detect(context: DetectorContext) -> SweepReading:
    """Sweep every shard, then emit; raise `SweepIncompleteError` (and emit nothing) otherwise.

    Each failed shard or contribution read is reported once through `on_failure` before the
    sweep refuses, so one unreadable item is visible on its own and never hidden behind a partial
    count. Contributions are read only after every shard was read: a stamped `COMPLETE#`'s
    precompute member, and (ADR-140) an unstamped one's bake member, then its precompute member
    only when bake is stored.
    """
    reader = context.reader
    hold = reader.read_hold()
    now = context.now()
    shards: dict[str, tuple[DueObservation, ...] | None] = {}
    for shard in due_shards():
        try:
            shards[shard] = reader.read_shard(shard)
        except GraphStoreUnavailableError as error:
            context.on_failure(error)
            shards[shard] = None
    contributions: dict[str, ContributionObservation | None] = {}
    unarmed: dict[str, tuple[ContributionObservation | None, ContributionObservation | None]] = {}
    if all(items is not None for items in shards.values()):
        read = [item for items in shards.values() for item in items or ()]
        for grant in bake_contribution_candidates(read, now):
            try:
                contributions[grant] = reader.read_contribution(grant, CohortMember.PRECOMPUTE)
            except GraphStoreUnavailableError as error:
                context.on_failure(error)
        for grant in unarmed_contribution_candidates(read):
            try:
                bake = reader.read_contribution(grant, CohortMember.BAKE)
                precompute = (
                    None
                    if bake is None
                    else reader.read_contribution(grant, CohortMember.PRECOMPUTE)
                )
            except GraphStoreUnavailableError as error:
                context.on_failure(error)
                continue
            unarmed[grant] = (bake, precompute)
    reading = sweep(shards, due_shards(), hold, now, contributions, unarmed)
    context.emit(reading, context.scope)
    return reading


def summary(reading: SweepReading) -> dict[str, object]:
    """Return the terminal log's bounded details, with each counted flow's per-shard evidence."""
    return {
        "shards": reading.shards,
        "open_flows": reading.open_flows,
        "past_ceiling": reading.past_ceiling_count,
        "without_root_clock": reading.without_root_clock_count,
        "promotion_age_seconds": round(reading.promotion_age_seconds, 3),
        "promotions_measured": reading.promotions_measured,
        "promotions_skipped_held": reading.promotions_skipped_held,
        "bake_tasks_failed": len(reading.bake_tasks_failed),
        "bake_retry_exhausted": len(reading.bake_retry_exhausted),
        "bake_precompute_blocked": len(reading.bake_precompute_blocked),
        "bake_finalizer_blocked": len(reading.bake_finalizer_blocked),
        "per_shard": reading.per_shard(),
    }


_LOGGER: Final = Logger(service="adlytics-execution-graph-stuck-flow")


@lru_cache(maxsize=1)
def _live_reader() -> DynamoDueShardReader:
    """Resolve the live table's reader once per execution environment.

    Not at import, so the probe runner imports `detect` without the deployed configuration.
    """
    settings = run_request_api_settings()
    return DynamoDueShardReader(settings.execution_graph_table, boto3.client("dynamodb"))


def _report(error: BaseException) -> None:
    report_error(error, Report(operation=OPERATION, correlation_id=OPERATION, logger=_LOGGER))


@flushes_metrics
def handler(_event: dict[str, Any], _context: object) -> dict[str, object]:
    """Run one live sweep; an incomplete sweep is reported once and emits no gauge."""
    shard_failures: list[BaseException] = []

    def on_failure(error: BaseException) -> None:
        shard_failures.append(error)
        _report(error)

    context = DetectorContext(
        _live_reader(), emit_stuck_flow_reading, lambda: datetime.now(UTC), on_failure
    )
    try:
        reading = detect(context)
    except (SweepIncompleteError, GraphStoreUnavailableError) as error:
        # Each failed shard was reported once already; report the refusal only when it has
        # its own cause (the hold read, or an item the protocol does not declare).
        if not shard_failures:
            _report(error)
        report_operation(
            OperationLog(
                operation=OPERATION,
                component="execution_graph_worker",
                phase="terminal",
                outcome="incomplete",
                severity=LogSeverity.WARNING,
                details={"scope": AuthorityScope.LIVE.value},
            ),
            _LOGGER,
        )
        return {"complete": False}
    details = {"scope": AuthorityScope.LIVE.value, **summary(reading)}
    report_operation(
        OperationLog(
            operation=OPERATION,
            component="execution_graph_worker",
            phase="terminal",
            outcome="completed",
            severity=LogSeverity.WARNING if reading.past_ceiling_count else LogSeverity.INFO,
            details=details,
        ),
        _LOGGER,
    )
    return {"complete": True, **details}
