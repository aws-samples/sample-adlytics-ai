# SPDX-License-Identifier: MIT-0
"""The release issuer: the only path from eligibility to a grant, and grant apply (S4).

Every grant follows one order (BF-1): exact pre-grant parity, the co-release domain's active
ordinal, a complete `TARGET_RESERVATION_PROOF`, the grant, then apply.

1. `RELEASE#<run_id>` in the run's due shard says the run's pre-grant nodes may have settled.
   The `materialize_raw` settlement writes it once (S6 §2.2); `grant_apply` is never sent to
   an executor. The release sweep polls it: `NOT_READY` keeps it while parity is incomplete.
2. The run takes the domain's next ordinal by CAS on the idle `RELEASE_ORDER#<domain>/HEAD`. A
   domain head held by another run is `RELEASE_ORDER_BUSY`: the item stays and the run waits, so
   two plans that share targets reserve them in one serial order. A holder that leaves OPEN
   without a grant (cancelled, replaced, fenced or paused) returns the ordinal on its own next
   pass, or the waiting run returns it for the holder, so no run of the domain is stranded.
3. Each target's reservation is appended on its tail, then the proof is sealed after every
   reservation is ConditionChecked.
4. The grant transaction sets `grant_id` on `RUN#` (conditioned on OPEN, no grant and the read
   revision, V-29), ConditionChecks the active ordinal, the proof, `r >= H(U)` on the graph head
   and the domain's block, creates the grant, seals one lineage step and writes `APPLY#`. The
   legacy head is read before the transaction (it is static after GRAPH). A run whose
   retrieval is older than `H(U)` is fenced `older_than_current` instead (V-28).
5. Apply (release point 2) ConditionChecks and advances `H(U)`, moves the run to GRANTED, frees
   its slot, wakes `PROMOTE#<U>`, returns the domain head to idle, settles `grant_apply`,
   writes `publish` READY and `precompute` and `bake` PENDING (S6 §2.2; the promoter's arm pass
   arms them in the domain's serving order), and puts `COMPLETE#<run_id>`. POST_GRANT nodes
   exist only from here, bound to the exact grant and served revision. `APPLY#` is the re-drive
   of a grant that committed but did not apply (S1A-9 / R5-4, S3V3-1): the sweep retries it
   until apply commits.

An apply-time refusal after a grant (`INPUT_REVISION_SUPERSEDED` or `SLOT_NOT_OWNED`) is defense
in depth, reachable only by fault injection, and is a post-grant lineage failure: the
co-release domain is BLOCKED until owner review (BF-4), the run is FAILED and the ordinal goes
back to idle, so no undefined state remains and clearing the block restores release order. A
superseded or fenced run is never granted, so the units it completed leave no published pointer
(V-16).

Deployed disabled with the other graph workers while the mode is not GRAPH.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Literal, Protocol, cast

from adapters.jobs.execution_graph.isolation import FailureSink, ignore_failure, isolated
from adapters.outbound.telemetry.graph_metrics import emit_serving_withheld
from adapters.outbound.telemetry.reporting import LogSeverity, OperationLog
from domain.operations.execution_graph_dispatch import (
    DOMAIN_LOCK_HELD,
    RUN_STATE_CHANGED,
    SLOT_NOT_OWNED,
    TERMINAL_NODE_STATES,
    GraphGuardError,
    NodePhase,
    NodeState,
    ServingCandidate,
    ServingFrontierVector,
)
from domain.operations.execution_graph_release import (
    CONFLICT_DOMAIN_BLOCKED,
    GRANT_APPLY_NODE,
    GRANT_PARITY_INCOMPLETE,
    INPUT_REVISION_SUPERSEDED,
    LINEAGE_FAILED_OWNER_REVIEW,
    RELEASE_GRANT_RESERVATION_PROOF_REQUIRED,
    RELEASE_ORDER_BUSY,
    ApplyDecision,
    ApplyView,
    CohortContribution,
    CohortMember,
    CohortSettlement,
    GrantDecision,
    GrantView,
    ReleaseRefusedError,
    ReservationSet,
    ServingBinding,
    UnarmedPrecomputeRefusedError,
    UnarmedPrecomputeView,
    bind_serving,
    consumer_effect_record,
    decide_apply,
    decide_grant,
    failure_entries,
    grant_identity,
    next_lineage_step,
    post_grant_body,
    pre_grant_parity,
    published_head,
    release_conflict_domain,
    reservation_set,
    reservation_targets,
    run_post_grant_terminal,
    serving_withheld,
    settle_cohort,
    unarmed_precompute_contribution,
)
from domain.operations.generated_execution_graphs import BOUNDS, SERVING_COALESCING, TEMPLATES
from domain.operations.run_request import RunState
from ports.outbound.execution_graph_dispatch import (
    GraphDispatchStore,
    GraphStoreUnavailableError,
    NodeRecord,
    NodeSpec,
    RunRecord,
)
from ports.outbound.execution_graph_release import (
    ApplyCommit,
    BakeGraphStore,
    GrantCommit,
    GrantRecord,
    GraphReleaseStore,
    LegacyHeadReader,
    PostGrantFailure,
    ReleaseOrderHead,
    RetiredRelease,
    ServingDirectDrainRequest,
    ServingDomainBootstrapRequest,
    ServingDomainHead,
    ServingGraphReleaseStore,
    ServingMutationResult,
    SupersededGrant,
    UnarmedPrecomputeSettlement,
)

__all__ = [
    "Contributed",
    "LegacyHeadUnavailableError",
    "ReleaseContext",
    "ReleaseCounts",
    "ReleaseOutcome",
    "ReleaseStore",
    "apply_run",
    "bind_once",
    "contribute",
    "direct_binding",
    "issue_grant",
    "post_grant_specs",
    "record_contribution_once",
    "release_run",
    "release_shard",
    "serving_binding_for",
    "settle_unarmed_cohort",
    "try_complete_granted",
]

_REDECIDE: Final = int(BOUNDS["max_redecide"])


class ReleaseStore(
    GraphDispatchStore, GraphReleaseStore, ServingGraphReleaseStore, BakeGraphStore, Protocol
):
    """The graph store as the release issuer sees it."""

    def read_node(self, run_id: str, name: str) -> NodeRecord | None:
        """Read one node by its template name."""
        ...


class LegacyHeadUnavailableError(ReleaseRefusedError):
    """A run with no recorded window cannot read its legacy head; nothing is granted."""

    def __init__(self) -> None:
        super().__init__("LEGACY_HEAD_UNAVAILABLE")


def _no_report(line: OperationLog) -> None:
    """Drop a lifecycle line (the default when no logger is wired)."""
    del line


@dataclass(frozen=True, slots=True)
class ReleaseContext:
    """The store, the legacy-head reader and the clock one release pass runs with."""

    store: ReleaseStore
    legacy: LegacyHeadReader
    now: Callable[[], datetime]
    on_failure: FailureSink = ignore_failure
    domain_override: str | None = None
    """Probe-table tests only: a per-test co-release domain, so no two runs share a head."""
    serving_signal: Callable[[Literal[0, 1]], None] = emit_serving_withheld
    """ADR-139/140: `GraphServingWithheld`; the Release emits only `1`, for a withheld binding."""
    report: Callable[[OperationLog], None] = _no_report
    """The Release's lifecycle lines; the handler wires `report_operation` to its logger."""

    def domain_of(self, run: RunRecord) -> str:
        """Return the run's co-release domain: the template's, unless a probe overrides it."""
        return self.domain_override or release_conflict_domain(run.template)


