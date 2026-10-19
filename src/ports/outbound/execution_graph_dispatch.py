# SPDX-License-Identifier: MIT-0
"""Outbound port for graph dispatch, claim, settlement and promotion (slice S3).

Every read is strongly consistent. Every write is one conditioned transaction. A transaction
whose read-time conditions moved raises `GraphGuardError` carrying the typed signal of the first
member that failed (S1A-10); the caller re-reads and decides again. A `TransactionConflict`
raises `GraphTransactionConflictError`, which the claim path retries within the generated bound
and counts (R2-7). Any other failure raises `GraphStoreUnavailableError`, and nothing committed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from domain.operations.execution_graph import ModeState
from domain.operations.execution_graph_dispatch import (
    ArmScope,
    FenceReason,
    GraphGuardError,
    NodePhase,
    NodeState,
    ReturnReason,
    ServingAuthorityIdentity,
)
from domain.operations.run_request import RunState, Waiter
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.execution_graph import CommandReceipt, ReceiptRecord

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "ArmNodeRequest",
    "ArmRequest",
    "ArmUnitBatchRequest",
    "BatchMemberRef",
    "BatchPermitRequest",
    "BatchSendReceipt",
    "ClaimRecord",
    "ClaimRequest",
    "DueItem",
    "FenceRunRequest",
    "GraphDispatchStore",
    "GraphMessage",
    "GraphStoreUnavailableError",
    "GraphTransactionConflictError",
    "HoldRequest",
    "MaterializationSettlement",
    "NodeRecord",
    "NodeSettlement",
    "NodeSpec",
    "OutboxRecord",
    "PromotionCommit",
    "PromotionRead",
    "PutNodesRequest",
    "ReturnUnitRequest",
    "RunCommand",
    "RunRecord",
    "SendReceipt",
    "ServingGraphDispatchStore",
    "ServingPublicationAuthority",
    "ServingPublicationMutationResult",
    "ServingPublicationSettlementRequest",
    "SuccessorRequest",
    "UnitMember",
    "UnitSetGuardError",
    "UnitSetMember",
    "UnitSetSettlement",
]


class GraphStoreUnavailableError(DependencyError):
    """The graph store could not be read or written; nothing committed."""

    def __init__(self, description: str) -> None:
        super().__init__(ErrorCode("EXEC_GRAPH", 20), description)


class GraphTransactionConflictError(Exception):
    """DynamoDB cancelled the transaction for a concurrent transaction on one of its items."""


@dataclass(frozen=True, slots=True)
class RunRecord:
    """`RUN#<run_id>`: the one record that decides compute start (V-10)."""

    run_id: str
    state: RunState
    authority_epoch: int
    conflict_unit: str
    fingerprint: str
    retrieved_at: str
    root_flow_id: str
    graph_id: str
    template: str
    generation: int
    dispatch_generation: int
    revision: int
    receipt_request_id: str
    compute_started_at: str | None = None
    grant_id: str | None = None
    fence_reason: FenceReason | None = None
    promoted: bool = False
    """The promoter made this waiter `A`; its receipt is the waiter's `QUEUED_LATEST`."""
    window: str | None = None
    """S4: the dataset window prefix (`U` before digesting); the legacy head lists under it."""
    root_clock_started_at: str | None = None
    """S5: the manifest version's `LastModified`; every obligation of the run is due from it."""
    grant_ordinal: int | None = None
    """S6 (AGG4-16): the grant's domain ordinal, set on `RUN#` by the grant transaction."""
    serving_domain: str | None = None
    cohort_revision: str | None = None
    serving_input_revision: str | None = None
    serving_plan_digest: str | None = None
    serving_run_ids: tuple[str, ...] = ()
    """Slice 3 active winner context, stamped atomically when the precompute node arms."""
    terminal_at: str | None = None
    """Caller-owned UTC completion instant; adapters never read a clock for this field."""
    item_bytes: int = field(default=0, compare=False)
    """Slice 7 (F3): the item's size as read, by the shared byte oracle; 0 when not read."""


