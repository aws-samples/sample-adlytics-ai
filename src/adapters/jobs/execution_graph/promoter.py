# SPDX-License-Identifier: MIT-0
"""The promoter (S3, V-20, V-25, V-26): one promotion path for every waiter.

Three writers wake a conflict unit `U` with `ADD wake_seq 1`: the FENCING transition (release
point 1), grant apply (S4, release point 2) and a held waiter's receipt (row 16). The promoter
reads each shard strongly, collects every `U` with a `PROMOTE#<U>` or a `WAITER#<U>` item, reads
`PROMOTE#`, `SLOT#`, `WAITER#`, `HEAD#` and the hold, and commits one conditioned branch
(`decide_promotion`):

- slot occupied: delete the wakeup on its `wake_seq`, ConditionCheck the slot revision;
- slot free, no waiter: delete the wakeup on its `wake_seq`, ConditionCheck both absent;
- slot free, held waiter, hold set: write nothing;
- slot free, a waiter, the co-release domain BLOCKED (S4, BF-4): write nothing; the waiter
  stays counted by the stuck-flow sweep until owner review, and promotion ConditionChecks the
  block absent;
- slot free, waiter older than `H`: `SUPERSEDED(older_than_current)`;
- otherwise the waiter becomes `A` (run, slot, `INSTANTIATE#`), and instantiation runs inline.

A failed condition is `PROMOTE_WAKE_CHANGED`: a wakeup or release landed after the read, so the
promoter re-reads and decides again, within the generated re-decide bound; anything left is
taken by the next pass. A `WAITER#` with the slot free and no `PROMOTE#` is the lost-wakeup
backstop and goes through the same branches. The sweep also completes every `INSTANTIATE#`
item older than the grace period. A held waiter's promotion ConditionChecks the hold revision it
read, so a hold set again after the read refuses the commit and the re-read skips it (S3V-9).
Each unit and each obligation is isolated, so one failure never ends the pass (S3V-7).

S6 §2.2, the arm pass (`arm_pass`), runs beside it: one mechanism arms every successor. It reads
every shard's `ARM#` items first, then, when any is `serving`, walks all due shards'
`COMPLETE#` items (AGG4-14). Per item it reads the run and its nodes strongly and arms the
scope's eligible PENDING routed nodes one per transaction, mirroring `arm_roots`. A held domain
lock is a `lock_wait`; serving nodes are armed only by the domain election, which a lower open
grant ordinal blocks; a failed `COMPLETE#` shard read arms no serving node. `ARM#` retires with
the scope's last PENDING routed node (AGG4-1); a run that is not live deletes it alone.

Slice 7 (1b, §2.2 "Batch arming"; D30-D33, D41): `units`-scope nodes are armed per table, not
per node. The pass collects every OPEN run's eligible PENDING `write_unit` nodes across all
`ARM#` items, groups them by `TABLE#<k>`, grants each table once through `take_grant` (oldest
first, a solo node alone, at most `MAX_BATCH_MEMBERS`) and commits each grant with
`arm_unit_batch`. A `PAUSED` run's units wait (`paused_wait`). A run's `ARM#` is deleted in the
group transaction that commits its last PENDING unit, decided from committed groups only (N6).

Deployed disabled with the dispatcher while the mode is not GRAPH.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from adapters.jobs.execution_graph.instantiate import (
    InstantiateRequest,
    NodePlanner,
    NodePlanUnavailableError,
    instantiate_run,
)
from adapters.jobs.execution_graph.isolation import FailureSink, ignore_failure, isolated
from adapters.outbound.telemetry.tracing import (
    EXECUTION_GRAPH_PROMOTER_DOMAIN,
    Recording,
    span,
    trace_boundary,
)
from domain.operations.execution_graph import (
    AuthorityMode,
    epoch_for_mode,
    graph_id,
    run_id,
)
from domain.operations.execution_graph_dispatch import (
    DOMAIN_LOCK_HELD,
    LIVE_RUN_STATES,
    MAX_BATCH_MEMBERS,
    PROMOTE_WAKE_CHANGED,
    ArmScope,
    CompleteObligation,
    GraphGuardError,
    NodeState,
    PromoterBranch,
    PromoterView,
    arm_eligible,
    batch_id_for,
    decide_promotion,
    is_write_unit,
    newest_eligible_candidate,
    scope_pending,
    serving_gap,
    take_grant,
    validate_serving_candidates,
)
from domain.operations.execution_graph_release import (
    SERVING_PLAN_DIGEST,
    ServingBinding,
    ServingInputFact,
    alias_serving,
    cohort_revision,
    serving_input_revision,
)
from domain.operations.generated_execution_graphs import (
    BOUNDS,
    CONTRACT_REVISION,
    PROTOCOL_VERSION,
    SERVING_COALESCING,
)
from domain.operations.run_request import RunState
from domain.operations.status import canonical_authority_digest
from domain.shared_kernel.errors import ErrorCode, InvariantError
from ports.outbound.execution_graph_dispatch import (
    ArmNodeRequest,
    ArmUnitBatchRequest,
    DueItem,
    GraphDispatchStore,
    GraphTransactionConflictError,
    NodeRecord,
    PromotionCommit,
    PromotionRead,
    RunRecord,
    ServingGraphDispatchStore,
    ServingPublicationAuthority,
    UnitMember,
)
from ports.outbound.execution_graph_release import (
    ActivateServingWinnerRequest,
    DrainFinalAliasAndActivateNextRequest,
    DrainFinalAliasAndActivateNextResult,
    ServingAliasDrainRequest,
    ServingCandidateRead,
    ServingDomainElectionResult,
    ServingDomainReadResult,
    ServingElectionOutcome,
    ServingGraphReleaseStore,
)

__all__ = [
    "FRESH_ELECTION_SEGMENTS",
    "INSTANTIATE_GRACE",
    "ArmCounts",
    "FreshElectionAttemptTiming",
    "FreshElectionInjectedConflictError",
    "FreshElectionRehearsal",
    "FreshElectionRehearsalRefusedError",
    "PromoterContext",
    "PromotionCounts",
    "UnitCandidate",
    "arm_pass",
    "arm_run",
    "arm_unit_groups",
    "coalesce_serving",
    "collect_unit_candidates",
    "promote_shard",
    "promote_unit",
    "read_arms",
    "rehearse_fresh_election",
    "unconsumed_arms",
    "walk_complete",
]

INSTANTIATE_GRACE: Final = timedelta(seconds=60)
"""How old an `INSTANTIATE#` item is before the sweep completes it instead of the inline caller.

