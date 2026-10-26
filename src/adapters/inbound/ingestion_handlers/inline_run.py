# SPDX-License-Identifier: MIT-0
"""The one shared per-table inline-run boundary for every Stage-1 inline writer.

Before the final switch the transitional delivery Lambda and the two operators
(``ingest_delivery`` and ``reprocess_semantic_v2``) are inline writers
(write-discipline §9). Each drives one physical table at a time through this
module rather than through its own copy: ``PersistInlineRunRequest`` with only
the closed writer kind, the exact table lease, every merge/verify statement
through the shared §5 loop bound to the ``RUN#`` owner, and
``FinalizeInlineRequest`` while the lease is held. The one
``TABLE_INLINE_RUN`` span opens after run persistence and before the first
statement, carries only the persisted public run identity plus the declared
safe locators, and closes with the run's closed outcome (§10).

Callers supply nothing status-owned: public identity, topology, and event
meaning come from the domain's inline-run identity and lifecycle proof
derivations only. This module reads no configuration and constructs no client;
the calling writer composes :class:`InlineRunRuntime` from its own settings.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Protocol, cast

from adapters.inbound.ingestion_handlers.table_attempt_protocol import (
    PROJECTION_INVALID,
    AttemptReads,
    LocalStatementRefusedError,
    StatementCompleted,
    StatementParked,
    StatementProtocolRuntime,
    StatementResult,
    StatementRun,
    instant,
    run_statement,
    verification_holds,
)
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import TABLE_INLINE_RUN, span, trace_boundary
from domain.operations.stage1_lifecycle_head import (
    Stage1LifecycleHeadTuple,
    Stage1LifecycleProof,
    Stage1LifecycleProofRequest,
    Stage1LifecycleTransition,
    derive_stage1_lifecycle_proof,
    stage1_head_authority_document,
)
from domain.operations.stage1_operational_status import (
    InlineIdentityRequest,
    LegitimateRootSource,
    OwnerDisposition,
    PointerExecutionSource,
    PointerExecutionSourceKind,
    PointerIdentityRequest,
    PriorOwnerEventSource,
    RootDiscriminator,
    SameTransactionTerminalSource,
    SettleStage1AthenaUsageRequest,
    Stage1IdentityResult,
    Stage1OwnerTransition,
    Stage1TopologyRequest,
    derive_inline_identity,
)
from domain.operations.stage1_operational_status import (
    ProviderTerminalState as StatusProviderTerminalState,
)
from domain.operations.statement_protocol import (
    InsufficientStatementBudget,
    StatementStartBounds,
    unit_statement_kinds,
)
from domain.operations.status import (
    MEASUREMENT_REFS,
    CompletionOutcome,
    EvaluationBinding,
    FlowState,
)
from domain.operations.table_attempt import (
    AttemptObservation,
    ProviderTerminalState,
    RunIdentityRequest,
    WriterKind,
)
from domain.operations.table_lease import TableLeaseRecord, inline_lease_holder
from domain.shared_kernel.errors import DomainError, InvariantError
from ports.outbound.projection_coordinator import (
    RUN_RESERVATION_CAP,
    AcquireTableFenceOutcome,
    AcquireTableFenceRequest,
    AthenaAttemptSettlement,
    BeginInlineRunOwner,
    CurrentPointerOutcome,
    FinalizeInlineRequest,
    GraphClaimFence,
    InlineRunAttemptOwner,
    PersistInlineRunRequest,
    PointerOutcomeKind,
    ProjectionCoordinatorPort,
    RecordStage1RefusalRequest,
    RefusedInlineRunObservation,
    ReleaseTableFenceRequest,
    Stage1RefusalReason,
    Stage1RefusalTag,
    Stage1SettlementExpansion,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from aws_lambda_powertools import Logger

    from adapters.outbound.athena.semantic_storage import UnitWorkload
    from adapters.outbound.athena.table_attempt_statements import UnitStatementComposition
    from ports.outbound.table_attempt import TableAttemptStore
    from ports.outbound.table_attempt_start import TableAttemptStartClient
    from ports.outbound.table_attempt_terminal import TableAttemptTerminalClient
    from ports.outbound.unit_staging import UnitStager

__all__ = [
    "InlineAuthorityReads",
    "InlineRunOutcome",
    "InlineRunRequest",
    "InlineRunResult",
    "InlineRunRuntime",
    "ServablePointerTarget",
    "run_inline_table",
]

_INLINE_MEASUREMENT: Final = "semantic_inline_table_run"
_INLINE_STAGE_RECORDED: Final = "inline_run_recorded"
_INLINE_STAGE_DISPOSITION: Final = "inline_run_disposition"
_INLINE_STAGE_TERMINAL: Final = "inline_run_terminal"
_POINTER_STAGE: Final = "projection_servable"
_POINTER_REBUILD_LIMIT: Final = 3
# Every inline writer's first recorded event is a legitimate root whose discriminator
# is fixed by the status contract per closed writer kind; no other pairing exists.
_ROOT_BY_WRITER: Final[dict[WriterKind, tuple[Stage1OwnerTransition, RootDiscriminator]]] = {
    WriterKind.TRANSITIONAL_DELIVERY: (
        Stage1OwnerTransition.FIRST_TRANSITIONAL_INLINE_RECORDED,
        RootDiscriminator.TRANSITIONAL_DELIVERY_INLINE,
    ),
    WriterKind.INGEST_DELIVERY_OPERATOR: (
        Stage1OwnerTransition.FIRST_INGEST_OPERATOR_INLINE_RECORDED,
        RootDiscriminator.INGEST_DELIVERY_OPERATOR_INLINE,
    ),
    WriterKind.REPROCESS_SEMANTIC_V2_OPERATOR: (
        Stage1OwnerTransition.FIRST_REPROCESS_INLINE_RECORDED,
        RootDiscriminator.REPROCESS_SEMANTIC_V2_OPERATOR_INLINE,
    ),
    WriterKind.GRAPH_UNIT: (
        Stage1OwnerTransition.FIRST_GRAPH_UNIT_INLINE_RECORDED,
        RootDiscriminator.GRAPH_UNIT_INLINE,
    ),
}


# --- Seams and typed request/result ---------------------------------------------------------------


class InlineAuthorityReads(AttemptReads, Protocol):
    """The strong reads one inline run needs beside the shared fence/attempt reads."""

    def run_item(self, table_id: str, run_id: str) -> dict[str, object] | None:
        """Strongly read the exact ``TABLE_ATTEMPT#<table>/RUN#<run id>`` item."""
        ...

    def head(self, flow_id: str) -> Stage1LifecycleHeadTuple | None:
        """Strongly read the run flow's lifecycle ``HEAD``, or ``None`` before the first event."""
        ...

    def flow_root_clock(self, flow_id: str) -> datetime:
        """Strongly read the flow's retained root event clock for successor transitions."""
        ...

    def lease(self, table_id: str) -> TableLeaseRecord | None:
        """Strongly read the table's ``TABLE#`` lease record, or ``None`` when absent."""
        ...

    def current_item(self, partition_identity: str) -> dict[str, object] | None:
        """Strongly read one canonical partition's ``/CURRENT`` pointer item."""
        ...