class ReleaseOutcome(StrEnum):
    """How one release or apply attempt ended."""

    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    NOT_LIVE = "not_live"
    BLOCKED = "blocked"
    NOT_READY = "not_ready"
    SUPERSEDED = "superseded"
    WAITING_FOR_ORDER = "waiting_for_order"
    POST_GRANT_LOCK_WAIT = "post_grant_lock_wait"
    LINEAGE_FAILED = "lineage_failed"


@dataclass
class ReleaseCounts:
    """What one release sweep did, by outcome."""

    outcomes: dict[str, int] = field(default_factory=dict[str, int])
    guards: int = 0
    conflicts: int = 0
    failed: int = 0
    completed: int = 0
    """S6 §2.8 (AGG3-7): granted runs this pass completed."""
    completion_pending: int = 0
    """`COMPLETE#` items whose run is not yet terminal or bound; the next pass retries them."""
    unarmed_settled: int = 0
    """ADR-140: winners whose bake skipped the finalizer that this pass settled and bound."""

    def add(self, outcome: ReleaseOutcome) -> None:
        """Count one outcome."""
        self.outcomes[outcome.value] = self.outcomes.get(outcome.value, 0) + 1


def _now(context: ReleaseContext) -> str:
    return context.now().isoformat()


def _serving_bootstrap_request(
    context: ReleaseContext, commit: ApplyCommit
) -> ServingDomainBootstrapRequest:
    """Build a domain-only bootstrap proposal without reading a clock."""
    authority = context.store.read_serving_domain(commit.grant.domain)
    head = authority.head or ServingDomainHead(commit.grant.domain, "IDLE", 0)
    shard_count = int(SERVING_COALESCING["serving_frontier_shards"])
    frontier = authority.frontier or ServingFrontierVector(
        revisions=(0,) * shard_count,
        visible_counts=(0,) * shard_count,
        eligible_counts=(0,) * shard_count,
    )
    return ServingDomainBootstrapRequest(commit, head, frontier)