A Python constant, not a contract bound: the S1 contract is pinned while the lagging stacks are
accepted (owner-gate Item 14), so it moves into `bounds` at the next contract revision.
"""
_REDECIDE: Final = int(BOUNDS["max_redecide"])
_TEMPLATE: Final = "ingestion_manifest_to_served.v1"
_LIVE: Final = LIVE_RUN_STATES
_ARM_INVARIANT: Final = ErrorCode("EXEC_GRAPH", 26)
"""An `ARM#` or `COMPLETE#` item that breaks the arming protocol: reported, never guessed."""
_PROMOTER_CONTEXT_INVALID: Final = ErrorCode("EXEC_GRAPH", 34)
_PROMOTER_CLOCK_INVALID: Final = ErrorCode("EXEC_GRAPH", 35)
_NEXT_ELECTION_MIN_REMAINING_MS: Final = int(SERVING_COALESCING["next_election_min_remaining_ms"])
_DOMAIN_FAILED: Final = "failed"
"""The promoter-domain child's closed outcome when its attempt raised; every other outcome is a
`ServingElectionOutcome` value."""


class PromoterStore(
    GraphDispatchStore, ServingGraphDispatchStore, ServingGraphReleaseStore, Protocol
):
    """Every store operation a promoter pass calls, typed so a composition that lacks one fails.

    The production composition is `DynamoGraphReleaseStore` (`workers.py`): the serving election,
    capacity and alias-drain calls exist only there, not on `DynamoGraphDispatchStore`.
    """

    def read_serving_binding(self, grant_id: str) -> ServingBinding | None:
        """Read one serving binding by its grant."""
        ...

    def read_serving_publication_authority(self, run_id: str) -> ServingPublicationAuthority:
        """Strongly resolve the run's COMPLETE identity and live frontier shard."""
        ...


@dataclass(frozen=True, slots=True)
class PromoterContext:
    """The store, node planner, clock and mode one promoter pass runs with."""

    store: PromoterStore
    planner: NodePlanner
    now: Callable[[], datetime]
    mode: AuthorityMode = AuthorityMode.GRAPH
    on_failure: FailureSink = ignore_failure


@dataclass
class PromotionCounts:
    """What one promoter pass did, by branch."""

    branches: dict[str, int] = field(default_factory=dict[str, int])
    guards: int = 0
    instantiated: int = 0
    instantiation_deferred: int = 0
    conflicts: int = 0
    failed: int = 0

    def add(self, branch: PromoterBranch) -> None:
        """Count one decided branch."""
        self.branches[branch.value] = self.branches.get(branch.value, 0) + 1


def _view(read: PromotionRead) -> PromoterView:
    return PromoterView(
        wake_seq=read.wake_seq,
        slot_revision=None if read.slot_run_id is None else read.slot_revision,
        waiter=read.waiter,
        hold=read.hold,
        head=read.head,
        blocked=read.blocked,
    )


def _details(context: PromoterContext, read: PromotionRead) -> dict[str, str]:
    details = {
        "authority_epoch": str(epoch_for_mode(context.mode)),
        "mode": context.mode.value,
        "recorded_at": context.now().isoformat(),
        "protocol_version": PROTOCOL_VERSION,
        "contract_revision": str(int(CONTRACT_REVISION)),
    }
    waiter = read.waiter
    if waiter is not None:
        receipt = context.store.read_receipt(waiter.request_id)
        if receipt is not None and receipt.root_flow_id is not None:
            details["root_flow_id"] = receipt.root_flow_id
    return details


def _promoted_run(context: PromoterContext, read: PromotionRead) -> RunRecord:
    waiter = read.waiter
    if waiter is None:
        raise GraphGuardError(PROMOTE_WAKE_CHANGED)
    receipt = context.store.read_receipt(waiter.request_id)
    if receipt is None or receipt.root_flow_id is None:
        raise GraphGuardError(PROMOTE_WAKE_CHANGED)
    epoch = epoch_for_mode(context.mode)
    generation = receipt.retry_generation or 0
    return RunRecord(
        run_id=run_id(waiter.fingerprint, epoch, generation),
        state=RunState.OPEN,
        authority_epoch=epoch,
        conflict_unit=read.conflict_unit,
        fingerprint=waiter.fingerprint,
        retrieved_at=waiter.r.isoformat(),
        root_flow_id=receipt.root_flow_id,
        graph_id=graph_id(waiter.fingerprint, epoch, generation),
        template=_TEMPLATE,
        generation=generation,
        dispatch_generation=0,
        revision=1,
        receipt_request_id=waiter.request_id,
        promoted=True,
        window=waiter.window,
        # S5: the waiter and the run it becomes are due from one root clock.
        root_clock_started_at=waiter.root_clock_started_at or receipt.root_clock_started_at,
    )


def _instantiate(context: PromoterContext, run: str, counts: PromotionCounts) -> None:
    try:
        instantiate_run(
            context.store,
            InstantiateRequest(run, context.planner, lambda: context.now().isoformat()),
        )
    except NodePlanUnavailableError, GraphGuardError:
        counts.instantiation_deferred += 1
        return
    counts.instantiated += 1


