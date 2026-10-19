# SPDX-License-Identifier: MIT-0
"""Graph dispatch, claim, settlement, promotion and TableWriter acceptance (slice S3).

The execution-graph authority owns dependency readiness, dispatch permission and node
settlement. This module holds the pure rules every S3 writer shares:

- the node lifecycle and the signals each transition refuses with. `PLAN_DISPATCH_FENCED`
  refuses a permit after a generation advance (BF-2); `EXECUTION_SUPERSEDED` refuses a claim on
  a fenced or terminal run (V-9); a READY node with no permit settles `FENCED_UNSENT` (V-11);
- the claim predicate and the takeover rule for an expired claim lease (V-9, r4/r5);
- the fence reasons and the run terminal each settles to; a run turns terminal only when every
  node is terminal and its `INSTANTIATE#` obligation is gone (V-26, r5);
- the promoter's four conditioned branches and its lost-wakeup backstop (V-20, r5);
- TableWriter's mode-by-envelope acceptance table (V-12) and where the mode ConditionCheck goes
  (V-13: legacy finalize only after a `LEGACY_DRAINING` read, never on a graph claim);
- provider-accounted envelope sizing at the 262,144-byte ceiling, with a deterministic split
  along roster boundaries or `delivery_payload_oversize` (FR-4);
- the graph envelope: the canonical owner body plus its dispatch binding, whose digest covers
  the owner body only, so the binding the envelope carries is derivable from it.

The S1 contract (`contracts/operations/execution-graphs.yaml`) is pinned while the lagging
stacks are accepted (owner-gate Item 14), so S3's closed sets live here, beside the S1
vocabulary they extend. Bounds come from the generated contract.

Pure: no I/O, no clock, no AWS.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol, cast

from domain.operations.execution_graph import AuthorityMode
from domain.operations.generated_execution_graphs import (
    BOUNDS,
    ROUTES,
    SERVING_COALESCING,
    TEMPLATES,
)
from domain.operations.run_request import RunState, Waiter
from domain.operations.status import canonical_authority_digest

__all__ = [
    "AUTHORITY_MODE_CHANGED",
    "AUTHORITY_MODE_DRAINING",
    "AUTOMATIC_ADMISSION_HOLD_REQUIRES_GRAPH",
    "BACKFILL_AFTER_CUTOVER",
    "CLAIM_BINDING_CONFLICT",
    "CLAIM_LEASE_LIVE",
    "CLAIM_LEASE_STALE",
    "DELIVERY_PAYLOAD_OVERSIZE",
    "DOMAIN_LOCK_HELD",
    "EXECUTION_SUPERSEDED",
    "GRAPH_BINDING_FIELDS",
    "GRAPH_DISPATCH_NOT_ACTIVE",
    "GRAPH_UNIT_BATCH_ACCOUNTING",
    "GRAPH_UNIT_BATCH_UNSETTLED",
    "GRAPH_UNIT_CLAIM_REFUSED_LIVE",
    "GRAPH_UNIT_EMPTY_CLAIMS",
    "GRAPH_UNIT_EXECUTION_NOT_DEPLOYED",
    "GRAPH_UNIT_SET_COMPOSITION",
    "LEGACY_BAKE_AFTER_CUTOVER",
    "LEGACY_ENVELOPE_AFTER_CUTOVER",
    "LEGACY_REPLAY_ACKNOWLEDGED",
    "LIVE_RUN_STATES",
    "MAX_BATCH_MEMBERS",
    "NODE_PLAN_NOT_DEPLOYED",
    "NODE_STATE_CHANGED",
    "PLAN_DISPATCH_FENCED",
    "POST_GRANT_OVERSIZE",
    "PROMOTE_WAKE_CHANGED",
    "PROVIDER_CEILING_BYTES",
    "PROVIDER_PAYLOAD_OVERSIZE",
    "RUN_ARMING_OPEN",
    "RUN_GRANT_PENDING_APPLY",
    "RUN_INSTANTIATION_OPEN",
    "RUN_NOT_INSTANTIABLE",
    "RUN_STATE_CHANGED",
    "SEMANTIC_WRITE_ROUTE",
    "SENT_NODE_STATES",
    "SETTLE_CHUNK_MEMBERS",
    "SLOT_NOT_OWNED",
    "STREAM_TRIGGER_AFTER_CUTOVER",
    "TABLE_LOCK_PREFIX",
    "TERMINAL_NODE_STATES",
    "WRITE_UNIT",
    "WRITE_UNIT_BATCHING_PENDING_PROBE",
    "WRITE_UNIT_SETTLE_PATH",
    "Acceptance",
    "ArmScope",
    "ArmableNode",
    "AttemptFact",
    "ClaimOutcome",
    "ClaimView",
    "CompleteObligation",
    "EnvelopeKind",
    "EnvelopeOversizeError",
    "FenceReason",
    "GrantCandidate",
    "GraphGuardError",
    "NodePhase",
    "NodeState",
    "PromoterBranch",
    "PromoterView",
    "ReturnReason",
    "RosterSplit",
    "ServingAuthorityIdentity",
    "ServingCandidate",
    "ServingFrontierVector",
    "TakeoverOutcome",
    "arm_eligible",
    "batch_id_for",
    "bind_graph_envelope",
    "cohort_root_clock",
    "decide_claim",
    "decide_promotion",
    "decide_takeover",
    "election_transaction_member_count",
    "execution_set_id_for",
    "fence_terminal_state",
    "grant_order_key",
    "is_write_unit",
    "legacy_finalize_needs_mode_check",
    "node_route",
    "owner_envelope_digest",
    "provider_accounted_bytes",
    "require_serving_authority_identity",
    "run_may_terminate",
    "scope_pending",
    "serving_frontier_shard",
    "serving_gap",
    "sets_arm_alone",
    "sets_final_return",
    "split_roster",
    "table_writer_acceptance",
    "take_grant",
    "template_node",
    "unit_final_attempt",
    "unit_may_return",
    "validate_serving_candidates",
]

PLAN_DISPATCH_FENCED: Final = "PLAN_DISPATCH_FENCED"
"""BF-2: a permit not acquired before a generation advance is refused; nothing is sent."""
EXECUTION_SUPERSEDED: Final = "EXECUTION_SUPERSEDED"
"""V-9: a claim on a fenced or terminal run; acknowledged with zero effect."""
PROMOTE_WAKE_CHANGED: Final = "PROMOTE_WAKE_CHANGED"
"""V-20 (r5): a promoter delete whose `wake_seq` or slot condition failed; re-read."""
SLOT_NOT_OWNED: Final = "SLOT_NOT_OWNED"
"""V-25: a release that no longer owns `SLOT#<U>`; no receipt, re-read."""
RUN_NOT_INSTANTIABLE: Final = "RUN_NOT_INSTANTIABLE"
"""V-26 (r5): a node put or arming transaction whose `RUN#` state or generation moved."""
RUN_INSTANTIATION_OPEN: Final = "RUN_INSTANTIATION_OPEN"
"""V-26 (r5): a run-terminal transaction attempted while `INSTANTIATE#` exists."""
RUN_GRANT_PENDING_APPLY: Final = "RUN_GRANT_PENDING_APPLY"
"""V-29: pause or cancel against a run whose grant committed and has not applied."""
AUTOMATIC_ADMISSION_HOLD_REQUIRES_GRAPH: Final = "AUTOMATIC_ADMISSION_HOLD_REQUIRES_GRAPH"
"""V-14(b) (r5): the hold commands are accepted only when the mode reads GRAPH."""
GRAPH_DISPATCH_NOT_ACTIVE: Final = "GRAPH_DISPATCH_NOT_ACTIVE"
"""V-12: a graph envelope outside GRAPH; zero effect, unacknowledged, invariant metric."""
LEGACY_ENVELOPE_AFTER_CUTOVER: Final = "LEGACY_ENVELOPE_AFTER_CUTOVER"
"""V-12/V-23: a legacy envelope in GRAPH with no exact legacy terminal."""
STREAM_TRIGGER_AFTER_CUTOVER: Final = "STREAM_TRIGGER_AFTER_CUTOVER"
"""S6 §2.9 (D6): a publication-stream record reached the enqueue or precompute stream worker
in GRAPH; zero effect, acknowledged, counted."""
LEGACY_BAKE_AFTER_CUTOVER: Final = "LEGACY_BAKE_AFTER_CUTOVER"
"""S6 §2.9 (D6, AGG3-6): a legacy (grant-less) bake intent or pointer write in GRAPH; zero
effect, acknowledged, counted."""
BACKFILL_AFTER_CUTOVER: Final = "BACKFILL_AFTER_CUTOVER"
"""S6 §2.9 (D11-independent): the deploy-time backfill in GRAPH sends nothing; counted."""
LEGACY_REPLAY_ACKNOWLEDGED: Final = "LEGACY_REPLAY_ACKNOWLEDGED"
"""V-23: an exact duplicate of a finalized legacy unit in GRAPH; acknowledged, zero effect."""
AUTHORITY_MODE_CHANGED: Final = "AUTHORITY_MODE_CHANGED"
"""V-13(a): the GRAPH CAS committed between a batch's LEGACY_DRAINING read and its finalize."""
AUTHORITY_MODE_DRAINING: Final = "AUTHORITY_MODE_DRAINING"
"""V-13(b): the drain CAS committed between an ingress mode read and its intent submission."""
DELIVERY_PAYLOAD_OVERSIZE: Final = "delivery_payload_oversize"
"""FR-4: an indivisible roster item above the provider ceiling denies the whole graph."""
PROVIDER_PAYLOAD_OVERSIZE: Final = "provider_payload_oversize"
"""FR-4: the provider rejected a locally accepted envelope; an immutable node failure."""
DOMAIN_LOCK_HELD: Final = "DOMAIN_LOCK_HELD"
"""A physical-table domain lock another node holds; arming waits for its release."""
CLAIM_LEASE_LIVE: Final = "CLAIM_LEASE_LIVE"
"""An exact duplicate observing a live claim stays unacknowledged for retry."""
CLAIM_BINDING_CONFLICT: Final = "CLAIM_BINDING_CONFLICT"
"""An envelope whose dispatch binding disagrees with the node's: fatal, never claimed."""
NODE_STATE_CHANGED: Final = "NODE_STATE_CHANGED"
"""A node or outbox revision moved between the read and the transaction; re-read."""
RUN_STATE_CHANGED: Final = "RUN_STATE_CHANGED"
"""A `RUN#` revision or state moved between the read and the transaction; re-read."""
NODE_PLAN_NOT_DEPLOYED: Final = "NODE_PLAN_NOT_DEPLOYED"
"""The ingestion template's dispatch plan arrives with the S6 materialization port."""
GRAPH_UNIT_EXECUTION_NOT_DEPLOYED: Final = "GRAPH_UNIT_EXECUTION_NOT_DEPLOYED"
"""A claimed graph unit's table execution arrives with S6 (V-12's GRAPH rows)."""
RUN_ARMING_OPEN: Final = "RUN_ARMING_OPEN"
"""S6 §2.2: a fence or run-terminal transaction attempted while `ARM#<run_id>` exists."""
CLAIM_LEASE_STALE: Final = "CLAIM_LEASE_STALE"
"""S6 §2.3: a lease release whose lease version moved (a reclaimer holds it): refused, ignored."""
POST_GRANT_OVERSIZE: Final = "post_grant_oversize"
"""S6 §2.7 (D23): a POST_GRANT envelope the provider rejected as oversized, a defect-only path.

The node is never failed (a granted run has one exit): it stays `PERMITTED` with its outbox row
`SENDING`, and `GraphPostGrantInvariant` is emitted on every dispatcher pass."""