def _run(context: ReleaseContext, run_id: str) -> RunRecord:
    run = context.store.read_run(run_id)
    if run is None:
        raise ReleaseRefusedError(RUN_STATE_CHANGED)
    return run


def _head(context: ReleaseContext, run: RunRecord) -> tuple[datetime | None, int | None]:
    """`H(U)` and the graph head's epoch: the legacy head is read before any transaction."""
    graph, epoch = context.store.read_graph_head(run.conflict_unit)
    if run.window is None:
        raise LegacyHeadUnavailableError
    legacy = context.legacy.head_after(run.window, datetime.fromisoformat(run.retrieved_at))
    return published_head(graph, legacy), epoch


def _reservation(  # noqa: PLR0913, PLR0917 -- one private set builder
    run: RunRecord, order: ReleaseOrderHead, nodes: tuple[NodeRecord, ...], ordinal: int
) -> ReservationSet:
    targets = reservation_targets((node.name, node.phase, node.lock_key) for node in nodes)
    return reservation_set(run.run_id, order.domain, ordinal, targets)


def _proof_complete(context: ReleaseContext, reservation: ReservationSet) -> bool:
    digest = context.store.proof_digest(reservation)
    return digest is not None and digest == reservation.digest


_REFUSAL: Final = {
    GrantDecision.NOT_LIVE: RUN_STATE_CHANGED,
    GrantDecision.BLOCKED: CONFLICT_DOMAIN_BLOCKED,
    GrantDecision.NOT_READY: GRANT_PARITY_INCOMPLETE,
    GrantDecision.SUPERSEDED: INPUT_REVISION_SUPERSEDED,
    GrantDecision.PROOF_REQUIRED: RELEASE_GRANT_RESERVATION_PROOF_REQUIRED,
}


def issue_grant(  # noqa: PLR0913 -- the command and its two presented values
    context: ReleaseContext, run_id: str, *, ordinal: int | None, expected_revision: int | None
) -> GrantRecord:
    """Run the grant command (BF-1): refuse with the exact signal, or commit exactly one grant.

    The caller presents the ordinal it holds and the `RUN#` revision it read. A missing or stale
    ordinal, an incomplete proof or a moved revision is `RELEASE_GRANT_RESERVATION_PROOF_REQUIRED`
    with no write. An exact replay returns the grant the run already carries.
    """
    store = context.store
    run = _run(context, run_id)
    if run.grant_id is not None:
        replay = store.read_grant(run.grant_id)
        if replay is None:
            raise ReleaseRefusedError(RUN_STATE_CHANGED)
        return replay
    nodes = store.read_nodes(run_id)
    domain = context.domain_of(run)
    order = store.read_release_order(domain)
    held = ordinal is not None and order.active_run_id == run_id and order.active_ordinal == ordinal
    reservation = _reservation(run, order, nodes, ordinal) if ordinal is not None else None
    head, graph_epoch = _head(context, run)
    view = GrantView(
        run_state=run.state,
        grant_id_present=False,
        parity=pre_grant_parity((node.name, node.phase, node.state) for node in nodes),
        holds_active_ordinal=held,
        proof_complete=reservation is not None and _proof_complete(context, reservation),
        revision_matches=expected_revision == run.revision,
        blocked=store.read_block(domain) is not None,
        r=datetime.fromisoformat(run.retrieved_at),
        head=head,
    )
    decision = decide_grant(view)
    if decision is not GrantDecision.GRANT:
        raise ReleaseRefusedError(_REFUSAL[decision])
    assert reservation is not None and ordinal is not None  # noqa: S101 -- GRANT implies both
    lineage_head = store.read_lineage(run.conflict_unit)
    grant_id = grant_identity(run_id, ordinal)
    step = next_lineage_step(lineage_head, grant_id)
    grant = GrantRecord(
        grant_id=grant_id,
        run_id=run_id,
        domain=domain,
        conflict_unit=run.conflict_unit,
        ordinal=ordinal,
        set_id=reservation.set_id,
        proof_digest=reservation.digest,
        retrieved_at=run.retrieved_at,
        head_at_grant=None if head is None else head.isoformat(),
        lineage_sequence=step.sequence,
        recorded_at=_now(context),
    )
    store.issue_grant(GrantCommit(run, grant, reservation, order, step, lineage_head, graph_epoch))
    return grant