def promote_unit(
    context: PromoterContext, conflict_unit: str, counts: PromotionCounts
) -> PromoterBranch:
    """Decide and commit one unit's branch, re-reading on `PROMOTE_WAKE_CHANGED`."""
    for _ in range(_REDECIDE):
        read = context.store.read_promotion(conflict_unit)
        branch = decide_promotion(_view(read))
        if branch in {
            PromoterBranch.NOTHING,
            PromoterBranch.SKIP_HELD,
            PromoterBranch.SKIP_BLOCKED,
        }:
            counts.add(branch)
            return branch
        try:
            details = _details(context, read)
            new_run = (
                _promoted_run(context, read) if branch is PromoterBranch.PROMOTE_WAITER else None
            )
            context.store.promote(
                PromotionCommit(
                    read=read,
                    branch=branch.value,
                    recorded_at=details["recorded_at"],
                    new_run=new_run,
                    details=details,
                )
            )
        except GraphGuardError:
            counts.guards += 1
            continue
        counts.add(branch)
        if new_run is not None:
            _instantiate(context, new_run.run_id, counts)
        return branch
    return PromoterBranch.NOTHING


@dataclass
class ArmCounts:
    """What one arm pass did (S6 §2.2)."""

    arms: int = 0
    """`ARM#` items read before the `COMPLETE#` walk began."""
    armed: int = 0
    retired: int = 0
    """`ARM#` items deleted: with the scope's last node, or alone for a run that is not live."""
    lock_wait: int = 0
    coalesced: int = 0
    predecessor_wait: int = 0
    walk_failed: int = 0
    """1 when a `COMPLETE#` shard read failed, so this pass armed no serving node."""
    guards: int = 0
    conflicts: int = 0
    failed: int = 0
    failed_shards: int = 0
    paused_wait: int = 0
    """Slice 7: units of a `PAUSED` run left PENDING; they arm after resume."""
    batches_armed: int = 0
    """Slice 7: `TABLE#<k>` grants committed (one `arm_unit_batch` each)."""
    batch_members: int = 0
    """Slice 7: units those grants armed."""


def read_arms(context: PromoterContext, shards: Sequence[str], counts: ArmCounts) -> list[DueItem]:
    """Read every shard's `ARM#` items; a shard whose read fails is reported and skipped."""
    arms: list[DueItem] = []
    for shard in shards:

        def read(shard: str = shard) -> None:
            arms.extend(context.store.read_due(shard, "ARM#"))

        if not isolated(read, context.on_failure).ok:
            counts.failed_shards += 1
    counts.arms = len(arms)
    return arms


def walk_complete(
    context: PromoterContext, shards: Sequence[str]
) -> tuple[CompleteObligation, ...] | None:
    """Read every shard's open `COMPLETE#` items, or None when any shard read failed (§2.1)."""
    found: list[CompleteObligation] = []
    for shard in shards:
        try:
            items = context.store.read_due(shard, "COMPLETE#")
        except Exception as error:
            # A failed shard read means the walk is incomplete: this pass arms no serving node.
            context.on_failure(error)
            return None
        for item in items:
            if item.domain is None or item.grant_id is None or item.grant_ordinal is None:
                context.on_failure(
                    InvariantError(
                        _ARM_INVARIANT, "a COMPLETE# item is missing its serving-order fields."
                    )
                )
                return None
            found.append(
                CompleteObligation(item.subject, item.domain, item.grant_id, item.grant_ordinal)
            )
    return tuple(found)


def arm_run(
    context: PromoterContext,
    item: DueItem,
    _walk: tuple[CompleteObligation, ...] | None,
    counts: ArmCounts,
) -> None:
    """Arm one `ARM#` item's eligible nodes, one node per transaction (§2.2, AGG4-1, AGG4-14).

    A serving node is armed only when the run holds its domain's lowest open `COMPLETE#`
    ordinal in this pass's complete walk, which began after the `ARM#` item was read. `ARM#` is
    deleted by the transaction that arms the scope's last PENDING routed node, whatever its
    predecessors' state; the eligible set decides only which node this pass may arm.
    """
    store, scope = context.store, item.scope
    if scope is None:
        raise InvariantError(_ARM_INVARIANT, "an ARM# item carries no scope.")
    run = store.read_run(item.subject)
    if run is None:
        raise InvariantError(_ARM_INVARIANT, "an ARM# item names a run that does not exist.")
    live = run.state is RunState.GRANTED if scope is ArmScope.SERVING else run.state in _LIVE
    if not live:
        try:
            store.retire_arm(run, scope)
        except GraphGuardError:
            counts.guards += 1
            return
        counts.retired += 1
        return
    nodes = store.read_nodes(run.run_id)
    remaining = len(scope_pending(scope, run.template, nodes))
    if remaining == 0:
        raise InvariantError(_ARM_INVARIANT, "an ARM# names a scope with no PENDING routed node.")
    if scope is ArmScope.SERVING:
        execution = (
            None if run.grant_id is None else store.read_serving_execution(run.grant_id, run.run_id)
        )
        if execution is None or execution.head.winner_run_id != run.run_id:
            return
    eligible = arm_eligible(scope, run.template, nodes)
    if not eligible:
        counts.predecessor_wait += 1
        return
    for node in eligible:
        request = ArmNodeRequest(run, node, remaining == 1, context.now().isoformat())
        try:
            store.arm_node(request)
        except GraphGuardError as guard:
            if guard.signal == DOMAIN_LOCK_HELD:
                # A durable wait: the node stays PENDING and `ARM#` stays for the next pass.
                counts.lock_wait += 1
                continue
            counts.guards += 1
            return
        counts.armed += 1
        remaining -= 1
        if request.retire:
            counts.retired += 1