PROVIDER_CEILING_BYTES: Final = 262_144
"""The provider-accounted SQS ceiling every envelope must fit (FR-4)."""

# --- 1b: per-table micro-batching of `write_unit` execution (S6 part 2, slice 7) ---------------

WRITE_UNIT_SETTLE_PATH: Final = "WRITE_UNIT_SETTLE_PATH"
"""D34 (F9): `settle_node` was asked to settle a `write_unit` `SUCCEEDED`; `settle_unit_set` is
the one path, so the store refuses before any write."""
GRAPH_UNIT_BATCH_UNSETTLED: Final = "GRAPH_UNIT_BATCH_UNSETTLED"
"""§2.5 step 6: a success-set chunk's guard was exhausted; its members are unresolved."""
GRAPH_UNIT_SET_COMPOSITION: Final = "GRAPH_UNIT_SET_COMPOSITION"
"""§2.5 step 3 (M3, N1): composing a set's union raised, or formation yielded no set; a defect."""
GRAPH_UNIT_CLAIM_REFUSED_LIVE: Final = "GRAPH_UNIT_CLAIM_REFUSED_LIVE"
"""§2.5 step 8 (AR2, M1): Begin refused a set's claims while every re-read claim was live."""
GRAPH_UNIT_EMPTY_CLAIMS: Final = "GRAPH_UNIT_EMPTY_CLAIMS"
"""§2.5 (F5): a `GRAPH_UNIT` attempt's claim thunk returned no fence; refused before any write."""
GRAPH_UNIT_BATCH_ACCOUNTING: Final = "GRAPH_UNIT_BATCH_ACCOUNTING"
"""§2.5 step 8: a listed member is in no class, or in two; the batch is reported unacknowledged."""
WRITE_UNIT_BATCHING_PENDING_PROBE: Final = "WRITE_UNIT_BATCHING_PENDING_PROBE"
"""D40: the time rule met a null `statement_allowance_ms`; the deploy gate should have refused."""