@dataclass(frozen=True, slots=True)
class InlineRunRuntime:
    """The ports, limits, and clocks an inline writer composes for its runs.

    Limits arrive already resolved from the caller's own §11 settings accessor so
    this module never reads configuration itself.
    """

    coordinator: ProjectionCoordinatorPort
    attempts: TableAttemptStore
    start: TableAttemptStartClient
    terminals: TableAttemptTerminalClient
    reads: InlineAuthorityReads
    stager: UnitStager
    """ADR-149: writes each run's staged unit just before its MERGE begins."""
    bounds: StatementStartBounds
    lease_duration_seconds: int
    wall: Callable[[], datetime]
    monotonic_ms: Callable[[], int]
    sleep_ms: Callable[[int], None]
    logger: Logger

    def __post_init__(self) -> None:
        if isinstance(self.lease_duration_seconds, bool) or self.lease_duration_seconds < 1:
            raise ValueError("the table lease duration must be a positive integer of seconds.")


@dataclass(frozen=True, slots=True)
class ServablePointerTarget:
    """One named canonical-partition ``/CURRENT`` pointer a current-table run projects."""

    partition_key: str
    publication_id: str
    fencing_token: int

    def __post_init__(self) -> None:
        if not self.partition_key.strip() or not self.publication_id.strip():
            raise ValueError("a servable pointer target names its partition and publication.")
        if isinstance(self.fencing_token, bool) or self.fencing_token < 1:
            raise ValueError("a servable pointer target carries a positive fencing token.")