@dataclass(frozen=True, slots=True)
class UnitCandidate:
    """One eligible PENDING `write_unit` node with the OPEN run it was read under (D41)."""

    run: RunRecord
    node: NodeRecord

    @property
    def root_clock_started_at(self) -> str | None:
        """The run's root clock: older runs are granted first."""
        return self.run.root_clock_started_at

    @property
    def run_id(self) -> str:
        """The run, the tie-breaker of the grant order."""
        return self.run.run_id

    @property
    def arm_alone(self) -> bool:
        """The node runs alone (a member-attributable or final-attempt return)."""
        return self.node.arm_alone


def collect_unit_candidates(  # noqa: PLR0913, PLR0917 -- context, item, counts, found
    context: PromoterContext,
    item: DueItem,
    counts: ArmCounts,
    found: tuple[list[UnitCandidate], dict[str, int]],
) -> None:
    """Read one `units` `ARM#` item's run and nodes strongly and collect its eligible units.

    A run that is not live has its `ARM#` retired alone; a `PAUSED` run's units are counted
    `paused_wait` and stay PENDING; an OPEN run's eligible `write_unit` nodes are collected with
    that run's PENDING count, from which "last" is decided (N6).
    """
    candidates, pending = found
    store = context.store
    run = store.read_run(item.subject)
    if run is None:
        raise InvariantError(_ARM_INVARIANT, "an ARM# item names a run that does not exist.")
    if run.state not in _LIVE:
        try:
            store.retire_arm(run, ArmScope.UNITS)
        except GraphGuardError:
            counts.guards += 1
            return
        counts.retired += 1
        return
    nodes = store.read_nodes(run.run_id)
    remaining = scope_pending(ArmScope.UNITS, run.template, nodes)
    if not remaining:
        raise InvariantError(_ARM_INVARIANT, "an ARM# names a scope with no PENDING routed node.")
    eligible = arm_eligible(ArmScope.UNITS, run.template, nodes)
    if not eligible:
        counts.predecessor_wait += 1
        return
    if any(not is_write_unit(node.name) for node in eligible):
        raise InvariantError(_ARM_INVARIANT, "a units-scope node is not a write_unit node.")
    if run.state is RunState.PAUSED:
        counts.paused_wait += len(eligible)
        return
    pending[run.run_id] = len(remaining)
    candidates.extend(UnitCandidate(run, node) for node in eligible)


def _reread_group(context: PromoterContext, group: Sequence[UnitCandidate]) -> list[UnitCandidate]:
    """Re-read a guarded group strongly; keep members still PENDING on their OPEN run."""
    kept: list[UnitCandidate] = []
    for candidate in group:
        run = context.store.read_run(candidate.run.run_id)
        if run is None or run.state is not RunState.OPEN:
            continue
        if run.dispatch_generation != candidate.run.dispatch_generation:
            continue
        node = next(
            (
                n
                for n in context.store.read_nodes(run.run_id)
                if n.node_id == candidate.node.node_id
            ),
            None,
        )
        if node is not None and node.state is NodeState.PENDING:
            kept.append(UnitCandidate(run, node))
    return kept


def _arm_group(  # noqa: PLR0913, PLR0917 -- the pass context, one group, its counts
    context: PromoterContext,
    group: list[UnitCandidate],
    pending: dict[str, int],
    counts: ArmCounts,
) -> None:
    """Grant one table once (D41) and commit it, re-reading on any guard but a held lock."""
    lock_key = group[0].node.lock_key

    def grant(taken: Sequence[UnitCandidate], armed_at: str) -> ArmUnitBatchRequest:
        return ArmUnitBatchRequest(
            lock_key=lock_key,
            batch_id=batch_id_for(lock_key, armed_at, [item.node.node_id for item in taken]),
            members=tuple(UnitMember(item.run, item.node) for item in taken),
            retire_runs=tuple(
                candidate.run_id for candidate in taken if pending.get(candidate.run_id, 0) == 1
            ),
            recorded_at=armed_at,
        )

    for _ in range(_REDECIDE):
        armed_at = context.now().isoformat()
        taken = take_grant(
            group,
            MAX_BATCH_MEMBERS,
            # F3: the longest oldest-first prefix whose arming transaction fits in bytes.
            fits=lambda prefix, at=armed_at: context.store.unit_batch_fits(grant(prefix, at)),
        )
        if not taken:
            return
        request = grant(taken, armed_at)
        retire = request.retire_runs
        try:
            context.store.arm_unit_batch(request)
        except GraphGuardError as guard:
            if guard.signal == DOMAIN_LOCK_HELD:
                # A durable wait: every member stays PENDING and its `ARM#` stays.
                counts.lock_wait += len(taken)
                return
            group = _reread_group(context, group)
            continue
        counts.armed += len(taken)
        counts.batches_armed += 1
        counts.batch_members += len(taken)
        counts.retired += len(retire)
        for candidate in taken:
            pending[candidate.run_id] = pending.get(candidate.run_id, 0) - 1
        return
    counts.guards += 1