def post_grant_specs(run: RunRecord, grant: GrantRecord) -> tuple[NodeSpec, ...]:
    """Return the template's POST_GRANT nodes, each bound to the exact grant and served revision."""
    served = datetime.fromisoformat(run.retrieved_at)
    specs: list[NodeSpec] = []
    for item in TEMPLATES[run.template]["nodes"]:
        if str(item.get("phase", NodePhase.PRE_GRANT.value)) != NodePhase.POST_GRANT.value:
            continue
        if item.get("dynamic") is True:
            continue  # plan 4.6: the bake fan-out puts bake_task and bake_finalize
        name = str(item["node"])
        specs.append(
            NodeSpec(
                name=name,
                after=tuple(str(after) for after in item["after"]),
                phase=NodePhase.POST_GRANT,
                lock_key=f"POST_GRANT#{name}#{run.conflict_unit}",
                group_id=f"{name}#{run.conflict_unit}",
                body=post_grant_body(
                    node=name,
                    grant_id=grant.grant_id,
                    run_id=run.run_id,
                    conflict_unit=run.conflict_unit,
                    served_revision=served,
                    root_flow_id=run.root_flow_id,
                ),
            )
        )
    return tuple(specs)


def _fail_post_grant(  # noqa: PLR0913 -- one private failure writer
    context: ReleaseContext, run: RunRecord, grant: GrantRecord, *, slot_owned: bool
) -> ReleaseOutcome:
    order = context.store.read_release_order(grant.domain)
    held = order if order.active_run_id == run.run_id else None
    context.store.record_post_grant_failure(
        PostGrantFailure(run, grant, slot_owned, LINEAGE_FAILED_OWNER_REVIEW, _now(context), held)
    )
    return ReleaseOutcome.LINEAGE_FAILED


_KEEPS_TRIGGER: Final = frozenset({RunState.OPEN, RunState.PAUSED})


def _retire(context: ReleaseContext, run: RunRecord, order: ReleaseOrderHead | None) -> None:
    """Return an ungranted, no-longer-OPEN run's ordinal; retire its trigger once terminal."""
    terminal = run.state not in _KEEPS_TRIGGER
    if order is None and not terminal:
        return
    context.store.retire_release(RetiredRelease(run, order, retire_trigger=terminal))


def _holder_left_open(context: ReleaseContext, order: ReleaseOrderHead) -> RunRecord | None:
    """Return the ordinal's holder when it left OPEN without a grant, so it cannot release it."""
    if order.active_run_id is None:
        return None
    holder = context.store.read_run(order.active_run_id)
    if holder is None or holder.state is RunState.OPEN or holder.grant_id is not None:
        return None
    return holder