MAX_BATCH_MEMBERS: Final = (int(BOUNDS["max_transaction_items"]) - 2) // 4
"""D32: each member costs at most 4 arming actions (node, token, its run's `RUN#` and `ARM#`
delete) beside the lock and the outbox row, so `2 + 4n <= 100` gives 24. Derived, never set."""
SETTLE_CHUNK_MEMBERS: Final = (int(BOUNDS["max_transaction_items"]) - 1) // 3
"""§2.5 step 6: each settled member costs 3 actions (node, token, claim) beside one lock `ADD`."""
WRITE_UNIT: Final = "write_unit"
"""The dynamic template node; an instance is named `write_unit:<unit_key>` (§2.1)."""
TABLE_LOCK_PREFIX: Final = "TABLE#"
"""A `write_unit` node's counted lock, `TABLE#<unit_key>` (§2.1, D33)."""
SEMANTIC_WRITE_ROUTE: Final = "semantic_write"


def is_write_unit(name: str) -> bool:
    """Return whether a node name is a `write_unit:<k>` instance of the dynamic group."""
    head, separator, key = name.partition(":")
    return head == WRITE_UNIT and bool(separator) and bool(key)


def batch_id_for(lock_key: str, armed_at: str, node_ids: Sequence[str]) -> str:
    """Return a grant's batch identity; `armed_at` makes every grant distinct (§2.2)."""
    return canonical_authority_digest(["graph-unit-batch/v1", lock_key, armed_at, *node_ids])


def execution_set_id_for(batch_id: str, node_ids: Sequence[str]) -> str:
    """Return an execution set's identity, stable across receives for the same members (§2.5)."""
    return canonical_authority_digest(["graph-unit-set/v1", batch_id, *node_ids])


def unit_final_attempt(receive_count: int, admission: int, *, final_return: bool) -> bool:
    """D35 (F5, AR3): a member is final on `final_return`, or once either count reaches the cap.

    `admission` persists on the claim across returns and second copies, so the cap is per member,
    not per message; `admission <= receive_count` within one message (difference 5).
    """
    cap = int(ROUTES[SEMANTIC_WRITE_ROUTE]["max_receive_count"])
    return final_return or max(receive_count, admission) >= cap


def unit_may_return(*, final_return: bool) -> bool:
    """Return whether a member may be returned: not once it carries `final_return` (F5)."""
    return not final_return


class ReturnReason(StrEnum):
    """Why a member went back to PENDING without holding its table (§2.5 "Return", closed)."""

    MEMBER_FAILED = "member_failed"
    """TableWriter: the member's own execution failed with attempts left."""
    FINAL_UNISOLATED = "final_unisolated"
    """TableWriter: a budget outcome on the final attempt; the next attempt runs alone."""
    RUN_PAUSED = "run_paused"
    """Dispatcher: the member's run paused before the permit."""
    GENERATION_MOVED = "generation_moved"
    """Dispatcher: the member's run moved to a new generation before the permit."""


_ARM_ALONE_REASONS: Final = frozenset({ReturnReason.MEMBER_FAILED, ReturnReason.FINAL_UNISOLATED})


def sets_arm_alone(reason: ReturnReason) -> bool:
    """Return whether the reason sets `arm_alone` (D41); a dispatcher return never does."""
    return reason in _ARM_ALONE_REASONS


def sets_final_return(reason: ReturnReason) -> bool:
    """Return whether the reason sets `final_return`; only `final_unisolated` does (step 8)."""
    return reason is ReturnReason.FINAL_UNISOLATED


class GrantCandidate(Protocol):
    """The fields the arming order reads for one PENDING `write_unit` node (D41)."""

    @property
    def root_clock_started_at(self) -> str | None:
        """The run's root clock; older runs are granted first."""
        ...

    @property
    def run_id(self) -> str:
        """The run; the tie-breaker of the order."""
        ...

    @property
    def arm_alone(self) -> bool:
        """Set by a member-attributable or final-attempt return: the node runs alone."""
        ...


def grant_order_key(candidate: GrantCandidate) -> tuple[str, str]:
    """Order a table group oldest first by (`root_clock_started_at`, `run_id`) (D41)."""
    return (candidate.root_clock_started_at or "", candidate.run_id)