@dataclass(frozen=True, slots=True)
class InlineRunRequest:
    """One physical table's inline run: closed writer kind, identities, and statement roster.

    ``execution_id`` is the run's immutable internal execution identity; the public
    status UUIDs are derived from it and never supplied. ``servable`` is empty for
    source and candidate tables, which take the lease but own no pointers.

    S6 §2.5: a graph unit (``WriterKind.GRAPH_UNIT``) passes ``servable=()`` (the unit writes
    no pointer; ``publish`` stages its targets) and ``graph_claims``, which every Begin carries
    so a takeover's lease bump refuses the attempt (``GraphAuthorityRefusedError``). Slice 7
    (1b): one fence per execution-set member, re-read by the thunk for each Begin.
    """

    writer_kind: WriterKind
    operation_id: str
    execution_id: str
    physical_table_id: str
    table_kind: str
    workload: UnitWorkload
    composition: UnitStatementComposition
    remaining_budget_ms: Callable[[], int]
    servable: tuple[ServablePointerTarget, ...] = ()
    pointer_finalization_payload: str | None = None
    graph_claims: Callable[[], tuple[GraphClaimFence, ...]] | None = None

    def __post_init__(self) -> None:
        if not self.table_kind.strip():
            raise ValueError("an inline run names its exact table kind.")
        if (self.writer_kind is WriterKind.GRAPH_UNIT) != (self.graph_claims is not None):
            raise ValueError("exactly a graph-unit run carries the graph claim fence.")
        if self.writer_kind is WriterKind.GRAPH_UNIT and self.servable:
            raise ValueError("a graph-unit run owns no servable pointers.")
        if f"{self.workload.database}.{self.workload.table.name}" != self.physical_table_id:
            raise ValueError("the workload's database-qualified table must be the run's table.")
        partitions = [target.partition_key for target in self.servable]
        if len(set(partitions)) != len(partitions):
            raise ValueError("servable pointer targets must name distinct partitions.")
        self.run_identity()  # canonical tuple or typed rejection

    def run_identity(self) -> RunIdentityRequest:
        """Return the validated immutable ``run_id`` tuple this request names."""
        return RunIdentityRequest(
            writer_kind=self.writer_kind,
            operation_id=self.operation_id,
            execution_id=self.execution_id,
            physical_table_id=self.physical_table_id,
        )

    def identity_request(self) -> InlineIdentityRequest:
        """Return the status-contract identity member for this run."""
        return InlineIdentityRequest(
            self.writer_kind.value, self.operation_id, self.physical_table_id, self.execution_id
        )

    @property
    def statement_kinds(self) -> tuple[str, ...]:
        """The immutable statement roster persisted with the run."""
        return tuple(kind.value for kind in unit_statement_kinds(self.composition.merge_count))


class InlineRunOutcome:
    """The closed outcome labels one inline run reports; ``retryable`` marks the typed retries."""

    FINALIZED: Final = "finalized"
    REFUSED_TABLE_BUSY: Final = "refused_table_busy"
    REFUSED_TABLE_ATTEMPT_PENDING: Final = "refused_table_attempt_pending"
    INSUFFICIENT_BUDGET: Final = "insufficient_budget"
    ATTEMPT_PARKED: Final = "attempt_parked"
    STATEMENT_FAILED: Final = "statement_failed"
    VERIFICATION_MISMATCH: Final = "verification_mismatch"
    ATTEMPT_BUDGET_EXHAUSTED: Final = "attempt_budget_exhausted"
    """S6 §2.5: a graph unit's Begin would pass the 10-reservation run cap (no Athena call)."""


@dataclass(frozen=True, slots=True)
class InlineRunResult:
    """One inline run's typed outcome, persisted run identity, and public flow identity.

    The public flow/execution pair is the owner-persisted identity proof the delivery's
    structured terminal record carries so the §13 reporter's transport family can join
    terminals by exact identity rather than by any forbidden heuristic.
    """

    outcome: str
    run_id: str
    retryable: bool
    flow_id: str
    execution_id: str
    predecessor_execution_id: str | None
    deadline_cancelled: bool = False
    """Slice 7 (D43): a `STATEMENT_FAILED` whose provider terminal followed this invocation's
    own cancellation request at the cancellation deadline, a time outcome. False otherwise."""


def _result(  # noqa: PLR0913 - the typed result and its one D43 flag
    ctx: _RunContext, outcome: str, *, retryable: bool, deadline_cancelled: bool = False
) -> InlineRunResult:
    """Build the typed result carrying the run's persisted public identity proof."""
    return InlineRunResult(
        outcome=outcome,
        run_id=ctx.run_id,
        retryable=retryable,
        flow_id=ctx.established_flow_id,
        execution_id=ctx.established_execution_id,
        predecessor_execution_id=ctx.established_predecessor_execution_id,
        deadline_cancelled=deadline_cancelled,
    )


# --- Private run state ----------------------------------------------------------------------------


@dataclass(slots=True)
class _RunContext:
    """One inline run's mutable state after run persistence."""

    runtime: InlineRunRuntime
    request: InlineRunRequest
    identity: Stage1IdentityResult
    run_id: str
    head: Stage1LifecycleHeadTuple
    clock_started_at: datetime
    established_flow_id: str
    established_execution_id: str
    established_predecessor_execution_id: str | None
    lease: TableLeaseRecord | None

    @property
    def table_id(self) -> str:
        return self.request.physical_table_id


