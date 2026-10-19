# SPDX-License-Identifier: MIT-0
"""The one §5 table-attempt statement loop shared by every Stage-1 writer.

The queued table writer and the inline-run driver own different authorities — a
plan ``UNIT#`` versus a ``RUN#`` — but drive every merge/verify statement through
the same protocol: monotonic deadlines, the closed step-2 validator,
``BeginTableAttemptRequest``, the dedicated one-call start client,
``RecordSubmittedAttemptRequest`` on ``Accepted``, bounded terminal-first polling
with cancellation at ``Q`` and parking at ``S``, and
``CompleteTableAttemptRequest`` on a provider terminal. This module holds that
loop once. Each owner supplies only its fresh owner snapshots and its reporting
seam through :class:`StatementOwner`; the loop never reads owner state itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Protocol

from adapters.outbound.athena.semantic_storage import UnitWorkload, ordered_records
from adapters.outbound.athena.semantic_storage import (
    _expected_records as _expected_readback_records,  # pyright: ignore[reportPrivateUsage] — the one §2.2 step-5 readback renderer; extraction belongs to a later slice
)
from adapters.outbound.telemetry.reporting import LogSeverity
from domain.operations.statement_protocol import (
    InsufficientStatementBudget,
    LocalStatementRefusal,
    StatementDeadlines,
    StatementStartBounds,
    ValidateStatementStartRequest,
    derive_statement_deadlines,
    validate_statement_start,
)
from domain.operations.table_attempt import (
    AttemptObservation,
    FenceObservation,
    ProviderTerminalState,
)
from domain.shared_kernel.errors import GRAPH_UNIT_EMPTY_CLAIMS_CODE, ErrorCode, InvariantError
from ports.outbound.projection_coordinator import (
    BeginInlineRunOwner,
    BeginQueuedUnitOwner,
    BeginTableAttemptRequest,
    CompleteTableAttemptRequest,
    GraphClaimFence,
    ParkAttemptRequest,
    ProjectionCoordinatorPort,
    RequestAttemptCancellationRequest,
    TableAttemptOwnerSnapshot,
)
from ports.outbound.table_attempt import RecordSubmittedAttemptRequest, TableAttemptStore
from ports.outbound.table_attempt_start import (
    Accepted,
    StartTableAttemptRequest,
    TableAttemptStartClient,
)
from ports.outbound.table_attempt_terminal import (
    ObserveTableAttemptResult,
    ProviderTerminalObservation,
    TableAttemptTerminalClient,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from adapters.outbound.athena.table_attempt_statements import ComposedStatement
    from domain.operations.table_lease import TableLeaseRecord

__all__ = [
    "PROJECTION_INVALID",
    "AttemptReads",
    "LocalStatementRefusedError",
    "StatementCompleted",
    "StatementOwner",
    "StatementParked",
    "StatementProtocolRuntime",
    "StatementResult",
    "StatementRun",
    "epoch_micros",
    "instant",
    "readback_matches",
    "run_statement",
]

PROJECTION_INVALID: Final = ErrorCode("TABLE_WRITER", 2)
# Bounded §5 step-4 poll cadence: derived behaviour, not a §11 setting — every
# wait is additionally bounded by the next monotonic Q/S deadline, so the loop
# always ends by parking at S inside the Lambda budget. The cadence starts at
# 250 ms and doubles to a 500 ms cap. The cap was 5 s until 2026-10-10: a ~4.1 s
# p50 Iceberg MERGE was then first observed at ~7.75 s (250+500+1000+2000+4000),
# so about 5 s of a 20 s three-MERGE unit was poll quantization. At 500 ms the
# observation lag is at most 0.5 s per statement. 500 ms rather than 1 s because
# the extra cost is only API calls: one attempt polls at most 2/s, so even 16
# writers stay at 32 `GetQueryExecution`/s against the 500/s account quota
# (cited with its quota context in `adapters.outbound.athena.execution`).
_POLL_INITIAL_MS: Final = 250
_POLL_MAX_MS: Final = 500
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND: Final = timedelta(microseconds=1)


def epoch_micros(moment: datetime) -> int:
    """Convert one timezone-aware instant to UTC epoch microseconds."""
    if moment.tzinfo is None:
        raise ValueError("writer instants must be timezone-aware.")
    return (moment.astimezone(UTC) - _EPOCH) // _MICROSECOND


def instant(moment: datetime) -> str:
    """Render one instant as the 20-digit epoch-microseconds coordinator instant."""
    return f"{epoch_micros(moment):020d}"


# --- Seams ----------------------------------------------------------------------------------------


class AttemptReads(Protocol):
    """The strong fence and attempt reads the loop needs between steps."""

    def fence(self, table_id: str) -> FenceObservation | None:
        """Strongly read the table's ``FENCE`` observation, or ``None`` when absent."""
        ...

    def attempt(self, table_id: str, attempt_id: str) -> AttemptObservation | None:
        """Strongly read one attempt observation, or ``None`` when absent."""
        ...