def take_grant[C: GrantCandidate](
    group: Sequence[C], bound: int, *, fits: Callable[[tuple[C, ...]], bool] | None = None
) -> tuple[C, ...]:
    """Return the members one `TABLE#<k>` grant takes, in grant order (D41).

    The head always gets the grant, so nothing starves: an `arm_alone` head is granted alone,
    otherwise the longest prefix without `arm_alone`, stopping at the first one, the bound, or
    (slice 7, F3) the first member whose prefix `fits` refuses, the arming transaction's size
    bound. The head alone always fits, as one node item is at most 400 KB.
    """
    if bound < 1:
        raise ValueError("a table grant takes at least one member.")
    ordered = sorted(group, key=grant_order_key)
    if not ordered:
        return ()
    if ordered[0].arm_alone:
        return (ordered[0],)
    taken: list[C] = []
    for candidate in ordered:
        if candidate.arm_alone or len(taken) == bound:
            break
        if taken and fits is not None and not fits((*taken, candidate)):
            break
        taken.append(candidate)
    return tuple(taken)


class GraphGuardError(Exception):
    """A domain guard: the transition would break an invariant. No receipt, no mutation.

    The caller re-reads and decides again, so a guard is never a final outcome.
    """

    def __init__(self, signal: str) -> None:
        super().__init__(signal)
        self.signal = signal


class EnvelopeOversizeError(ValueError):
    """An indivisible roster item exceeds the ceiling: the whole graph is refused (FR-4)."""

    def __init__(self, measured: int) -> None:
        super().__init__(
            f"{DELIVERY_PAYLOAD_OVERSIZE}: {measured} bytes > {PROVIDER_CEILING_BYTES}"
        )
        self.signal = DELIVERY_PAYLOAD_OVERSIZE
        self.measured = measured


class NodeState(StrEnum):
    """A graph node's lifecycle.

    `WAITING_INPUT` is the DB25 precompute-only hold: it owns no token, lock or outbox until
    the exact bake-effect roster is ready. Ordinary `PENDING` nodes are put by instantiation
    and hold nothing. Arming makes a node `READY` with its runtime token, physical-table domain
    lock and outbox row. A permit makes it `PERMITTED`; the dispatcher's send receipt makes it
    `DISPATCHED`. The graph execution claim makes it `CLAIMED`. A takeover with an open table
    attempt parks it in `AWAITING_PROVIDER_TERMINAL` under a `VERIFY#` obligation. The rest are
    terminal.
    """

    WAITING_INPUT = "WAITING_INPUT"
    PENDING = "PENDING"
    READY = "READY"
    PERMITTED = "PERMITTED"
    DISPATCHED = "DISPATCHED"
    CLAIMED = "CLAIMED"
    AWAITING_PROVIDER_TERMINAL = "AWAITING_PROVIDER_TERMINAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    FENCED_UNSENT = "FENCED_UNSENT"
    SUPERSEDED_UNCLAIMED = "SUPERSEDED_UNCLAIMED"
    COALESCED = "COALESCED"
    """Serving loser or exact-input reuse terminalized against a proven direct binding."""


TERMINAL_NODE_STATES: Final = frozenset(
    {
        NodeState.SUCCEEDED,
        NodeState.FAILED,
        NodeState.FENCED_UNSENT,
        NodeState.SUPERSEDED_UNCLAIMED,
        NodeState.COALESCED,
    }
)
SENT_NODE_STATES: Final = frozenset({NodeState.PERMITTED, NodeState.DISPATCHED})
"""A permitted node may already be on the queue; the send receipt can trail the claim."""


class NodePhase(StrEnum):
    """Whether a node runs before the release grant or only after it applies."""

    PRE_GRANT = "PRE_GRANT"
    POST_GRANT = "POST_GRANT"


class FenceReason(StrEnum):
    """Why a run moved to FENCING; each maps to one later terminal state."""

    CANCELLED = "cancelled"
    OVERSIZE = "oversize"
    NODE_FAILED = "node_failed"
    OLDER_THAN_CURRENT = "older_than_current"
    REPLACED = "replaced"
    INVALIDATED = "input_invalidated"


_FENCE_TERMINAL: Final = {
    FenceReason.CANCELLED: RunState.CANCELLED,
    FenceReason.OVERSIZE: RunState.FAILED,
    FenceReason.NODE_FAILED: RunState.FAILED,
    FenceReason.OLDER_THAN_CURRENT: RunState.SUPERSEDED,
    FenceReason.REPLACED: RunState.SUPERSEDED,
    FenceReason.INVALIDATED: RunState.SUPERSEDED,
}
RELEASING_FENCE_REASONS: Final = frozenset(
    {
        FenceReason.CANCELLED,
        FenceReason.OVERSIZE,
        FenceReason.NODE_FAILED,
        FenceReason.OLDER_THAN_CURRENT,
    }
)
"""Release point 1 (r4): every FENCING transition except a replacing admission frees the slot."""
LIVE_RUN_STATES: Final = frozenset({RunState.OPEN, RunState.PAUSED})


def fence_terminal_state(reason: FenceReason) -> RunState:
    """Return the terminal a fenced run settles to once every node is terminal."""
    return _FENCE_TERMINAL[reason]


# --- Arming successors and the serving order (S6 §2.1, §2.2; D12, D20, AGG4-1, AGG4-14) ---------


class ArmScope(StrEnum):
    """What an `ARM#<run_id>` obligation arms.

    `units` is written by the `materialize_raw` settlement (the PRE_GRANT unit group);
    `serving` by the `publish` settlement (`precompute`, then `bake`).
    """

    UNITS = "units"
    SERVING = "serving"


_SCOPE_PHASE: Final = {ArmScope.UNITS: NodePhase.PRE_GRANT, ArmScope.SERVING: NodePhase.POST_GRANT}
_SERVING_AFTER_TERMINAL: Final = frozenset({"precompute", "bake", "bake_task"})
"""Template nodes whose `FAILED` instance still readies its successor in the serving scope.

A cohort member (`precompute`, `bake`) settles `FAILED` after contributing, so its successor
arms. A `FAILED` roster task (`bake_task:<key>`) arms `bake_finalize` once every task is
terminal; the finalizer then settles the cohort withheld (ADR-140, B4)."""