class _StatementFailedError(Exception):
    """One statement reached a closed non-retryable disposition."""

    def __init__(self, outcome: str, description: str, *, deadline_cancelled: bool = False) -> None:
        super().__init__(description)
        self.outcome = outcome
        self.deadline_cancelled = deadline_cancelled


def _invariant(description: str, **context: object) -> InvariantError:
    return InvariantError(PROJECTION_INVALID, description, context=dict(context))


def _report(  # noqa: PLR0913, PLR0917 - one private reporting seam
    ctx: _RunContext, operation: str, outcome: str, severity: LogSeverity, **details: object
) -> None:
    report_operation(
        OperationLog(
            operation=operation,
            component="inline_run",
            phase="run",
            outcome=outcome,
            severity=severity,
            details={
                "writer_kind": ctx.request.writer_kind.value,
                "internal_operation_id": ctx.request.operation_id,
                "physical_table_id": ctx.table_id,
                "run_id": ctx.run_id,
                # §4.8: the receive (legacy inline) or set (GRAPH) every line belongs to.
                "execution_id": ctx.request.execution_id,
                **details,
            },
        ),
        ctx.runtime.logger,
    )


def _fresh_run_item(ctx: _RunContext) -> dict[str, object]:
    item = ctx.runtime.reads.run_item(ctx.table_id, ctx.run_id)
    if item is None:
        raise _invariant("The inline run item disappeared during processing.", run_id=ctx.run_id)
    return item


def _required_lease(ctx: _RunContext) -> TableLeaseRecord:
    if ctx.lease is None:
        raise _invariant("Statement processing requires the held table lease.")
    return ctx.lease


# --- Lifecycle proofs -----------------------------------------------------------------------------


def _internal_key(request: InlineRunRequest) -> tuple[str, ...]:
    return (
        request.writer_kind.value,
        request.operation_id,
        request.physical_table_id,
        request.execution_id,
    )


def _recorded_proof(  # noqa: PLR0913, PLR0917 - one private proof constructor
    request: InlineRunRequest,
    head: Stage1LifecycleHeadTuple | None,
    now: datetime,
    clock_started_at: datetime | None,
) -> Stage1LifecycleProof:
    if head is None:
        transition, discriminator = _ROOT_BY_WRITER[request.writer_kind]
        topology = Stage1TopologyRequest(transition, LegitimateRootSource(discriminator))
    else:
        topology = _later_topology(head)
    return derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=request.identity_request(),
            internal_key=_internal_key(request),
            topology=topology,
            transition=Stage1LifecycleTransition(
                "semantic_inline_run_recorded", FlowState.RUNNING, None, None
            ),
            stage=_INLINE_STAGE_RECORDED,
            authority_at=now,
            prior_head=head,
            clock_started_at=clock_started_at,
        )
    )


def _later_topology(head: Stage1LifecycleHeadTuple) -> Stage1TopologyRequest:
    return Stage1TopologyRequest(
        Stage1OwnerTransition.LATER_INLINE_EVENT,
        PriorOwnerEventSource(
            head.parent_flow_id, head.latest_event_id, stage1_head_authority_document(head)
        ),
    )


def _run_event_proof(  # noqa: PLR0913, PLR0917 - one private proof constructor
    ctx: _RunContext,
    transition_id: str,
    state: FlowState,
    outcome: CompletionOutcome | None,
    typed_reason: str | None,
    stage: str,
    now: datetime,
) -> Stage1LifecycleProof:
    return derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=ctx.request.identity_request(),
            internal_key=_internal_key(ctx.request),
            topology=_later_topology(ctx.head),
            transition=Stage1LifecycleTransition(transition_id, state, outcome, typed_reason),
            stage=stage,
            authority_at=now,
            prior_head=ctx.head,
            clock_started_at=ctx.clock_started_at,
        )
    )


def _pointer_proof(  # noqa: PLR0913, PLR0917 - one private proof constructor
    ctx: _RunContext,
    target: ServablePointerTarget,
    terminal: Stage1LifecycleProof,
    now: datetime,
) -> Stage1LifecycleProof:
    return derive_stage1_lifecycle_proof(
        Stage1LifecycleProofRequest(
            identity=PointerIdentityRequest(
                target.partition_key,
                target.publication_id,
                target.fencing_token,
                PointerExecutionSource(
                    PointerExecutionSourceKind.INLINE_RUN_EXECUTION, ctx.request.execution_id
                ),
            ),
            internal_key=(target.partition_key, target.publication_id, target.fencing_token),
            topology=Stage1TopologyRequest(
                Stage1OwnerTransition.INLINE_NONZERO_POINTER,
                SameTransactionTerminalSource(terminal.flow_id, terminal.event_id),
            ),
            transition=Stage1LifecycleTransition(
                "semantic_projection_servable",
                FlowState.COMPLETED,
                CompletionOutcome.SUCCEEDED,
                None,
            ),
            stage=_POINTER_STAGE,
            authority_at=now,
            prior_head=None,
            publication_partition_key=target.partition_key,
        )
    )


