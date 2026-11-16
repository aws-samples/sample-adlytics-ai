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
per node. The pass collects OPEN runs' eligible PENDING `write_unit` nodes from the units `ARM#`
items, oldest run first, groups them by `TABLE#<k>`, grants each table once through `take_grant`
(oldest first, a solo node alone, at most `MAX_BATCH_MEMBERS`) and commits each grant with
`arm_unit_batch`. A `PAUSED` run's units wait (`paused_wait`). A run's `ARM#` is deleted in the
group transaction that commits its last PENDING unit, decided from committed groups only (N6).

ADR-162: every loop is bounded by the invocation's remaining time (`budget.py`). The waiter
phase stops at `PROMOTE_RESERVE_MS`, units collection at `UNITS_COLLECT_RESERVE_MS`, and table
grants and serving arming at `ITEM_RESERVE_MS`. Each domain's ACTIVE winner is armed before any
other `ARM#` item, so a serving backlog never hides its finalizer. Collection is oldest first by
the D41 key, so every run it does not reach is younger than every run it collected and each
grant still takes its table's oldest unit. What a pass leaves is counted `deferred` and read
again, strongly, by the next pass; nothing in memory crosses invocations.

Serving (ADR-162 §7-8): an election reads its candidates in grant-ordinal order until
`SERVING_STAGE_RESERVE_MS`; a roster that does not fit one pass is staged in `ELECTION#` pages
under the head version and committed, with the same functions over the same complete roster,
by the pass that reads its last candidate. A drain reads only the alias it drains next and
repeats until the same reserve, so one pass drains many aliases.

Deployed disabled with the dispatcher while the mode is not GRAPH.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from adapters.jobs.execution_graph.budget import (
    CLOCK_INVALID,
    ITEM_RESERVE_MS,
    NO_DEADLINE,
    PROMOTE_RESERVE_MS,
    SERVING_STAGE_RESERVE_MS,
    UNITS_COLLECT_RESERVE_MS,
    PassBudget,
    remaining_reader,
)
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
    grant_order_key,
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
    ServingDomainHead,
    ServingDomainReadResult,
    ServingElectionOutcome,
    ServingGraphReleaseStore,
    ServingStagePage,
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
_PROMOTER_CLOCK_INVALID: Final = CLOCK_INVALID
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
    deferred: int = 0
    """ADR-162: units and instantiations this pass left unstarted because its budget stopped it."""

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
    deferred: int = 0
    """ADR-162: `ARM#` items this pass left unstarted because its budget stopped it."""
    grants_deferred: int = 0
    """ADR-162: `TABLE#<k>` grants this pass left unstarted because its budget stopped it."""
    serving_staged: int = 0
    """ADR-162: election candidates read and staged this pass for a later pass's commit."""
    serving_unstaged: int = 0
    """ADR-162: election candidates this pass's budget left unread; a later pass reads them."""
    serving_legacy_winners: int = 0
    """ADR-162 §10: serving arms of a winner elected before §10, with no `SERVING_ROSTER`; its
    readers bind and decide eligibility by reading its members, as that election's code did."""


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


def _is_serving_winner(store: PromoterStore, run: RunRecord, counts: ArmCounts) -> bool:
    """Whether `run` is its cohort's elected winner; a pre-§10 (legacy) winner is counted."""
    execution = (
        None if run.grant_id is None else store.read_serving_execution(run.grant_id, run.run_id)
    )
    if execution is None or execution.head.winner_run_id != run.run_id:
        return False
    if execution.contributor_roots is None:
        counts.serving_legacy_winners += 1
    return True


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
    if scope is ArmScope.SERVING and not _is_serving_winner(store, run, counts):
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