def arm_unit_groups(  # noqa: PLR0913, PLR0917 -- the pass context, its units, its counts
    context: PromoterContext,
    candidates: Sequence[UnitCandidate],
    pending: dict[str, int],
    counts: ArmCounts,
) -> None:
    """Group the collected units by `TABLE#<k>` and grant each table once, each isolated."""
    groups: dict[str, list[UnitCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.node.lock_key, []).append(candidate)
    for lock_key in sorted(groups):
        result = isolated(
            lambda group=groups[lock_key]: _arm_group(context, group, pending, counts),
            context.on_failure,
        )
        counts.conflicts += result.conflicts
        counts.failed += 0 if result.ok else 1


def _candidate_reads(
    store: PromoterStore, run_ids: Sequence[str]
) -> tuple[ServingCandidateRead, ...]:
    """Read and validate every eligible candidate without truncating a domain roster."""
    reads = tuple(store.read_serving_candidate(run_id) for run_id in run_ids)
    if any(item is None for item in reads):
        return ()
    typed = tuple(item for item in reads if item is not None)
    validate_serving_candidates(tuple(item.candidate for item in typed))
    return typed


def _revisions(reads: Sequence[Any], frontier: Any) -> tuple[str, str]:
    candidates = tuple(item.candidate for item in reads)
    domain = candidates[0].domain
    cohort = cohort_revision(domain, candidates, frontier)
    facts = tuple(
        ServingInputFact(
            partition=item.target_digest,
            publication_revision=item.target_digest,
            target_digest=item.target_digest,
            prompt_digest=SERVING_PLAN_DIGEST,
            input_digest=item.target_digest,
        )
        for item in reads
    )
    return cohort, serving_input_revision(domain, facts, SERVING_PLAN_DIGEST)


def _read_serving_iteration(  # noqa: PLR0913, PLR0917 -- the pass inputs plus the rehearsal seam
    context: PromoterContext,
    shards: Sequence[str],
    counts: ArmCounts,
    rehearsal: FreshElectionRehearsal | None = None,
) -> tuple[tuple[DueItem, ...], tuple[CompleteObligation, ...]]:
    """Read a wholly fresh ARM set followed by one complete obligation walk."""
    arms = tuple(_timed(rehearsal, "arm_read", lambda: read_arms(context, shards, counts)))
    walk = _timed(rehearsal, "complete_walk", lambda: walk_complete(context, shards))
    if walk is None:
        counts.walk_failed += 1
        return arms, ()
    return arms, walk


def _candidate_digest(candidates: Sequence[Any]) -> str:
    return canonical_authority_digest(
        [
            "graph-serving-candidates/v1",
            [
                [
                    item.run_id,
                    item.grant_id,
                    item.grant_ordinal,
                    item.publication_effect_digest,
                ]
                for item in candidates
            ],
        ]
    )


def _remaining_reader(remaining_ms: Callable[[], object]) -> Callable[[], int]:
    prior: int | None = None

    def read() -> int:
        nonlocal prior
        # The Lambda callable is untrusted at runtime; validate the reading as an object.
        value = remaining_ms()
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            or (prior is not None and value > prior)
        ):
            raise InvariantError(
                _PROMOTER_CLOCK_INVALID, "the promoter remaining clock is invalid."
            )
        prior = value
        return value

    return read


@trace_boundary(EXECUTION_GRAPH_PROMOTER_DOMAIN)
@contextmanager
def _promoter_domain(aws_request_id: str, domain: str, cohort: str) -> Generator[Recording]:
    """Open one attempted cohort's typed child; a failure marks it before the pass reports it."""
    with span(
        EXECUTION_GRAPH_PROMOTER_DOMAIN,
        aws_request_id=aws_request_id,
        domain=domain,
        cohort_revision=cohort,
        outcome="attempting",
    ) as recording:
        try:
            yield recording
        except Exception as error:
            recording.record(outcome=_DOMAIN_FAILED)
            recording.note_failure(error)
            raise


FRESH_ELECTION_SEGMENTS: Final = (
    "arm_read",
    "complete_walk",
    "candidate_read",
    "head_frontier_read",
    "activation",
)
"""The timed parts of one fresh final-alias iteration (`FreshElectionTimingV1`, plan item 12)."""


class FreshElectionInjectedConflictError(GraphTransactionConflictError):
    """A conflict the timing rehearsal injected in place of the final transaction call."""


class FreshElectionRehearsalRefusedError(RuntimeError):
    """The rehearsal world is not at A's final alias; nothing was written."""


@dataclass(frozen=True, slots=True)
class FreshElectionAttemptTiming:
    """One rehearsal attempt: each fresh-iteration segment, the retry, and the guarded total.

    `total_ms` runs from the passing guard check to the end of the final transaction call, which
    is the interval `next_election_min_remaining_ms` must cover. `retry_ms` runs from an injected
    conflict to the next attempt's passing guard check.
    """

    attempt: int
    injected_conflict: bool
    segments_ms: Mapping[str, float]
    retry_ms: float
    total_ms: float


@dataclass(slots=True)
class FreshElectionRehearsal:
    """The purpose-named, non-clearing `FreshElectionTimingV1` rehearsal input (plan item 12).

    Only `rehearse_fresh_election` accepts it, and no runtime composition constructs one: with no
    rehearsal every promoter path times nothing and injects nothing. While `conflicts` remain,
    the final transaction call raises `FreshElectionInjectedConflictError` instead of writing,
    so one sample runs the whole configured conflict envelope before one real activation.
    """

    conflicts: int
    clock: Callable[[], float] = time.perf_counter
    attempts: list[FreshElectionAttemptTiming] = field(
        default_factory=list[FreshElectionAttemptTiming], init=False
    )
    _segments: dict[str, float] = field(default_factory=dict[str, float], init=False)
    _guard_at: float | None = field(default=None, init=False)
    _conflict_at: float | None = field(default=None, init=False)
    _retry_ms: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        if isinstance(self.conflicts, bool) or self.conflicts < 0:
            raise ValueError("the rehearsal conflict count must be a nonnegative integer.")

    def timed[T](self, segment: str, action: Callable[[], T]) -> T:
        """Run one segment and add its wall time, whether it returns or raises."""
        if segment not in FRESH_ELECTION_SEGMENTS:
            raise ValueError(f"unknown rehearsal segment {segment!r}.")
        started = self.clock()
        try:
            return action()
        finally:
            elapsed = (self.clock() - started) * 1000
            self._segments[segment] = self._segments.get(segment, 0.0) + elapsed

    def guard_passed(self) -> None:
        """Mark the passing guard check that starts the guarded interval of this attempt."""
        now = self.clock()
        if self._conflict_at is not None:
            self._retry_ms = (now - self._conflict_at) * 1000
            self._conflict_at = None
        self._segments = {}
        self._guard_at = now

    def before_activation(self) -> None:
        """Inject the next configured conflict instead of the final transaction call."""
        if self._guard_at is None:
            raise FreshElectionRehearsalRefusedError("no guard check preceded the activation.")
        if len(self.attempts) < self.conflicts:
            raise FreshElectionInjectedConflictError("FreshElectionTimingV1 injected conflict")

    def end_attempt(self, *, injected_conflict: bool) -> None:
        """Close the attempt that began at the last passing guard check."""
        if self._guard_at is None:
            raise FreshElectionRehearsalRefusedError("an attempt ended without a guard check.")
        now = self.clock()
        self.attempts.append(
            FreshElectionAttemptTiming(
                attempt=len(self.attempts) + 1,
                injected_conflict=injected_conflict,
                segments_ms={
                    name: self._segments.get(name, 0.0) for name in FRESH_ELECTION_SEGMENTS
                },
                retry_ms=self._retry_ms,
                total_ms=(now - self._guard_at) * 1000,
            )
        )
        self._guard_at = None
        self._retry_ms = 0.0
        self._conflict_at = now if injected_conflict else None