def apply_run(context: ReleaseContext, run_id: str) -> ReleaseOutcome:  # noqa: PLR0911
    """Apply a committed grant (release point 2); the `APPLY#` sweep re-drives it."""
    store = context.store
    for _ in range(_REDECIDE):
        run = _run(context, run_id)
        if run.grant_id is None:
            return ReleaseOutcome.NOT_LIVE
        grant = store.read_grant(run.grant_id)
        if grant is None:
            raise ReleaseRefusedError(RUN_STATE_CHANGED)
        if run.state is RunState.FAILED:
            return ReleaseOutcome.LINEAGE_FAILED
        slot_owned = store.read_promotion(run.conflict_unit).slot_run_id == run_id
        head, graph_epoch = _head(context, run)
        decision = decide_apply(
            ApplyView(
                run_state=run.state,
                run_grant_id=run.grant_id,
                grant_id=grant.grant_id,
                slot_owned=slot_owned,
                r=datetime.fromisoformat(run.retrieved_at),
                head=head,
            )
        )
        if decision is ApplyDecision.ALREADY_APPLIED:
            return ReleaseOutcome.ALREADY_APPLIED
        if decision is ApplyDecision.LINEAGE_FAILURE:
            return _fail_post_grant(context, run, grant, slot_owned=slot_owned)
        order = store.read_release_order(grant.domain)
        try:
            apply = ApplyCommit(
                run=run,
                grant=grant,
                order=order,
                grant_apply=store.read_node(run_id, GRANT_APPLY_NODE),
                post_grant=post_grant_specs(run, grant),
                graph_head_epoch=graph_epoch,
                recorded_at=_now(context),
            )
            store.bootstrap_serving_domain_and_apply_grant(
                _serving_bootstrap_request(context, apply)
            )
        except GraphGuardError as guard:
            if guard.signal == DOMAIN_LOCK_HELD:
                return ReleaseOutcome.POST_GRANT_LOCK_WAIT
            if guard.signal in {INPUT_REVISION_SUPERSEDED, SLOT_NOT_OWNED}:
                return _fail_post_grant(context, run, grant, slot_owned=slot_owned)
            continue
        return ReleaseOutcome.APPLIED
    raise GraphGuardError(RUN_STATE_CHANGED)


def release_run(context: ReleaseContext, run_id: str) -> ReleaseOutcome:  # noqa: PLR0911, PLR0912
    """Take one run from eligibility through its grant to apply, in the one BF-1 order."""
    store = context.store
    for _ in range(_REDECIDE):
        run = _run(context, run_id)
        if run.grant_id is not None:
            return apply_run(context, run_id)
        domain = context.domain_of(run)
        order = store.read_release_order(domain)
        holds = order.active_run_id == run_id and order.active_ordinal is not None
        if run.state is not RunState.OPEN:
            # V-16: a superseded, fenced or paused run is never granted. The ordinal it holds
            # goes back to idle, so no other run of the domain is stranded behind it.
            try:
                _retire(context, run, order if holds else None)
            except GraphGuardError:
                continue
            return ReleaseOutcome.NOT_LIVE
        if store.read_block(domain) is not None:
            return ReleaseOutcome.BLOCKED
        nodes = store.read_nodes(run_id)
        if not pre_grant_parity((node.name, node.phase, node.state) for node in nodes):
            return ReleaseOutcome.NOT_READY
        head, _ = _head(context, run)
        if head is not None and datetime.fromisoformat(run.retrieved_at) < head:
            try:
                store.refuse_superseded(
                    SupersededGrant(run, order if holds else None, _now(context))
                )
            except GraphGuardError:
                continue
            return ReleaseOutcome.SUPERSEDED
        try:
            if not holds:
                if order.active_run_id is not None:
                    holder = _holder_left_open(context, order)
                    if holder is None:
                        return ReleaseOutcome.WAITING_FOR_ORDER
                    # The holder's own trigger may be gone: return its ordinal for it.
                    _retire(context, holder, order)
                    continue
                reservation = _reservation(run, order, nodes, order.next_ordinal)
                store.acquire_ordinal(order, run, reservation)
                continue
            assert order.active_ordinal is not None  # noqa: S101 -- holds implies an ordinal
            reservation = _reservation(run, order, nodes, order.active_ordinal)
            reserved = set(store.read_reserved_targets(reservation))
            for target in reservation.targets:
                if target not in reserved:
                    store.reserve_target(reservation, target)
            if not _proof_complete(context, reservation):
                store.seal_proof(reservation)
            issue_grant(
                context, run_id, ordinal=order.active_ordinal, expected_revision=run.revision
            )
        except GraphGuardError as guard:
            if guard.signal == RELEASE_ORDER_BUSY and not holds:
                return ReleaseOutcome.WAITING_FOR_ORDER
            continue
        except ReleaseRefusedError as refused:
            if refused.signal in {RUN_STATE_CHANGED, RELEASE_GRANT_RESERVATION_PROOF_REQUIRED}:
                continue
            raise
    raise GraphGuardError(RUN_STATE_CHANGED)