class ArmableNode(Protocol):
    """The node fields the arm decision reads (a `NodeRecord` satisfies it)."""

    @property
    def name(self) -> str:
        """The node name; a dynamic node is `<template node>:<key>`."""
        ...

    @property
    def phase(self) -> NodePhase:
        """PRE_GRANT or POST_GRANT."""
        ...

    @property
    def state(self) -> NodeState:
        """The node's lifecycle state."""
        ...

    @property
    def after(self) -> tuple[str, ...]:
        """Template predecessor names; a dynamic name matches every node of its group."""
        ...


def template_node(name: str) -> str:
    """Return the template node a node instance belongs to (`write_unit:<k>` -> `write_unit`)."""
    return name.split(":", 1)[0]


def node_route(template: str, name: str) -> str | None:
    """Return the generated contract route of a node, or None (`grant_apply`, never dispatched)."""
    wanted = template_node(name)
    for item in TEMPLATES[template]["nodes"]:
        if str(item["node"]) == wanted:
            route = item.get("route")
            return None if route is None else str(route)
    return None


def _predecessor_ready(scope: ArmScope, node: ArmableNode) -> bool:
    if node.state is NodeState.SUCCEEDED:
        return True
    return (
        scope is ArmScope.SERVING
        and template_node(node.name) in _SERVING_AFTER_TERMINAL
        and node.state is NodeState.FAILED
    )


def scope_pending[N: ArmableNode](
    scope: ArmScope, template: str, nodes: Sequence[N]
) -> tuple[N, ...]:
    """Return the scope's PENDING routed nodes; `ARM#` retires when the last of them arms."""
    phase = _SCOPE_PHASE[scope]
    return tuple(
        node
        for node in nodes
        if node.phase is phase
        and node.state is NodeState.PENDING
        and node_route(template, node.name) is not None
    )


def arm_eligible[N: ArmableNode](
    scope: ArmScope, template: str, nodes: Sequence[N]
) -> tuple[N, ...]:
    """Return the scope's PENDING routed nodes whose every predecessor is ready (§2.2).

    For `units` a predecessor is ready once it `SUCCEEDED`. For `serving`, `precompute` is ready
    once `publish` `SUCCEEDED`, `bake` once `precompute` is terminal, and `bake_finalize` once
    every `bake_task` is terminal. A template predecessor with no node instance is never ready.
    """

    def ready(predecessor: str) -> bool:
        matched = [node for node in nodes if template_node(node.name) == predecessor]
        return bool(matched) and all(_predecessor_ready(scope, node) for node in matched)

    return tuple(
        node
        for node in scope_pending(scope, template, nodes)
        if all(ready(predecessor) for predecessor in node.after)
    )


@dataclass(frozen=True, slots=True)
class CompleteObligation:
    """`COMPLETE#<run_id>` as the serving-order walk read it: the run's place in its domain."""

    run_id: str
    domain: str
    grant_id: str
    grant_ordinal: int


_SERVING_ROSTER_MAX: Final = int(SERVING_COALESCING["serving_candidate_roster_max"])
_SERVING_FRONTIER_SHARDS: Final = int(SERVING_COALESCING["serving_frontier_shards"])
_ELECTED_TRANSACTION_MEMBERS: Final = _SERVING_FRONTIER_SHARDS + 8 + 8 + 3
_NO_ELIGIBLE_TRANSACTION_MEMBERS: Final = _SERVING_FRONTIER_SHARDS + 8 + 2


@dataclass(frozen=True, slots=True)
class ServingAuthorityIdentity:
    """The immutable identity shared by every Slice 2 serving authority transition."""

    run_id: str
    grant_id: str
    grant_ordinal: int
    domain: str

    def __post_init__(self) -> None:
        if any(not value for value in (self.run_id, self.grant_id, self.domain)):
            raise ValueError("a serving authority identity field cannot be blank.")
        if self.grant_ordinal < 0:
            raise ValueError("a serving authority grant ordinal cannot be negative.")


def require_serving_authority_identity(
    expected: ServingAuthorityIdentity, actual: ServingAuthorityIdentity
) -> None:
    """Refuse a success or replay unless every immutable authority field is exact."""
    if actual != expected:
        raise ValueError("serving_authority_identity_mismatch")


@dataclass(frozen=True, slots=True)
class ServingCandidate:
    """One fully settled publication eligible for a domain serving election (design §2.2)."""

    run_id: str
    grant_id: str
    root_flow_id: str
    domain: str
    grant_ordinal: int
    publication_effect_digest: str
    run_state: RunState = RunState.GRANTED
    arm_matches: bool = True
    complete_matches: bool = True
    publish_state: NodeState = NodeState.SUCCEEDED
    precompute_state: NodeState = NodeState.WAITING_INPUT
    bake_state: NodeState = NodeState.PENDING
    runtime_state_present: bool = False
    binding_present: bool = False
    root_clock_started_at: str | None = None
    """The run's root clock, so a cohort obligation can be due from its earliest root (S5)."""


def cohort_root_clock(candidates: Sequence[ServingCandidate]) -> str | None:
    """Return a cohort's earliest candidate root clock, or None when any candidate lacks one.

    `COALESCE#` is a flow obligation, so the stuck-flow sweep needs its root clock; an unknown
    clock might be the earliest, so one missing clock leaves the obligation unclocked (and
    counted apart by its own alarm) rather than given a later, guessed due time.
    """
    clocks = [candidate.root_clock_started_at for candidate in candidates]
    if not clocks or any(clock is None for clock in clocks):
        return None
    return min(cast("list[str]", clocks), key=datetime.fromisoformat)