# --- §9 step 2: run persistence -------------------------------------------------------------------


def _persist_run(runtime: InlineRunRuntime, request: InlineRunRequest) -> _RunContext:
    identity = derive_inline_identity(request.identity_request())
    head = runtime.reads.head(identity.flow_id)
    now = runtime.wall()
    clock = None if head is None else runtime.reads.flow_root_clock(identity.flow_id)
    proof = _recorded_proof(request, head, now, clock)
    result = runtime.coordinator.persist_inline_run(
        PersistInlineRunRequest(
            writer_kind=request.writer_kind,
            operation_id=request.operation_id,
            execution_id=request.execution_id,
            physical_table_id=request.physical_table_id,
            table_kind=request.table_kind,
            statement_intent_kinds=request.statement_kinds,
            pointer_finalization_payload=request.pointer_finalization_payload,
            created_at=instant(now),
            proof=proof,
        )
    )
    resulting = proof.resulting_head
    if resulting is None:  # pragma: no cover - run proofs always carry a head effect
        raise _invariant("The recorded proof carries no resulting run head.")
    # Exact replay reuses the persisted identity: the durable head is authoritative
    # over the locally derived one whenever it already exists. The flow clock is
    # likewise read back from the retained root event, which durably exists once
    # persistence returns, so every later transition repeats the applied root clock.
    persisted_head = runtime.reads.head(identity.flow_id)
    return _RunContext(
        runtime=runtime,
        request=request,
        identity=identity,
        run_id=result.run_id,
        head=persisted_head if persisted_head is not None else resulting,
        clock_started_at=runtime.reads.flow_root_clock(identity.flow_id),
        # The coordinator has now atomically persisted this proof, or classified its
        # retained event bytes as an exact/advanced replay. Keep the complete public
        # execution proof from that established transaction; never mix a locally derived
        # identity with whatever newer HEAD happens to be visible when the result returns.
        established_flow_id=proof.flow_id,
        established_execution_id=proof.execution_id,
        established_predecessor_execution_id=resulting.predecessor_execution_id,
        lease=None,
    )


# --- §9 step 3: fence, lease, and inline refusals -------------------------------------------------


def _record_refusal(ctx: _RunContext, reason: Stage1RefusalReason) -> None:
    now = ctx.runtime.wall()
    proof = _run_event_proof(
        ctx,
        "semantic_inline_run_refused",
        FlowState.RETRYING,
        None,
        reason.value.lower(),
        _INLINE_STAGE_DISPOSITION,
        now,
    )
    ctx.runtime.coordinator.record_stage1_refusal(
        RecordStage1RefusalRequest(
            tag=Stage1RefusalTag.INLINE,
            reason=reason,
            refusal_instant=instant(now),
            proof=proof,
            inline=RefusedInlineRunObservation(
                InlineRunAttemptOwner(ctx.table_id, ctx.run_id, _fresh_run_item(ctx))
            ),
        )
    )
    resulting = proof.resulting_head
    if resulting is not None:
        ctx.head = resulting
    _report(ctx, "semantic_inline_run_refused", reason.value, LogSeverity.WARNING)


def _acquire_lease(ctx: _RunContext) -> str | None:
    """Run the fence check and lease acquisition; refusals return their retryable label."""
    runtime = ctx.runtime
    fence = runtime.reads.fence(ctx.table_id)
    if fence is not None and fence.state in ("active", "parked"):
        _record_refusal(ctx, Stage1RefusalReason.TABLE_ATTEMPT_PENDING)
        return InlineRunOutcome.REFUSED_TABLE_ATTEMPT_PENDING
    now = runtime.wall()
    holder = inline_lease_holder(ctx.request.run_identity())
    expires = (
        now.replace(microsecond=0) + timedelta(seconds=runtime.lease_duration_seconds)
    ).isoformat()
    result = runtime.coordinator.acquire_table_fence(
        AcquireTableFenceRequest(
            physical_table_id=ctx.table_id,
            holder=holder,
            operation_instant=instant(now),
            new_expires_at=expires,
            observed_lease=runtime.reads.lease(ctx.table_id),
            observed_fence=fence,
        )
    )
    if result.outcome is AcquireTableFenceOutcome.TABLE_BUSY:
        _record_refusal(ctx, Stage1RefusalReason.TABLE_BUSY)
        return InlineRunOutcome.REFUSED_TABLE_BUSY
    if result.outcome is AcquireTableFenceOutcome.TABLE_ATTEMPT_PENDING:
        _record_refusal(ctx, Stage1RefusalReason.TABLE_ATTEMPT_PENDING)
        return InlineRunOutcome.REFUSED_TABLE_ATTEMPT_PENDING
    if result.fencing_token is None or result.expires_at is None:  # pragma: no cover
        raise _invariant("An acquired table fence carries its grant fields.")
    ctx.lease = TableLeaseRecord(
        physical_table_id=ctx.table_id,
        holder=holder,
        fencing_token=result.fencing_token,
        expires_at=result.expires_at,
    )
    return None