def try_complete_granted(  # noqa: PLR0911 -- every unready guard returns None before the clock
    context: ReleaseContext, run_id: str
) -> ServingMutationResult | None:
    """Complete a bound GRANTED run with exactly one caller-owned UTC clock read."""
    store = context.store
    run = store.read_run(run_id)
    if run is None or run.state is not RunState.GRANTED or run.grant_id is None:
        return None
    nodes = tuple(node for node in store.read_nodes(run_id) if node.phase is NodePhase.POST_GRANT)
    if not nodes or not run_post_grant_terminal([node.state for node in nodes]):
        return None
    binding = store.read_serving_binding(run.grant_id)
    if binding is None:
        return None
    execution = store.read_serving_execution(run.grant_id, run.run_id)
    if execution is None or run.grant_ordinal is None:
        return None
    publish = next((node for node in store.read_nodes(run_id) if node.name == "publish"), None)
    if publish is None or publish.effect_digest is None:
        return None
    winner = ServingCandidate(
        run_id=run.run_id,
        grant_id=run.grant_id,
        root_flow_id=run.root_flow_id,
        domain=execution.head.domain,
        grant_ordinal=run.grant_ordinal,
        publication_effect_digest=publish.effect_digest,
    )
    authority = store.read_serving_domain(execution.head.domain)
    if authority.head is None:
        raise GraphStoreUnavailableError("a serving execution has no domain head.")
    if serving_withheld(binding):
        _signal_withheld(context, run.run_id, binding)
    instant = context.now()
    if instant.tzinfo is None or instant.utcoffset() is None or instant.utcoffset() != timedelta(0):
        raise ValueError("the completion clock must be timezone-aware UTC.")
    try:
        return store.complete_granted_run(
            run,
            nodes,
            ServingDirectDrainRequest(authority.head, binding, winner),
            terminal_at=instant.isoformat(),
        )
    except GraphGuardError:
        return None


def _signal_withheld(context: ReleaseContext, run_id: str, binding: ServingBinding) -> None:
    """ADR-140: signal a withheld binding the Release completes (U2), at least once.

    `COMPLETE#` survives until the completion commits, so a crash after this emission re-emits
    on the next pass; a raising emitter fails the item and the next pass retries. The Release
    never emits `0`: only a fresh `FINALIZED` commit clears the alarm.
    """
    context.serving_signal(1)
    context.report(
        OperationLog(
            operation="execution_graph.release",
            component="execution_graph_worker",
            phase="complete",
            outcome="serving_withheld",
            severity=LogSeverity.WARNING,
            details={
                "run_id": run_id,
                "failure_reasons": sorted({reason for _key, reason in failure_entries(binding)}),
                "unavailable_count": len(binding.unavailable),
            },
        )
    )


def _utc_now(context: ReleaseContext) -> datetime:
    instant = context.now()
    if instant.tzinfo is None or instant.utcoffset() is None or instant.utcoffset() != timedelta(0):
        raise ValueError("the release clock must be timezone-aware UTC.")
    return instant


def _authority_absent(run: RunRecord, name: str) -> UnarmedPrecomputeRefusedError:
    return UnarmedPrecomputeRefusedError(
        run.run_id, cast("str", run.grant_id), f"authority_absent:{name}"
    )


