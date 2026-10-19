# SPDX-License-Identifier: MIT-0
"""Lambda entrypoints for the graph-authority workers (S3).

Five scheduled functions, each its own IAM boundary: the dispatcher (the only graph sender),
the fence settler, the promoter, the node verifier and (S4) the release issuer, the only grant
writer. S3 deployed their schedules disabled; S6 part 2 enables them (§2.11 row 9). Every
handler reads the mode strongly first and does nothing outside GRAPH, so an invocation in
LEGACY, SHADOW or LEGACY_DRAINING makes zero graph writes and zero sends (P3: "zero graph
sends" holds by the mode read, not by the schedule).

Configuration comes from `adapters.config`; tracing is installed at module scope like every
other entry point (`configure_tracing`), with `@flushes_traces` on each handler. The graph
table resolves at module scope through the run-request settings (`EXECUTION_GRAPH_TABLE`).
The dispatcher's route queues (S6 §2.1, one per generated contract route) resolve inside its
handler (`graph_dispatcher_settings`), because
only the dispatcher function carries them.

A pass never ends on one item or one shard (S3V-7): every item failure and every shard whose
read failed is reported once through `report_error` and counted, and the pass completes with
outcome `completed_with_failures`. Each item is durable, so the next pass takes it again.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from functools import partial
from typing import Any, Final, cast

import boto3
from aws_lambda_powertools import Logger

from adapters.config import (
    graph_dispatcher_settings,
    run_request_api_settings,
    table_attempt_reconciler_settings,
    tracing_settings,
)
from adapters.jobs.execution_graph.dispatcher import (
    DispatchContext,
    DispatchCounts,
    RoutedGraphSender,
    dispatch_shard,
)
from adapters.jobs.execution_graph.fence_settler import FenceCounts, due_shards, settle_shard
from adapters.jobs.execution_graph.instantiate import ingestion_node_plan
from adapters.jobs.execution_graph.isolation import isolated
from adapters.jobs.execution_graph.node_verifier import (
    ClaimAttemptFacts,
    VerifierContext,
    VerifyCounts,
    verify_shard,
)
from adapters.jobs.execution_graph.promoter import (
    PromoterContext,
    PromotionCounts,
    arm_pass,
    promote_shard,
)
from adapters.jobs.execution_graph.release import ReleaseContext, ReleaseCounts, release_shard
from adapters.outbound.dynamodb.execution_graph_dispatch import DynamoGraphDispatchStore
from adapters.outbound.dynamodb.execution_graph_release import DynamoGraphReleaseStore
from adapters.outbound.dynamodb.stage1_writer_authority import Stage1WriterAuthorityReader
from adapters.outbound.legacy_head import S3CoordinatorLegacyHead
from adapters.outbound.sqs.graph_dispatch import SqsGraphSender, SqsStandardGraphSender
from adapters.outbound.telemetry.bootstrap import configure_tracing, flushes_traces
from adapters.outbound.telemetry.graph_metrics import emit_post_grant_invariant
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import (
    EXECUTION_GRAPH_PROMOTER_PASS,
    span,
    trace_boundary,
)
from domain.operations.execution_graph import AuthorityMode
from domain.operations.execution_graph_dispatch import POST_GRANT_OVERSIZE
from domain.operations.execution_graph_release import INGESTION_TEMPLATE
from domain.shared_kernel.errors import ErrorCode, InvariantError
from ports.outbound.execution_graph_dispatch import NodeRecord

__all__ = [
    "dispatcher_handler",
    "fence_settler_handler",
    "node_verifier_handler",
    "promoter_handler",
    "release_handler",
]

# Every entry point installs its tracer provider at module scope; without it each span here is a
# no-op and `execution_graph.promoter_*` never reaches `aws/spans` (acceptance x5 rehearsal).
_TRACING_SETTINGS = tracing_settings()
_TRACING = configure_tracing(_TRACING_SETTINGS)
_GRAPH = run_request_api_settings()
_ATTEMPTS = table_attempt_reconciler_settings()
_LOGGER: Final = Logger(service="adlytics-execution-graph-workers")
# The startup line every traced entry point writes, so a zero-span window can be told apart
# from export loss.
report_operation(
    OperationLog(
        operation="execution_graph.workers",
        component="execution_graph.workers",
        phase="start",
        outcome="active" if _TRACING else "disabled",
        severity=LogSeverity.INFO,
        details={
            "stage": "startup",
            "tracing": _TRACING,
            "sample_ratio": _TRACING_SETTINGS.sample_ratio,
        },
    ),
    _LOGGER,
)
_DYNAMODB: Final = boto3.client("dynamodb")
_STORE: Final = DynamoGraphDispatchStore(_GRAPH.execution_graph_table, _DYNAMODB)
_RELEASE_STORE: Final = DynamoGraphReleaseStore(_GRAPH.execution_graph_table, _DYNAMODB)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _report(operation: str, error: BaseException) -> None:
    """Report one isolated item or shard failure once."""
    report_error(error, Report(operation=operation, correlation_id=operation, logger=_LOGGER))


def _report_line(line: OperationLog) -> None:
    """Write one worker lifecycle line (the Release's `serving_withheld` WARNING, ADR-140)."""
    report_operation(line, _LOGGER)


def _post_grant_invariant(node: NodeRecord) -> None:
    """Emit `GraphPostGrantInvariant` for a kept POST_GRANT oversize and log the node (D23)."""
    emit_post_grant_invariant(POST_GRANT_OVERSIZE)
    report_operation(
        OperationLog(
            operation="execution_graph.dispatcher",
            component="execution_graph_worker",
            phase="invariant",
            outcome=POST_GRANT_OVERSIZE,
            severity=LogSeverity.WARNING,
            details={"run_id": node.run_id, "node": node.name, "dispatch_id": node.dispatch_id},
        ),
        _LOGGER,
    )


def _run(operation: str, body: Callable[[], dict[str, object]]) -> dict[str, object]:
    """Gate one pass on mode GRAPH, run it, and report its terminal outcome once."""
    try:
        mode = _STORE.read_mode().mode
        if mode is not AuthorityMode.GRAPH:
            result: dict[str, object] = {"mode": mode.value, "skipped": True}
        else:
            result = {"mode": mode.value, "skipped": False, **body()}
    except Exception as error:
        _report(operation, error)
        raise
    failed = int(cast("int", result.get("failed", 0))) + int(
        cast("int", result.get("failed_shards", 0))
    )
    outcome = "disabled" if result["skipped"] else "completed"
    if failed:
        outcome = "completed_with_failures"
    report_operation(
        OperationLog(
            operation=operation,
            component="execution_graph_worker",
            phase="terminal",
            outcome=outcome,
            severity=LogSeverity.WARNING if failed else LogSeverity.INFO,
            details=result,
        ),
        _LOGGER,
    )
    return result


type _Counts = DispatchCounts | FenceCounts | VerifyCounts | PromotionCounts | ReleaseCounts


def _shards[C: _Counts](operation: str, one: Callable[[str], C]) -> tuple[list[C], int]:
    """Run one shard pass per due shard; a shard whose pass fails is reported and counted."""
    done: list[C] = []
    failed = 0
    for shard in due_shards():

        def run(shard: str = shard) -> None:
            done.append(one(shard))

        if not isolated(run, partial(_report, operation)).ok:
            failed += 1
    return done, failed


def _sum(counts: Sequence[_Counts], failed_shards: int) -> dict[str, object]:
    total: dict[str, int] = {}
    for item in counts:
        for name, value in asdict(item).items():
            if isinstance(value, int):
                total[name] = total.get(name, 0) + value
    return {**total, "failed_shards": failed_shards}


@flushes_traces
@flushes_metrics
def dispatcher_handler(_event: dict[str, Any], _context: object) -> dict[str, object]:
    """Permit, send and close every outbox row (BF-2)."""
    operation = "execution_graph.dispatcher"

    def body() -> dict[str, object]:
        queues = graph_dispatcher_settings()
        sqs = boto3.client("sqs")
        # S6 §2.1: one sender per generated contract route; only delivery is a standard queue.
        sender = RoutedGraphSender(
            INGESTION_TEMPLATE,
            {
                "delivery": SqsStandardGraphSender(queues.delivery_queue_url, sqs),
                "semantic_write": SqsGraphSender(queues.semantic_write_queue_url, sqs),
                "bake": SqsGraphSender(queues.bake_queue_url, sqs),
                "precompute": SqsGraphSender(queues.precompute_queue_url, sqs),
            },
        )
        context = DispatchContext(
            _STORE,
            sender,
            _now,
            partial(_report, operation),
            on_post_grant_invariant=_post_grant_invariant,
        )
        return _sum(*_shards(operation, partial(dispatch_shard, context)))

    return _run(operation, body)


@flushes_traces
@flushes_metrics
def fence_settler_handler(_event: dict[str, Any], _context: object) -> dict[str, object]:
    """Settle every FENCING run's permitless READY nodes, then its terminal (V-11)."""
    operation = "execution_graph.fence_settler"

    def one(shard: str) -> FenceCounts:
        return settle_shard(_STORE, shard, _now, partial(_report, operation))

    return _run(operation, lambda: _sum(*_shards(operation, one)))


@flushes_traces
@flushes_metrics
@trace_boundary(EXECUTION_GRAPH_PROMOTER_PASS)
def promoter_handler(_event: dict[str, Any], lambda_context: object) -> dict[str, object]:
    """Promote woken waiters and run rolling serving elections with live Lambda context."""
    operation = "execution_graph.promoter"
    remaining_ms = getattr(lambda_context, "get_remaining_time_in_millis", None)
    aws_request_id = getattr(lambda_context, "aws_request_id", None)
    if not callable(remaining_ms) or not isinstance(aws_request_id, str) or not aws_request_id:
        raise InvariantError(ErrorCode("EXEC_GRAPH", 34), "the promoter Lambda context is invalid.")

    def body() -> dict[str, object]:
        # The serving election calls `DynamoGraphReleaseStore`-only operations (PromoterStore).
        context = PromoterContext(
            _RELEASE_STORE,
            ingestion_node_plan,
            lambda: datetime.now(UTC),
            on_failure=partial(_report, operation),
        )
        passes, failed_shards = _shards(operation, partial(promote_shard, context))
        # S6 §2.2: the arm pass runs beside instantiation, over every due shard at once, so
        # every `ARM#` read precedes the one `COMPLETE#` walk (AGG4-14).
        # `arm_pass` validates every reading (positive, integer, monotonic) before use.
        clock = cast("Callable[[], int]", remaining_ms)
        arming = arm_pass(context, due_shards(), clock, aws_request_id)
        return {
            "guards": sum(item.guards for item in passes) + arming.guards,
            "instantiated": sum(item.instantiated for item in passes),
            "instantiation_deferred": sum(item.instantiation_deferred for item in passes),
            "armed": arming.armed,
            "arm_retired": arming.retired,
            "arm_lock_wait": arming.lock_wait,
            "serving_coalesced": arming.coalesced,
            "arm_predecessor_wait": arming.predecessor_wait,
            "arm_walk_failed": arming.walk_failed,
            "conflicts": sum(item.conflicts for item in passes) + arming.conflicts,
            "failed": sum(item.failed for item in passes) + arming.failed,
            "failed_shards": failed_shards + arming.failed_shards,
        }

    with span(
        EXECUTION_GRAPH_PROMOTER_PASS,
        aws_request_id=aws_request_id,
        outcome="started",
    ) as recording:
        result = _run(operation, body)
        recording.record(outcome="completed_with_failures" if result.get("failed") else "completed")
        return result


@flushes_traces
@flushes_metrics
def release_handler(_event: dict[str, Any], _context: object) -> dict[str, object]:
    """Issue grants and re-drive committed grants to apply (S4, BF-1, BF-8, V-28)."""
    operation = "execution_graph.release"

    def body() -> dict[str, object]:
        reader = Stage1WriterAuthorityReader(
            _ATTEMPTS.publications_table, _ATTEMPTS.leases_table, _DYNAMODB
        )
        context = ReleaseContext(
            _RELEASE_STORE,
            S3CoordinatorLegacyHead(_GRAPH.raw_bucket, boto3.client("s3"), reader),
            lambda: datetime.now(UTC),
            on_failure=partial(_report, operation),
            report=_report_line,
        )
        passes, failed_shards = _shards(operation, partial(release_shard, context))
        outcomes: dict[str, int] = {}
        for item in passes:
            for name, count in item.outcomes.items():
                outcomes[name] = outcomes.get(name, 0) + count
        return {
            **outcomes,
            "conflicts": sum(item.conflicts for item in passes),
            "failed": sum(item.failed for item in passes),
            "completed": sum(item.completed for item in passes),
            "completion_pending": sum(item.completion_pending for item in passes),
            "unarmed_settled": sum(item.unarmed_settled for item in passes),
            "failed_shards": failed_shards,
        }

    return _run(operation, body)


@flushes_traces
@flushes_metrics
def node_verifier_handler(_event: dict[str, Any], _context: object) -> dict[str, object]:
    """Settle parked takeovers from provider terminals (V-9, r5)."""
    operation = "execution_graph.node_verifier"

    def body() -> dict[str, object]:
        reader = Stage1WriterAuthorityReader(
            _ATTEMPTS.publications_table, _ATTEMPTS.leases_table, _DYNAMODB
        )
        context = VerifierContext(
            _STORE, ClaimAttemptFacts(reader), _now, partial(_report, operation)
        )
        return _sum(*_shards(operation, partial(verify_shard, context)))

    return _run(operation, body)