def _release_lease(ctx: _RunContext) -> None:
    """Release the held lease against an absent-or-clear fence (§9 step 5)."""
    lease = ctx.lease
    if lease is None:
        return
    ctx.lease = None
    fence = ctx.runtime.reads.fence(ctx.table_id)
    if fence is not None and fence.state in ("active", "parked"):
        # Only provider-terminal completion clears the fence; leave the parked pair.
        return
    try:
        ctx.runtime.coordinator.release_table_fence(
            ReleaseTableFenceRequest(
                physical_table_id=ctx.table_id, lease=lease, observed_fence=fence
            )
        )
    except DomainError:
        # A lost release condition is availability-only: the lease reaches expiry.
        _report(ctx, "semantic_table_lease_release", "lost", LogSeverity.WARNING)


# --- §9 step 3: statements through the shared §5 loop ---------------------------------------------


class _InlineStatementOwner:
    """The run's §5 owner seam: fresh ``RUN#`` snapshots and the run's reporting."""

    __slots__ = ("_ctx",)

    def __init__(self, ctx: _RunContext) -> None:
        self._ctx = ctx

    def begin_owner(self) -> BeginInlineRunOwner:
        """Re-read the run; the port's own cap rejects Begin at the 10-reservation run cap.

        S6 §2.5: a graph unit preflights the cap, so reaching it is the typed final outcome
        ``attempt_budget_exhausted`` rather than an unclassified refusal.
        """
        owner = self.attempt_owner()
        if (
            self._ctx.request.writer_kind is WriterKind.GRAPH_UNIT
            and _reservations(owner) >= RUN_RESERVATION_CAP
        ):
            raise _StatementFailedError(
                InlineRunOutcome.ATTEMPT_BUDGET_EXHAUSTED,
                "the graph unit run reached its permanent 10-reservation cap.",
            )
        return BeginInlineRunOwner(owner)

    def attempt_owner(self) -> InlineRunAttemptOwner:
        """Re-read the exact run snapshot completion and cancellation condition on."""
        ctx = self._ctx
        return InlineRunAttemptOwner(ctx.table_id, ctx.run_id, _fresh_run_item(ctx))

    def report(
        self, operation: str, outcome: str, severity: LogSeverity, **details: object
    ) -> None:
        """Emit through the run's one private reporting seam."""
        _report(self._ctx, operation, outcome, severity, **details)