def settle_unarmed_cohort(context: ReleaseContext, run_id: str) -> ServingMutationResult | None:
    """ADR-140: settle a winner whose bake ended without its finalizer (U1/U2), or wait.

    Only the finalizer arms precompute, so a bake that settled `NO_ELIGIBLE` (U1) or on a
    consumer invariant (U2) would leave precompute `WAITING_INPUT`, the cohort unbound and the
    domain head `ACTIVE` forever. Reading only stored items, so whichever Baker version wrote
    them, this settles precompute `NO_ELIGIBLE` and binds the cohort in one transaction; the
    completion that follows drains the head as for any bound run. None (wait) unless the run is
    `GRANTED`, precompute `WAITING_INPUT`, the bake terminal and its member stored. A stored
    shape outside U1/U2, or missing authority after those guards, is
    `UnarmedPrecomputeRefusedError` (`GRAPH_PRECOMPUTE/7`): a store-shape defect that pages.
    """
    store = context.store
    run = store.read_run(run_id)
    if run is None or run.state is not RunState.GRANTED or run.grant_id is None:
        return None
    grant = run.grant_id
    nodes = store.read_nodes(run_id)
    by_name = {node.name: node for node in nodes}
    precompute_node, bake_node = by_name.get("precompute"), by_name.get("bake")
    if (precompute_node is not None and precompute_node.state is not NodeState.WAITING_INPUT) or (
        bake_node is not None and bake_node.state not in TERMINAL_NODE_STATES
    ):
        return None
    stored = {item.member: item for item in store.read_contributions(grant)}
    bake = stored.get(CohortMember.BAKE)
    if bake is None:
        return None
    # After these guards the rule settles or refuses (a missing node is `node_absent`).
    contribution = unarmed_precompute_contribution(
        UnarmedPrecomputeView(
            run_id=run_id,
            grant_id=grant,
            root_flow_id=run.root_flow_id,
            nodes=tuple((node.name, node.state) for node in nodes),
            bake=bake,
            precompute=stored.get(CohortMember.PRECOMPUTE),
            at=_utc_now(context),
        )
    )
    if contribution is None or precompute_node is None:
        return None
    execution = store.read_serving_execution(grant, run_id)
    if execution is None:
        raise _authority_absent(run, "serving_execution")
    head = store.read_serving_domain(execution.head.domain).head
    if (
        head is None
        or head.state != "ACTIVE"
        or head.winner_run_id != run_id
        or head.cohort_revision != execution.head.cohort_revision
    ):
        raise _authority_absent(run, "active_head")
    if run.grant_ordinal is None:
        raise _authority_absent(run, "grant_ordinal")
    publish = by_name.get("publish")
    if publish is None or publish.effect_digest is None:
        raise _authority_absent(run, "publish_effect")
    settlement = settle_cohort(grant, tuple(CohortMember), (bake, contribution))
    if settlement is None:
        raise ValueError("an unarmed cohort with both members did not settle.")
    record, digest = consumer_effect_record(
        CohortMember.PRECOMPUTE, contribution, {"observed_union_count": 0}
    )
    try:
        return store.settle_unarmed_precompute(
            UnarmedPrecomputeSettlement(
                run=run,
                head=head,
                bake=bake,
                precompute=contribution,
                node=precompute_node,
                binding=direct_binding(store, settlement, run),
                effect_record=record,
                effect_digest=digest,
                recorded_at=contribution.settled_at.isoformat(),
            )
        )
    except GraphGuardError:
        return None


@dataclass(frozen=True, slots=True)
class Contributed:
    """What `contribute` settled from: the stored contribution, and the binding once bound."""

    contribution: CohortContribution
    """The member's stored contribution. On a replay it is the first attempt's, not the new one."""
    binding: ServingBinding | None
    replayed: bool


def record_contribution_once(
    store: ReleaseStore, contribution: CohortContribution
) -> tuple[CohortContribution, bool]:
    """Record one sibling's terminal once; return the stored one and whether it was a replay.

    S6 §2.8 (AGG3-1): the grant's contributions are read first. When this member's already
    exists, nothing is recorded or recomputed and the caller settles from the stored one,
    because an `UNAVAILABLE` list (or `settled_at`) can differ between attempts and the store
    refuses any replay that is not identical.
    """
    stored = next(
        (
            item
            for item in store.read_contributions(contribution.grant_id)
            if item.member is contribution.member
        ),
        None,
    )
    if stored is None:
        store.record_contribution(contribution)
    return (contribution if stored is None else stored), stored is not None


def serving_binding_for(
    store: ReleaseStore, grant_id: str, run: RunRecord
) -> ServingBinding | None:
    """Derive the direct binding of a complete cohort, or None while a member is missing.

    Pure derivation over stored authority: the existing binding when one is stored, else the
    one `bind_serving` would write. The V3 finalizer writes it inside its final transaction
    (plan 4.6); `contribute` writes it alone.
    """
    settlement = settle_cohort(grant_id, tuple(CohortMember), store.read_contributions(grant_id))
    if settlement is None:
        return None
    existing = store.read_serving_binding(grant_id)
    if existing is not None:
        return existing
    return direct_binding(store, settlement, run)