@dataclass(frozen=True, slots=True)
class NodeSpec:
    """One node instantiation puts: its edges, phase, domain lock and immutable envelope."""

    name: str
    after: tuple[str, ...]
    phase: NodePhase
    lock_key: str
    """The physical-table domain lock the node holds from arming until it settles."""
    group_id: str
    """`MessageGroupId`: the unit key, the physical-table serialization boundary (ADR-075)."""
    body: str
    """The owner body, a JSON object without the dispatch binding. Instantiation binds it to the
    node's dispatch identity and stores and sizes the exact envelope bytes (FR-4, S3V-2)."""


@dataclass(frozen=True, slots=True)
class NodeRecord:
    """`RUN#<run_id>/NODE#<name>`."""

    run_id: str
    name: str
    node_id: str
    state: NodeState
    revision: int
    phase: NodePhase
    after: tuple[str, ...]
    lock_key: str
    group_id: str
    body: str
    envelope_digest: str
    measured_bytes: int
    dispatch_id: str
    armed_generation: int | None = None
    permit_generation: int | None = None
    claim_record_present: bool = False
    effect_digest: str | None = None
    failure_reason: str | None = None
    """S6 §2.4: why a `FAILED` node failed (for example `materialization_interrupted`)."""
    effect_record: str | None = None
    """S6 §2.4: the settled node's canonical JSON effect record (`effect_digest` hashes it)."""
    batch_id: str | None = None
    """Slice 7 (1b): the batch the node was last armed in; a return removes it (§2.5)."""
    returned_at: str | None = None
    return_reason: str | None = None
    """Slice 7: the latest `ReturnReason` value, set by `return_unit`."""
    arm_alone: bool = False
    """Slice 7 (D41): the node is armed alone from now on; never removed once set."""
    final_return: bool = False
    """Slice 7 (D35): the node was returned `final_unisolated`; its next attempt is final."""
    failure_code: str | None = None
    """Slice 7 (D35): the error code of the outcome that failed a `FAILED` node."""
    armed_at: str | None = None
    """When the node was last armed (§2.2); a batch's `batch_id` derives from it."""
    serving_domain: str | None = None
    cohort_revision: str | None = None
    serving_input_revision: str | None = None
    serving_plan_digest: str | None = None
    serving_run_ids: tuple[str, ...] = ()
    """Slice 3 winner context stamped by the atomic election."""
    item_bytes: int = field(default=0, compare=False)
    """Slice 7 (F3): the item's size as read, by the shared byte oracle; 0 when not read."""


@dataclass(frozen=True, slots=True)
class BatchMemberRef:
    """One member of a unit batch outbox row: a reference, never the unit's bytes (D31)."""

    run_id: str
    node_id: str
    dispatch_id: str
    armed_generation: int


@dataclass(frozen=True, slots=True)
class GraphMessage:
    """One message the dispatcher sends: the one send path (S6 §2.2, pass 5 M2).

    `node` is the name routing reads. Every node kind but a unit batch builds the message from
    its node (`GraphMessage.of`); a unit batch builds it with `node="write_unit"`, the composed
    batch body, group `<k>` and the batch deduplication identity.
    """

    node: str
    body: str
    group_id: str
    deduplication_id: str

    @classmethod
    def of(cls, node: NodeRecord) -> GraphMessage:
        """Return the message for one node's exact persisted envelope."""
        return cls(node.name, node.body, node.group_id, node.dispatch_id)


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    """`DUE#<n>/OUTBOX#<node_id>`: the only thing the dispatcher may send from."""

    shard: str
    node_id: str
    run_id: str
    node_name: str
    generation: int
    revision: int
    state: str
    """`OPEN` before the permit, `SENDING` after it."""
    members: tuple[BatchMemberRef, ...] = ()
    """Slice 7: a unit batch row's members in arming order (`OUTBOX#<batch_id>`); empty for a
    node row. A batch row's `node_id` is the batch ID, `run_id` its head member's run and
    `node_name` `write_unit`."""
    permitted: tuple[str, ...] = ()
    """Slice 7: the node IDs a SENDING batch row's permit admitted, in member order (F7)."""
    lock_key: str | None = None
    """Slice 7: a batch row's `TABLE#<k>`."""


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    """`EXEC_CLAIM#<node_id>`: the graph execution claim, the only claim for graph envelopes."""

    node_id: str
    run_id: str
    lease_version: int
    lease_expires_at: str
    dispatch_id: str
    claimed_at: str
    queue_enqueued_at: str | None = None
    first_received_at: str | None = None
    table_id: str | None = None
    """Set by the graph-envelope BeginTableAttempt (S6) together with `attempt_id`."""
    attempt_id: str | None = None
    root_flow_id: str | None = None
    """S6 (§2.10): the run's root flow, so the claim's projection row joins its flow."""
    admission: int = 0
    """S6 (§2.10): how many times the claim was admitted (CLAIM is 1, each RECLAIM adds 1).

    A lease bump (a takeover, a settlement) is not an admission. 0 on a pre-S6 claim item."""
    admitted_at: str | None = None
    """S6: when the latest admission (CLAIM or RECLAIM) committed."""
    materialization_began_at: str | None = None
    """S6 §2.4: set by the first re-attestation before a `materialize_raw` fetch; a takeover
    reads it as the fact that materialization may have begun (`MaterializationFacts`)."""
    bake_selection_started: bool = False
    """Plan 4.3 (DB21): the bake fan-out began its one current selection under this claim; a
    reclaim that finds it set with no stored pin is the fatal `durable_bake_source_pin_absent`."""
    batch_id: str | None = None
    """Slice 7 (F11): the batch of the latest admission, set on CLAIM and each RECLAIM."""


