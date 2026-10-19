# SPDX-License-Identifier: MIT-0
"""``adapters.jobs.verify_stage1_preswitch``: the sole owner of the §2.5 read-only verifications.

Three closed modes, each run through the §3.2 two-session bootstrap against the dedicated
``Stage1PreswitchVerificationRole`` (the one named exception to the no-``Scan`` rule):

- ``verify-pre-switch`` proves each Stage-1 GSI ``ACTIVE`` with its pinned schema through
  ``DescribeTable``, then reads every page of the publications table with strong consistency and
  proves the ready-field rule, exact canonicity of every reserved-namespace row, and the absence of
  stray reconciliation attributes;
- ``verify-edge`` proves the forward-edge evidence for one target handoff state;
- ``verify-drain`` owns the uninterrupted-quiet drain timer over repeated all-clear reads, and
  re-runs the whole bootstrap before every read after the first, so no read outlives its 900 s
  session; a re-run that resolves different targets is a blocking result.

Each run emits one complete structured report (pages read, per-check pass/fail, every violating
key). A thrown error, partial page set, or failed check is a blocking (nonzero) result. Nothing here
writes, repairs, adopts, or backfills.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import boto3
from aws_lambda_powertools import Logger

from adapters.config import TracingSettings, parse_drain_observation_interval
from adapters.jobs.stage1_operator_bootstrap import (
    PUBLICATIONS_TABLE_KEY,
    BootstrapRequest,
    BootstrapResult,
    bootstrap_operator_session,
)
from adapters.jobs.verify_stage1_preswitch.checks import (
    Check,
    EdgeObservation,
    canonical_namespace_checks,
    edge_checks,
    execution_evidence,
    gsi_schema_check,
    ready_field_check,
    stray_reconciliation_check,
)
from adapters.jobs.verify_stage1_preswitch.drain import DrainObservation, DrainTimer
from adapters.outbound.telemetry.bootstrap import configure_tracing, flush_traces
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flush_metrics,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import (
    VERIFY_STAGE1_PRESWITCH_PROCESS,
    span,
    trace_boundary,
)
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError

if TYPE_CHECKING:
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef

    Item = dict[str, AttributeValueTypeDef]

__all__ = ["build_parser", "main"]

LOGGER = Logger(service="adlytics-verify-stage1-preswitch", level="INFO")
_OPERATION = VERIFY_STAGE1_PRESWITCH_PROCESS
_COMPONENT = "verify_stage1_preswitch.operator"
ROLE_KEY = "stage1PreswitchVerificationRoleArn"
QUEUE_KEY = "semanticWriteQueueArn"
DLQ_KEY = "semanticWriteDlqArn"
FUNCTION_KEY = "deliveryFunctionArn"
PARAMETER_KEYS = (ROLE_KEY, QUEUE_KEY, DLQ_KEY, FUNCTION_KEY, PUBLICATIONS_TABLE_KEY)
MODES = ("verify-pre-switch", "verify-edge", "verify-drain")
EDGE_TARGETS = ("INLINE_QUIESCED", "QUEUED_ACCUMULATE", "QUEUED_ACTIVE")
READ_FAILED = ErrorCode("VERIFY_STAGE1_PRESWITCH", 1)
TARGETS_CHANGED = ErrorCode("VERIFY_STAGE1_PRESWITCH", 2)
_ACCOUNT = re.compile(r"\A\d{12}\Z")
_QUEUE_COUNT_NAMES = (
    "ApproximateNumberOfMessages",
    "ApproximateNumberOfMessagesNotVisible",
    "ApproximateNumberOfMessagesDelayed",
)
_DRAIN_POLL_SECONDS = 15.0


def _non_blank(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("value must not be blank")
    return value.strip()


def _profile(value: str) -> str:
    normalized = _non_blank(value)
    if normalized.casefold() == "default":
        raise argparse.ArgumentTypeError("an explicit nondefault AWS profile is required")
    return normalized


def _account(value: str) -> str:
    normalized = _non_blank(value)
    if _ACCOUNT.match(normalized) is None:
        raise argparse.ArgumentTypeError("expected account must be a 12-digit AWS account id")
    return normalized


def _positive(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the three-mode parser; every target-affecting value is explicit."""
    parser = argparse.ArgumentParser(
        prog="python -m adapters.jobs.verify_stage1_preswitch",
        description="Read-only Stage-1 pre-switch, forward-edge, and drain verification.",
    )
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--profile", required=True, type=_profile)
    shared.add_argument("--region", required=True, type=_non_blank)
    shared.add_argument("--expected-account", required=True, type=_account)
    shared.add_argument("--env", required=True, type=_non_blank)
    shared.add_argument("--operation-id", required=True, type=_non_blank)
    shared.add_argument("--report", type=_non_blank, help="Write the JSON report to this path.")
    shared.add_argument("--trace", action="store_true")
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("verify-pre-switch", parents=[shared])
    edge = modes.add_parser("verify-edge", parents=[shared])
    edge.add_argument("--target", required=True, choices=EDGE_TARGETS)
    drain = modes.add_parser("verify-drain", parents=[shared])
    drain.add_argument(
        "--max-seconds",
        type=_positive,
        default=None,
        help="Give up after this many seconds without a passing window (default: interval + 120).",
    )
    return parser