def direct_binding(
    store: ReleaseStore, settlement: CohortSettlement, run: RunRecord
) -> ServingBinding:
    """Derive a settled cohort's direct binding from the winner's exact active authority.

    Shared by `serving_binding_for` (the finalizer and `contribute`) and the Release's unarmed
    settlement (ADR-140), so every writer binds the same bytes.
    """
    grant_id = settlement.grant_id
    execution = store.read_serving_execution(grant_id, run.run_id)
    if (
        execution is None
        or execution.head.winner_run_id != run.run_id
        or execution.head.cohort_revision is None
        or execution.head.serving_input_revision is None
    ):
        raise ValueError("a serving contribution has no exact active winner/cohort/day authority.")
    cohort_runs = tuple(store.read_run(run_id) for run_id in execution.run_ids)
    if any(candidate is None for candidate in cohort_runs):
        raise ValueError("an active serving cohort names a missing candidate run.")
    contributor_roots = tuple(
        candidate.root_flow_id for candidate in cohort_runs if candidate is not None
    )
    return bind_serving(
        settlement,
        conflict_unit=run.conflict_unit,
        served_revision=datetime.fromisoformat(run.retrieved_at),
        contributor_roots=contributor_roots,
        root_flow_id=run.root_flow_id,
        authority_epoch=run.authority_epoch,
        domain=execution.head.domain,
        cohort_revision_value=execution.head.cohort_revision,
        serving_input_revision_value=execution.head.serving_input_revision,
        plan_digest=execution.plan_digest,
        winner_run_id=run.run_id,
        execution_attempts=1,
    )


def contribute(
    store: ReleaseStore, contribution: CohortContribution, run: RunRecord
) -> Contributed:
    """Record one sibling's terminal once; bind serving once the cohort is complete.

    The binding step runs either way; it is idempotent.
    """
    effective, replayed = record_contribution_once(store, contribution)
    existing = store.read_serving_binding(contribution.grant_id)
    if existing is not None:
        return Contributed(effective, existing, replayed)
    binding = serving_binding_for(store, contribution.grant_id, run)
    if binding is None:
        return Contributed(effective, None, replayed)
    return Contributed(effective, bind_once(store, binding), replayed)


def bind_once(store: ReleaseStore, binding: ServingBinding) -> ServingBinding:
    """Bind the cohort, or adopt the binding a concurrent sibling wrote first (ADR-140).

    `bind_serving` admits one binding per grant (`attribute_not_exists`), so precompute's
    `contribute` and an abandoning finalizer can race to it; the loser adopts the stored one.
    A guard with no stored binding is not a lost race and is re-raised.
    """
    try:
        store.bind_serving(binding)
    except GraphGuardError:
        stored = store.read_serving_binding(binding.grant_id)
        if stored is None:
            raise
        return stored
    return binding


def _complete(context: ReleaseContext, run_id: str, counts: ReleaseCounts) -> None:
    if settle_unarmed_cohort(context, run_id) is not None:
        counts.unarmed_settled += 1
    if try_complete_granted(context, run_id) is not None:
        counts.completed += 1
    else:
        counts.completion_pending += 1


def release_shard(context: ReleaseContext, shard: str) -> ReleaseCounts:
    """One release pass over a shard: `RELEASE#` triggers, `APPLY#` re-drives, `COMPLETE#`.

    S6 §2.8 (AGG3-7): the third loop makes the release worker the run-completion writer. A
    `COMPLETE#` stays until completion commits, so a failed completion is retried each pass.
    """
    counts = ReleaseCounts()
    store = context.store
    for item in store.read_due(shard, "RELEASE#"):
        _isolate(context, lambda run=item.subject: counts.add(release_run(context, run)), counts)
    for item in store.read_due(shard, "APPLY#"):
        _isolate(context, lambda run=item.subject: counts.add(apply_run(context, run)), counts)
    for item in store.read_due(shard, "COMPLETE#"):
        _isolate(context, lambda run=item.subject: _complete(context, run, counts), counts)
    return counts


def _isolate(context: ReleaseContext, action: Callable[[], None], counts: ReleaseCounts) -> None:
    result = isolated(action, context.on_failure)
    counts.conflicts += result.conflicts
    counts.failed += 0 if result.ok else 1