class StatementOwner(Protocol):
    """Owner-specific snapshots and reporting the shared loop must not derive itself.

    ``begin_owner`` returns the fresh Begin owner after the owner's own preflight —
    queued reservation caps raise the owner's typed signals there, before any
    validation or Athena call. ``attempt_owner`` returns the fresh owner snapshot
    that completion and cancellation condition on.
    """

    def begin_owner(self) -> BeginQueuedUnitOwner | BeginInlineRunOwner:
        """Re-read the owner, apply its own caps, and shape the fresh Begin owner."""
        ...

    def attempt_owner(self) -> TableAttemptOwnerSnapshot:
        """Re-read the exact owner snapshot completion and cancellation condition on."""
        ...

    def report(
        self, operation: str, outcome: str, severity: LogSeverity, **details: object
    ) -> None:
        """Emit one structured operation record through the owner's reporting seam."""
        ...


@dataclass(frozen=True, slots=True)
class StatementProtocolRuntime:
    """The ports and clocks one §5 loop drives; owners compose it from their runtime."""

    coordinator: ProjectionCoordinatorPort
    attempts: TableAttemptStore
    start: TableAttemptStartClient
    terminals: TableAttemptTerminalClient
    reads: AttemptReads
    wall: Callable[[], datetime]
    monotonic_ms: Callable[[], int]
    sleep_ms: Callable[[int], None]


@dataclass(frozen=True, slots=True)
class StatementRun:
    """One owner's held-lease statement context for the §5 loop."""

    runtime: StatementProtocolRuntime
    owner: StatementOwner
    physical_table_id: str
    lease: TableLeaseRecord
    writer_execution_id: str
    bounds: StatementStartBounds
    remaining_budget_ms: Callable[[], int]
    graph_claims: Callable[[], tuple[GraphClaimFence, ...]] | None = None
    """S6 §2.5: a graph execution set's claim fences, one per member, strongly re-read for each
    Begin (V-9). Only a graph-unit run carries the thunk; an empty result is an invariant."""

    def __post_init__(self) -> None:
        if self.lease.physical_table_id != self.physical_table_id:
            raise ValueError("the held lease must name the statement's table.")
        if not self.writer_execution_id.strip():
            raise ValueError("a statement run names its writer execution.")


# --- Typed statement results ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatementCompleted:
    """One provider terminal was observed and its attempt accounting closed."""

    state: ProviderTerminalState
    query_execution_id: str
    cancel_requested: bool = False
    """Slice 7 (D43): the poll loop requested cancellation at the cancellation deadline before
    this terminal, so a failed terminal is a time outcome. Typed: no message text is parsed."""


@dataclass(frozen=True, slots=True)
class StatementParked:
    """The safety deadline parked the exact attempt/fence pair for reconciliation."""

    attempt_id: str


type StatementResult = StatementCompleted | StatementParked | InsufficientStatementBudget


class LocalStatementRefusedError(Exception):
    """The closed §5 step-2 validator refused a statement before any Begin (no Athena call)."""

    def __init__(self, refusal: LocalStatementRefusal) -> None:
        super().__init__(f"local statement validation refused: {refusal.reason.value}.")
        self.refusal = refusal


def _invariant(description: str, **context: object) -> InvariantError:
    return InvariantError(PROJECTION_INVALID, description, context=dict(context))


def _required_fence(run: StatementRun) -> FenceObservation:
    fence = run.runtime.reads.fence(run.physical_table_id)
    if fence is None:
        raise _invariant("The attempt fence disappeared during statement processing.")
    return fence