def arm_unit_groups(  # noqa: PLR0913, PLR0917 -- the pass context, its units, its counts, budget
    context: PromoterContext,
    candidates: Sequence[UnitCandidate],
    pending: dict[str, int],
    counts: ArmCounts,
    budget: PassBudget = NO_DEADLINE,
) -> None:
    """Group the collected units by `TABLE#<k>` and grant each table once, each isolated.

    Tables are granted in the order of their oldest member (D41 key, then the table), so a table
    the budget does not reach this pass is the one with the youngest head, never a fixed one
    (ADR-162). No grant starts below `ITEM_RESERVE_MS`; its units stay PENDING for the next pass.
    """
    groups: dict[str, list[UnitCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.node.lock_key, []).append(candidate)
    order = sorted(
        groups, key=lambda key: (min(grant_order_key(item) for item in groups[key]), key)
    )
    for index, lock_key in enumerate(order):
        if not budget.allows(ITEM_RESERVE_MS):
            counts.grants_deferred += len(order) - index
            return
        result = isolated(
            lambda group=groups[lock_key]: _arm_group(context, group, pending, counts),
            context.on_failure,
        )
        counts.conflicts += result.conflicts
        counts.failed += 0 if result.ok else 1


def _candidate_read(store: PromoterStore, run_id: str) -> ServingCandidateRead | None:
    """Read and validate one candidate; None when it is not (yet) an eligible candidate."""
    read = store.read_serving_candidate(run_id)
    if read is not None:
        validate_serving_candidates((read.candidate,))
    return read


def _stage_id(head: ServingDomainHead) -> str:
    """Name the staged election by the head version every staged fact was read under.

    Only this promoter's own serving transitions change a waiting candidate, and each one moves
    the head version, so a page under another version is dead (ADR-162).
    """
    return canonical_authority_digest(
        [
            "graph-serving-stage/v1",
            head.domain,
            head.version,
            head.cohort_revision or "",
            head.elected_frontier_digest or "",
        ]
    )


@dataclass(frozen=True, slots=True)
class _ElectionInputs:
    """What an election is decided from: the head and frontier, the roster ids and the walk."""

    authority: ServingDomainReadResult
    run_ids: tuple[str, ...]
    walk: tuple[CompleteObligation, ...]

    @property
    def head(self) -> ServingDomainHead:
        """The strongly read head; an election never runs without one."""
        head = self.authority.head
        if head is None or self.authority.frontier is None:
            raise InvariantError(_ARM_INVARIANT, "fresh serving authority is incomplete.")
        return head


@dataclass(frozen=True, slots=True)
class _Roster:
    """A domain's election roster, or None while some candidate is still unread (ADR-162)."""

    reads: tuple[ServingCandidateRead, ...] | None
    pages: tuple[ServingStagePage, ...]
    """The live stage pages, deleted once the election commits."""
    fresh: tuple[ServingCandidateRead, ...] = ()
    """The reads this pass made that no page holds yet."""


def _staged_roster(  # noqa: PLR0913, PLR0917 -- the pass, its domain, inputs and carried reads
    state: _DomainPass,
    domain: str,
    inputs: _ElectionInputs,
    carried: Sequence[ServingCandidateRead] = (),
) -> _Roster:
    """Read every candidate of `run_ids` not already staged, while the pass's budget allows.

    Candidates are read in grant-ordinal order, so each pass extends one prefix. When every
    candidate is read, the roster is returned in `run_ids` order, exactly what one unpaged read
    would return; otherwise this pass's reads are written as stage pages and None is returned.
    An unreadable candidate empties the roster, as one unpaged read does. A page of a dead stage
    is deleted. `carried` are this pass's earlier unwritten reads under the same stage.
    """
    store, counts = state.context.store, state.counts
    run_ids, walk = inputs.run_ids, inputs.walk
    stage = _stage_id(inputs.head)
    pages = store.read_serving_stage(domain)
    live = tuple(page for page in pages if page.stage_id == stage)
    dead = tuple(page for page in pages if page.stage_id != stage)
    if dead:
        store.delete_serving_stage(dead)
    known: dict[str, ServingCandidateRead] = {}
    for read in (*(read for page in live for read in page.reads), *carried):
        if read.candidate.run_id in known:
            raise InvariantError(_ARM_INVARIANT, "a staged serving candidate is duplicated.")
        known[read.candidate.run_id] = read
    ordinals = {item.run_id: (item.grant_ordinal, item.grant_id) for item in walk}
    unstaged = sorted(
        (run_id for run_id in run_ids if run_id not in known),
        key=lambda run_id: (*ordinals.get(run_id, (-1, "")), run_id),
    )
    fresh: list[ServingCandidateRead] = list(carried)
    for index, member in enumerate(unstaged):
        if not state.budget.allows(SERVING_STAGE_RESERVE_MS):
            _keep(state, (domain, stage), _Roster(None, live, tuple(fresh)))
            counts.serving_unstaged += len(unstaged) - index
            return _Roster(None, live)
        read = _candidate_read(store, member)
        if read is None:
            return _Roster((), live)
        fresh.append(read)
        known[member] = read
    roster = tuple(known[member] for member in run_ids)
    return _Roster(roster, live, tuple(fresh))


def _spill(  # noqa: PLR0913, PLR0917 -- the store, the stage, its live pages and the new reads
    store: PromoterStore,
    stage: tuple[str, str],
    live: Sequence[ServingStagePage],
    fresh: Sequence[ServingCandidateRead],
) -> tuple[ServingStagePage, ...]:
    """Write this pass's reads as the stage's next pages, so no later pass reads them again."""
    if not fresh:
        return tuple(live)
    domain, stage_id = stage
    start = max((page.page for page in live), default=-1) + 1
    written = store.write_serving_stage(ServingStagePage(domain, stage_id, start, tuple(fresh)))
    return (*live, *written)


def _keep(state: _DomainPass, stage: tuple[str, str], roster: _Roster) -> None:
    """Write a roster's unwritten reads as stage pages and count them as staged."""
    _spill(state.context.store, stage, roster.pages, roster.fresh)
    state.counts.serving_staged += len(roster.fresh)


def _settled_roster(  # noqa: PLR0913, PLR0917 -- the pass, its domain, first inputs, re-read
    state: _DomainPass,
    domain: str,
    first: _ElectionInputs,
    reread: Callable[[], _ElectionInputs],
) -> tuple[_Roster, _ElectionInputs]:
    """Complete the roster, then read the head, frontier and walk again before the commit.

    ADR-162 §8: the commit ConditionChecks the frontier it read, so any publication that settles
    between that read and the commit refuses it (about 22 a minute on dev). The first frontier
    was read before every candidate read, the second after them, so only the second leaves the
    commit exposed for the candidates that settled meanwhile alone; no cutoff decides it. The
    reads stay in memory and are carried into the second roster, so a small election still
    writes no page, and a refused commit writes nothing (only this pass's unwritten reads are
    read again by the next pass).
    """
    roster = _staged_roster(state, domain, first)
    if not roster.reads or not roster.fresh:
        return roster, first
    again = reread()
    carried = roster.fresh if _stage_id(again.head) == _stage_id(first.head) else ()
    return _staged_roster(state, domain, again, carried), again


def _clear_stage(context: PromoterContext, pages: Sequence[ServingStagePage]) -> None:
    """Delete a committed election's pages; one left behind is dead and goes next time."""
    if pages:
        isolated(lambda: context.store.delete_serving_stage(pages), context.on_failure)


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


_remaining_reader: Final = remaining_reader
"""The promoter's validated Lambda clock, shared with the release (`budget.remaining_reader`)."""


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
    winners: set[str] = field(default_factory=set[str])
    """ADR-162: each domain's ACTIVE winner as this pass read its head, for `arm_pass` to arm
    first. A head this pass activates is not added: its winner's `ARM#` went with activation."""

    @property
    def budget(self) -> PassBudget:
        """The pass's remaining time, through the same validated reader (ADR-162)."""
        return PassBudget(self.read_remaining)


def coalesce_serving(  # noqa: PLR0913, PLR0917
    context: PromoterContext,
    shards: Sequence[str],
    counts: ArmCounts,
    remaining_ms: Callable[[], int],
    aws_request_id: str,
    winners: set[str] | None = None,
) -> tuple[ServingDomainElectionResult, ...]:
    """Elect or drain each domain from wholly fresh rolling authority reads.

    Each domain is isolated: its failure marks its typed child, is reported once through the
    pass's failure sink, and the next domain is still decided. A conflict is not retried here,
    because only a wholly fresh iteration may retry it; the next invocation is that iteration. An
    invalid Lambda clock is a pass failure and ends the pass. Each ACTIVE head's winner is added
    to `winners` when given (ADR-162), so the arm pass reaches it before any other serving item.
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
        winners=set() if winners is None else winners,
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
    if authority.head.state == "ACTIVE" and authority.head.winner_run_id:
        state.winners.add(authority.head.winner_run_id)
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
    """Activate the newest eligible candidate on an IDLE head, or report why none can be.

    The roster is read under the pass's budget and staged across passes when it does not fit
    one (ADR-162); the commit evaluates the same functions over the same complete roster.
    """
    context, head, frontier = state.context, authority.head, authority.frontier
    assert head is not None and frontier is not None  # noqa: S101 -- checked by the caller
    domain_walk = tuple(item for item in state.walk if item.domain == domain)
    run_ids = tuple(item.run_id for item in domain_walk if item.run_id in state.arm_ids)
    if not run_ids:
        return ServingDomainElectionResult(domain, ServingElectionOutcome.NO_ELIGIBLE)

    def reread() -> _ElectionInputs:
        arms, walk = _read_serving_iteration(context, state.shards, state.counts)
        fresh_walk = tuple(item for item in walk if item.domain == domain)
        serving = {item.subject for item in arms if item.scope is ArmScope.SERVING}
        return _ElectionInputs(
            context.store.read_serving_domain(domain),
            tuple(item.run_id for item in fresh_walk if item.run_id in serving),
            fresh_walk,
        )

    roster, inputs = _settled_roster(
        state, domain, _ElectionInputs(authority, run_ids, domain_walk), reread
    )
    if roster.reads is None:
        # No cohort exists until every candidate is read, so the attempt is named by its stage.
        with _promoter_domain(state.aws_request_id, domain, _stage_id(inputs.head)) as recording:
            recording.record(outcome=ServingElectionOutcome.STAGED)
        return ServingDomainElectionResult(domain, ServingElectionOutcome.STAGED)
    reads = roster.reads
    head, frontier, domain_walk = inputs.head, inputs.authority.frontier, inputs.walk
    assert frontier is not None  # noqa: S101 -- `inputs.head` proved the frontier present
    if head.state != "IDLE":
        return ServingDomainElectionResult(domain, ServingElectionOutcome.NO_ELIGIBLE)
    validate_serving_candidates(tuple(item.candidate for item in reads))
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
        request = ActivateServingWinnerRequest(
            head,
            frontier,
            candidates,
            winner,
            cohort,
            input_revision,
            context.now().isoformat(),
            SERVING_PLAN_DIGEST,
        )
        context.store.activate_serving_winner(request)
        state.counts.armed += 1
        state.counts.retired += 1
        recording.record(outcome=ServingElectionOutcome.ELECTED)
        _clear_stage(context, roster.pages)
        return ServingDomainElectionResult(
            domain,
            ServingElectionOutcome.ELECTED,
            elected_cohort_revision=cohort,
        )


def _newest_first(
    run_ids: Sequence[str], walk: Sequence[CompleteObligation], domain: str
) -> list[str]:
    """Order a cohort newest grant first from the walk's `COMPLETE#` ordinals (no run reads).

    A member the walk did not see sorts first, so its read (which finds no `COMPLETE#`) stops
    the drain before any alias moves, as one unpaged read of the cohort does.
    """
    ordinals = {item.run_id: item.grant_ordinal for item in walk if item.domain == domain}
    return sorted(
        run_ids, key=lambda run_id: (run_id in ordinals, -ordinals.get(run_id, 0), run_id)
    )


def _drain_bound(
    state: _DomainPass, domain: str, authority: ServingDomainReadResult
) -> ServingDomainElectionResult | None:
    """Alias bound candidates newest first while the budget allows, then drain the final alias.

    ADR-162: each alias reads only its own candidate, and aliases repeat within one pass until
    `SERVING_STAGE_RESERVE_MS`; the first alias of a pass keeps today's election guard alone. A
    non-final alias drain condition-checks the head and never moves it, so one head read serves
    every drain of the pass. The final alias drains with the successor election in one
    transaction, over a roster staged across passes when it does not fit one.
    """
    context, counts, aws_request_id = state.context, state.counts, state.aws_request_id
    head = authority.head
    assert head is not None and head.direct_grant_id is not None  # noqa: S101 -- caller checked
    direct = context.store.read_serving_binding(head.direct_grant_id)
    if direct is None:
        raise InvariantError(_ARM_INVARIANT, "a draining head has no direct binding.")
    obligations = tuple(
        item
        for shard in state.shards
        for item in context.store.read_due(shard, "COALESCE#")
        if item.subject == head.cohort_revision and item.run_ids
    )
    if len(obligations) != 1:
        raise InvariantError(_ARM_INVARIANT, "a draining head has no unique cohort obligation.")
    obligation = obligations[0]
    order = _newest_first(obligation.run_ids, state.walk, domain)
    if state.read_remaining() < _NEXT_ELECTION_MIN_REMAINING_MS:
        return ServingDomainElectionResult(
            domain,
            ServingElectionOutcome.DRAIN_INCOMPLETE,
            drained_cohort_revision=head.cohort_revision,
        )
    rehearsal = state.rehearsal
    if rehearsal is not None:
        if len(order) != 1:
            # The rehearsal times only A's final alias; it never writes a non-final drain.
            raise FreshElectionRehearsalRefusedError(
                "the rehearsal domain is not at A's last alias."
            )
        rehearsal.guard_passed()
    incomplete = ServingDomainElectionResult(
        domain,
        ServingElectionOutcome.DRAIN_INCOMPLETE,
        drained_cohort_revision=head.cohort_revision,
    )
    drained = 0
    while True:
        if drained and not state.budget.allows(SERVING_STAGE_RESERVE_MS):
            return incomplete
        current = _candidate_read(context.store, order[0])
        if current is None:
            return incomplete if drained else None
        terminal_at = context.now()
        alias = alias_serving(
            direct,
            current.candidate,
            direct.cohort_revision or head.cohort_revision or "",
            bound_at=terminal_at,
        )
        if len(order) == 1:
            break
        context.store.drain_alias_binding(
            ServingAliasDrainRequest(
                head,
                direct,
                alias,
                current.candidate,
                current.identity,
                terminal_at.isoformat(),
                tuple(order[1:]),
                aws_request_id,
            )
        )
        counts.coalesced += 1
        drained += 1
        order = order[1:]
    return _drain_final(state, domain, (head, direct, obligation), (current, alias, terminal_at))


def _drain_final(  # noqa: PLR0913, PLR0917 -- the pass, its domain, the bound cohort, the alias
    state: _DomainPass,
    domain: str,
    bound: tuple[ServingDomainHead, ServingBinding, DueItem],
    final: tuple[ServingCandidateRead, ServingBinding, datetime],
) -> ServingDomainElectionResult:
    """Drain the final alias and elect the successor from a complete (staged) roster."""
    context, counts, rehearsal = state.context, state.counts, state.rehearsal
    head, direct, obligation = bound
    current, alias, terminal_at = final

    def iteration() -> _ElectionInputs:
        fresh_arms, fresh_walk = _read_serving_iteration(context, state.shards, counts, rehearsal)
        fresh_authority = _timed(
            rehearsal, "head_frontier_read", lambda: context.store.read_serving_domain(domain)
        )
        successor_ids = tuple(
            item.subject
            for item in fresh_arms
            if item.scope is ArmScope.SERVING and item.subject != current.candidate.run_id
        )
        return _ElectionInputs(fresh_authority, successor_ids, fresh_walk)

    first = iteration()
    roster, inputs = _timed(
        rehearsal, "candidate_read", lambda: _settled_roster(state, domain, first, iteration)
    )
    fresh_authority, fresh_head, fresh_walk = inputs.authority, inputs.head, inputs.walk
    assert fresh_authority.frontier is not None  # noqa: S101 -- `inputs.head` proved it present
    if roster.reads is None:
        return ServingDomainElectionResult(
            domain,
            ServingElectionOutcome.STAGED,
            drained_cohort_revision=head.cohort_revision,
        )
    successor_reads = roster.reads
    successor_candidates = tuple(item.candidate for item in successor_reads)
    validate_serving_candidates(successor_candidates)
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
        fresh_head,
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
        state.aws_request_id,
        tuple(item.coordinate for item in successor_reads),
    )

    def activate() -> DrainFinalAliasAndActivateNextResult:
        if rehearsal is not None:
            rehearsal.before_activation()
        return context.store.drain_final_alias_and_activate_next(request)

    result = _timed(rehearsal, "activation", activate)
    counts.coalesced += 1
    _clear_stage(context, roster.pages)
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
    """Run one fresh rolling election/drain pass plus independent unit batching (ADR-162).

    After the election, the serving `ARM#` of each domain's ACTIVE winner is armed first, so a
    winner's finalizer is reached whatever its shard. Then units `ARM#` items are collected
    oldest first by the D41 key while at least `UNITS_COLLECT_RESERVE_MS` remains and granted
    per table, then every other `ARM#` item is armed oldest first while `ITEM_RESERVE_MS`
    remains. Every uncollected run is younger than every collected one, so each table's grant
    still goes to its oldest PENDING unit. An item the budget leaves unstarted is counted
    `deferred` and stays in its shard for the next pass.
    """
    counts = ArmCounts()
    budget = PassBudget.of(remaining_ms)
    winners: set[str] = set()
    coalesce_serving(context, shards, counts, remaining_ms, aws_request_id, winners)
    arms = read_arms(context, shards, counts)
    walk = walk_complete(context, shards)
    if walk is None:
        counts.walk_failed += 1

    def oldest(item: DueItem) -> tuple[str, str]:
        return (item.root_clock_started_at or "", item.subject)

    def first(item: DueItem) -> bool:
        return item.scope is ArmScope.SERVING and item.subject in winners

    _arm_serving(context, [item for item in arms if first(item)], walk, counts, budget)
    units = sorted((item for item in arms if item.scope is ArmScope.UNITS), key=oldest)
    candidates: list[UnitCandidate] = []
    pending: dict[str, int] = {}
    for index, item in enumerate(units):
        if not budget.allows(UNITS_COLLECT_RESERVE_MS):
            counts.deferred += len(units) - index
            break
        _count_arm(
            context,
            lambda item=item: collect_unit_candidates(context, item, counts, (candidates, pending)),
            counts,
        )
    arm_unit_groups(context, candidates, pending, counts, budget)
    others = sorted(
        (item for item in arms if item.scope is not ArmScope.UNITS and not first(item)),
        key=oldest,
    )
    _arm_serving(context, others, walk, counts, budget)
    return counts


def _arm_serving(  # noqa: PLR0913, PLR0917 -- the pass context, its items, walk, counts, budget
    context: PromoterContext,
    items: Sequence[DueItem],
    walk: tuple[CompleteObligation, ...] | None,
    counts: ArmCounts,
    budget: PassBudget,
) -> None:
    """Arm each serving `ARM#` item in order while `ITEM_RESERVE_MS` remains; count the rest."""
    for index, item in enumerate(items):
        if not budget.allows(ITEM_RESERVE_MS):
            counts.deferred += len(items) - index
            return
        # TD-290: a serving item this pass's election consumed is not armed again.
        if walk is not None and not unconsumed_arms(context, (item,)):
            continue
        _count_arm(context, lambda item=item: arm_run(context, item, walk, counts), counts)


def _count_arm(context: PromoterContext, action: Callable[[], None], counts: ArmCounts) -> None:
    result = isolated(action, context.on_failure)
    counts.conflicts += result.conflicts
    counts.failed += 0 if result.ok else 1


def promote_shard(
    context: PromoterContext, shard: str, budget: PassBudget = NO_DEADLINE
) -> PromotionCounts:
    """One promoter pass over a shard: every woken or waiting unit, then stale instantiations.

    No unit or instantiation starts below `PROMOTE_RESERVE_MS`, so the arm pass after it keeps
    its time (ADR-162). The shard is read even once the budget has stopped the pass, so every
    unit and stale instantiation left unstarted, in this shard or a later one, is counted
    `deferred`; each stays for the next pass.
    """
    counts = PromotionCounts()
    store = context.store
    stopped = not budget.allows(PROMOTE_RESERVE_MS)
    units = {item.subject for item in store.read_due(shard, "PROMOTE#")}
    units |= {item.subject for item in store.read_due(shard, "WAITER#")}
    stopped = _promote_while(
        sorted(units),
        stopped,
        budget,
        lambda unit: _isolate(context, lambda: _promote(context, unit, counts), counts),
        counts,
    )
    cutoff = context.now() - INSTANTIATE_GRACE
    stale = [
        item.subject
        for item in store.read_due(shard, "INSTANTIATE#")
        if item.due_at is None or datetime.fromisoformat(item.due_at) <= cutoff
    ]
    _promote_while(
        stale,
        stopped,
        budget,
        lambda run: _isolate(context, lambda: _instantiate(context, run, counts), counts),
        counts,
    )
    return counts


def _promote_while(  # noqa: PLR0913, PLR0917 -- the subjects, the stop state, budget, action, counts
    subjects: Sequence[str],
    stopped: bool,
    budget: PassBudget,
    action: Callable[[str], None],
    counts: PromotionCounts,
) -> bool:
    """Run `action` on each subject while `PROMOTE_RESERVE_MS` remains; count the rest.

    Return whether the budget has stopped the pass. Once stopped, the clock is not read again.
    """
    for index, subject in enumerate(subjects):
        if stopped or not budget.allows(PROMOTE_RESERVE_MS):
            counts.deferred += len(subjects) - index
            return True
        action(subject)
    return stopped


def _promote(context: PromoterContext, unit: str, counts: PromotionCounts) -> None:
    promote_unit(context, unit, counts)


def _isolate(context: PromoterContext, action: Callable[[], None], counts: PromotionCounts) -> None:
    result = isolated(action, context.on_failure)
    counts.conflicts += result.conflicts
    counts.failed += 0 if result.ok else 1