# --- Live reads (every page, strongly consistent) ------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Targets:
    table: str
    queue_arn: str
    dlq_arn: str
    function_arn: str


def _queue_url(queue_arn: str) -> str:
    _, _, _, region, account, name = queue_arn.split(":", 5)
    return f"https://sqs.{region}.amazonaws.com/{account}/{name}"


class _Reads:
    """The exact reads the verification role permits, from the assumed session only."""

    def __init__(self, assumed: boto3.Session, targets: _Targets) -> None:
        self._dynamodb: Any = assumed.client("dynamodb")
        self._sqs: Any = assumed.client("sqs")
        self._lambda: Any = assumed.client("lambda")
        self._targets = targets

    def describe_table(self) -> Mapping[str, object]:
        try:
            return cast(
                "Mapping[str, object]",
                # aws-pagination: DynamoDB DescribeTable reads one named table.
                self._dynamodb.describe_table(TableName=self._targets.table)["Table"],
            )
        except Exception as error:
            raise DependencyError(
                READ_FAILED, "DescribeTable failed.", context={"cause": type(error).__name__}
            ) from None

    def scan_all_pages(self) -> tuple[list[Item], int]:
        """Read every page with strong consistency; any failure is a blocking result."""
        items: list[Item] = []
        pages = 0
        start_key: Mapping[str, object] | None = None
        while True:
            request: dict[str, object] = {
                "TableName": self._targets.table,
                "ConsistentRead": True,
            }
            if start_key is not None:
                request["ExclusiveStartKey"] = start_key
            try:
                response = self._dynamodb.scan(**request)
            except Exception as error:
                raise DependencyError(
                    READ_FAILED,
                    "A publications-table page could not be read.",
                    context={"cause": type(error).__name__, "pages_read": pages},
                ) from None
            pages += 1
            items.extend(cast("list[Item]", response.get("Items", [])))
            start_key = response.get("LastEvaluatedKey")
            if start_key is None:
                return items, pages

    def queue_counts(self, queue_arn: str) -> dict[str, int]:
        try:
            attributes = self._sqs.get_queue_attributes(
                QueueUrl=_queue_url(queue_arn), AttributeNames=list(_QUEUE_COUNT_NAMES)
            ).get("Attributes", {})
        except Exception as error:
            raise DependencyError(
                READ_FAILED, "GetQueueAttributes failed.", context={"cause": type(error).__name__}
            ) from None
        return {name: int(attributes.get(name, "0")) for name in _QUEUE_COUNT_NAMES}

    def reserved_concurrency(self) -> int | None:
        try:
            response = self._lambda.get_function_concurrency(
                FunctionName=self._targets.function_arn
            )
        except Exception as error:
            raise DependencyError(
                READ_FAILED,
                "GetFunctionConcurrency failed.",
                context={"cause": type(error).__name__},
            ) from None
        value = response.get("ReservedConcurrentExecutions")
        return int(value) if isinstance(value, int) else None

    def rendered_environment(self) -> Mapping[str, object]:
        try:
            configuration = self._lambda.get_function(FunctionName=self._targets.function_arn)[
                "Configuration"
            ]
        except Exception as error:
            raise DependencyError(
                READ_FAILED, "GetFunction failed.", context={"cause": type(error).__name__}
            ) from None
        return cast(
            "Mapping[str, object]", configuration.get("Environment", {}).get("Variables", {})
        )

    def handoff_state(self, environment: str) -> Item | None:
        try:
            response = self._dynamodb.get_item(
                TableName=self._targets.table,
                Key={
                    "partition_key": {"S": f"PROJECTION_HANDOFF#{environment}"},
                    "record_key": {"S": "STATE"},
                },
                ConsistentRead=True,
            )
        except Exception as error:
            raise DependencyError(
                READ_FAILED,
                "The handoff STATE could not be read.",
                context={"cause": type(error).__name__},
            ) from None
        item = response.get("Item")
        return None if item is None else cast("Item", dict(item))