@dataclass(frozen=True, slots=True)
class DueItem:
    """One item in a fixed due shard (`DUE#<n>`), read with a strongly consistent query."""

    shard: str
    sk: str
    kind: str
    subject: str
    """The run, node or conflict unit the item names."""
    due_at: str | None = None
    wake_seq: int | None = None
    held: bool = False
    woken_at: str | None = None
    revision: int | None = None
    run_id: str | None = None
    root_clock_started_at: str | None = None
    """S5: the root clock the obligation is due from (absent on pre-S5 items)."""
    scope: ArmScope | None = None
    """S6: an `ARM#` item's scope."""
    domain: str | None = None
    """S6: a `COMPLETE#` item's co-release domain."""
    grant_id: str | None = None
    grant_ordinal: int | None = None
    """S6: a `COMPLETE#` item's grant and its domain ordinal."""
    state: str | None = None
    run_ids: tuple[str, ...] = ()
    """Rolling cohort ownership carried by durable DUE items."""


@dataclass(frozen=True, slots=True)
class ServingPublicationAuthority:
    """No-day publication identity plus one strongly read live-frontier shard."""

    run_id: str
    grant_id: str
    grant_ordinal: int
    domain: str
    shard: int
    shard_revision: int
    visible_count: int
    eligible_count: int

    @property
    def identity(self) -> ServingAuthorityIdentity:
        """The publication run's serving authority identity."""
        return ServingAuthorityIdentity(self.run_id, self.grant_id, self.grant_ordinal, self.domain)


class ServingPublicationMutationResult(StrEnum):
    """Result of the marker's one visible-to-eligible transition."""

    APPLIED = "applied"
    REPLAY = "replay"
    FRONTIER_RACE = "frontier_race"


@dataclass(frozen=True, slots=True)
class ServingPublicationSettlementRequest:
    """Settle publication against its exact marker and fresh frontier authority."""

    settlement: NodeSettlement
    successors: SuccessorRequest
    authority: ServingPublicationAuthority
    target_digest: str


@dataclass(frozen=True, slots=True)
class PutNodesRequest:
    """Put a graph's nodes (idempotently), conditioned on the run's state and generation."""

    run: RunRecord
    nodes: tuple[NodeSpec, ...]
    recorded_at: str


@dataclass(frozen=True, slots=True)
class ArmRequest:
    """The final instantiation transaction: arm the roots and delete `INSTANTIATE#`."""

    run: RunRecord
    roots: tuple[NodeRecord, ...]
    recorded_at: str