@dataclass(frozen=True, slots=True)
class ServingFrontierVector:
    """The complete immutable view of all serving-frontier shard revisions and counters."""

    revisions: tuple[int, ...]
    visible_counts: tuple[int, ...]
    eligible_counts: tuple[int, ...]

    def __post_init__(self) -> None:
        if any(
            len(values) != _SERVING_FRONTIER_SHARDS
            for values in (self.revisions, self.visible_counts, self.eligible_counts)
        ):
            raise ValueError("a serving frontier vector must contain every protocol shard.")
        if any(
            value < 0
            for values in (self.revisions, self.visible_counts, self.eligible_counts)
            for value in values
        ):
            raise ValueError("serving frontier revisions and counts cannot be negative.")
        if any(
            eligible > visible
            for visible, eligible in zip(self.visible_counts, self.eligible_counts, strict=True)
        ):
            raise ValueError("each serving frontier shard must satisfy eligible <= visible.")

    @property
    def visible_count(self) -> int:
        """Return the exact visible total across all protocol shards."""
        return sum(self.visible_counts)

    @property
    def eligible_count(self) -> int:
        """Return the exact eligible total across all protocol shards."""
        return sum(self.eligible_counts)

    @property
    def digest(self) -> str:
        """Return the canonical digest fencing every shard's revision and counts."""
        return canonical_authority_digest(
            [
                "graph-serving-frontier/v1",
                self.revisions,
                self.visible_counts,
                self.eligible_counts,
            ]
        )


def validate_serving_candidates(
    candidates: Sequence[ServingCandidate],
) -> tuple[ServingCandidate, ...]:
    """Validate and canonically order one complete candidate roster; never truncate it."""
    if len(candidates) > _SERVING_ROSTER_MAX:
        raise ValueError("the serving candidate roster exceeds its dedicated protocol bound.")
    if not candidates:
        return ()
    domains = {candidate.domain for candidate in candidates}
    if len(domains) != 1:
        raise ValueError("one serving election contains exactly one domain.")
    ordinals: set[int] = set()
    for candidate in candidates:
        identities = (
            candidate.run_id,
            candidate.grant_id,
            candidate.root_flow_id,
            candidate.domain,
            candidate.publication_effect_digest,
        )
        if any(not identity for identity in identities):
            raise ValueError("a serving candidate identity cannot be blank.")
        if candidate.grant_ordinal < 0 or candidate.grant_ordinal in ordinals:
            raise ValueError("serving grant ordinals must be non-negative and unique.")
        ordinals.add(candidate.grant_ordinal)
        if candidate.run_state is not RunState.GRANTED:
            raise ValueError("a serving candidate run must be GRANTED.")
        if not (candidate.arm_matches and candidate.complete_matches):
            raise ValueError("a serving candidate must match its ARM and COMPLETE authority.")
        if candidate.publish_state is not NodeState.SUCCEEDED:
            raise ValueError("a serving candidate publication must be SUCCEEDED.")
        if (
            candidate.precompute_state is not NodeState.WAITING_INPUT
            or candidate.bake_state is not NodeState.PENDING
        ):
            raise ValueError("a serving candidate's worker nodes must both be PENDING.")
        if candidate.runtime_state_present or candidate.binding_present:
            raise ValueError(
                "a serving candidate cannot already hold runtime or binding authority."
            )
    return tuple(
        sorted(candidates, key=lambda item: (item.grant_ordinal, item.grant_id, item.run_id))
    )


def serving_gap(
    walk: Sequence[CompleteObligation], candidates: Sequence[ServingCandidate]
) -> CompleteObligation | None:
    """Return the first lower open COMPLETE obligation absent from the eligible roster."""
    ordered = validate_serving_candidates(candidates)
    if not ordered:
        return None
    winner = ordered[-1]
    relevant = tuple(obligation for obligation in walk if obligation.domain == winner.domain)
    ordinals = [obligation.grant_ordinal for obligation in relevant]
    if len(ordinals) != len(set(ordinals)):
        raise ValueError("open COMPLETE obligations cannot collide on a grant ordinal.")
    eligible_obligations = {
        (candidate.run_id, candidate.domain, candidate.grant_id, candidate.grant_ordinal)
        for candidate in ordered
    }
    gaps = [
        obligation
        for obligation in relevant
        if obligation.grant_ordinal < winner.grant_ordinal
        and (
            obligation.run_id,
            obligation.domain,
            obligation.grant_id,
            obligation.grant_ordinal,
        )
        not in eligible_obligations
    ]
    return min(
        gaps, key=lambda item: (item.grant_ordinal, item.grant_id, item.run_id), default=None
    )


def newest_eligible_candidate(
    candidates: Sequence[ServingCandidate],
) -> ServingCandidate | None:
    """Choose the deterministic newest candidate after exact roster validation."""
    ordered = validate_serving_candidates(candidates)
    return ordered[-1] if ordered else None


def election_transaction_member_count(*, successor: bool = True) -> int:
    """Return the fixed elected or no-eligible final-drain transaction size."""
    return _ELECTED_TRANSACTION_MEMBERS if successor else _NO_ELIGIBLE_TRANSACTION_MEMBERS


def serving_frontier_shard(run_id: str) -> int:
    """Select one protocol frontier shard from the leading SHA-256 bits of a run ID."""
    if not run_id:
        raise ValueError("a serving frontier shard requires a run id.")
    if _SERVING_FRONTIER_SHARDS & (_SERVING_FRONTIER_SHARDS - 1):
        raise ValueError("the serving frontier shard count must be a power of two.")
    bits = _SERVING_FRONTIER_SHARDS.bit_length() - 1
    digest = hashlib.sha256(run_id.encode("utf-8")).digest()
    return digest[0] >> (8 - bits)


def run_may_terminate(nodes: Sequence[NodeState], *, instantiation_open: bool) -> bool:
    """Return whether every node is terminal and `INSTANTIATE#` is gone (r5)."""
    return not instantiation_open and all(state in TERMINAL_NODE_STATES for state in nodes)


# --- The claim predicate and the takeover (V-9, V-10) -------------------------------------------


class AttemptFact(StrEnum):
    """The node's table attempt, read strongly under the existing lease and provider fence."""

    NONE = "none"
    OPEN = "open"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ClaimView:
    """Everything the claim predicate reads, strongly."""

    run_state: RunState
    phase: NodePhase
    node_state: NodeState
    binding_matches: bool
    """dispatch_id, envelope digest and permit generation all equal the node's."""
    claim_present: bool
    lease_expired: bool
    batch_matches: bool = True
    """1b (N1): a batch member's node carries the envelope's `batch_id`; other kinds pass True."""


