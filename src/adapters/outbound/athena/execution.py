# SPDX-License-Identifier: MIT-0
"""Submitting a statement to Athena, waiting for it, and reading what came back.

Shared by everything that talks to Athena: the read path in `query.py` and the write path the
seed job uses. Extracted rather than duplicated because the parts most easily got wrong are
identical for both — the terminal-state set, the fact that every response field is optional,
and that a failure must become a `DependencyError` rather than an SDK exception.

**No SDK exception crosses into the domain.** Every `ClientError`, throttle and timeout becomes
a `DependencyError` with a code, and one reaching the domain means retries were exhausted.
Nothing user-facing carries a query id, a bucket name or an ARN: Athena's own failure messages
name tables and columns, so they go in the error context, which is logged, never in the
description, which can reach a caller.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from botocore.exceptions import ClientError

from adapters.outbound.athena.parameters import sql_literal
from domain.shared_kernel.errors import DependencyError, ErrorCode

if TYPE_CHECKING:  # pragma: no cover — kept out of the Lambda import path
    from types_boto3_athena.client import AthenaClient

__all__ = [
    "MAX_WAIT_SECONDS",
    "POLL_INTERVAL_SECONDS",
    "QUERY_FAILED",
    "QUERY_TIMED_OUT",
    "AthenaTarget",
    "StatementOutcome",
    "StatementRequest",
    "await_completion",
    "fetch_rows",
    "start",
]

QUERY_FAILED = ErrorCode("ATHENA", 1)
QUERY_TIMED_OUT = ErrorCode("ATHENA", 2)

POLL_INTERVAL_SECONDS: Final = 0.25
"""How often to ask whether a statement has finished.

Short, because a tool call has a user waiting and Athena's own planning overhead dominates
anything saved by polling less. `GetQueryExecution` replenishes at 500/second per account in
us-east-1 with a burst multiplier of 1, so this rate is far inside the quota — but the quota is
account-wide and shared with every concurrent statement, which is what makes polling faster a real
cost rather than a free one [verified 2026-08-22, AWS General Reference].

**A graduated interval was tried and reverted, because the premise was measured to be wrong.** The
reasoning was that a flat 0.25 s made this process sleep through a quarter-second of an already
finished query. It does not: a statement answered entirely from a reused result still transits
QUEUED then RUNNING and only reports `SUCCEEDED` 256 to 391 ms after `start` returns [verified
2026-08-22 against deployed dev, three runs]. The sleep is not dead time, and backing off from
0.05 s moved a cache-hit query from ~615 ms to ~553 ms — a gain smaller than the 135 ms run-to-run
spread in Athena's own state transitions, bought with three tuning constants and two extra
account-quota calls per statement. The sentence above turns out to be right, including for a
reused result.
"""

MAX_WAIT_SECONDS: Final = 30.0
"""How long an interactive statement may take before it is abandoned.

Below the API Gateway integration timeout deliberately: a function outliving its caller burns
cost and returns nothing. A read needing longer is answering at a grain the interactive
workgroup should not serve, and the scan cutoff usually stops it first.
"""

BATCH_MAX_WAIT_SECONDS: Final = 600.0
"""The same, for a statement nobody is waiting on.