# --- Modes ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Run:
    """One verification run's fixed identity and its report sink."""

    mode: str
    environment: str
    operation_id: str
    execution_id: str
    report_path: Path | None

    def report(self, checks: Sequence[Check], facts: _PageFacts) -> dict[str, object]:
        """Assemble the one complete structured report for this run."""
        return {
            "schema_version": "1",
            "mode": self.mode,
            "environment": self.environment,
            "operation_id": self.operation_id,
            "execution_id": self.execution_id,
            "generated_at": datetime.now(tz=UTC).isoformat(),
            "pages_read": facts.pages_read,
            "row_count": facts.row_count,
            "checks": [check.document() for check in checks],
            "passed": all(check.passed for check in checks) and facts.pages_read > 0,
            **dict(facts.extra),
        }

    def lifecycle(self, *, phase: str, outcome: str, details: Mapping[str, object]) -> None:
        """Emit one structured lifecycle record carrying only the run's own identity."""
        report_operation(
            OperationLog(
                operation=_OPERATION,
                component=_COMPONENT,
                phase=phase,
                outcome=outcome,
                severity=LogSeverity.INFO if outcome != "failed" else LogSeverity.WARNING,
                details={
                    "operation_id": self.operation_id,
                    "execution_id": self.execution_id,
                    "mode": self.mode,
                    **details,
                },
            ),
            LOGGER,
        )


@dataclass(frozen=True, slots=True)
class _PageFacts:
    """How much was read, plus mode-specific report members."""

    pages_read: int
    row_count: int
    extra: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _DrainRead:
    """One complete all-clear read across every evidence class."""

    evidence: Mapping[str, object]
    queue: Mapping[str, int]
    dlq: Mapping[str, int]
    concurrency: int | None

    def reset_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if sum(self.queue.values()):
            reasons.append("queue")
        if sum(self.dlq.values()):
            reasons.append("dlq")
        if self.concurrency != 0 or self.evidence["nonterminal_runs"]:
            reasons.append("execution")
        if self.evidence["nonterminal_plans"] or self.evidence["nonterminal_units"]:
            reasons.append("plan")
        if self.evidence["nonterminal_attempts"]:
            reasons.append("attempt")
        if self.evidence["pending_fences"]:
            reasons.append("pending_fence")
        if self.evidence["blocked_fences"]:
            reasons.append("blocked_fence")
        return tuple(reasons)


@dataclass(frozen=True, slots=True)
class _Verification:
    """The three modes over one bootstrapped read boundary."""

    reads: _Reads
    run: _Run
    targets: _Targets
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    renew: Callable[[], _Reads] | None = None

    def pre_switch(self) -> dict[str, object]:
        # aws-pagination: _Reads.describe_table, this module's typed reader, not an AWS client.
        schema = gsi_schema_check(self.reads.describe_table())
        items, pages = self.reads.scan_all_pages()
        checks = (
            schema,
            ready_field_check(items),
            *canonical_namespace_checks(items),
            stray_reconciliation_check(items),
        )
        return self.run.report(checks, _PageFacts(pages, len(items), {}))

    def edge(self, target: str) -> dict[str, object]:
        items, pages = self.reads.scan_all_pages()
        observation = EdgeObservation(
            target=target,
            items=items,
            delivery_reserved_concurrency=self.reads.reserved_concurrency(),
            queue_counts=self.reads.queue_counts(self.targets.queue_arn),
            dlq_counts=self.reads.queue_counts(self.targets.dlq_arn),
            handoff_state=self.reads.handoff_state(self.run.environment),
        )
        extra = {"target": target, "execution_evidence": execution_evidence(items).document()}
        return self.run.report(edge_checks(observation), _PageFacts(pages, len(items), extra))

    def _drain_read(self, reads: _Reads) -> tuple[_DrainRead, int, int]:
        items, pages = reads.scan_all_pages()
        read = _DrainRead(
            evidence=execution_evidence(items).document(),
            queue=reads.queue_counts(self.targets.queue_arn),
            dlq=reads.queue_counts(self.targets.dlq_arn),
            concurrency=reads.reserved_concurrency(),
        )
        return read, pages, len(items)

    def drain(self, max_seconds: int | None) -> dict[str, object]:
        interval = parse_drain_observation_interval(self.reads.rendered_environment())
        timer = DrainTimer(interval)
        budget = max_seconds if max_seconds is not None else interval + 120
        started = self.clock()
        pages_total = rows = 0
        observed = 0
        while True:
            try:
                # A full read can take most of one 900 s session, and a passing window needs at
                # least two, so every read after the first runs on a freshly bootstrapped session.
                reads = self.reads if observed == 0 or self.renew is None else self.renew()
                read, pages, rows = self._drain_read(reads)
                reasons = read.reset_reasons()
            # error-propagation: operator CLI; failed_read resets the quiet timer, so it only delays
            except DependencyError:
                reasons, pages = ("failed_read",), 0
            observed += 1
            pages_total += pages
            timer.observe(DrainObservation(self.clock() - started, reasons))
            if timer.passing or self.clock() - started >= budget:
                break
            self.sleep(_DRAIN_POLL_SECONDS)
        reset_classes = sorted(
            {reason for reset in timer.resets for reason in cast("list[str]", reset["reasons"])}
        )
        check = Check(
            "uninterrupted_quiet_interval",
            timer.passing,
            () if timer.passing else tuple(reset_classes) or ("timer",),
            {"budget_seconds": budget},
        )
        return self.run.report((check,), _PageFacts(pages_total, rows, {"drain": timer.document()}))