class ClaimOutcome(StrEnum):
    """What the graph execution claim does with one received envelope."""

    CLAIM = "claim"
    """Commit the claim; the first claim of a run sets `compute_started_at` (V-10)."""
    RECLAIM = "reclaim"
    """An expired lease on a live run: bump the lease version and resume from effect authority."""
    LIVE_DUPLICATE = "live_duplicate"
    """A live claim exists: stay unacknowledged for retry."""
    SUPERSEDE_UNCLAIMED = "supersede_unclaimed"
    """A fenced or terminal run with no claim record: settle and acknowledge (V-9)."""
    TAKEOVER = "takeover"
    """A fenced or terminal run whose claim lease expired: settle from attempt facts (r4)."""
    ALREADY_SETTLED = "already_settled"
    """A duplicate after the node settled: acknowledge with zero effect."""
    BINDING_CONFLICT = "binding_conflict"
    """The envelope disagrees with the node's dispatch binding: fatal."""


def _claimable_run(view: ClaimView) -> bool:
    if view.phase is NodePhase.POST_GRANT:
        return view.run_state is RunState.GRANTED
    return view.run_state in LIVE_RUN_STATES


def decide_claim(view: ClaimView) -> ClaimOutcome:  # noqa: PLR0911 -- one return per outcome
    """Decide one received graph envelope (design.md, "Claim predicate", r3-r5)."""
    if view.node_state in TERMINAL_NODE_STATES:
        return ClaimOutcome.ALREADY_SETTLED
    if not (view.binding_matches and view.batch_matches):
        return ClaimOutcome.BINDING_CONFLICT
    if view.node_state is NodeState.AWAITING_PROVIDER_TERMINAL:
        return ClaimOutcome.ALREADY_SETTLED
    live = _claimable_run(view)
    if view.claim_present:
        if not view.lease_expired:
            return ClaimOutcome.LIVE_DUPLICATE
        return ClaimOutcome.RECLAIM if live else ClaimOutcome.TAKEOVER
    if view.node_state not in SENT_NODE_STATES:
        return ClaimOutcome.BINDING_CONFLICT
    return ClaimOutcome.CLAIM if live else ClaimOutcome.SUPERSEDE_UNCLAIMED


class TakeoverOutcome(StrEnum):
    """How a takeover settles from the node's table-attempt and provider-terminal facts."""

    SUPERSEDE_UNCLAIMED = "supersede_unclaimed"
    """No attempt began: `SUPERSEDED_UNCLAIMED` with `claim_record_present=true` (r5, R4-7)."""
    AWAIT_PROVIDER_TERMINAL = "await_provider_terminal"
    """An attempt is open: `AWAITING_PROVIDER_TERMINAL` plus `VERIFY#`, then acknowledge."""
    SETTLE_SUCCEEDED = "settle_succeeded"
    SETTLE_FAILED = "settle_failed"


def decide_takeover(fact: AttemptFact) -> TakeoverOutcome:
    """Map the node's attempt fact to its takeover settlement (V-9, r5)."""
    return {
        AttemptFact.NONE: TakeoverOutcome.SUPERSEDE_UNCLAIMED,
        AttemptFact.OPEN: TakeoverOutcome.AWAIT_PROVIDER_TERMINAL,
        AttemptFact.SUCCEEDED: TakeoverOutcome.SETTLE_SUCCEEDED,
        AttemptFact.FAILED: TakeoverOutcome.SETTLE_FAILED,
    }[fact]


# --- The promoter (V-20, r5) --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PromoterView:
    """One conflict unit as the promoter reads it, strongly."""

    wake_seq: int | None
    """`PROMOTE#<U>`'s wake sequence, or None when no item exists (the backstop pass)."""
    slot_revision: int | None
    """`SLOT#<U>`'s revision while it names a run, or None when the slot is free."""
    waiter: Waiter | None
    hold: bool
    head: datetime | None
    blocked: bool = False
    """S4: the co-release domain is BLOCKED (BF-4); no waiter may become `A` (row 6)."""


class PromoterBranch(StrEnum):
    """The promoter's conditioned transaction for one unit."""

    OCCUPIED = "occupied"
    """Delete `PROMOTE#` on `wake_seq`, ConditionCheck the slot revision; promote nothing."""
    FREE_NO_WAITER = "free_no_waiter"
    """Delete `PROMOTE#` on `wake_seq`; ConditionCheck slot and waiter both absent."""
    SKIP_HELD = "skip_held"
    """A held waiter while the hold is set: write nothing."""
    SKIP_BLOCKED = "skip_blocked"
    """S4: a waiter while the co-release domain is BLOCKED: write nothing; it stays counted."""
    SUPERSEDE_WAITER = "supersede_waiter"
    """`W.r < H`: `SUPERSEDED(older_than_current)`; delete the waiter and the wakeup."""
    PROMOTE_WAITER = "promote_waiter"
    """`W` becomes `A`: run, slot, `INSTANTIATE#`; delete the waiter and the wakeup."""
    NOTHING = "nothing"
    """No wakeup and nothing stranded."""


def decide_promotion(view: PromoterView) -> PromoterBranch:
    """Choose the promoter branch; a missing wakeup with a stranded waiter is the backstop."""
    waiter = view.waiter
    if view.slot_revision is not None:
        return PromoterBranch.NOTHING if view.wake_seq is None else PromoterBranch.OCCUPIED
    if waiter is None:
        return PromoterBranch.NOTHING if view.wake_seq is None else PromoterBranch.FREE_NO_WAITER
    if view.blocked:
        return PromoterBranch.SKIP_BLOCKED
    if waiter.held and view.hold:
        return PromoterBranch.SKIP_HELD
    if view.head is not None and waiter.r < view.head:
        return PromoterBranch.SUPERSEDE_WAITER
    return PromoterBranch.PROMOTE_WAITER