def _timed[T](rehearsal: FreshElectionRehearsal | None, segment: str, action: Callable[[], T]) -> T:
    """Time one segment under a rehearsal; with none, call straight through."""
    return action() if rehearsal is None else rehearsal.timed(segment, action)


@dataclass(frozen=True, slots=True)
class _DomainPass:
    """The one fresh iteration every domain of a promoter pass is decided from."""

    context: PromoterContext
    shards: Sequence[str]
    counts: ArmCounts
    read_remaining: Callable[[], int]
    aws_request_id: str
    walk: tuple[CompleteObligation, ...]
    arm_ids: frozenset[str]
    rehearsal: FreshElectionRehearsal | None = None


def coalesce_serving(  # noqa: PLR0913, PLR0917
    context: PromoterContext,
    shards: Sequence[str],
    counts: ArmCounts,
    remaining_ms: Callable[[], int],
    aws_request_id: str,
) -> tuple[ServingDomainElectionResult, ...]:
    """Elect or drain each domain from wholly fresh rolling authority reads.

    Each domain is isolated: its failure marks its typed child, is reported once through the
    pass's failure sink, and the next domain is still decided. A conflict is not retried here,
    because only a wholly fresh iteration may retry it; the next invocation is that iteration. An
    invalid Lambda clock is a pass failure and ends the pass.
    """
    if not callable(remaining_ms) or not aws_request_id:
        raise InvariantError(_PROMOTER_CONTEXT_INVALID, "the promoter Lambda context is invalid.")
    read_remaining = _remaining_reader(remaining_ms)
    arms, walk = _read_serving_iteration(context, shards, counts)
    state = _DomainPass(
        context,
        shards,
        counts,
        read_remaining,
        aws_request_id,
        walk,
        frozenset(item.subject for item in arms if item.scope is ArmScope.SERVING),
    )
    results: list[ServingDomainElectionResult] = []
    for domain in sorted({item.domain for item in walk}):
        try:
            result = _coalesce_domain(state, domain)
        except Exception as error:
            if isinstance(error, InvariantError) and error.code == _PROMOTER_CLOCK_INVALID:
                raise
            context.on_failure(error)
            counts.failed += 1
            continue
        if result is not None:
            results.append(result)
    return tuple(results)


def _coalesce_domain(state: _DomainPass, domain: str) -> ServingDomainElectionResult | None:
    """Decide one domain: elect on an IDLE head, or drain a bound cohort."""
    authority = state.context.store.read_serving_domain(domain)
    if authority.head is None or authority.frontier is None:
        return None
    if authority.head.state == "IDLE":
        return _elect_idle(state, domain, authority)
    if authority.head.state != "BOUND_DRAINING" or authority.head.direct_grant_id is None:
        return None
    cohort = authority.head.cohort_revision
    if not cohort:
        raise InvariantError(_ARM_INVARIANT, "a draining head has no cohort revision.")
    with _promoter_domain(state.aws_request_id, domain, cohort) as recording:
        result = _drain_bound(state, domain, authority)
        recording.record(
            outcome=(ServingElectionOutcome.NO_ELIGIBLE if result is None else result.outcome)
        )
        return result


def _elect_idle(
    state: _DomainPass, domain: str, authority: ServingDomainReadResult
) -> ServingDomainElectionResult:
    """Activate the newest eligible candidate on an IDLE head, or report why none can be."""
    context, head, frontier = state.context, authority.head, authority.frontier
    assert head is not None and frontier is not None  # noqa: S101 -- checked by the caller
    domain_walk = tuple(item for item in state.walk if item.domain == domain)
    run_ids = tuple(item.run_id for item in domain_walk if item.run_id in state.arm_ids)
    reads = _candidate_reads(context.store, run_ids)
    candidates = tuple(item.candidate for item in reads)
    if not candidates:
        return ServingDomainElectionResult(domain, ServingElectionOutcome.NO_ELIGIBLE)
    cohort, input_revision = _revisions(reads, frontier)
    with _promoter_domain(state.aws_request_id, domain, cohort) as recording:
        if serving_gap(domain_walk, candidates) is not None:
            recording.record(outcome=ServingElectionOutcome.WAITING_LOWER_ORDINAL)
            return ServingDomainElectionResult(domain, ServingElectionOutcome.WAITING_LOWER_ORDINAL)
        winner = newest_eligible_candidate(candidates)
        assert winner is not None  # noqa: S101 -- nonempty validated candidates
        context.store.activate_serving_winner(
            ActivateServingWinnerRequest(
                head,
                frontier,
                candidates,
                winner,
                cohort,
                input_revision,
                context.now().isoformat(),
                SERVING_PLAN_DIGEST,
            )
        )
        state.counts.armed += 1
        state.counts.retired += 1
        recording.record(outcome=ServingElectionOutcome.ELECTED)
        return ServingDomainElectionResult(
            domain,
            ServingElectionOutcome.ELECTED,
            elected_cohort_revision=cohort,
        )