def _required_attempt(run: StatementRun, attempt_id: str) -> AttemptObservation:
    attempt = run.runtime.reads.attempt(run.physical_table_id, attempt_id)
    if attempt is None:
        raise _invariant("The attempt record disappeared during statement processing.")
    return attempt


def _complete_attempt(
    run: StatementRun, attempt_id: str, observation: ProviderTerminalObservation
) -> None:
    run.runtime.coordinator.complete_table_attempt(
        CompleteTableAttemptRequest(
            physical_table_id=run.physical_table_id,
            fence=_required_fence(run),
            attempt=_required_attempt(run, attempt_id),
            provider_state=observation.state,
            terminal_at=instant(run.runtime.wall()),
            owner=run.owner.attempt_owner(),
            query_count=observation.query_count,
            scanned_bytes=observation.scanned_bytes,
            engine_ms=observation.engine_ms,
        )
    )


def _park_attempt(run: StatementRun, attempt_id: str) -> None:
    run.runtime.coordinator.park_attempt(
        ParkAttemptRequest(
            physical_table_id=run.physical_table_id,
            lease=run.lease,
            fence=_required_fence(run),
            attempt=_required_attempt(run, attempt_id),
            park_instant=instant(run.runtime.wall()),
        )
    )


def _request_cancellation(run: StatementRun, attempt_id: str, query_execution_id: str) -> None:
    run.runtime.coordinator.request_attempt_cancellation(
        RequestAttemptCancellationRequest(
            physical_table_id=run.physical_table_id,
            fence=_required_fence(run),
            attempt=_required_attempt(run, attempt_id),
            cancel_requested_at=instant(run.runtime.wall()),
            owner=run.owner.attempt_owner(),
        )
    )
    run.runtime.terminals.stop(query_execution_id)


def _drive_attempt(  # noqa: PLR0913, PLR0917 - the one bounded §5 poll loop
    run: StatementRun,
    deadlines: StatementDeadlines,
    attempt_id: str,
    query_execution_id: str | None,
) -> StatementResult:
    """Run the §5 step-4 terminal-first poll loop under the monotonic deadlines.

    Every iteration first processes an already returned provider terminal. Only
    then do the deadlines apply: at ``S`` the attempt parks without a new poll;
    at ``Q`` with a durable valid identity the cancellation is persisted and
    ``StopQueryExecution`` is called exactly once; otherwise one bounded poll or
    sleep follows. Without a durable valid identity no poll, cancellation, or
    Stop ever occurs — the one Start call per invocation is already spent, so
    the loop only waits for ``S`` and parks.
    """
    runtime = run.runtime
    cancel_requested = False
    observation: ObserveTableAttemptResult | None = None
    interval_ms = _POLL_INITIAL_MS
    while True:
        if isinstance(observation, ProviderTerminalObservation):
            if query_execution_id is None:  # pragma: no cover - a terminal needs its identity
                raise _invariant("A provider terminal was observed without a query identity.")
            _complete_attempt(run, attempt_id, observation)
            return StatementCompleted(
                observation.state, query_execution_id, cancel_requested=cancel_requested
            )
        now = runtime.monotonic_ms()
        if now >= deadlines.safety_deadline_ms:
            _park_attempt(run, attempt_id)
            return StatementParked(attempt_id)
        if query_execution_id is None:
            runtime.sleep_ms(max(1, min(_POLL_MAX_MS, deadlines.safety_deadline_ms - now)))
            continue
        if now >= deadlines.cancellation_deadline_ms and not cancel_requested:
            _request_cancellation(run, attempt_id, query_execution_id)
            cancel_requested = True
        observation = runtime.terminals.observe(query_execution_id)
        if isinstance(observation, ProviderTerminalObservation):
            continue
        bound = (
            deadlines.safety_deadline_ms if cancel_requested else deadlines.cancellation_deadline_ms
        )
        runtime.sleep_ms(max(1, min(interval_ms, bound - runtime.monotonic_ms())))
        interval_ms = min(interval_ms * 2, _POLL_MAX_MS)


def readback_matches(workload: UnitWorkload, rows: tuple[tuple[str | None, ...], ...]) -> bool:
    """Compare one verification readback with the immutable objects (§2.2 step 5)."""
    table = workload.table
    partition_column = table.partition_keys[0]
    partition_index = next(
        index for index, column in enumerate(table.columns) if column.name == partition_column
    )
    grouped: dict[str, list[tuple[str | None, ...]]] = {}
    for row in rows:
        grouped.setdefault(row[partition_index] or "", []).append(tuple(row))
    identities = {identity for identity, _ in workload.records_by_identity}
    if set(grouped) - identities:
        return False
    for identity, records in workload.records_by_identity:
        expected = _expected_readback_records(table, ordered_records(table, records))
        if tuple(grouped.get(identity, ())) != expected:
            return False
    return True