# --- TableWriter acceptance by mode and envelope (V-12, V-13) -----------------------------------


class EnvelopeKind(StrEnum):
    """A legacy envelope carries the six owner fields; a graph envelope adds its dispatch."""

    LEGACY = "legacy"
    GRAPH = "graph"


class Acceptance(StrEnum):
    """TableWriter's decision for one envelope under one strongly read mode."""

    LEGACY_PROTOCOL = "legacy_protocol"
    LEGACY_PROTOCOL_MODE_CHECKED = "legacy_protocol_mode_checked"
    LEGACY_REPLAY_ACKNOWLEDGED = LEGACY_REPLAY_ACKNOWLEDGED
    LEGACY_ENVELOPE_AFTER_CUTOVER = LEGACY_ENVELOPE_AFTER_CUTOVER
    GRAPH_DISPATCH_NOT_ACTIVE = GRAPH_DISPATCH_NOT_ACTIVE
    GRAPH_CLAIM = "graph_claim"


def table_writer_acceptance(
    kind: EnvelopeKind, mode: AuthorityMode, *, legacy_terminal: bool
) -> Acceptance:
    """Apply the mode-by-envelope table (design.md, "TableWriter acceptance", r3)."""
    if kind is EnvelopeKind.GRAPH:
        if mode is AuthorityMode.GRAPH:
            return Acceptance.GRAPH_CLAIM
        return Acceptance.GRAPH_DISPATCH_NOT_ACTIVE
    if mode is AuthorityMode.GRAPH:
        if legacy_terminal:
            return Acceptance.LEGACY_REPLAY_ACKNOWLEDGED
        return Acceptance.LEGACY_ENVELOPE_AFTER_CUTOVER
    if mode is AuthorityMode.LEGACY_DRAINING:
        return Acceptance.LEGACY_PROTOCOL_MODE_CHECKED
    return Acceptance.LEGACY_PROTOCOL


def legacy_finalize_needs_mode_check(mode: AuthorityMode) -> bool:
    """Legacy finalize ConditionChecks mode != GRAPH only after a LEGACY_DRAINING read (V-13)."""
    return mode is AuthorityMode.LEGACY_DRAINING


# --- The graph envelope and its dispatch binding (V-9, V-12) ------------------------------------

GRAPH_BINDING_FIELDS: Final = frozenset({"dispatch_id", "run_id", "node_id"})
"""What a graph envelope carries beside its owner body (TableWriter's six §4 fields)."""


def _owner_fields(body: str) -> dict[str, object]:
    parsed: object = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("a graph envelope body must be a JSON object.")
    return {str(name): value for name, value in cast("dict[object, object]", parsed).items()}


def _canonical(fields: Mapping[str, object]) -> str:
    return json.dumps(dict(fields), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def owner_envelope_digest(body: str) -> str:
    """Return the SHA-256 of the canonical owner body, without the dispatch binding.

    The dispatch identity is derived from this digest, and the envelope then carries that
    identity, so the digest cannot cover the binding fields: a body cannot contain its own hash.
    Instantiation and the claim both compute it, from the owner body and the received body.
    """
    owner = {
        name: value
        for name, value in _owner_fields(body).items()
        if name not in GRAPH_BINDING_FIELDS
    }
    return hashlib.sha256(_canonical(owner).encode("utf-8")).hexdigest()


def bind_graph_envelope(owner_body: str, binding: Mapping[str, str]) -> str:
    """Return the exact envelope bytes: the canonical owner body plus its dispatch binding."""
    if frozenset(binding) != GRAPH_BINDING_FIELDS:
        raise ValueError("a graph envelope binding is exactly dispatch_id, run_id and node_id.")
    owner = _owner_fields(owner_body)
    if GRAPH_BINDING_FIELDS & frozenset(owner):
        raise ValueError("an owner body may not carry the dispatch binding fields.")
    return _canonical({**owner, **binding})


# --- Envelope sizing (FR-4) ---------------------------------------------------------------------

_ATTRIBUTE_TYPE: Final = "String"


def provider_accounted_bytes(body: str, attributes: Mapping[str, str]) -> int:
    """Return SQS's accounted size: the UTF-8 body plus each attribute's name, type and value."""
    measured = len(body.encode("utf-8"))
    for name, value in attributes.items():
        measured += len(name.encode("utf-8"))
        measured += len(_ATTRIBUTE_TYPE.encode("utf-8"))
        measured += len(value.encode("utf-8"))
    return measured


@dataclass(frozen=True, slots=True)
class RosterSplit:
    """One envelope's roster slice and its measured bytes."""

    items: tuple[str, ...]
    body: str
    measured: int


def split_roster(
    roster: Sequence[str],
    render: Callable[[tuple[str, ...]], str],
    attributes: Mapping[str, str],
) -> tuple[RosterSplit, ...]:
    """Pack consecutive roster items into the fewest envelopes at or below the ceiling.

    Deterministic: the roster order decides every boundary. An item that does not fit alone
    raises `EnvelopeOversizeError` (`delivery_payload_oversize`), so no envelope is admitted.
    """
    splits: list[RosterSplit] = []
    current: list[str] = []
    for item in roster:
        candidate = (*current, item)
        body = render(candidate)
        measured = provider_accounted_bytes(body, attributes)
        if measured <= PROVIDER_CEILING_BYTES:
            current.append(item)
            continue
        if not current:
            raise EnvelopeOversizeError(measured)
        closed = tuple(current)
        closed_body = render(closed)
        splits.append(
            RosterSplit(closed, closed_body, provider_accounted_bytes(closed_body, attributes))
        )
        alone = render((item,))
        alone_bytes = provider_accounted_bytes(alone, attributes)
        if alone_bytes > PROVIDER_CEILING_BYTES:
            raise EnvelopeOversizeError(alone_bytes)
        current = [item]
    if current:
        closed = tuple(current)
        closed_body = render(closed)
        splits.append(
            RosterSplit(closed, closed_body, provider_accounted_bytes(closed_body, attributes))
        )
    return tuple(splits)