def _drain_bound(
    state: _DomainPass, domain: str, authority: ServingDomainReadResult
) -> ServingDomainElectionResult | None:
    """Alias the newest bound candidate, or drain the final alias and activate the successor."""
    context, shards, counts, aws_request_id = (
        state.context,
        state.shards,
        state.counts,
        state.aws_request_id,
    )
    head = authority.head
    assert head is not None and head.direct_grant_id is not None  # noqa: S101 -- caller checked
    direct = context.store.read_serving_binding(head.direct_grant_id)
    if direct is None:
        raise InvariantError(_ARM_INVARIANT, "a draining head has no direct binding.")
    obligations = tuple(
        item
        for shard in shards
        for item in context.store.read_due(shard, "COALESCE#")
        if item.subject == head.cohort_revision and item.run_ids
    )
    if len(obligations) != 1:
        raise InvariantError(_ARM_INVARIANT, "a draining head has no unique cohort obligation.")
    obligation = obligations[0]
    reads = sorted(
        _candidate_reads(context.store, obligation.run_ids),
        key=lambda item: item.candidate.grant_ordinal,
        reverse=True,
    )
    if not reads:
        return None
    if state.read_remaining() < _NEXT_ELECTION_MIN_REMAINING_MS:
        return ServingDomainElectionResult(
            domain,
            ServingElectionOutcome.DRAIN_INCOMPLETE,
            drained_cohort_revision=head.cohort_revision,
        )
    rehearsal = state.rehearsal
    if rehearsal is not None:
        if len(reads) != 1:
            # The rehearsal times only A's final alias; it never writes a non-final drain.
            raise FreshElectionRehearsalRefusedError(
                "the rehearsal domain is not at A's last alias."
            )
        rehearsal.guard_passed()
    current = reads[0]
    terminal_at = context.now()
    alias = alias_serving(
        direct,
        current.candidate,
        direct.cohort_revision or head.cohort_revision or "",
        bound_at=terminal_at,
    )
    remaining = tuple(item.candidate.run_id for item in reads[1:])
    if remaining:
        context.store.drain_alias_binding(
            ServingAliasDrainRequest(
                head,
                direct,
                alias,
                current.candidate,
                current.identity,
                terminal_at.isoformat(),
                remaining,
                aws_request_id,
            )
        )
        counts.coalesced += 1
        return ServingDomainElectionResult(
            domain,
            ServingElectionOutcome.DRAIN_INCOMPLETE,
            drained_cohort_revision=head.cohort_revision,
        )
    fresh_arms, fresh_walk = _read_serving_iteration(context, shards, counts, rehearsal)
    successor_ids = tuple(
        item.subject
        for item in fresh_arms
        if item.scope is ArmScope.SERVING and item.subject != current.candidate.run_id
    )
    successor_reads = _timed(
        rehearsal, "candidate_read", lambda: _candidate_reads(context.store, successor_ids)
    )
    successor_candidates = tuple(item.candidate for item in successor_reads)
    fresh_authority = _timed(
        rehearsal, "head_frontier_read", lambda: context.store.read_serving_domain(domain)
    )
    if fresh_authority.head is None or fresh_authority.frontier is None:
        raise InvariantError(_ARM_INVARIANT, "fresh serving authority is incomplete.")
    successor = newest_eligible_candidate(successor_candidates)
    successor_cohort = None
    successor_input = None
    if successor is not None:
        # The final alias's own COMPLETE# is deleted by this same transaction, so it is
        # never a lower gap for the successor; every other lower obligation still is.
        undrained = tuple(item for item in fresh_walk if item.run_id != current.candidate.run_id)
        if serving_gap(undrained, successor_candidates) is not None:
            return ServingDomainElectionResult(
                domain,
                ServingElectionOutcome.WAITING_LOWER_ORDINAL,
                drained_cohort_revision=head.cohort_revision,
            )
        successor_cohort, successor_input = _revisions(successor_reads, fresh_authority.frontier)
    candidate_digest = _candidate_digest(successor_candidates)
    request = DrainFinalAliasAndActivateNextRequest(
        fresh_authority.head,
        direct,
        alias,
        current.candidate,
        current.identity,
        obligation,
        terminal_at.isoformat(),
        fresh_authority.frontier,
        successor_candidates,
        candidate_digest,
        successor,
        successor_cohort,
        successor_input,
        SERVING_PLAN_DIGEST if successor is not None else None,
        aws_request_id,
    )

    def activate() -> DrainFinalAliasAndActivateNextResult:
        if rehearsal is not None:
            rehearsal.before_activation()
        return context.store.drain_final_alias_and_activate_next(request)

    result = _timed(rehearsal, "activation", activate)
    counts.coalesced += 1
    return ServingDomainElectionResult(
        domain,
        result.outcome,
        drained_cohort_revision=head.cohort_revision,
        elected_cohort_revision=result.transition.elected_cohort_revision,
        transition=result.transition,
    )