# --- Entry point ----------------------------------------------------------------------------------


def _emit(report: Mapping[str, object], path: Path | None) -> None:
    rendered = json.dumps(report, sort_keys=True, indent=1) + "\n"
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    print(rendered, end="")  # noqa: T201


def _targets(bootstrapped: BootstrapResult) -> _Targets:
    values = bootstrapped.values
    return _Targets(
        table=values[PUBLICATIONS_TABLE_KEY],
        queue_arn=values[QUEUE_KEY],
        dlq_arn=values[DLQ_KEY],
        function_arn=values[FUNCTION_KEY],
    )


@dataclass(frozen=True, slots=True)
class _Sessions:
    """The run's first bootstrapped session, and how to bootstrap a fresh one for a later read."""

    first: BootstrapResult
    rebootstrap: Callable[[], BootstrapResult]


def _verify(args: argparse.Namespace, run: _Run, sessions: _Sessions) -> dict[str, object]:
    bootstrapped = sessions.first
    targets = _targets(bootstrapped)

    def renewed_reads() -> _Reads:
        again = sessions.rebootstrap()
        if _targets(again) != targets:
            raise InvariantError(
                TARGETS_CHANGED, "A re-run bootstrap resolved different verification targets."
            )
        return _Reads(again.assumed, targets)

    verification = _Verification(
        _Reads(bootstrapped.assumed, targets), run, targets, renew=renewed_reads
    )
    if run.mode == "verify-pre-switch":
        return verification.pre_switch()
    if run.mode == "verify-edge":
        return verification.edge(cast("str", args.target))
    return verification.drain(cast("int | None", args.max_seconds))


@trace_boundary(VERIFY_STAGE1_PRESWITCH_PROCESS)
def main(argv: list[str] | None = None) -> int:
    """Validate, bootstrap, read every page, decide, and emit one complete report."""
    args = build_parser().parse_args(argv)
    run = _Run(
        mode=cast("str", args.mode),
        environment=cast("str", args.env),
        operation_id=cast("str", args.operation_id),
        execution_id=uuid.uuid4().hex,
        report_path=None if args.report is None else Path(cast("str", args.report)),
    )
    request = BootstrapRequest(
        profile=args.profile,
        region=args.region,
        expected_account=args.expected_account,
        environment=args.env,
        operation_id=run.operation_id,
        role_key=ROLE_KEY,
        parameter_keys=PARAMETER_KEYS,
    )
    ambient = boto3.Session(profile_name=args.profile, region_name=args.region)
    tracing = TracingSettings(
        enabled=args.trace, aws_region=args.region, environment=args.env, sample_ratio=1.0
    )
    try:
        configure_tracing(tracing, session=ambient._session)
        with span(
            VERIFY_STAGE1_PRESWITCH_PROCESS,
            operation_id=run.operation_id,
            execution_id=run.execution_id,
            predecessor_execution_id="none",
            plan_digest="none",
            stage="bootstrap",
            completed=0,
            total=1,
            mode=run.mode,
        ) as root:
            run.lifecycle(phase="start", outcome="started", details={})
            bootstrapped = bootstrap_operator_session(ambient, request)
            run.lifecycle(phase="identity", outcome="verified", details={})
            sessions = _Sessions(
                first=bootstrapped,
                rebootstrap=lambda: bootstrap_operator_session(ambient, request),
            )
            report = _verify(args, run, sessions)
            passed = bool(report["passed"])
            root.record(
                stage="verify",
                completed=1,
                outcome="passed" if passed else "failed",
                pages_read=cast("int", report["pages_read"]),
            )
            run.lifecycle(
                phase="terminal",
                outcome="passed" if passed else "failed",
                details={"pages_read": report["pages_read"], "row_count": report["row_count"]},
            )
            _emit(report, run.report_path)
            return 0 if passed else 1
    except Exception as error:
        report_error(
            error, Report(operation=_OPERATION, correlation_id=run.execution_id, logger=LOGGER)
        )
        run.lifecycle(phase="terminal", outcome="failed", details={"blocking": "exception"})
        return 1
    finally:
        try:
            flush_metrics()
        finally:
            flush_traces()


if __name__ == "__main__":
    sys.exit(main())