@dataclass(frozen=True, slots=True)
class SuccessorRequest:
    """S6 §2.2: successors put PENDING, the `ARM#` obligation that arms them, and `RELEASE#`.

    The `materialize_raw` settlement (slice 3) writes the unit nodes with scope `units` and the
    release trigger; the `publish` settlement (slice 4b) writes scope `serving` and no nodes. A
    settlement joins these members to its own transaction (`build_successors`).
    """

    run: RunRecord
    nodes: tuple[NodeSpec, ...]
    """Put PENDING: the node item only, with no token, lock or outbox."""
    scope: ArmScope | None
    """The `ARM#<run_id>` scope, or None to write no `ARM#`."""
    release: bool
    """Write `RELEASE#<run_id>`, stamped with the run's root clock (S5-10)."""
    recorded_at: str


@dataclass(frozen=True, slots=True)
class ArmNodeRequest:
    """S6 §2.2: the arm pass's one-node transaction (PENDING to READY, token, lock, outbox)."""

    run: RunRecord
    node: NodeRecord
    retire: bool
    """This is the scope's last PENDING routed node: delete `ARM#<run_id>` in the same commit."""
    recorded_at: str


@dataclass(frozen=True, slots=True)
class SendReceipt:
    """The dispatcher's durable enqueue-attempt receipt that closes one outbox row."""

    node: NodeRecord
    outbox: OutboxRecord
    message_id: str
    sent_at: str


@dataclass(frozen=True, slots=True)
class ClaimRequest:
    """One graph execution claim, reclaim or takeover against strongly read state."""

    run: RunRecord
    node: NodeRecord
    claim: ClaimRecord | None
    """The existing claim, for a reclaim or takeover; None for a first claim."""
    lease_expires_at: str
    claimed_at: str
    queue_enqueued_at: str | None = None
    first_received_at: str | None = None
    batch_id: str | None = None
    """Slice 7: a unit batch member's `batch_id`, recorded on the claim with this admission."""


@dataclass(frozen=True, slots=True)
class NodeSettlement:
    """Settle one node to a terminal state and release its lock and runtime token."""

    run: RunRecord
    node: NodeRecord
    state: NodeState
    recorded_at: str
    claim: ClaimRecord | None = None
    """A takeover bumps this claim's lease version in the same transaction."""
    verify_open: bool = False
    """Delete the node's `VERIFY#` obligation in the same transaction."""
    effect_digest: str | None = None
    failure_reason: str | None = None
    """S6 §2.4: recorded on a `FAILED` node."""
    effect_record: str | None = None
    """S6 §2.4: the canonical JSON effect record `effect_digest` hashes, stored on the node."""
    failure_code: str | None = None
    """Slice 7 (D35): recorded on a `FAILED` node beside `failure_reason`."""


@dataclass(frozen=True, slots=True)
class UnitMember:
    """One `write_unit` node with the run it was read under."""

    run: RunRecord
    node: NodeRecord


@dataclass(frozen=True, slots=True)
class ArmUnitBatchRequest:
    """Slice 7 (§2.2): one table group's grant, `arm_unit_batch`, in one transaction."""

    lock_key: str
    batch_id: str
    members: tuple[UnitMember, ...]
    """In grant order (D41); the head's run names the outbox row and its root clock (D-c)."""
    retire_runs: tuple[str, ...]
    """Runs whose last PENDING `units` node this commit arms: their `ARM#` is deleted (N6)."""
    recorded_at: str


@dataclass(frozen=True, slots=True)
class BatchPermitRequest:
    """Slice 7 (§2.2): OPEN -> SENDING with the permitted members, each READY -> PERMITTED."""

    outbox: OutboxRecord
    permitted: tuple[UnitMember, ...]
    recorded_at: str


@dataclass(frozen=True, slots=True)
class BatchSendReceipt:
    """Slice 7: close a SENDING batch row and move each listed member to DISPATCHED."""

    outbox: OutboxRecord
    members: tuple[NodeRecord, ...]
    message_id: str
    sent_at: str


@dataclass(frozen=True, slots=True)
class ReturnUnitRequest:
    """Slice 7 (§2.5 "Return", D35): put a member back to PENDING without holding its table."""

    run: RunRecord
    node: NodeRecord
    reason: ReturnReason
    recorded_at: str
    claim: ClaimRecord | None = None
    """TableWriter's held claim, whose lease is released; None from the dispatcher."""