def _owner_kind(owner: BeginQueuedUnitOwner | BeginInlineRunOwner) -> str:
    return "queued_unit" if isinstance(owner, BeginQueuedUnitOwner) else "inline_run"


def _owner_ref(owner: BeginQueuedUnitOwner | BeginInlineRunOwner) -> str:
    return owner.unit.plan_key if isinstance(owner, BeginQueuedUnitOwner) else owner.run.run_id


def _ordinal(owner: BeginQueuedUnitOwner | BeginInlineRunOwner) -> int:
    if isinstance(owner, BeginQueuedUnitOwner):
        return owner.plan_reservation_count + 1
    return owner.next_ordinal


def _graph_fences(run: StatementRun) -> tuple[GraphClaimFence, ...]:
    """Evaluate the claim thunk once for this Begin; a graph-unit thunk may not return ()."""
    if run.graph_claims is None:
        return ()
    fences = run.graph_claims()
    if not fences:
        raise InvariantError(
            GRAPH_UNIT_EMPTY_CLAIMS_CODE,
            "a graph-unit attempt carries no claim fence.",
            context={"physical_table_id": run.physical_table_id},
        )
    return fences


def run_statement(run: StatementRun, statement: ComposedStatement) -> StatementResult:
    """Drive one composed statement through §5 steps 1-5 for the held owner.

    A ``LocalStatementRefusal`` raises ``ValueError`` here: validation happens
    before Begin, so no attempt or reservation exists and the owner decides its
    own failure disposition from the raised refusal reason.
    """
    runtime = run.runtime
    deadlines = derive_statement_deadlines(runtime.monotonic_ms(), run.remaining_budget_ms())
    if isinstance(deadlines, InsufficientStatementBudget):
        return deadlines
    owner = run.owner.begin_owner()
    validation = validate_statement_start(
        ValidateStatementStartRequest(
            statement_kind=statement.kind.value,
            owner_kind=_owner_kind(owner),
            owner_ref=_owner_ref(owner),
            physical_table_id=run.physical_table_id,
            lease_holder=run.lease.holder,
            fencing_token=run.lease.fencing_token,
            ordinal=_ordinal(owner),
            start_envelope=statement.start_envelope,
            rendered_sql=statement.sql,
            parameters=statement.parameters,
            bounds=run.bounds,
        )
    )
    if isinstance(validation, LocalStatementRefusal):
        raise LocalStatementRefusedError(validation)
    fences = _graph_fences(run)
    begin = runtime.coordinator.begin_table_attempt(
        BeginTableAttemptRequest(
            physical_table_id=run.physical_table_id,
            lease=run.lease,
            operation_instant=instant(runtime.wall()),
            observed_fence=runtime.reads.fence(run.physical_table_id),
            owner=owner,
            writer_execution_id=run.writer_execution_id,
            statement_kind=statement.kind.value,
            start_envelope=statement.start_envelope,
            graph_claims=fences,
        )
    )
    start = runtime.start.start_table_attempt(
        StartTableAttemptRequest(
            start_envelope=statement.start_envelope,
            client_request_token=begin.client_request_token,
        )
    )
    query_execution_id: str | None = None
    if isinstance(start, Accepted):
        runtime.attempts.record_submitted_attempt(
            RecordSubmittedAttemptRequest(
                physical_table_id=run.physical_table_id,
                attempt_id=begin.attempt_id,
                envelope_digest=begin.envelope_digest,
                client_request_token=begin.client_request_token,
                query_execution_id=start.query_execution_id,
                submitted_at=instant(runtime.wall()),
                sanitized_start_evidence=start.sanitized_evidence,
            )
        )
        query_execution_id = start.query_execution_id
    else:
        run.owner.report(
            "semantic_statement_start_ambiguous",
            start.reason.value,
            LogSeverity.WARNING,
            attempt_id=begin.attempt_id,
        )
    return _drive_attempt(run, deadlines, begin.attempt_id, query_execution_id)