A seed or a load reads and writes whole partitions by design, so holding it to an interactive
budget would abandon correct work halfway through — and a half-applied write is worse than a
slow one.
"""

TERMINAL: Final = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


@dataclass(frozen=True, slots=True)
class AthenaTarget:
    """Which workgroups and database a caller runs against.

    Both workgroups together rather than one, because a caller that writes almost always also
    reads: a merge has to know what is currently in force before it can decide anything. Holding
    only the batch workgroup would push a small gating read onto the budget sized for a rewrite,
    and holding only the interactive one would push a rewrite onto the budget sized for a
    question. Which one a given statement uses is settled by the method that runs it.

    Named values rather than a `Settings` object, so an adapter that needs three strings does not
    depend on the shape of every other setting — the argument `SeedTarget` records.

    `batch_workgroup` keeps its historical name; it carries the BAKE DML workgroup
    (`adlytics-<env>-v2-bake`, SEMANTIC-M1), never the retained `batch` one.
    """

    interactive_workgroup: str
    batch_workgroup: str
    database: str


@dataclass(frozen=True, slots=True)
class StatementRequest:
    """One statement, its bound values, and where to run it.

    A dataclass rather than a hand-written initialiser: five related fields is exactly the
    case the hexagonal rules name for a request object, and a generated `__init__` also keeps
    the arity check pointed at real functions.
    """

    sql: str
    parameters: tuple[str, ...]
    workgroup: str
    database: str
    reuse_minutes: int


def start(client: AthenaClient, request: StatementRequest) -> str:
    """Submit a statement with its values encoded, and return the execution id.

    **Every parameter is encoded here, and this is the only place it happens.** Athena parses an
    `ExecutionParameters` entry as a SQL expression rather than binding it as a value, so an
    unquoted one is evaluated: `2026-08-10` arrives as the integer 2008 and `upper('x')` runs.
    `sql_literal` makes each entry an unambiguous varchar, and the statement casts from there.
    See `parameters.py` for what was measured.

    One choke point rather than one per composer, because a composer that forgot would not fail
    — it would produce a statement that works for most values and is wrong for a date.

    Result reuse is set here because it cannot be set on the workgroup — [verified]
    `AWS::Athena::WorkGroup` has no result-reuse property, so F-14's "set the reuse age
    deliberately" is necessarily per-statement.
    """
    encoded = [sql_literal(value) for value in request.parameters]
    # Inlined at each call site rather than built as a variable, so pyright checks it
    # against `ResultReuseConfigurationTypeDef` from context instead of inferring a plain dict.
    reuse_enabled = request.reuse_minutes > 0
    try:
        if request.parameters:
            started = client.start_query_execution(
                QueryString=request.sql,
                WorkGroup=request.workgroup,
                QueryExecutionContext={"Database": request.database},
                ExecutionParameters=encoded,
                ResultReuseConfiguration={
                    "ResultReuseByAgeConfiguration": {
                        "Enabled": reuse_enabled,
                        "MaxAgeInMinutes": request.reuse_minutes,
                    }
                },
            )
        else:
            # A statement with no placeholders must omit the key entirely. Athena rejects an
            # empty `ExecutionParameters` list rather than treating it as "none".
            started = client.start_query_execution(
                QueryString=request.sql,
                WorkGroup=request.workgroup,
                QueryExecutionContext={"Database": request.database},
                ResultReuseConfiguration={
                    "ResultReuseByAgeConfiguration": {
                        "Enabled": reuse_enabled,
                        "MaxAgeInMinutes": request.reuse_minutes,
                    }
                },
            )
    except ClientError as exc:
        error = exc.response.get("Error", {})
        raise DependencyError(
            QUERY_FAILED,
            "The statement could not be submitted.",
            context={
                "cause": type(exc).__name__,
                "workgroup": request.workgroup,
                "aws_code": str(error.get("Code", "")),
            },
        ) from None
    except Exception as exc:
        raise DependencyError(
            QUERY_FAILED,
            "The statement could not be submitted.",
            context={"cause": type(exc).__name__, "workgroup": request.workgroup},
        ) from None
    return started["QueryExecutionId"]


@dataclass(frozen=True, slots=True)
class StatementOutcome:
    """What a completed statement cost, beyond the rows it produced.

    Every field comes out of the one `GetQueryExecution` response already being polled, so
    carrying them costs no extra call. Together they are what makes a span explain its own
    duration rather than merely state it — the observability rule that a duration alone cannot
    distinguish a slow query from a large one.

    `reused_previous_result` is the one that answers a question nothing could answer before: result
    reuse is requested on every statement, and until this was read there was no way to tell whether
    it was ever hit.

    Defaulted throughout because every one is `NotRequired` in the API model. A statement that
    succeeded while reporting no statistics is odd, not fatal, and a zero here is visibly a
    non-answer where a raise would lose a good result.
    """

    bytes_scanned: int = 0
    reused_previous_result: bool = False
    engine_millis: int = 0
    planning_millis: int = 0
    queue_millis: int = 0
    total_millis: int = 0
    service_processing_millis: int = 0
    statistics_present: bool = True
    """Whether the response carried a `Statistics` block at all (A2.2: absence is recorded)."""


@dataclass(frozen=True, slots=True)
class _Progress:
    """One poll: where the statement is, why if it stopped, and what it has cost so far."""

    state: str
    reason: str
    outcome: StatementOutcome


def await_completion(client: AthenaClient, execution_id: str, *, limit: float) -> StatementOutcome:
    """Poll to a terminal state and return what the statement cost.

    The cost figures are returned rather than discarded because they are the only measure of what a
    statement cost, and a tool that cannot report them cannot explain a bill or a wait. Three of
    the four callers ignore the result; the read path does not.
    """
    deadline = time.monotonic() + limit
    while True:
        progress = _state_of(client, execution_id)
        if progress.state == "SUCCEEDED":
            return progress.outcome
        if progress.state in TERMINAL:
            raise DependencyError(
                QUERY_FAILED,
                "The statement did not complete.",
                # `reason` names tables and columns, and carries Athena's scan-cutoff message
                # when the F-14 breaker fires, so it stays in context.
                context={
                    "state": progress.state,
                    "reason": progress.reason,
                    "query_id": execution_id,
                },
            )
        if time.monotonic() >= deadline:
            cancellation: dict[str, object]
            try:
                client.stop_query_execution(QueryExecutionId=execution_id)
            # error-propagation: recorded as cancellation_cause on the DependencyError raised below
            except Exception as exc:
                cancellation = {
                    "cancellation_requested": False,
                    "cancellation_cause": type(exc).__name__,
                }
            else:
                cancellation = {"cancellation_requested": True}
            raise DependencyError(
                QUERY_TIMED_OUT,
                "The statement took too long.",
                context={
                    "waited_seconds": limit,
                    "query_id": execution_id,
                    **cancellation,
                },
            )
        time.sleep(POLL_INTERVAL_SECONDS)


def _state_of(client: AthenaClient, execution_id: str) -> _Progress:
    """Read state, failure reason and cost, tolerating every optional field.

    `QueryExecution`, `Status`, `Statistics` and `ResultReuseInformation` are each `NotRequired` in
    the API model, so every one is read with a default rather than indexed. `ReusedPreviousResult`
    is required *within* `ResultReuseInformation`, so the only question is whether the block is
    present at all — and Athena omits it rather than reporting false.
    """
    try:
        response = client.get_query_execution(QueryExecutionId=execution_id)
    except Exception as exc:
        raise DependencyError(
            QUERY_FAILED,
            "The statement status could not be read.",
            context={"cause": type(exc).__name__, "query_id": execution_id},
        ) from None

    execution = response.get("QueryExecution", {})
    status = execution.get("Status", {})
    statistics = execution.get("Statistics", {})
    reuse = statistics.get("ResultReuseInformation")
    return _Progress(
        state=status.get("State", "UNKNOWN"),
        reason=status.get("StateChangeReason", ""),
        outcome=StatementOutcome(
            bytes_scanned=statistics.get("DataScannedInBytes", 0),
            reused_previous_result=reuse is not None and reuse["ReusedPreviousResult"],
            engine_millis=statistics.get("EngineExecutionTimeInMillis", 0),
            planning_millis=statistics.get("QueryPlanningTimeInMillis", 0),
            queue_millis=statistics.get("QueryQueueTimeInMillis", 0),
            total_millis=statistics.get("TotalExecutionTimeInMillis", 0),
            service_processing_millis=statistics.get("ServiceProcessingTimeInMillis", 0),
            statistics_present="Statistics" in execution,
        ),
    )


def fetch_rows(client: AthenaClient, execution_id: str) -> list[list[str | None]]:
    """Read every page of a result, dropping Athena's header row.

    **The header is a real row in the response.** Athena returns the column names as the first
    row of a `SELECT`, including when nothing matched, so a naive read reports one extra row
    whose every value is a column name — which surfaces much later as a measurement failure
    rather than here.
    """
    collected: list[list[str | None]] = []
    token: str | None = None
    first_page = True

    while True:
        try:
            page = (
                client.get_query_results(QueryExecutionId=execution_id)
                if token is None
                else client.get_query_results(QueryExecutionId=execution_id, NextToken=token)
            )
        except Exception as exc:
            raise DependencyError(
                QUERY_FAILED,
                "The statement results could not be read.",
                context={"cause": type(exc).__name__, "query_id": execution_id},
            ) from None

        for index, row in enumerate(page.get("ResultSet", {}).get("Rows", [])):
            if first_page and index == 0:
                continue
            collected.append([datum.get("VarCharValue") for datum in row.get("Data", [])])

        first_page = False
        token = page.get("NextToken")
        if token is None:
            return collected