@dataclass(frozen=True, slots=True)
class UnitSetMember:
    """One member of a success set: its node, the claim that fences it, and its effect."""

    run: RunRecord
    node: NodeRecord
    effect_digest: str
    claim: ClaimRecord | None = None
    effect_record: str | None = None
    """None for a takeover or the verifier, which settle from attempt facts (F8)."""


@dataclass(frozen=True, slots=True)
class UnitSetSettlement:
    """Slice 7 (D34): `settle_unit_set`, the only `SUCCEEDED` path for a `write_unit` node."""

    lock_key: str
    batch_id: str
    members: tuple[UnitSetMember, ...]
    recorded_at: str
    verify_open: bool = False
    """The verifier's settlement also deletes each member's `VERIFY#`."""


class UnitSetGuardError(GraphGuardError):
    """A success-set chunk's guard: the chunks before it committed (`committed` node IDs)."""

    def __init__(self, signal: str, committed: tuple[str, ...]) -> None:
        super().__init__(signal)
        self.committed = committed


@dataclass(frozen=True, slots=True)
class FenceRunRequest:
    """Release point 1: move a run to FENCING, advance its generation, free its slot."""

    run: RunRecord
    reason: FenceReason
    recorded_at: str
    receipt: CommandReceipt | None = None
    """`cancel_plan`'s command receipt, written in the same transaction."""


@dataclass(frozen=True, slots=True)
class MaterializationSettlement:
    """S6 §2.4: the `materialize_raw` settlement, one lease-fenced transaction.

    `settlement` contributes `settle_node`'s members (node transition, token and lock releases,
    lease bump on `settlement.claim`). A `SUCCEEDED` settlement joins `successors` (the PENDING
    unit nodes, `ARM#` and `RELEASE#`) and a `RUN#` ConditionCheck on state {OPEN, PAUSED} and
    the read dispatch generation. A `FAILED` settlement joins `fence` (`build_fence_run`) only
    while the run is OPEN or PAUSED; without it, a `RUN#` check that the run is not live.
    """

    settlement: NodeSettlement
    successors: SuccessorRequest | None = None
    fence: FenceRunRequest | None = None


@dataclass(frozen=True, slots=True)
class RunCommand:
    """`pause_plan` or `resume_plan`: a generation advance plus its command receipt."""

    run: RunRecord
    receipt: CommandReceipt
    ready: tuple[OutboxRecord, ...] = ()
    """For resume: the READY nodes' outbox rows, re-armed under the new generation."""


@dataclass(frozen=True, slots=True)
class HoldRequest:
    """Set or clear the automatic-admission hold on the mode item (GRAPH only)."""

    mode: ModeState
    hold: bool
    receipt: CommandReceipt


@dataclass(frozen=True, slots=True)
class PromotionRead:
    """One conflict unit as the promoter reads it."""

    conflict_unit: str
    wake_seq: int | None
    slot_revision: int
    """The slot item's revision, 0 when absent."""
    slot_run_id: str | None
    """The run the slot names; None when the slot is free."""
    waiter: Waiter | None
    head: datetime | None
    hold: bool
    hold_revision: int = 0
    """The mode item's hold revision; a held waiter's promotion ConditionChecks it (S3V-9)."""
    blocked: bool = False
    """S4: the co-release domain's `BLOCK#` exists (BF-4); promotion ConditionChecks it absent."""


@dataclass(frozen=True, slots=True)
class PromotionCommit:
    """One promoter branch's conditioned transaction."""

    read: PromotionRead
    branch: str
    recorded_at: str
    new_run: RunRecord | None = None
    superseded_request_id: str | None = None
    details: dict[str, str] = field(default_factory=dict[str, str])


class ServingGraphDispatchStore(Protocol):
    """No-day publication authority operations over the cumulative live frontier."""

    def read_serving_publication_authority(self, run_id: str) -> ServingPublicationAuthority:
        """Strongly read the run identity and its current live-frontier shard."""
        ...

    def build_publication_flip_participant(  # noqa: PLR0913 -- exact flip authority inputs
        self,
        authority: ServingPublicationAuthority,
        *,
        root_flow_id: str,
        target_digest: str,
        flipped_at: str,
    ) -> object:
        """Build the COMPLETE/frontier-fenced visible-marker transaction participant."""
        ...

    def settle_publish(
        self, request: ServingPublicationSettlementRequest
    ) -> ServingPublicationMutationResult:
        """Make the marker eligible and advance the cumulative frontier once."""
        ...


