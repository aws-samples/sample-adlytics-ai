# SPDX-License-Identifier: MIT-0
"""The settlement half of graph-unit execution in TableWriter (S6 §2.5; D5, D14).

TableWriter runs a claimed `write_unit:<k>` node through the shared inline run
(`run_inline_table`, `WriterKind.GRAPH_UNIT`, `servable=()`); this module decides and commits
what each inline outcome does to the node, under §2.4's settlement rule:

- `refused_table_busy`, `refused_table_attempt_pending`, `insufficient_budget`,
  `attempt_parked`: while attempts remain, the lease is released and the message stays
  unacknowledged; on the final attempt the node settles `FAILED` with the outcome as its reason,
  the run fenced `NODE_FAILED` while it is live.
- `statement_failed` on a member alone (slice 7, difference 4): a return while attempts remain
  (`member_failed`, `arm_alone`, its lock share released, acknowledged), `FAILED` when final.
- `verification_mismatch` and a checksum mismatch: immediately final, the same settlement.
- Begin at the 10-reservation run cap: final, `FAILED` `attempt_budget_exhausted`, fenced.
- `finalized`: `SUCCEEDED`, lease-fenced, with the effect record, releasing `TABLE#<k>` (D14).

An exception is classified by error class (`classify_failure`): a `DependencyError` is
retryable while receives remain, any other `AdlyticsError` is final at once, and an unclassified
exception is retryable until the final receive. Every final outcome is reported once with
`GRAPH_RUN_FAILED` and makes no legacy coordinator write. A guard failure re-reads and
re-decides up to `max_redecide`; a lost claim acknowledges with zero effect.

The effect record names the unit's servable targets (at most 37) and its Stage-1 inline flow,
so a v2 explanation joins the node to that flow by exact identity; `publish` stages the targets.

Slice 7 (1b): "final" is the member's final attempt, `unit_final_attempt` over the receive count,
the claim's `admission` and `final_return` (difference 5); a success set settles through
`settle_unit_set` (`settle_set_succeeded`, D34); and every member of a batch ends in exactly one
`MemberClass` (§2.5 step 8).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from adapters.inbound.ingestion_handlers.graph_claim import release_claim_lease
from adapters.inbound.ingestion_handlers.graph_materialize import (
    GRAPH_RUN_FAILED,
    classify_failure,
)
from adapters.inbound.ingestion_handlers.inline_run import InlineRunOutcome
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    report_error,
    report_operation,
)
from domain.operations.execution_graph_dispatch import (
    LIVE_RUN_STATES,
    TERMINAL_NODE_STATES,
    FenceReason,
    GraphGuardError,
    NodeState,
    ReturnReason,
    unit_final_attempt,
)
from domain.operations.generated_execution_graphs import BOUNDS, ROUTES
from domain.operations.status import canonical_authority_digest
from domain.operations.write_plan import MAX_OBJECTS_PER_UNIT
from domain.shared_kernel.errors import (
    GRAPH_UNIT_BATCH_UNSETTLED_CODE,
    AdlyticsError,
    ErrorCode,
    InvariantError,
)
from ports.outbound.execution_graph_dispatch import (
    ClaimRecord,
    FenceRunRequest,
    GraphDispatchStore,
    GraphTransactionConflictError,
    MaterializationSettlement,
    NodeRecord,
    NodeSettlement,
    ReturnUnitRequest,
    RunRecord,
    UnitSetGuardError,
    UnitSetMember,
    UnitSetSettlement,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

__all__ = [
    "CHECKSUM_MISMATCH",
    "MAX_RECEIVES",
    "UNIT_EFFECT_NAMESPACE",
    "UNIT_NODE",
    "GraphUnitFailedError",
    "MemberClass",
    "MemberSettled",
    "UnitAction",
    "UnitBatchSettled",
    "UnitClaim",
    "UnitSettled",
    "UnitSuccess",
    "UnitTarget",
    "claim_lost",
    "decide",
    "failure_code",
    "member_class",
    "retry",
    "return_member",
    "settle_error",
    "settle_failed",
    "settle_set_succeeded",
    "settle_shaping_error",
    "unit_effect_digest",
    "unit_effect_record",
]

UNIT_NODE: Final = "write_unit"
MAX_RECEIVES: Final = int(ROUTES["semantic_write"]["max_receive_count"])
"""The semantic-write route's receive budget; it replaces the legacy writer's setting."""
UNIT_EFFECT_NAMESPACE: Final = "execution-graph-unit-effect/v1"
CHECKSUM_MISMATCH: Final = "checksum_mismatch"
_MAX_REDECIDE: Final = int(BOUNDS["max_redecide"])
_OPERATION: Final = "semantic_table_writer"
_RETRY_WHILE_RECEIVES_REMAIN: Final = frozenset(
    {
        InlineRunOutcome.REFUSED_TABLE_BUSY,
        InlineRunOutcome.REFUSED_TABLE_ATTEMPT_PENDING,
        InlineRunOutcome.INSUFFICIENT_BUDGET,
        InlineRunOutcome.ATTEMPT_PARKED,
    }
)
_RETURN_WHILE_ATTEMPTS_REMAIN: Final = frozenset({InlineRunOutcome.STATEMENT_FAILED})
"""Slice 7 (difference 4): a lone member's own statement failure returns it, never holds the
table across a retry."""
_IMMEDIATELY_FINAL: Final = frozenset(
    {
        InlineRunOutcome.VERIFICATION_MISMATCH,
        InlineRunOutcome.ATTEMPT_BUDGET_EXHAUSTED,
        CHECKSUM_MISMATCH,
    }
)


class UnitAction(StrEnum):
    """What one inline outcome does to the unit node."""

    SUCCEED = "succeed"
    RETRY = "retry"
    RETURN = "return"
    FAIL = "fail"


def decide(outcome: str, *, final: bool) -> UnitAction:
    """Apply the §2.5 outcome table to a set of exactly one member's inline outcome.

    `final` is the member's `unit_final_attempt`. A set of more than one member takes the
    split, unresolved or budget rows in TableWriter instead (§2.5 steps 7 and 8).
    """
    if outcome == InlineRunOutcome.FINALIZED:
        return UnitAction.SUCCEED
    if outcome in _RETRY_WHILE_RECEIVES_REMAIN:
        return UnitAction.FAIL if final else UnitAction.RETRY
    if outcome in _RETURN_WHILE_ATTEMPTS_REMAIN:
        return UnitAction.FAIL if final else UnitAction.RETURN
    if outcome in _IMMEDIATELY_FINAL:
        return UnitAction.FAIL
    raise InvariantError(
        ErrorCode("TABLE_WRITER", 5),
        "An inline run returned an outcome outside the graph-unit table.",
        context={"outcome": outcome},
    )


def failure_code(cause: BaseException | None) -> str:
    """Return the error code of the outcome that failed a node (D35: the node's `failure_code`)."""
    if cause is None:
        return "none"
    if isinstance(cause, AdlyticsError):
        return str(cause.code)
    return type(cause).__name__


class GraphUnitFailedError(InvariantError):
    """One final `write_unit` settlement, reported once (`GRAPH_RUN_FAILED`).

    Slice 7 (D35): the context also names the member's batch and its `error_code`, so the
    alarm's log line names the batch the member failed in.
    """

    def __init__(  # noqa: PLR0913 -- the reason, the cause and two keyword-only facts
        self,
        failure_reason: str,
        cause: BaseException | None,
        *,
        fenced: bool,
        batch_id: str | None = None,
    ) -> None:
        cause_code = failure_code(cause)
        super().__init__(
            ErrorCode("TABLE_WRITER", 6),
            "a write_unit node settled FAILED; its run is fenced or already fencing.",
            context={
                "signal": GRAPH_RUN_FAILED,
                "failure_reason": failure_reason,
                "cause": cause_code,
                "fenced": fenced,
                "batch_id": batch_id,
                "error_code": cause_code,
            },
        )
        self.signal = GRAPH_RUN_FAILED


# --- The effect record ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnitTarget:
    """One servable target of a finalized unit: what `publish` stages (§2.6)."""

    partition: str
    publication_id: str
    fencing_token: int


def unit_effect_record(
    unit_key: str, stage1_flow_id: str, targets: tuple[UnitTarget, ...]
) -> tuple[str, str]:
    """Return a finalized unit's canonical effect record and its digest (D14)."""
    if len(targets) > MAX_OBJECTS_PER_UNIT:
        raise InvariantError(
            ErrorCode("TABLE_WRITER", 5),
            "A graph unit names more servable targets than a unit carries objects.",
            context={"targets": len(targets)},
        )
    ordered = sorted(targets, key=lambda target: (target.partition, target.publication_id))
    record = json.dumps(
        {
            "schema_version": "1",
            "node": UNIT_NODE,
            "unit_key": unit_key,
            "stage1_flow_id": stage1_flow_id,
            "servable_targets": [
                {
                    "partition": target.partition,
                    "publication_id": target.publication_id,
                    "fencing_token": target.fencing_token,
                }
                for target in ordered
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return record, unit_effect_digest(record)


def unit_effect_digest(record: str) -> str:
    """Hash one unit effect record; `publish` re-hashes it on validation (§2.6)."""
    return canonical_authority_digest([UNIT_EFFECT_NAMESPACE, record])


# --- Settlement -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnitClaim:
    """One claimed unit node and what its settlement needs."""

    store: GraphDispatchStore
    claim: ClaimRecord
    dispatch_id: str
    receive_count: int
    now: Callable[[], datetime]
    report: Report
    final_return: bool = False
    """Slice 7: the node carries `final_return`, so this attempt is its last (D35)."""
    batch_id: str | None = None
    """Slice 7: the batch the member was claimed in."""

    @property
    def admission(self) -> int:
        """The claim's admission counter: 1 at CLAIM, plus one per RECLAIM (difference 5)."""
        return self.claim.admission

    @property
    def final(self) -> bool:
        """Whether this is the member's final attempt (`unit_final_attempt`, D35, F5)."""
        return unit_final_attempt(
            self.receive_count, self.admission, final_return=self.final_return
        )

    @property
    def run_id(self) -> str:
        """The claimed node's run."""
        return self.claim.run_id

    @property
    def node_id(self) -> str:
        """The claimed node."""
        return self.claim.node_id


@dataclass(frozen=True, slots=True)
class UnitSettled:
    """One receive's acknowledgement decision and the node state it left."""

    acknowledge: bool
    outcome: str
    node_state: NodeState | None = None
    failure_reason: str | None = None


class MemberClass(StrEnum):
    """The one class every listed batch member ends an invocation in (§2.5 step 8)."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETURNED = "returned"
    SKIPPED = "skipped"
    """Stale, lost, already settled, superseded or taken over: acknowledged, zero effect."""
    LIVE_DUPLICATE = "live_duplicate"
    UNRESOLVED = "unresolved"


_CLASS_BY_OUTCOME: Final = {
    "graph_unit_succeeded": MemberClass.SUCCEEDED,
    "graph_unit_failed": MemberClass.FAILED,
    "graph_unit_returned": MemberClass.RETURNED,
    "graph_unit_retryable": MemberClass.UNRESOLVED,
    "graph_redecide_exhausted": MemberClass.UNRESOLVED,
    "graph_unit_claim_refused_live": MemberClass.UNRESOLVED,
    "graph_claim_live": MemberClass.LIVE_DUPLICATE,
    "graph_claim_lost": MemberClass.SKIPPED,
    "graph_already_settled": MemberClass.SKIPPED,
    "graph_batch_member_stale": MemberClass.SKIPPED,
    "execution_superseded": MemberClass.SKIPPED,
    "graph_awaiting_provider_terminal": MemberClass.SKIPPED,
}


def member_class(settled: UnitSettled) -> MemberClass:
    """Classify one member's settlement; an outcome outside the closed map is an invariant."""
    found = _CLASS_BY_OUTCOME.get(settled.outcome)
    if found is None:
        raise InvariantError(
            ErrorCode("TABLE_WRITER", 5),
            "a batch member's settlement is outside the closed member classes.",
            context={"outcome": settled.outcome},
        )
    return found


@dataclass(frozen=True, slots=True)
class MemberSettled:
    """One batch member's class and the settlement that put it there (M3)."""

    node_id: str
    member_class: MemberClass
    settled: UnitSettled


@dataclass(frozen=True, slots=True)
class UnitBatchSettled:
    """One batch invocation's acknowledgement, its one counted outcome, and every member (M3)."""

    acknowledge: bool
    outcome: str
    members: tuple[MemberSettled, ...]


@dataclass(frozen=True, slots=True)
class UnitSuccess:
    """One finalized member and its own effect record (§2.5 step 6)."""

    unit: UnitClaim
    record: str
    digest: str


@dataclass(frozen=True, slots=True)
class _Current:
    run: RunRecord
    node: NodeRecord
    lost: bool


def _log(unit: UnitClaim, outcome: str, severity: LogSeverity, **details: object) -> None:
    report_operation(
        OperationLog(
            operation=_OPERATION,
            component="table_writer",
            phase="graph_unit",
            outcome=outcome,
            severity=severity,
            details={
                "run_id": unit.run_id,
                "node_id": unit.node_id,
                "dispatch_id": unit.dispatch_id,
                "receive_count": unit.receive_count,
                **details,
            },
        ),
        unit.report.logger,
    )


def _reread(unit: UnitClaim) -> _Current:
    run = unit.store.read_run(unit.run_id)
    node = None
    if run is not None:
        node = next(
            (item for item in unit.store.read_nodes(unit.run_id) if item.node_id == unit.node_id),
            None,
        )
    if run is None or node is None:
        raise InvariantError(
            ErrorCode("EXEC_GRAPH", 23), "a graph envelope names a run or node that does not exist."
        )
    claim = unit.store.read_claim(unit.node_id)
    lost = claim is None or claim.lease_version != unit.claim.lease_version
    return _Current(run, node, lost)


def claim_lost(unit: UnitClaim) -> UnitSettled:
    """Acknowledge a lost claim with zero effect: a takeover's new holder owns the node (V-9)."""
    _log(unit, "graph_claim_lost", LogSeverity.WARNING)
    return UnitSettled(acknowledge=True, outcome="graph_claim_lost")


def _exhausted(unit: UnitClaim) -> UnitSettled:
    release_claim_lease(unit.store, unit.claim, unit.now())
    _log(unit, "graph_redecide_exhausted", LogSeverity.INFO)
    return UnitSettled(acknowledge=False, outcome="graph_redecide_exhausted")


def retry(unit: UnitClaim, reason: str, cause: BaseException | None = None) -> UnitSettled:
    """Release the lease first (§2.3) and leave the message for the next receive."""
    if cause is not None:
        report_error(cause, unit.report)
    release_claim_lease(unit.store, unit.claim, unit.now())
    _log(unit, "graph_unit_retryable", LogSeverity.WARNING, reason=reason)
    return UnitSettled(
        acknowledge=False,
        outcome="graph_unit_retryable",
        node_state=NodeState.CLAIMED,
        failure_reason=reason,
    )


def settle_failed(unit: UnitClaim, reason: str, cause: BaseException | None) -> UnitSettled:
    """Settle the node `FAILED` itself, fencing the run `NODE_FAILED` only while it is live.

    Slice 7 (D35): one transaction that also decrements the member's `TABLE#<k>` share (the
    store's counted release) and records `failure_code`; the report raises the existing
    semantic-table-writer alarm.
    """
    for _ in range(_MAX_REDECIDE):
        current = _reread(unit)
        if current.lost:
            return claim_lost(unit)
        if current.node.state in TERMINAL_NODE_STATES:
            return UnitSettled(True, "graph_already_settled", current.node.state)
        now = unit.now().isoformat()
        live = current.run.state in LIVE_RUN_STATES
        try:
            unit.store.settle_materialization(
                MaterializationSettlement(
                    NodeSettlement(
                        current.run,
                        current.node,
                        NodeState.FAILED,
                        now,
                        claim=unit.claim,
                        failure_reason=reason,
                        failure_code=failure_code(cause),
                    ),
                    fence=FenceRunRequest(current.run, FenceReason.NODE_FAILED, now)
                    if live
                    else None,
                )
            )
        except GraphGuardError, GraphTransactionConflictError:
            continue
        report_error(
            GraphUnitFailedError(reason, cause, fenced=live, batch_id=unit.batch_id), unit.report
        )
        return UnitSettled(True, "graph_unit_failed", NodeState.FAILED, reason)
    return _exhausted(unit)


def settle_error(unit: UnitClaim, error: BaseException) -> UnitSettled:
    """Classify one run-level exception once, by error class, and retry or settle it.

    Slice 7: the run's exceptions keep the built rule, now the unresolved path with the lock
    held, with `final` from `unit_final_attempt`.
    """
    retryable, reason = classify_failure(error, final=unit.final)
    if retryable:
        return retry(unit, reason, error)
    return settle_failed(unit, reason, error)


def settle_shaping_error(unit: UnitClaim, error: BaseException) -> UnitSettled:
    """Classify one shaping or member-alone composition exception (difference 4).

    `classify_failure` decides retryable or not, unchanged (`materialize_raw` shares it). A
    retryable one returns the member `member_failed` while attempts remain; anything else, or
    the final attempt, settles `FAILED` with its reason.
    """
    retryable, reason = classify_failure(error, final=unit.final)
    if retryable:
        return return_member(unit, ReturnReason.MEMBER_FAILED, reason, cause=error)
    return settle_failed(unit, reason, error)


def return_member(  # noqa: PLR0913 -- the member, why, its failure and the cause
    unit: UnitClaim,
    reason: ReturnReason,
    failure_reason: str,
    *,
    cause: BaseException | None = None,
) -> UnitSettled:
    """Return a member to PENDING without holding its table, re-deciding on a guard (D35).

    A member whose run is no longer live is not returned: it settles `FAILED` with its own
    failure reason, with no fence member, as today. A lost claim is acknowledged with zero
    effect, and a terminal node is already settled.
    """
    for _ in range(_MAX_REDECIDE):
        current = _reread(unit)
        if current.lost:
            return claim_lost(unit)
        if current.node.state in TERMINAL_NODE_STATES:
            return UnitSettled(True, "graph_already_settled", current.node.state)
        if current.run.state not in LIVE_RUN_STATES:
            return settle_failed(unit, failure_reason, cause)
        try:
            unit.store.return_unit(
                ReturnUnitRequest(
                    current.run,
                    current.node,
                    reason,
                    unit.now().isoformat(),
                    claim=unit.claim,
                )
            )
        except GraphGuardError, GraphTransactionConflictError:
            continue
        _log(
            unit,
            "graph_unit_returned",
            LogSeverity.WARNING,
            return_reason=reason.value,
            failure_reason=failure_reason,
            cause=failure_code(cause),
            batch_id=unit.batch_id,
        )
        return UnitSettled(True, "graph_unit_returned", NodeState.PENDING, failure_reason)
    return _exhausted(unit)


def _unsettled(units: Sequence[UnitClaim], report: Report) -> dict[str, UnitSettled]:
    """Exhausted settlement guards: every remaining member is unresolved, reported once."""
    if units:
        report_error(
            InvariantError(
                GRAPH_UNIT_BATCH_UNSETTLED_CODE,
                "a write_unit success set exhausted its settlement guard; its members are "
                "unresolved.",
                context={"node_ids": [unit.node_id for unit in units]},
            ),
            report,
        )
    return {unit.node_id: _exhausted(unit) for unit in units}


def settle_set_succeeded(
    successes: Sequence[UnitSuccess],
    *,
    lock_key: str,
    batch_id: str,
) -> dict[str, UnitSettled]:
    """D34: settle a finalized set's members `SUCCEEDED`, per chunk, through `settle_unit_set`.

    A guard re-reads every remaining member: a lost claim or an already-terminal node is
    dropped with zero effect, members a chunk already committed are settled, and the rest
    retry, at most `max_redecide` times. Exhaustion leaves them unresolved, reported once with
    `GRAPH_UNIT_BATCH_UNSETTLED`. Returns each member's settlement by node ID.
    """
    settled: dict[str, UnitSettled] = {}
    remaining = list(successes)
    for _ in range(_MAX_REDECIDE):
        live: list[tuple[UnitSuccess, _Current]] = []
        for success in remaining:
            current = _reread(success.unit)
            if current.lost:
                settled[success.unit.node_id] = claim_lost(success.unit)
            elif current.node.state in TERMINAL_NODE_STATES:
                settled[success.unit.node_id] = UnitSettled(
                    True, "graph_already_settled", current.node.state
                )
            else:
                live.append((success, current))
        if not live:
            return settled
        store, now = live[0][0].unit.store, live[0][0].unit.now
        request = UnitSetSettlement(
            lock_key=lock_key,
            batch_id=batch_id,
            members=tuple(
                UnitSetMember(
                    current.run,
                    current.node,
                    success.digest,
                    claim=success.unit.claim,
                    effect_record=success.record,
                )
                for success, current in live
            ),
            recorded_at=now().isoformat(),
        )
        committed: set[str]
        try:
            committed = set(store.settle_unit_set(request))
        except UnitSetGuardError as guard:
            committed = set(guard.committed)
        except GraphGuardError, GraphTransactionConflictError:
            committed = set()
        for success, _current in live:
            if success.unit.node_id in committed:
                _log(success.unit, "graph_unit_succeeded", LogSeverity.INFO)
                settled[success.unit.node_id] = UnitSettled(
                    True, "graph_unit_succeeded", NodeState.SUCCEEDED
                )
        remaining = [success for success, _ in live if success.unit.node_id not in committed]
        if not remaining:
            return settled
    report = remaining[0].unit.report
    settled.update(_unsettled([success.unit for success in remaining], report))
    return settled