def rehearse_fresh_election(  # noqa: PLR0913, PLR0917 -- the rehearsal's explicit inputs
    context: PromoterContext,
    shards: Sequence[str],
    domain: str,
    rehearsal: FreshElectionRehearsal,
    invocation_clock: Callable[[], Callable[[], int]],
    aws_request_id: str,
) -> ServingDomainElectionResult:
    """Run one non-clearing `FreshElectionTimingV1` sample on a domain at A's final alias.

    Every attempt is what the next Lambda invocation would run after a conflict: a new
    remaining-time callable from `invocation_clock`, a new pass-start iteration, one strong head
    read, then `_drain_bound`'s guard check and wholly fresh final iteration. The first
    `rehearsal.conflicts` attempts end at the injected conflict, which replaces the final
    transaction call and writes nothing; the next commits the one real fixed-member transaction.
    Anything else (a non-final alias, a lower-ordinal wait, a guard refusal, a real conflict or a
    store failure) ends the sample with no activation, and nothing is retried or repaired.
    """
    # The rehearsal is an operator input; validate it as an object, as the Lambda clock is.
    if not isinstance(rehearsal, FreshElectionRehearsal) or not callable(invocation_clock):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError("a fresh-election rehearsal needs its rehearsal input and clock.")
    if not aws_request_id or not domain:
        raise InvariantError(_PROMOTER_CONTEXT_INVALID, "the rehearsal context is invalid.")
    for _ in range(rehearsal.conflicts + 1):
        counts = ArmCounts()
        arms, walk = _read_serving_iteration(context, shards, counts)
        state = _DomainPass(
            context,
            shards,
            counts,
            _remaining_reader(invocation_clock()),
            aws_request_id,
            walk,
            frozenset(item.subject for item in arms if item.scope is ArmScope.SERVING),
            rehearsal,
        )
        authority = context.store.read_serving_domain(domain)
        head = authority.head
        if (
            head is None
            or authority.frontier is None
            or head.state != "BOUND_DRAINING"
            or head.direct_grant_id is None
        ):
            raise FreshElectionRehearsalRefusedError("the rehearsal domain is not draining.")
        try:
            result = _drain_bound(state, domain, authority)
        except FreshElectionInjectedConflictError:
            rehearsal.end_attempt(injected_conflict=True)
            continue
        if result is None or result.outcome not in {
            ServingElectionOutcome.ELECTED,
            ServingElectionOutcome.NO_ELIGIBLE,
        }:
            outcome = None if result is None else result.outcome.value
            raise FreshElectionRehearsalRefusedError(f"the rehearsal attempt ended {outcome}.")
        rehearsal.end_attempt(injected_conflict=False)
        return result
    raise FreshElectionRehearsalRefusedError("the rehearsal envelope ended without activation.")


def unconsumed_arms(context: PromoterContext, arms: Sequence[DueItem]) -> list[DueItem]:
    """Drop the serving `ARM#` items this pass's election consumed (TD-290).

    `activate_serving_winner` and the alias drains delete the winner's or loser's serving `ARM#`
    in their own transaction, after `read_arms` read it. Each serving item read this pass is
    re-read by its exact key (strongly), and only one that still exists goes on to `arm_run`, so
    a consumed item is never re-armed while a genuinely orphaned one still reaches the
    `_ARM_INVARIANT` check. A failed re-read keeps the item: `arm_run` then decides, as before.
    """
    kept: list[DueItem] = []
    for item in arms:
        if item.scope is ArmScope.SERVING:
            present: list[bool] = []

            def read(item: DueItem = item, present: list[bool] = present) -> None:
                found = context.store.read_due(item.shard, item.sk)
                present.append(any(entry.sk == item.sk for entry in found))

            if isolated(read, context.on_failure).ok and not present[0]:
                continue
        kept.append(item)
    return kept


def arm_pass(  # noqa: PLR0913, PLR0917
    context: PromoterContext,
    shards: Sequence[str],
    remaining_ms: Callable[[], int],
    aws_request_id: str,
) -> ArmCounts:
    """Run one fresh rolling election/drain pass plus independent unit batching."""
    counts = ArmCounts()
    coalesce_serving(context, shards, counts, remaining_ms, aws_request_id)
    arms = read_arms(context, shards, counts)
    walk = walk_complete(context, shards)
    if walk is None:
        counts.walk_failed += 1
    else:
        arms = unconsumed_arms(context, arms)
    candidates: list[UnitCandidate] = []
    pending: dict[str, int] = {}
    for item in arms:
        if item.scope is ArmScope.UNITS:
            action = lambda item=item: collect_unit_candidates(  # noqa: E731
                context, item, counts, (candidates, pending)
            )
        else:
            action = lambda item=item: arm_run(context, item, walk, counts)  # noqa: E731
        result = isolated(action, context.on_failure)
        counts.conflicts += result.conflicts
        counts.failed += 0 if result.ok else 1
    arm_unit_groups(context, candidates, pending, counts)
    return counts


def promote_shard(context: PromoterContext, shard: str) -> PromotionCounts:
    """One promoter pass over a shard: every woken or waiting unit, then stale instantiations."""
    counts = PromotionCounts()
    store = context.store
    units = {item.subject for item in store.read_due(shard, "PROMOTE#")}
    units |= {item.subject for item in store.read_due(shard, "WAITER#")}
    for unit in sorted(units):
        _isolate(context, lambda unit=unit: _promote(context, unit, counts), counts)
    cutoff = context.now() - INSTANTIATE_GRACE
    for item in store.read_due(shard, "INSTANTIATE#"):
        due = None if item.due_at is None else datetime.fromisoformat(item.due_at)
        if due is not None and due > cutoff:
            continue
        _isolate(context, lambda run=item.subject: _instantiate(context, run, counts), counts)
    return counts


def _promote(context: PromoterContext, unit: str, counts: PromotionCounts) -> None:
    promote_unit(context, unit, counts)


def _isolate(context: PromoterContext, action: Callable[[], None], counts: PromotionCounts) -> None:
    result = isolated(action, context.on_failure)
    counts.conflicts += result.conflicts
    counts.failed += 0 if result.ok else 1