class GraphDispatchStore(Protocol):
    """The graph dispatch authority over the execution-graph table."""

    def read_mode(self) -> ModeState:
        """Read the mode item (and the hold it carries) strongly."""
        ...

    def read_run(self, run_id: str) -> RunRecord | None:
        """Read one run record."""
        ...

    def read_nodes(self, run_id: str) -> tuple[NodeRecord, ...]:
        """Read every node a run has put, in name order."""
        ...

    def read_claim(self, node_id: str) -> ClaimRecord | None:
        """Read a node's graph execution claim."""
        ...

    def read_due(self, shard: str, prefix: str) -> tuple[DueItem, ...]:
        """Read one due shard's items of one kind."""
        ...

    def read_outbox(self, shard: str, node_id: str) -> OutboxRecord | None:
        """Read one outbox row."""
        ...

    def read_promotion(self, conflict_unit: str) -> PromotionRead:
        """Read `PROMOTE#`, `SLOT#`, `WAITER#`, `HEAD#` and the hold for one unit."""
        ...

    def instantiation_open(self, run: RunRecord) -> bool:
        """Whether the run's `INSTANTIATE#` obligation still exists."""
        ...

    def put_nodes(self, request: PutNodesRequest) -> None:
        """Put the graph and its nodes, each `attribute_not_exists`, under the RUN# check."""
        ...

    def arm_roots(self, request: ArmRequest) -> None:
        """Arm the roots (READY, token, lock, outbox) and delete `INSTANTIATE#` in one txn."""
        ...

    def settle_fenced_instantiation(self, request: ArmRequest) -> None:
        """Settle every put node `FENCED_UNSENT` and delete `INSTANTIATE#` in one txn."""
        ...

    def put_successors(self, request: SuccessorRequest) -> None:
        """Put PENDING successors, `ARM#` and `RELEASE#` under the run's state check (S6)."""
        ...

    def arm_node(self, request: ArmNodeRequest) -> None:
        """Arm one PENDING node; delete `ARM#` when it is the scope's last (S6, AGG4-1)."""
        ...

    def retire_arm(self, run: RunRecord, scope: ArmScope) -> None:
        """Delete `ARM#` alone for a run that is no longer live for its scope (S6)."""
        ...

    def release_claim_lease(
        self,
        claim: ClaimRecord,
        released_at: str,
        progress: Mapping[str, int] | None = None,
    ) -> None:
        """Expire the held lease now, conditioned on its lease version (S6 §2.3).

        `progress` (S6 §2.6) sets `publish`'s progress counts on the claim in the same write.
        """
        ...

    def mark_bake_selection(self, claim: ClaimRecord) -> None:
        """Plan 4.3 (DB21): `SET bake_selection_started` on `EXEC_CLAIM#`, lease-fenced.

        A refusal is `CLAIM_LEASE_STALE`.
        """
        ...

    def read_receipt(self, request_id: str) -> ReceiptRecord | None:
        """Read one decision receipt (the promoter reads `W`'s identity from it)."""
        ...

    def acquire_permit(self, run: RunRecord, node: NodeRecord, outbox: OutboxRecord) -> None:
        """Acquire the send permit, or raise `PLAN_DISPATCH_FENCED` (BF-2)."""
        ...

    def record_send(self, receipt: SendReceipt) -> None:
        """Close the outbox row with the exact enqueue-attempt receipt."""
        ...

    def fail_send(self, settlement: NodeSettlement, fence: FenceRunRequest | None) -> None:
        """Fail the node on a provider oversize rejection; fence the run only while it is live."""
        ...

    def claim(self, request: ClaimRequest) -> ClaimRecord:
        """Commit a first claim or a reclaim (V-10's one CAS record)."""
        ...

    def await_provider_terminal(self, request: ClaimRequest) -> None:
        """Takeover with an open attempt: bump the lease, park the node, write `VERIFY#`."""
        ...

    def settle_node(self, settlement: NodeSettlement) -> None:
        """Settle a node terminal and release its lock and token (and `VERIFY#`)."""
        ...

    def reattest_materialization(self, claim: ClaimRecord, began_at: str) -> None:
        """S6 §2.4: lease-conditioned `EXEC_CLAIM#` write plus a `RUN#` OPEN/PAUSED check.

        Sets `materialization_began_at` (when the claim has none yet). Raises `GraphGuardError`
        when the lease version moved or the run is not live.
        """
        ...

    def read_serving_publication_authority(self, run_id: str) -> ServingPublicationAuthority:
        """Strongly read the run identity and its current live-frontier shard."""
        ...

    def build_publication_flip_participant(  # noqa: PLR0913 -- exact flip authority inputs
        self,
        authority: ServingPublicationAuthority,
        *,
        root_flow_id: str,
        target_digest: str,
        flipped_at: str,
    ) -> object:
        """Build the publication marker and live-frontier flip participant."""
        ...

    def settle_publish(
        self, request: ServingPublicationSettlementRequest
    ) -> ServingPublicationMutationResult:
        """Settle `publish`, freeze its marker coordinate, and advance eligibility."""
        ...

    def settle_materialization(self, request: MaterializationSettlement) -> None:
        """S6 §2.4: settle `materialize_raw` with its successors or its fence, in one txn."""
        ...

    def fence_unsent(self, run: RunRecord, node: NodeRecord) -> None:
        """Settle a READY permitless node of a FENCING run `FENCED_UNSENT` (V-11)."""
        ...

    def fence_pending(self, run: RunRecord, node: NodeRecord) -> None:
        """Settle a never-armed node of a FENCING run once `INSTANTIATE#` is gone (S3V-1)."""
        ...

    def fence_run(self, request: FenceRunRequest) -> None:
        """Release point 1: FENCING, generation + 1, slot cleared, `PROMOTE#` woken."""
        ...

    def terminalize_run(self, run: RunRecord, nodes: tuple[NodeRecord, ...]) -> RunState:
        """Move a FENCING run to its terminal; refused while `INSTANTIATE#` exists."""
        ...

    def pause_run(self, command: RunCommand) -> None:
        """OPEN -> PAUSED, generation + 1; refused with a grant pending apply."""
        ...

    def resume_run(self, command: RunCommand) -> None:
        """PAUSED -> OPEN, generation + 1, READY outbox rows re-armed."""
        ...

    def set_hold(self, request: HoldRequest) -> None:
        """Set or clear the hold on the mode item, conditioned on mode GRAPH."""
        ...

    def record_command(self, receipt: CommandReceipt) -> None:
        """Write one refusal command receipt and nothing else."""
        ...

    def read_command(self, command_id: str) -> CommandReceipt | None:
        """Read one command receipt (an exact replay returns it)."""
        ...

    def promote(self, commit: PromotionCommit) -> None:
        """Commit one promoter branch, or raise `PROMOTE_WAKE_CHANGED`."""
        ...

    def arm_unit_batch(self, request: ArmUnitBatchRequest) -> None:
        """Slice 7: arm one table group's members, the counted lock and one batch outbox row."""
        ...

    def unit_batch_fits(self, request: ArmUnitBatchRequest) -> bool:
        """Slice 7 (F3): whether `arm_unit_batch`'s one transaction is within the 4 MB bound."""
        ...

    def acquire_batch_permit(self, request: BatchPermitRequest) -> None:
        """Slice 7: permit a batch row's live members, or raise the member's guard."""
        ...

    def record_batch_send(self, receipt: BatchSendReceipt) -> None:
        """Slice 7: close a SENDING batch row; the listed members become DISPATCHED."""
        ...

    def delete_batch_row(self, outbox: OutboxRecord) -> None:
        """Slice 7: delete an OPEN batch row none of whose members may be permitted."""
        ...

    def return_unit(self, request: ReturnUnitRequest) -> None:
        """Slice 7: return a CLAIMED or READY member to PENDING and release its lock share."""
        ...

    def settle_unit_set(self, request: UnitSetSettlement) -> tuple[str, ...]:
        """Slice 7: settle a success set SUCCEEDED, chunked; return the committed node IDs.

        A guard in a later chunk raises `UnitSetGuardError` naming what already committed.
        """
        ...