def _reservations(owner: InlineRunAttemptOwner) -> int:
    value = owner.item.get("accepted_attempt_reservations", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invariant("The inline run's reservation count is not an integer.")
    return value


def _statement_run(ctx: _RunContext) -> StatementRun:
    runtime = ctx.runtime
    return StatementRun(
        runtime=StatementProtocolRuntime(
            coordinator=runtime.coordinator,
            attempts=runtime.attempts,
            start=runtime.start,
            terminals=runtime.terminals,
            reads=runtime.reads,
            stager=runtime.stager,
            wall=runtime.wall,
            monotonic_ms=runtime.monotonic_ms,
            sleep_ms=runtime.sleep_ms,
        ),
        owner=_InlineStatementOwner(ctx),
        physical_table_id=ctx.table_id,
        lease=_required_lease(ctx),
        writer_execution_id=ctx.request.execution_id,
        bounds=runtime.bounds,
        remaining_budget_ms=ctx.request.remaining_budget_ms,
        graph_claims=ctx.request.graph_claims,
    )


def _execute_statements(ctx: _RunContext, completed: Callable[[int], None]) -> str | None:
    """Drive every composed statement through §5; return a retryable label or ``None``."""
    composition = ctx.request.composition
    for index, statement in enumerate(composition.statements):
        result: StatementResult
        try:
            result = run_statement(_statement_run(ctx), statement)
        except LocalStatementRefusedError as refused:
            raise _StatementFailedError(
                InlineRunOutcome.STATEMENT_FAILED, str(refused)
            ) from refused
        if isinstance(result, InsufficientStatementBudget):
            _report(
                ctx,
                "semantic_statement_budget",
                "insufficient",
                LogSeverity.WARNING,
                remaining_budget_ms=result.remaining_budget_ms,
            )
            return InlineRunOutcome.INSUFFICIENT_BUDGET
        if isinstance(result, StatementParked):
            ctx.lease = None  # parked: only the reconciler may clear the pair
            _report(
                ctx,
                "semantic_attempt_parked",
                "parked",
                LogSeverity.WARNING,
                attempt_id=result.attempt_id,
            )
            return InlineRunOutcome.ATTEMPT_PARKED
        _require_succeeded(ctx, result, statement is composition.verification)
        completed(index + 1)
    return None


def _require_succeeded(ctx: _RunContext, result: StatementCompleted, verification: bool) -> None:
    if result.state is not ProviderTerminalState.SUCCEEDED:
        raise _StatementFailedError(
            InlineRunOutcome.STATEMENT_FAILED,
            f"the provider terminal is {result.state.value}.",
            deadline_cancelled=result.cancel_requested,
        )
    if verification and not verification_holds(
        ctx.request.composition, ctx.runtime.terminals.readback_rows(result.query_execution_id)
    ):
        raise _StatementFailedError(
            InlineRunOutcome.VERIFICATION_MISMATCH,
            "the verification counts differ from the staged unit.",
        )


# --- §9 step 4: lease-held finalization -----------------------------------------------------------


def _settlement_expansion(ctx: _RunContext, observed_at: datetime) -> Stage1SettlementExpansion:
    """Expand this run's unsettled accepted provider-terminal attempts for settlement."""
    references = _fresh_run_item(ctx).get("attempt_references")
    attempt_ids = (
        [value for value in cast("list[object]", references) if isinstance(value, str)]
        if isinstance(references, list)
        else []
    )
    members: list[AthenaAttemptSettlement] = []
    for attempt_id in attempt_ids:
        observation = ctx.runtime.reads.attempt(ctx.table_id, attempt_id)
        if observation is None:
            raise _invariant("A referenced table attempt is unreadable.", attempt_id=attempt_id)
        if observation.athena_usage_state != "unsettled":
            continue
        members.append(_attempt_settlement(ctx, attempt_id, observation, observed_at))
    return Stage1SettlementExpansion(
        owner_disposition=OwnerDisposition.SUCCESSFUL, attempts=tuple(members)
    )


def _attempt_settlement(  # noqa: PLR0913, PLR0917 - one private settlement constructor
    ctx: _RunContext, attempt_id: str, observation: AttemptObservation, observed_at: datetime
) -> AthenaAttemptSettlement:
    state = observation.state
    if state is None:
        raise _invariant("A referenced table attempt has no terminal state.")
    return AthenaAttemptSettlement(
        attempt=observation,
        settlement=SettleStage1AthenaUsageRequest(
            attempt_id=attempt_id,
            flow_id=ctx.identity.flow_id,
            execution_id=ctx.identity.execution_id,
            evaluation_binding=EvaluationBinding(MEASUREMENT_REFS[_INLINE_MEASUREMENT]),
            owner_disposition=OwnerDisposition.SUCCESSFUL,
            observed_at=observed_at,
            provider_terminal_state=StatusProviderTerminalState(state),
            scanned_bytes=observation.scanned_bytes,
            engine_ms=observation.engine_ms,
        ),
    )


def _pointer_outcomes(
    ctx: _RunContext, terminal: Stage1LifecycleProof, now: datetime
) -> tuple[CurrentPointerOutcome, ...]:
    """Pre-resolve every named current pointer while the lease remains held."""
    outcomes: list[CurrentPointerOutcome] = []
    for target in ctx.request.servable:
        current = ctx.runtime.reads.current_item(target.partition_key)
        if current is None or current.get("publication_id") is None:
            raise _invariant(
                "A named current pointer has no authority publication.",
                partition=target.partition_key,
            )
        projected = current.get("projected_fencing_token")
        superseded = (
            isinstance(projected, int)
            and not isinstance(projected, bool)
            and projected >= target.fencing_token
            and current.get("projected_publication_id") not in (None, "NONE")
        )
        outcomes.append(
            CurrentPointerOutcome(
                kind=(
                    PointerOutcomeKind.PROJECTION_SUPERSEDED
                    if superseded
                    else PointerOutcomeKind.ADVANCED
                ),
                partition_identity=target.partition_key,
                projected_publication_id=target.publication_id,
                projected_fencing_token=target.fencing_token,
                proof=_pointer_proof(ctx, target, terminal, now),
                observed_current=None if superseded else current,
            )
        )
    return tuple(outcomes)


def _finalize_run(ctx: _RunContext) -> None:
    """Execute the one lease-held finalization transaction, rebuilding on pointer races."""
    for _ in range(_POINTER_REBUILD_LIMIT):
        now = ctx.runtime.wall()
        terminal = _run_event_proof(
            ctx,
            "semantic_inline_run_terminal",
            FlowState.COMPLETED,
            CompletionOutcome.SUCCEEDED,
            None,
            _INLINE_STAGE_TERMINAL,
            now,
        )
        request = FinalizeInlineRequest(
            physical_table_id=ctx.table_id,
            run=InlineRunAttemptOwner(ctx.table_id, ctx.run_id, _fresh_run_item(ctx)),
            lease=_required_lease(ctx),
            observed_fence=ctx.runtime.reads.fence(ctx.table_id),
            pointers=_pointer_outcomes(ctx, terminal, now),
            settlement=_settlement_expansion(ctx, now),
            operation_instant=instant(now),
            proof=terminal,
        )
        try:
            ctx.runtime.coordinator.finalize_inline(request)
        except DomainError:
            # A pointer conditional conflict rebuilds the whole transaction after
            # strong re-read; a newer pointer is never a run failure.
            continue
        return
    raise DomainError(
        PROJECTION_INVALID,
        "Inline finalization kept losing its conditions; the run stays open for retry.",
    )


# --- The one shared inline-run boundary -----------------------------------------------------------


def _accepted_attempts(ctx: _RunContext) -> int | None:
    """Read the finalized run's accepted reservations once, for telemetry only (§4.8).

    The run is already FINALIZED and its lease released, so a failed read is reported once
    and the terminal line carries ``None``; it never changes the run's outcome.
    """
    try:
        return _reservations(InlineRunAttemptOwner(ctx.table_id, ctx.run_id, _fresh_run_item(ctx)))
    except Exception as error:
        report_error(
            error,
            Report(
                operation="semantic_inline_run_terminal",
                correlation_id=ctx.request.execution_id,
                logger=ctx.runtime.logger,
            ),
        )
        return None


def _causation_event_id(head: Stage1LifecycleHeadTuple) -> str:
    return head.causation_event_id if head.causation_event_id is not None else head.latest_event_id


@trace_boundary(TABLE_INLINE_RUN)
def run_inline_table(runtime: InlineRunRuntime, request: InlineRunRequest) -> InlineRunResult:
    """Drive one physical table's inline run: persist, lease, statements, finalize, release.

    The ``TABLE_INLINE_RUN`` span opens only after ``PersistInlineRunRequest`` has
    committed or exactly replayed, so every identity it carries is the persisted
    public run identity; exact replay reuses that identity. Refusals, parking, and
    budget exhaustion return typed retryable outcomes and leave the run open; a
    failed statement or verification mismatch returns a non-retryable outcome with
    the run still open, because the run-failure terminal is not yet a coordinator
    operation. Nothing here performs an SQS operation or reads configuration.
    """
    ctx = _persist_run(runtime, request)
    total = len(request.composition.statements)
    with span(
        TABLE_INLINE_RUN,
        flow_id=ctx.identity.flow_id,
        parent_flow_id=ctx.head.parent_flow_id,
        causation_event_id=_causation_event_id(ctx.head),
        execution_id=ctx.identity.execution_id,
        predecessor_execution_id=ctx.head.predecessor_execution_id,
        internal_operation_id=request.operation_id,
        writer_kind=request.writer_kind.value,
        table_kind=request.table_kind,
        stage="run",
        completed=0,
        total=total,
    ) as run_span:
        refused = _acquire_lease(ctx)
        if refused is not None:
            run_span.record(outcome=refused)
            return _result(ctx, refused, retryable=True)
        try:
            retryable = _execute_statements(ctx, lambda done: run_span.record(completed=done))
            if retryable is not None:
                run_span.record(outcome=retryable)
                return _result(ctx, retryable, retryable=True)
            _finalize_run(ctx)
        except _StatementFailedError as failure:
            _report(
                ctx,
                "semantic_statement_failed",
                failure.outcome,
                LogSeverity.WARNING,
                description=str(failure),
            )
            run_span.note_failure(failure)
            run_span.record(outcome=failure.outcome)
            return _result(
                ctx,
                failure.outcome,
                retryable=False,
                deadline_cancelled=failure.deadline_cancelled,
            )
        finally:
            _release_lease(ctx)
        _report(
            ctx,
            "semantic_inline_run_terminal",
            InlineRunOutcome.FINALIZED,
            LogSeverity.INFO,
            accepted_attempts=_accepted_attempts(ctx),
            rows=sum(len(records) for _, records in request.workload.records_by_identity),
        )
        run_span.record(completed=total, outcome=InlineRunOutcome.FINALIZED)
        return _result(ctx, InlineRunOutcome.FINALIZED, retryable=False)
