# SPDX-License-Identifier: MIT-0
"""DynamoDB adapter for graph dispatch, claim, settlement and promotion (S3).

Same table and keys as the S1 admission adapter. Items this slice adds:

- `RUN#<run_id>/NODE#<name>`: a node, put `PENDING` by instantiation;
- `GRAPH#<graph_id>/GRAPH`: the epoch-1 graph record, put with the first nodes;
- `TOKEN#<node_id>/TOKEN` and `LOCK#<lock_key>/LOCK`: the runtime token and the
  physical-table domain lock a node holds from arming until it settles;
- `EXEC_CLAIM#<node_id>/CLAIM`: the graph execution claim and its lease version;
- in the run's due shard `DUE#<n>`: `OUTBOX#<node_id>` (the only thing the dispatcher sends
  from), `VERIFY#<node_id>` (a takeover waiting for its provider terminal) and `FENCE#<run_id>`
  (a FENCING run the fence settler still has to finish);
- S6 §2.2: `ARM#<run_id>` (scope `units` or `serving`: PENDING successors the promoter's arm
  pass arms, deleted by the transaction that arms the scope's last one) and `RELEASE#<run_id>`
  (the release issuer's trigger), both written by `build_successors`;
- the automatic-admission hold, as attributes of the mode item, which only the operator writes.

Every write is one transaction of typed members (`graph_transactions`), so a lost race raises
its exact guard signal. Reads are strongly consistent `GetItem` and `Query` on the base table.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, cast

from botocore.exceptions import BotoCoreError, ClientError

from adapters.outbound.dynamodb.execution_graph import (
    command_item,
    due_shard,
    mode_state_from_item,
    read_receipt_item,
    receipt_item,
    root_clock_attribute,
)
from adapters.outbound.dynamodb.graph_transaction_bytes import (
    MAX_TRANSACTION_BYTES,
    ItemKey,
    item_bytes,
    record_bytes,
    transaction_bytes,
)
from adapters.outbound.dynamodb.graph_transactions import (
    Member,
    MemberSpec,
    check,
    delete,
    put,
    transact,
    update,
)
from adapters.outbound.dynamodb.items import write_bool, write_int, write_text
from domain.operations.execution_graph import (
    AuthorityMode,
    ModeState,
)
from domain.operations.execution_graph_dispatch import (
    AUTOMATIC_ADMISSION_HOLD_REQUIRES_GRAPH,
    CLAIM_LEASE_LIVE,
    CLAIM_LEASE_STALE,
    DOMAIN_LOCK_HELD,
    MAX_BATCH_MEMBERS,
    NODE_STATE_CHANGED,
    PLAN_DISPATCH_FENCED,
    PROMOTE_WAKE_CHANGED,
    PROVIDER_PAYLOAD_OVERSIZE,
    RUN_ARMING_OPEN,
    RUN_GRANT_PENDING_APPLY,
    RUN_INSTANTIATION_OPEN,
    RUN_NOT_INSTANTIABLE,
    RUN_STATE_CHANGED,
    SENT_NODE_STATES,
    SETTLE_CHUNK_MEMBERS,
    SLOT_NOT_OWNED,
    TABLE_LOCK_PREFIX,
    WRITE_UNIT,
    WRITE_UNIT_SETTLE_PATH,
    ArmScope,
    FenceReason,
    GraphGuardError,
    NodePhase,
    NodeState,
    PromoterBranch,
    ServingAuthorityIdentity,
    bind_graph_envelope,
    fence_terminal_state,
    is_write_unit,
    owner_envelope_digest,
    provider_accounted_bytes,
    serving_frontier_shard,
    sets_arm_alone,
    sets_final_return,
)
from domain.operations.execution_graph_release import INGESTION_RELEASE_DOMAIN
from domain.operations.generated_execution_graphs import BOUNDS, STORE
from domain.operations.run_request import RunState, Waiter
from domain.operations.status import canonical_authority_digest
from domain.shared_kernel.errors import (
    GRAPH_UNIT_BATCH_OVERSIZE_CODE,
    WRITE_UNIT_SETTLE_PATH_CODE,
    InvariantError,
)
from ports.outbound.execution_graph import CommandReceipt, ReceiptRecord
from ports.outbound.execution_graph_dispatch import (
    ArmNodeRequest,
    ArmRequest,
    ArmUnitBatchRequest,
    BatchMemberRef,
    BatchPermitRequest,
    BatchSendReceipt,
    ClaimRecord,
    ClaimRequest,
    DueItem,
    FenceRunRequest,
    GraphStoreUnavailableError,
    HoldRequest,
    MaterializationSettlement,
    NodeRecord,
    NodeSettlement,
    NodeSpec,
    OutboxRecord,
    PromotionCommit,
    PromotionRead,
    PutNodesRequest,
    ReturnUnitRequest,
    RunCommand,
    RunRecord,
    SendReceipt,
    ServingPublicationAuthority,
    ServingPublicationMutationResult,
    ServingPublicationSettlementRequest,
    SuccessorRequest,
    UnitMember,
    UnitSetGuardError,
    UnitSetMember,
    UnitSetSettlement,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from types_boto3_dynamodb.client import DynamoDBClient
    from types_boto3_dynamodb.type_defs import AttributeValueTypeDef, TransactWriteItemTypeDef

__all__ = [
    "BATCH_NODE",
    "DynamoGraphDispatchStore",
    "GraphEnvelope",
    "ServingPublicationAuthority",
    "arming_members",
    "batch_body",
    "batch_dispatch_id",
    "batch_row_shard",
    "build_fence_run",
    "build_successors",
    "build_terminal",
    "dispatch_id_for",
    "due_obligation_item",
    "envelope_digest",
    "grant_fence_checks",
    "graph_envelope",
    "node_id_for",
    "serving_authority_identity_attributes",
    "serving_authority_identity_from_item",
]

type Item = dict[str, AttributeValueTypeDef]

_MODE_PK: Final = str(STORE["mode_key"]["pk"])
_MODE_SK: Final = str(STORE["mode_key"]["sk"])
_BOUND: Final = int(BOUNDS["max_transaction_items"])
_PROGRESS_FIELDS: Final = frozenset({"publish_staged", "publish_total"})
_OPEN: Final = "OPEN"
_SENDING: Final = "SENDING"


def node_id_for(run_id: str, name: str) -> str:
    """Derive a node's identity from its run and template node name."""
    return canonical_authority_digest(["execution-graph-node/v1", run_id, name])


def envelope_digest(body: str) -> str:
    """Return the owner-body digest; instantiation and the claim both recompute it (S3V-2).

    It covers the canonical owner body without the dispatch binding, because the binding's
    `dispatch_id` is derived from it.
    """
    return owner_envelope_digest(body)


def dispatch_id_for(node_id: str, digest: str) -> str:
    """One logical dispatch identity per node and envelope, across retries and DLQ observation."""
    return canonical_authority_digest(["execution-graph-dispatch/v1", node_id, digest])


BATCH_NODE: Final = "write_unit_batch"
"""Slice 7 (D31): the semantic-write message for units is a batch of member references."""


def batch_body(batch_id: str, lock_key: str, members: Sequence[BatchMemberRef]) -> str:
    """Compose a unit batch envelope from its permitted members, in member order (§2.5).

    Compact sorted JSON, so the dispatcher's re-send from SENDING is byte-identical (F7).
    """
    return json.dumps(
        {
            "schema_version": "1",
            "node": BATCH_NODE,
            "batch_id": batch_id,
            "lock_key": lock_key,
            "members": [
                {"run_id": item.run_id, "node_id": item.node_id, "dispatch_id": item.dispatch_id}
                for item in members
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def batch_dispatch_id(batch_id: str, body: str) -> str:
    """Return the batch message deduplication ID, also its batch span `dispatch_id` (§2.10)."""
    return dispatch_id_for(batch_id, envelope_digest(body))


@dataclass(frozen=True, slots=True)
class GraphEnvelope:
    """One node's identity and the exact envelope bytes the dispatcher sends."""

    node_id: str
    digest: str
    dispatch_id: str
    body: str


def graph_envelope(run_id: str, spec: NodeSpec) -> GraphEnvelope:
    """Bind a node's owner body to its dispatch identity (the bytes instantiation stores)."""
    node_id = node_id_for(run_id, spec.name)
    digest = envelope_digest(spec.body)
    dispatch_id = dispatch_id_for(node_id, digest)
    body = bind_graph_envelope(
        spec.body, {"dispatch_id": dispatch_id, "run_id": run_id, "node_id": node_id}
    )
    return GraphEnvelope(node_id, digest, dispatch_id, body)


def _key(pk: str, sk: str) -> Item:
    return {"pk": write_text(pk), "sk": write_text(sk)}


def _text(item: Item, name: str) -> str | None:
    value = item.get(name, {}).get("S")
    return None if value is None else str(value)


def _int(item: Item, name: str) -> int | None:
    value = item.get(name, {}).get("N")
    return None if value is None else int(str(value))


def _req_text(item: Item, name: str) -> str:
    value = _text(item, name)
    if value is None:
        raise GraphStoreUnavailableError(f"a graph item is missing {name}.")
    return value


def _req_int(item: Item, name: str) -> int:
    value = _int(item, name)
    if value is None:
        raise GraphStoreUnavailableError(f"a graph item is missing {name}.")
    return value


def _run_key(run_id: str) -> Item:
    return _key(f"RUN#{run_id}", "RUN")


def _serving_frontier_key(domain: str, shard: int) -> Item:
    return _key(f"SERVING_FRONTIER#{domain}#{shard:02d}", "COUNTER")


def _serving_marker_key(run_id: str) -> Item:
    return _key(f"SERVING_PUBLICATION#{run_id}", "MARKER")


def serving_authority_identity_attributes(
    identity: ServingAuthorityIdentity,
) -> dict[str, AttributeValueTypeDef]:
    """Render the shared immutable serving identity into one DynamoDB item."""
    return {
        "run_id": write_text(identity.run_id),
        "grant_id": write_text(identity.grant_id),
        "grant_ordinal": write_int(identity.grant_ordinal),
        "domain": write_text(identity.domain),
    }


def serving_authority_identity_from_item(item: Item) -> ServingAuthorityIdentity:
    """Read the one shared immutable serving identity from a DynamoDB item."""
    return ServingAuthorityIdentity(
        run_id=_req_text(item, "run_id"),
        grant_id=_req_text(item, "grant_id"),
        grant_ordinal=_req_int(item, "grant_ordinal"),
        domain=_req_text(item, "domain"),
    )


def _node_key(node: NodeRecord) -> Item:
    return _key(f"RUN#{node.run_id}", f"NODE#{node.name}")


def _values(**values: AttributeValueTypeDef) -> dict[str, AttributeValueTypeDef]:
    return {f":{name}": value for name, value in values.items()}


def _run_signal(old: Item | None) -> str:
    """Resolve a `RUN#` member's old image: a pending grant is its own refusal (V-29)."""
    if old is not None and "grant_id" in old:
        return RUN_GRANT_PENDING_APPLY
    return RUN_STATE_CHANGED


def read_run_item(item: Item) -> RunRecord:
    """Parse one `RUN#` item."""
    reason = _text(item, "fence_reason")
    return RunRecord(
        promoted=bool(item.get("promoted", {}).get("BOOL", False)),
        run_id=_req_text(item, "pk").removeprefix("RUN#"),
        state=RunState(_req_text(item, "state")),
        authority_epoch=_req_int(item, "authority_epoch"),
        conflict_unit=_req_text(item, "conflict_unit"),
        fingerprint=_req_text(item, "fingerprint"),
        retrieved_at=_req_text(item, "retrieved_at"),
        root_flow_id=_req_text(item, "root_flow_id"),
        graph_id=_req_text(item, "graph_id"),
        template=_req_text(item, "template"),
        generation=_req_int(item, "generation"),
        dispatch_generation=_req_int(item, "dispatch_generation"),
        revision=_req_int(item, "revision"),
        receipt_request_id=_req_text(item, "receipt_request_id"),
        compute_started_at=_text(item, "compute_started_at"),
        grant_id=_text(item, "grant_id"),
        fence_reason=None if reason is None else FenceReason(reason),
        window=_text(item, "window"),
        root_clock_started_at=_text(item, "root_clock_started_at"),
        terminal_at=_text(item, "terminal_at"),
        grant_ordinal=_int(item, "grant_ordinal"),
        serving_domain=_text(item, "serving_domain"),
        cohort_revision=_text(item, "cohort_revision"),
        serving_input_revision=_text(item, "serving_input_revision"),
        serving_plan_digest=_text(item, "serving_plan_digest"),
        serving_run_ids=_text_list(item, "serving_run_ids"),
        item_bytes=item_bytes(item),
    )


def read_node_item(item: Item) -> NodeRecord:
    """Parse one `NODE#` item."""
    after = item.get("after", {}).get("L", [])
    return NodeRecord(
        run_id=_req_text(item, "run_id"),
        name=_req_text(item, "node"),
        node_id=_req_text(item, "node_id"),
        state=NodeState(_req_text(item, "state")),
        revision=_req_int(item, "revision"),
        phase=NodePhase(_req_text(item, "phase")),
        after=tuple(str(entry.get("S", "")) for entry in after),
        lock_key=_req_text(item, "lock_key"),
        group_id=_req_text(item, "group_id"),
        body=_req_text(item, "body"),
        envelope_digest=_req_text(item, "envelope_digest"),
        measured_bytes=_req_int(item, "measured_bytes"),
        dispatch_id=_req_text(item, "dispatch_id"),
        armed_generation=_int(item, "armed_generation"),
        permit_generation=_int(item, "permit_generation"),
        claim_record_present=bool(item.get("claim_record_present", {}).get("BOOL", False)),
        effect_digest=_text(item, "effect_digest"),
        failure_reason=_text(item, "failure_reason"),
        effect_record=_text(item, "effect_record"),
        batch_id=_text(item, "batch_id"),
        returned_at=_text(item, "returned_at"),
        return_reason=_text(item, "return_reason"),
        arm_alone=_bool(item, "arm_alone"),
        final_return=_bool(item, "final_return"),
        failure_code=_text(item, "failure_code"),
        armed_at=_text(item, "armed_at"),
        serving_domain=_text(item, "serving_domain"),
        cohort_revision=_text(item, "cohort_revision"),
        serving_input_revision=_text(item, "serving_input_revision"),
        serving_plan_digest=_text(item, "serving_plan_digest"),
        serving_run_ids=_text_list(item, "serving_run_ids"),
        item_bytes=item_bytes(item),
    )


def _bool(item: Item, name: str) -> bool:
    return bool(item.get(name, {}).get("BOOL", False))


def _batch_members(item: Item) -> tuple[BatchMemberRef, ...]:
    """Parse a batch outbox row's `members` list (slice 7); absent on a node row."""
    members: list[BatchMemberRef] = []
    for entry in item.get("members", {}).get("L", []):
        fields = cast("Item", entry.get("M", {}))
        members.append(
            BatchMemberRef(
                run_id=_req_text(fields, "run_id"),
                node_id=_req_text(fields, "node_id"),
                dispatch_id=_req_text(fields, "dispatch_id"),
                armed_generation=_req_int(fields, "armed_generation"),
            )
        )
    return tuple(members)


def _text_list(item: Item, name: str) -> tuple[str, ...]:
    return tuple(str(entry.get("S", "")) for entry in item.get(name, {}).get("L", []))


def _node_item(run: RunRecord, spec: NodeSpec, recorded_at: str) -> Item:
    envelope = graph_envelope(run.run_id, spec)
    return {
        **_key(f"RUN#{run.run_id}", f"NODE#{spec.name}"),
        "item_kind": write_text("node"),
        "authority_epoch": write_int(run.authority_epoch),
        "root_flow_id": write_text(run.root_flow_id),
        "run_id": write_text(run.run_id),
        "node": write_text(spec.name),
        "node_id": write_text(envelope.node_id),
        "state": write_text(NodeState.PENDING.value),
        "revision": write_int(1),
        "phase": write_text(spec.phase.value),
        "after": {"L": [write_text(name) for name in spec.after]},
        "lock_key": write_text(spec.lock_key),
        "group_id": write_text(spec.group_id),
        "body": write_text(envelope.body),
        "envelope_digest": write_text(envelope.digest),
        "measured_bytes": write_int(provider_accounted_bytes(envelope.body, {})),
        "dispatch_id": write_text(envelope.dispatch_id),
        "put_at": write_text(recorded_at),
    }


def _live_run(run_id: str, signal: str) -> Member:
    return check(
        MemberSpec(
            "",
            _run_key(run_id),
            signal,
            "#state = :open OR #state = :paused",
            _values(open=write_text(RunState.OPEN.value), paused=write_text(RunState.PAUSED.value)),
        )
    )


def _not_live_run(run_id: str) -> Member:
    return check(
        MemberSpec(
            "",
            _run_key(run_id),
            RUN_STATE_CHANGED,
            "#state <> :open AND #state <> :paused",
            _values(open=write_text(RunState.OPEN.value), paused=write_text(RunState.PAUSED.value)),
        )
    )


def _release_members(node: NodeRecord) -> list[Member]:
    """Delete the node's runtime token and release the domain lock it holds.

    Slice 7 (D33, N5): the release keys on the lock kind. A `TABLE#` lock is counted, held by
    one batch, so its member releases one share (`ADD open_members :minus_one` on its
    `holder_batch_id`); every other lock keeps its single holder and the holder delete.
    """
    token = delete(
        MemberSpec(
            "",
            _key(f"TOKEN#{node.node_id}", "TOKEN"),
            NODE_STATE_CHANGED,
            "attribute_exists(pk)",
        )
    )
    if node.lock_key.startswith(TABLE_LOCK_PREFIX):
        if node.batch_id is None:
            raise GraphStoreUnavailableError(
                "a TABLE# lock holder carries no batch; no uncounted table lock exists (N8)."
            )
        return [token, counted_release(node.lock_key, node.batch_id, 1)]
    return [
        token,
        delete(
            MemberSpec(
                "",
                _key(f"LOCK#{node.lock_key}", "LOCK"),
                NODE_STATE_CHANGED,
                "holder_node_id = :node",
                _values(node=write_text(node.node_id)),
            )
        ),
    ]


def _node_transition(
    node: NodeRecord, to: NodeState, sets: dict[str, AttributeValueTypeDef]
) -> Member:
    assignments = ["#state = :to", "revision = revision + :one"]
    values: dict[str, AttributeValueTypeDef] = {
        "to": write_text(to.value),
        "one": write_int(1),
        "from": write_text(node.state.value),
        "rev": write_int(node.revision),
    }
    for name, value in sets.items():
        assignments.append(f"{name} = :{name}")
        values[name] = value
    return update(
        MemberSpec(
            "",
            _node_key(node),
            NODE_STATE_CHANGED,
            "#state = :from AND revision = :rev",
            _values(**values),
        ),
        "SET " + ", ".join(assignments),
    )


node_transition_member = _node_transition
"""Plan 4.6: the one node-transition member shape, for the V3 final transaction."""


def release_members(node: NodeRecord) -> list[Member]:
    """Return the node's token delete and lock release (the finalizer's post-commit cleanup)."""
    return _release_members(node)


def _arm_key(run: RunRecord) -> Item:
    return _key(due_shard(run.conflict_unit), f"ARM#{run.run_id}")


def _arming_absent(run: RunRecord) -> Member:
    """S6 §2.2: no run turns terminal (or settles its PENDING nodes) while `ARM#` is open."""
    return check(MemberSpec("", _arm_key(run), RUN_ARMING_OPEN, "attribute_not_exists(pk)"))


# --- 1b: the counted table lock and the batch outbox row (slice 7, §2.2, §2.5, D33) ------------


def _lock_key_item(lock_key: str) -> Item:
    return _key(f"LOCK#{lock_key}", "LOCK")


def batch_row_shard(batch_id: str) -> str:
    """Return the due shard of `OUTBOX#<batch_id>`.

    The design names `due_shard(<k>)`, but `due_shard` reads a hex conflict-unit prefix and a
    unit key is a table name, so the row shards by its batch ID (a hex digest) instead. The
    dispatcher walks every shard, so the choice only spreads the rows.
    """
    return due_shard(batch_id)


def counted_release(lock_key: str, batch_id: str, count: int) -> Member:
    """Return `ADD open_members :minus_m` on `LOCK#TABLE#<k>`, refusing an over-release (D33).

    Conditioned `holder_batch_id = :batch AND open_members >= :m`, so the count can't go below
    zero and a stale batch can't release a later grant's share (`NODE_STATE_CHANGED`).
    """
    if count < 1:
        raise ValueError("a counted release releases at least one member.")
    return update(
        MemberSpec(
            "",
            _lock_key_item(lock_key),
            NODE_STATE_CHANGED,
            "holder_batch_id = :batch AND open_members >= :m",
            _values(batch=write_text(batch_id), m=write_int(count), minus=write_int(-count)),
        ),
        "ADD open_members :minus",
    )


def _token_delete(node: NodeRecord) -> Member:
    return delete(
        MemberSpec(
            "", _key(f"TOKEN#{node.node_id}", "TOKEN"), NODE_STATE_CHANGED, "attribute_exists(pk)"
        )
    )


def read_sizes(*records: RunRecord | NodeRecord) -> dict[ItemKey, int]:
    """Map each record's item to its size as read (F3), or its fields' size if never read."""
    sizes: dict[ItemKey, int] = {}
    for record in records:
        sk = "RUN" if isinstance(record, RunRecord) else f"NODE#{record.name}"
        sizes[(f"RUN#{record.run_id}", sk)] = record.item_bytes or record_bytes(record)
    return sizes


def success_chunks(
    request: UnitSetSettlement, writes: Callable[[UnitSetMember], list[Member]]
) -> tuple[tuple[UnitSetMember, ...], ...]:
    """Split a success set into chunks, in member order (§2.5 step 6; F3).

    A chunk holds at most `SETTLE_CHUNK_MEMBERS` members by action count (one lock `ADD` beside
    each member's `writes`) and fewer when its aggregate bound (`transaction_bytes`, the shared
    byte oracle over each member's node as read, its effect and its other targets, plus the
    lock) would pass `MAX_TRANSACTION_BYTES`. With at most 24 members one chunk is the normal
    case.
    """
    lock = transaction_bytes(
        [counted_release(request.lock_key, request.batch_id, SETTLE_CHUNK_MEMBERS)], {}
    )
    chunks: list[tuple[UnitSetMember, ...]] = []
    current: list[UnitSetMember] = []
    actions, size = 1, lock
    for member in request.members:
        members = writes(member)
        weight = transaction_bytes(members, read_sizes(member.run, member.node))
        full = len(current) == SETTLE_CHUNK_MEMBERS or actions + len(members) > _BOUND
        if current and (full or size + weight > MAX_TRANSACTION_BYTES):
            chunks.append(tuple(current))
            current, actions, size = [], 1, lock
        current.append(member)
        actions += len(members)
        size += weight
    if current:
        chunks.append(tuple(current))
    return tuple(chunks)


def _member_ref(member: UnitMember) -> AttributeValueTypeDef:
    return {
        "M": {
            "run_id": write_text(member.run.run_id),
            "node_id": write_text(member.node.node_id),
            "dispatch_id": write_text(member.node.dispatch_id),
            "armed_generation": write_int(member.run.dispatch_generation),
        }
    }


def _open_run_at(  # noqa: PLR0913 -- one RUN# member, every input explicit
    run_id: str, generation: int, *, signal: str, bump: bool
) -> Member:
    """`RUN#` state OPEN at a generation: an update bumping the revision, or a check."""
    condition = "#state = :open AND dispatch_generation = :gen"
    values = _values(open=write_text(RunState.OPEN.value), gen=write_int(generation))
    if not bump:
        return check(MemberSpec("", _run_key(run_id), signal, condition, values))
    values[":one"] = write_int(1)
    return update(
        MemberSpec("", _run_key(run_id), signal, condition, values),
        "SET revision = revision + :one",
    )


def _lease_release(claim: ClaimRecord, released_at: str) -> Member:
    """`release_claim_lease`'s condition as a transaction member (§2.5 "Return")."""
    return update(
        MemberSpec(
            "",
            _key(f"EXEC_CLAIM#{claim.node_id}", "CLAIM"),
            CLAIM_LEASE_STALE,
            "lease_version = :version",
            _values(released=write_text(released_at), version=write_int(claim.lease_version)),
        ),
        "SET lease_expires_at = :released",
    )


def _arm_upsert(run: RunRecord, recorded_at: str) -> Member:
    """Re-put `ARM#<run_id>` with scope `units`, or keep the one that exists (§2.5 "Return").

    Conditioned `attribute_not_exists(pk) OR #scope = :units`. The root clock is the run's, so
    re-setting it on an existing item writes the same value; `due_at` names the latest return.
    """
    values = _values(
        units=write_text(ArmScope.UNITS.value),
        arm=write_text("arm"),
        epoch=write_int(run.authority_epoch),
        run=write_text(run.run_id),
        at=write_text(recorded_at),
    )
    expression = (
        "SET #scope = :units, item_kind = :arm, authority_epoch = :epoch, run_id = :run, "
        "due_at = :at"
    )
    if run.root_clock_started_at is not None:
        values[":clock"] = write_text(run.root_clock_started_at)
        expression += ", root_clock_started_at = :clock"
    return update(
        MemberSpec(
            "",
            _arm_key(run),
            NODE_STATE_CHANGED,
            "attribute_not_exists(pk) OR #scope = :units",
            values,
        ),
        expression,
    )


def arming_members(  # noqa: PLR0913 -- optional election attributes
    run: RunRecord,
    node: NodeRecord,
    recorded_at: str,
    *,
    attributes: Item | None = None,
) -> list[Member]:
    """Arm one node under the run's read generation: READY, token, domain lock and outbox row.

    The shape every arming write shares (`arm_roots`, the arm pass), with `armed_at` beside
    `armed_generation` so each node has the start of its transition latency (G2-10, §2.10).
    """
    gen = write_int(run.dispatch_generation)
    epoch = write_int(run.authority_epoch)
    return [
        _node_transition(
            node,
            NodeState.READY,
            {
                "armed_generation": gen,
                "armed_at": write_text(recorded_at),
                **(attributes or {}),
            },
        ),
        put(
            MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
            {
                **_key(f"TOKEN#{node.node_id}", "TOKEN"),
                "item_kind": write_text("token"),
                "authority_epoch": epoch,
                "run_id": write_text(run.run_id),
                "node_id": write_text(node.node_id),
            },
        ),
        put(
            MemberSpec("", {}, DOMAIN_LOCK_HELD, "attribute_not_exists(pk)"),
            {
                **_key(f"LOCK#{node.lock_key}", "LOCK"),
                "item_kind": write_text("lock"),
                "authority_epoch": epoch,
                "holder_node_id": write_text(node.node_id),
                "run_id": write_text(run.run_id),
            },
        ),
        put(
            MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
            {
                **_key(due_shard(run.conflict_unit), f"OUTBOX#{node.node_id}"),
                "item_kind": write_text("outbox"),
                "authority_epoch": epoch,
                "run_id": write_text(run.run_id),
                "node": write_text(node.name),
                "generation": gen,
                "revision": write_int(1),
                "state": write_text(_OPEN),
                "due_at": write_text(recorded_at),
                **root_clock_attribute(run.root_clock_started_at),
            },
        ),
    ]


def due_obligation_item(
    run: RunRecord, kind: str, recorded_at: str, **extra: AttributeValueTypeDef
) -> Item:
    """Render one run's due obligation `DUE#<n>/<KIND>#<run_id>`, stamped with its root clock."""
    return {
        **_key(due_shard(run.conflict_unit), f"{kind.upper()}#{run.run_id}"),
        "item_kind": write_text(kind),
        "authority_epoch": write_int(run.authority_epoch),
        "run_id": write_text(run.run_id),
        "due_at": write_text(recorded_at),
        **extra,
        **root_clock_attribute(run.root_clock_started_at),
    }


def build_successors(request: SuccessorRequest) -> list[Member]:
    """S6 §2.2: the successor members a settlement joins to its own transaction.

    Each successor is put PENDING in the node shape instantiation writes, with no token, lock or
    outbox. `ARM#<run_id>` (its scope and the run's root clock) is the arming obligation the
    promoter's arm pass takes; `RELEASE#<run_id>` is the release issuer's trigger, stamped with
    the run's root clock (S5-10). No `RUN#` member: the settlement carries its own.
    """
    run = request.run
    members = [
        put(
            MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
            _node_item(run, spec, request.recorded_at),
        )
        for spec in request.nodes
    ]
    if request.scope is not None:
        members.append(
            put(
                MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
                due_obligation_item(
                    run, "arm", request.recorded_at, scope=write_text(request.scope.value)
                ),
            )
        )
    if request.release:
        members.append(
            put(
                MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
                due_obligation_item(run, "release", request.recorded_at),
            )
        )
    return members


def _granted_run(run: RunRecord, signal: str, generation: int | None = None) -> Member:
    condition = "#state = :granted"
    values = _values(granted=write_text(RunState.GRANTED.value))
    if generation is not None:
        condition += " AND dispatch_generation = :gen"
        values[":gen"] = write_int(generation)
    return check(MemberSpec("", _run_key(run.run_id), signal, condition, values))


def grant_fence_checks(  # noqa: PLR0913 -- one cross-table fence, every identity explicit
    table: str,
    *,
    run_id: str,
    grant_id: str,
    grant_ordinal: int,
    node_id: str,
    lease_version: int,
) -> tuple[TransactWriteItemTypeDef, TransactWriteItemTypeDef]:
    """S6 §2.6: the flip's two graph-table members, bound to the graph table's name.

    Member 2 is the grant fence, a `ConditionCheck` on `RUN#<run_id>` (state `GRANTED`, the grant
    and its ordinal); member 3 is the `EXEC_CLAIM#<node_id>` lease at the held version. The
    publications adapter adds them to its pointer update, so the graph key formats stay here.
    """
    run = check(
        MemberSpec(
            table,
            _run_key(run_id),
            RUN_STATE_CHANGED,
            "#state = :granted AND grant_id = :grant AND grant_ordinal = :ordinal",
            _values(
                granted=write_text(RunState.GRANTED.value),
                grant=write_text(grant_id),
                ordinal=write_int(grant_ordinal),
            ),
        )
    )
    lease = check(
        MemberSpec(
            table,
            _key(f"EXEC_CLAIM#{node_id}", "CLAIM"),
            CLAIM_LEASE_STALE,
            "lease_version = :version",
            _values(version=write_int(lease_version)),
        )
    )
    return run.item, lease.item


def build_fence_run(request: FenceRunRequest) -> list[Member]:
    """Release point 1 (r4): FENCING, generation + 1, slot freed, `PROMOTE#` woken, `FENCE#`."""
    run = request.run
    shard = due_shard(run.conflict_unit)
    members = [
        update(
            MemberSpec(
                "",
                _run_key(run.run_id),
                RUN_STATE_CHANGED,
                "(#state = :open OR #state = :paused) AND attribute_not_exists(grant_id) "
                "AND revision = :rev",
                _values(
                    fencing=write_text(RunState.FENCING.value),
                    open=write_text(RunState.OPEN.value),
                    paused=write_text(RunState.PAUSED.value),
                    one=write_int(1),
                    rev=write_int(run.revision),
                    reason=write_text(request.reason.value),
                    at=write_text(request.recorded_at),
                ),
                _run_signal,
            ),
            "SET #state = :fencing, dispatch_generation = dispatch_generation + :one, "
            "revision = revision + :one, fence_reason = :reason, fenced_at = :at",
        ),
        update(
            MemberSpec(
                "",
                _key(f"SLOT#{run.conflict_unit}", "SLOT"),
                SLOT_NOT_OWNED,
                "active_run_id = :run",
                _values(run=write_text(run.run_id), one=write_int(1)),
            ),
            "REMOVE active_run_id SET revision = revision + :one",
        ),
        update(
            MemberSpec(
                "",
                _key(shard, f"PROMOTE#{run.conflict_unit}"),
                PROMOTE_WAKE_CHANGED,
                None,
                _values(
                    one=write_int(1),
                    at=write_text(request.recorded_at),
                    held=write_bool(False),
                    unit=write_text(run.conflict_unit),
                    kind=write_text("promote"),
                    epoch=write_int(run.authority_epoch),
                ),
            ),
            "ADD wake_seq :one SET woken_at = :at, held = :held, conflict_unit = :unit, "
            "item_kind = :kind, authority_epoch = :epoch",
        ),
        put(
            MemberSpec("", {}, RUN_STATE_CHANGED, "attribute_not_exists(pk)"),
            {
                **_key(shard, f"FENCE#{run.run_id}"),
                "item_kind": write_text("fence"),
                "authority_epoch": write_int(run.authority_epoch),
                "run_id": write_text(run.run_id),
                "due_at": write_text(request.recorded_at),
                **root_clock_attribute(run.root_clock_started_at),
            },
        ),
    ]
    if request.receipt is not None:
        members.append(
            put(
                MemberSpec("", {}, RUN_STATE_CHANGED, "attribute_not_exists(pk)"),
                command_item(request.receipt),
            )
        )
    return members


def build_terminal(run: RunRecord, nodes: tuple[NodeRecord, ...]) -> list[Member]:
    """Build the run-terminal transaction: no slot member, `INSTANTIATE#` and `ARM#` absent.

    Every way a run ends passes through a release point first, so the terminal is reachable
    only from FENCING (V-25); a run end that bypasses both release points is refused here.
    """
    if run.state is not RunState.FENCING or run.fence_reason is None:
        raise ValueError("a run turns terminal only from FENCING (V-25).")
    shard = due_shard(run.conflict_unit)
    members = [
        update(
            MemberSpec(
                "",
                _run_key(run.run_id),
                RUN_STATE_CHANGED,
                "#state = :fencing AND revision = :rev",
                _values(
                    terminal=write_text(fence_terminal_state(run.fence_reason).value),
                    fencing=write_text(RunState.FENCING.value),
                    rev=write_int(run.revision),
                    one=write_int(1),
                ),
            ),
            "SET #state = :terminal, revision = revision + :one",
        ),
        check(
            MemberSpec(
                "",
                _key(shard, f"INSTANTIATE#{run.run_id}"),
                RUN_INSTANTIATION_OPEN,
                "attribute_not_exists(pk)",
            )
        ),
        _arming_absent(run),
        delete(MemberSpec("", _key(shard, f"FENCE#{run.run_id}"), RUN_STATE_CHANGED)),
    ]
    members.extend(
        check(
            MemberSpec(
                "",
                _node_key(node),
                NODE_STATE_CHANGED,
                "#state = :s AND revision = :rev",
                _values(s=write_text(node.state.value), rev=write_int(node.revision)),
            )
        )
        for node in nodes
    )
    return members


def _plain(item: Item) -> dict[str, object]:
    plain: dict[str, object] = {}
    for name, value in item.items():
        if "S" in value:
            plain[name] = str(value["S"])
        elif "N" in value:
            plain[name] = int(str(value["N"]))
        elif "BOOL" in value:
            plain[name] = bool(value["BOOL"])
    return plain


def _arm_batch_members(request: ArmUnitBatchRequest) -> list[Member]:
    """Build `arm_unit_batch`'s one transaction (§2.2), validating the grant first."""
    members = request.members

    if not members or len(members) > MAX_BATCH_MEMBERS:
        raise ValueError(f"a unit batch arms 1 to {MAX_BATCH_MEMBERS} members.")
    if not request.lock_key.startswith(TABLE_LOCK_PREFIX):
        raise ValueError("a unit batch holds a TABLE# lock.")
    head = members[0].run
    recorded = write_text(request.recorded_at)
    batch = write_text(request.batch_id)
    transaction: list[Member] = []
    runs: set[str] = set()
    for member in members:
        run, node = member.run, member.node
        if node.lock_key != request.lock_key or node.state is not NodeState.PENDING:
            raise ValueError("every batch member is a PENDING node of the batch's table.")
        transaction.append(
            _node_transition(
                node,
                NodeState.READY,
                {
                    "armed_generation": write_int(run.dispatch_generation),
                    "armed_at": recorded,
                    "batch_id": batch,
                },
            )
        )
        transaction.append(
            put(
                MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
                {
                    **_key(f"TOKEN#{node.node_id}", "TOKEN"),
                    "item_kind": write_text("token"),
                    "authority_epoch": write_int(run.authority_epoch),
                    "run_id": write_text(run.run_id),
                    "node_id": write_text(node.node_id),
                },
            )
        )
        if run.run_id in runs:
            continue
        runs.add(run.run_id)
        transaction.append(
            _open_run_at(run.run_id, run.dispatch_generation, signal=RUN_STATE_CHANGED, bump=True)
        )
        if run.run_id in request.retire_runs:
            transaction.append(
                delete(MemberSpec("", _arm_key(run), NODE_STATE_CHANGED, "attribute_exists(pk)"))
            )
    epoch = write_int(head.authority_epoch)
    transaction.append(
        put(
            MemberSpec(
                "",
                {},
                DOMAIN_LOCK_HELD,
                "attribute_not_exists(pk) OR open_members = :zero",
                _values(zero=write_int(0)),
            ),
            {
                **_lock_key_item(request.lock_key),
                "item_kind": write_text("lock"),
                "authority_epoch": epoch,
                "holder_batch_id": batch,
                "open_members": write_int(len(members)),
                "members": {"L": [write_text(item.node.node_id) for item in members]},
                "run_id": write_text(head.run_id),
            },
        )
    )
    transaction.append(
        put(
            MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
            {
                **_key(batch_row_shard(request.batch_id), f"OUTBOX#{request.batch_id}"),
                "item_kind": write_text("outbox"),
                "authority_epoch": epoch,
                "run_id": write_text(head.run_id),
                "node": write_text(WRITE_UNIT),
                "lock_key": write_text(request.lock_key),
                "batch_id": batch,
                "members": {"L": [_member_ref(item) for item in members]},
                "generation": write_int(head.dispatch_generation),
                "revision": write_int(1),
                "state": write_text(_OPEN),
                "due_at": recorded,
                **root_clock_attribute(head.root_clock_started_at),
            },
        )
    )
    return transaction


class DynamoGraphDispatchStore:
    """The graph dispatch authority over the live (or probe) execution-graph table."""

    def __init__(self, table: str, client: DynamoDBClient) -> None:
        self._table = table
        self._client = client

    # --- reads ----------------------------------------------------------------------------------

    def _get(self, key: Item) -> Item | None:
        try:
            found = self._client.get_item(TableName=self._table, Key=key, ConsistentRead=True)
        except (BotoCoreError, ClientError) as error:
            raise GraphStoreUnavailableError(
                f"execution-graph read failed: {type(error).__name__}"
            ) from None
        item = found.get("Item")
        return None if item is None else dict(item)

    def _query(self, pk: str, prefix: str) -> list[Item]:
        items: list[Item] = []
        start: dict[str, Any] | None = None
        while True:
            arguments: dict[str, Any] = {
                "TableName": self._table,
                "KeyConditionExpression": "pk = :pk AND begins_with(sk, :prefix)",
                "ExpressionAttributeValues": {":pk": write_text(pk), ":prefix": write_text(prefix)},
                "ConsistentRead": True,
            }
            if start is not None:
                arguments["ExclusiveStartKey"] = start
            try:
                page = self._client.query(**arguments)
            except (BotoCoreError, ClientError) as error:
                raise GraphStoreUnavailableError(
                    f"execution-graph query failed: {type(error).__name__}"
                ) from None
            items.extend(dict(item) for item in page.get("Items", []))
            start = cast("dict[str, Any] | None", page.get("LastEvaluatedKey"))
            if start is None:
                return items

    def _commit(self, members: list[Member]) -> None:
        transact(self._client, [self._bind(member) for member in members], _BOUND)

    def _bind(self, member: Member) -> Member:
        ((kind, body),) = cast("dict[str, dict[str, Any]]", member.item).items()
        bound = {**body, "TableName": self._table}
        return Member(cast("Any", {kind: bound}), member.signal, member.resolve)

    def read_mode(self) -> ModeState:
        """Read the mode item and its hold; absent reads LEGACY at revision 0."""
        return mode_state_from_item(self._get(_key(_MODE_PK, _MODE_SK)))

    def read_mode_item(self) -> dict[str, object] | None:
        """Read the exact mode item as plain attributes, for a cross-table ConditionCheck."""
        item = self._get(_key(_MODE_PK, _MODE_SK))
        return None if item is None else _plain(item)

    def read_claim_item(self, node_id: str) -> dict[str, object] | None:
        """S6 §2.5: read the exact `EXEC_CLAIM#` item as plain attributes for a Begin fence."""
        item = self._get(_key(f"EXEC_CLAIM#{node_id}", "CLAIM"))
        return None if item is None else _plain(item)

    def read_run(self, run_id: str) -> RunRecord | None:
        """Read one run record."""
        item = self._get(_run_key(run_id))
        return None if item is None else read_run_item(item)

    def read_nodes(self, run_id: str) -> tuple[NodeRecord, ...]:
        """Read every node a run has put, in name order."""
        return tuple(read_node_item(item) for item in self._query(f"RUN#{run_id}", "NODE#"))

    def read_claim(self, node_id: str) -> ClaimRecord | None:
        """Read a node's graph execution claim."""
        item = self._get(_key(f"EXEC_CLAIM#{node_id}", "CLAIM"))
        if item is None:
            return None
        return ClaimRecord(
            node_id=node_id,
            run_id=_req_text(item, "run_id"),
            lease_version=_req_int(item, "lease_version"),
            lease_expires_at=_req_text(item, "lease_expires_at"),
            dispatch_id=_req_text(item, "dispatch_id"),
            claimed_at=_req_text(item, "claimed_at"),
            queue_enqueued_at=_text(item, "queue_enqueued_at"),
            first_received_at=_text(item, "first_received_at"),
            table_id=_text(item, "table_id"),
            attempt_id=_text(item, "attempt_id"),
            root_flow_id=_text(item, "root_flow_id"),
            admission=_int(item, "admission") or 0,
            admitted_at=_text(item, "admitted_at"),
            materialization_began_at=_text(item, "materialization_began_at"),
            bake_selection_started=_bool(item, "bake_selection_started"),
            batch_id=_text(item, "batch_id"),
        )

    def read_due(self, shard: str, prefix: str) -> tuple[DueItem, ...]:
        """Read one due shard's items whose sort key starts with the prefix."""
        found: list[DueItem] = []
        for item in self._query(shard, prefix):
            sk = _req_text(item, "sk")
            scope = _text(item, "scope")
            found.append(
                DueItem(
                    shard=shard,
                    sk=sk,
                    kind=sk.split("#", 1)[0],
                    subject=sk.split("#", 1)[1],
                    due_at=_text(item, "due_at"),
                    wake_seq=_int(item, "wake_seq"),
                    held=bool(item.get("held", {}).get("BOOL", False)),
                    woken_at=_text(item, "woken_at"),
                    root_clock_started_at=_text(item, "root_clock_started_at"),
                    revision=_int(item, "revision"),
                    run_id=_text(item, "run_id"),
                    scope=None if scope is None else ArmScope(scope),
                    domain=_text(item, "domain"),
                    grant_id=_text(item, "grant_id"),
                    grant_ordinal=_int(item, "grant_ordinal"),
                    state=_text(item, "state"),
                    run_ids=_text_list(item, "run_ids"),
                )
            )
        return tuple(found)

    def read_serving_publication_authority(self, run_id: str) -> ServingPublicationAuthority:
        """Strongly resolve COMPLETE identity and the selected live-frontier shard."""
        run = self.read_run(run_id)
        if run is None or run.grant_id is None or run.grant_ordinal is None:
            raise GraphStoreUnavailableError("a serving publication run has no grant authority.")
        complete = self._get(_key(due_shard(run.conflict_unit), f"COMPLETE#{run_id}"))
        if complete is None:
            raise GraphStoreUnavailableError("a serving publication has no COMPLETE obligation.")
        domain = _req_text(complete, "domain")
        if (
            _req_text(complete, "grant_id") != run.grant_id
            or _req_int(complete, "grant_ordinal") != run.grant_ordinal
        ):
            raise GraphStoreUnavailableError("a COMPLETE obligation names another grant.")
        shard = serving_frontier_shard(run_id)
        frontier = self._get(_serving_frontier_key(domain, shard))
        if frontier is None:
            raise GraphStoreUnavailableError("serving publication frontier is incomplete.")
        return ServingPublicationAuthority(
            run_id=run_id,
            grant_id=run.grant_id,
            grant_ordinal=run.grant_ordinal,
            domain=domain,
            shard=shard,
            shard_revision=_req_int(frontier, "revision"),
            visible_count=_req_int(frontier, "visible_count"),
            eligible_count=_req_int(frontier, "eligible_count"),
        )

    def build_publication_flip_participant(  # noqa: PLR0913 -- exact flip authority inputs
        self,
        authority: ServingPublicationAuthority,
        *,
        root_flow_id: str,
        target_digest: str,
        flipped_at: str,
    ) -> tuple[tuple[TransactWriteItemTypeDef, ...], Item, Item]:
        """Build the COMPLETE/frontier-fenced visible-marker transaction participant."""
        run = self.read_run(authority.run_id)
        if run is None:
            raise GraphStoreUnavailableError("a serving publication run disappeared.")
        marker: Item = {
            **_serving_marker_key(authority.run_id),
            "item_kind": write_text("serving_publication"),
            "state": write_text("VISIBLE_UNSETTLED"),
            **serving_authority_identity_attributes(authority.identity),
            "root_flow_id": write_text(root_flow_id),
            "target_digest": write_text(target_digest),
            "frontier_shard": write_int(authority.shard),
            "flipped_at": write_text(flipped_at),
        }
        members = [
            check(
                MemberSpec(
                    "",
                    _key(due_shard(run.conflict_unit), f"COMPLETE#{authority.run_id}"),
                    NODE_STATE_CHANGED,
                    "grant_id = :grant AND grant_ordinal = :ordinal AND #domain = :domain",
                    _values(
                        grant=write_text(authority.grant_id),
                        ordinal=write_int(authority.grant_ordinal),
                        domain=write_text(authority.domain),
                    ),
                )
            ),
            put(MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"), marker),
            update(
                MemberSpec(
                    "",
                    _serving_frontier_key(authority.domain, authority.shard),
                    NODE_STATE_CHANGED,
                    "revision = :revision AND visible_count = :visible "
                    "AND eligible_count = :eligible",
                    _values(
                        revision=write_int(authority.shard_revision),
                        visible=write_int(authority.visible_count),
                        eligible=write_int(authority.eligible_count),
                        one=write_int(1),
                    ),
                ),
                "ADD visible_count :one SET revision = revision + :one",
            ),
        ]
        bound = tuple(self._bind(member).item for member in members)
        return bound, _serving_marker_key(authority.run_id), marker

    def read_outbox(self, shard: str, node_id: str) -> OutboxRecord | None:
        """Read one outbox row."""
        item = self._get(_key(shard, f"OUTBOX#{node_id}"))
        if item is None:
            return None
        return OutboxRecord(
            shard=shard,
            node_id=node_id,
            run_id=_req_text(item, "run_id"),
            node_name=_req_text(item, "node"),
            generation=_req_int(item, "generation"),
            revision=_req_int(item, "revision"),
            state=_req_text(item, "state"),
            members=_batch_members(item),
            permitted=_text_list(item, "permitted"),
            lock_key=_text(item, "lock_key"),
        )

    def read_promotion(self, conflict_unit: str) -> PromotionRead:
        """Read `PROMOTE#`, `SLOT#`, `WAITER#`, `HEAD#` and the hold for one unit."""
        shard = due_shard(conflict_unit)
        promote = self._get(_key(shard, f"PROMOTE#{conflict_unit}"))
        slot = self._get(_key(f"SLOT#{conflict_unit}", "SLOT"))
        waiter_item = self._get(_key(shard, f"WAITER#{conflict_unit}"))
        head_item = self._get(_key(f"HEAD#{conflict_unit}", "HEAD"))
        waiter = None
        if waiter_item is not None:
            waiter = Waiter(
                request_id=_req_text(waiter_item, "request_id"),
                fingerprint=_req_text(waiter_item, "fingerprint"),
                r=datetime.fromisoformat(_req_text(waiter_item, "retrieved_at")),
                held=bool(waiter_item.get("held", {}).get("BOOL", False)),
                revision=_req_int(waiter_item, "revision"),
                window=_text(waiter_item, "window"),
                root_clock_started_at=_text(waiter_item, "root_clock_started_at"),
            )
        head = None if head_item is None else _text(head_item, "retrieved_at")
        blocked = self._get(_key(f"BLOCK#{INGESTION_RELEASE_DOMAIN}", "BLOCK")) is not None
        mode = self.read_mode()
        return PromotionRead(
            conflict_unit=conflict_unit,
            wake_seq=None if promote is None else _int(promote, "wake_seq"),
            slot_revision=0 if slot is None else _int(slot, "revision") or 0,
            slot_run_id=None if slot is None else _text(slot, "active_run_id"),
            waiter=waiter,
            head=None if head is None else datetime.fromisoformat(head),
            hold=mode.hold,
            hold_revision=mode.hold_revision,
            blocked=blocked,
        )

    def read_command(self, command_id: str) -> CommandReceipt | None:
        """Read one command receipt, so an exact replay returns it (S3V-12)."""
        item = self._get(_key(f"COMMAND#{command_id}", "COMMAND"))
        if item is None:
            return None
        details = item.get("details", {}).get("M", {})
        return CommandReceipt(
            command_id=command_id,
            command=_req_text(item, "command"),
            principal=_req_text(item, "principal"),
            authority_epoch=_req_int(item, "authority_epoch"),
            mode=AuthorityMode(_req_text(item, "mode")),
            outcome=_req_text(item, "outcome"),
            recorded_at=_req_text(item, "recorded_at"),
            protocol_version=_req_text(item, "protocol_version"),
            details={str(name): str(value.get("S", "")) for name, value in details.items()},
        )

    def read_receipt(self, request_id: str) -> ReceiptRecord | None:
        """Read one decision receipt (the promoter reads `W`'s identity from it)."""
        item = self._get(_key(f"RECEIPT#{request_id}", "RECEIPT"))
        return None if item is None else read_receipt_item(item)

    def instantiation_open(self, run: RunRecord) -> bool:
        """Whether the run's `INSTANTIATE#` obligation still exists."""
        shard = due_shard(run.conflict_unit)
        return self._get(_key(shard, f"INSTANTIATE#{run.run_id}")) is not None

    # --- instantiation (V-26) -------------------------------------------------------------------

    def put_nodes(self, request: PutNodesRequest) -> None:
        """Put the graph and the missing nodes under one RUN# state-and-generation check."""
        run = request.run
        members: list[Member] = [
            check(
                MemberSpec(
                    "",
                    _run_key(run.run_id),
                    RUN_NOT_INSTANTIABLE,
                    "(#state = :open OR #state = :paused) AND dispatch_generation = :gen",
                    _values(
                        open=write_text(RunState.OPEN.value),
                        paused=write_text(RunState.PAUSED.value),
                        gen=write_int(run.dispatch_generation),
                    ),
                )
            )
        ]
        if self._get(_key(f"GRAPH#{run.graph_id}", "GRAPH")) is None:
            members.append(
                put(
                    MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
                    {
                        **_key(f"GRAPH#{run.graph_id}", "GRAPH"),
                        "item_kind": write_text("graph"),
                        "state": write_text("INSTANTIATED"),
                        "authority_epoch": write_int(run.authority_epoch),
                        "template": write_text(run.template),
                        "nodes": {"L": [write_text(spec.name) for spec in request.nodes]},
                        "fingerprint": write_text(run.fingerprint),
                        "root_flow_id": write_text(run.root_flow_id),
                        "receipt_request_id": write_text(run.receipt_request_id),
                        "run_id": write_text(run.run_id),
                    },
                )
            )
        members.extend(
            put(
                MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
                _node_item(run, spec, request.recorded_at),
            )
            for spec in request.nodes
        )
        self._commit(members)

    def arm_roots(self, request: ArmRequest) -> None:
        """Arm the roots and delete `INSTANTIATE#` in one transaction (r4, R3-7).

        The `RUN#` member bumps the run revision (S3V-6): a resume that read the run before this
        arming commits is then refused on its revision and re-reads, so it re-arms these outbox
        rows under its new generation instead of stranding them on the paused one.
        """
        run = request.run
        shard = due_shard(run.conflict_unit)
        gen = write_int(run.dispatch_generation)
        members: list[Member] = [
            update(
                MemberSpec(
                    "",
                    _run_key(run.run_id),
                    RUN_NOT_INSTANTIABLE,
                    "(#state = :open OR #state = :paused) AND dispatch_generation = :gen",
                    _values(
                        open=write_text(RunState.OPEN.value),
                        paused=write_text(RunState.PAUSED.value),
                        gen=gen,
                        one=write_int(1),
                    ),
                ),
                "SET revision = revision + :one",
            )
        ]
        for node in request.roots:
            members.extend(arming_members(run, node, request.recorded_at))
        members.append(
            delete(
                MemberSpec(
                    "",
                    _key(shard, f"INSTANTIATE#{run.run_id}"),
                    NODE_STATE_CHANGED,
                    "attribute_exists(pk)",
                )
            )
        )
        self._commit(members)

    def settle_fenced_instantiation(self, request: ArmRequest) -> None:
        """Settle a fenced run's put nodes `FENCED_UNSENT` and delete `INSTANTIATE#`."""
        run = request.run
        shard = due_shard(run.conflict_unit)
        members: list[Member] = [_not_live_run(run.run_id)]
        members.extend(
            _node_transition(node, NodeState.FENCED_UNSENT, {})
            for node in request.roots
            if node.state is NodeState.PENDING
        )
        members.append(
            delete(
                MemberSpec(
                    "",
                    _key(shard, f"INSTANTIATE#{run.run_id}"),
                    NODE_STATE_CHANGED,
                    "attribute_exists(pk)",
                )
            )
        )
        self._commit(members)

    # --- successors and the arm pass (S6 §2.2) --------------------------------------------------

    def put_successors(self, request: SuccessorRequest) -> None:
        """Commit `build_successors` alone, under the run's state check for the scope.

        A settlement joins `build_successors` to its own transaction instead; this standalone
        call is for a run whose settlement already committed (and for the store-level tests).
        """
        run = request.run
        if request.scope is ArmScope.SERVING:
            run_member = _granted_run(run, RUN_STATE_CHANGED)
        else:
            run_member = _live_run(run.run_id, RUN_STATE_CHANGED)
        self._commit([run_member, *build_successors(request)])

    def arm_node(self, request: ArmNodeRequest) -> None:
        """Arm one PENDING node, mirroring `arm_roots` (one node per transaction).

        The `RUN#` member conditions the state the node's phase needs and the generation read,
        and bumps the revision, so a racing resume re-reads and re-arms under its new generation.
        A held domain lock is `DOMAIN_LOCK_HELD` (a lock wait). When this is the scope's last
        PENDING routed node, `ARM#` is deleted in the same transaction (AGG4-1).
        """
        run, node = request.run, request.node
        gen = write_int(run.dispatch_generation)
        if node.phase is NodePhase.POST_GRANT:
            condition = "#state = :granted AND dispatch_generation = :gen"
            values = _values(granted=write_text(RunState.GRANTED.value), gen=gen, one=write_int(1))
        else:
            condition = "(#state = :open OR #state = :paused) AND dispatch_generation = :gen"
            values = _values(
                open=write_text(RunState.OPEN.value),
                paused=write_text(RunState.PAUSED.value),
                gen=gen,
                one=write_int(1),
            )
        members: list[Member] = [
            update(
                MemberSpec("", _run_key(run.run_id), RUN_STATE_CHANGED, condition, values),
                "SET revision = revision + :one",
            ),
            *arming_members(run, node, request.recorded_at),
        ]
        if request.retire:
            members.append(
                delete(MemberSpec("", _arm_key(run), NODE_STATE_CHANGED, "attribute_exists(pk)"))
            )
        self._commit(members)

    def retire_arm(self, run: RunRecord, scope: ArmScope) -> None:
        """Delete `ARM#` alone for a run that is no longer live for its scope.

        The fence settler's `fence_pending` then settles the PENDING nodes, which it refuses
        while `ARM#` exists (`RUN_ARMING_OPEN`).
        """
        if scope is ArmScope.SERVING:
            not_live = check(
                MemberSpec(
                    "",
                    _run_key(run.run_id),
                    RUN_STATE_CHANGED,
                    "#state <> :granted",
                    _values(granted=write_text(RunState.GRANTED.value)),
                )
            )
        else:
            not_live = _not_live_run(run.run_id)
        self._commit(
            [
                delete(MemberSpec("", _arm_key(run), NODE_STATE_CHANGED, "attribute_exists(pk)")),
                not_live,
            ]
        )

    # --- dispatch (BF-2) ------------------------------------------------------------------------

    def acquire_permit(self, run: RunRecord, node: NodeRecord, outbox: OutboxRecord) -> None:
        """Acquire the send permit, or raise `PLAN_DISPATCH_FENCED` (BF-2).

        S6 §2.7 (D13): the `RUN#` check branches on the node's phase. A PRE_GRANT node needs
        `OPEN`; a POST_GRANT node needs `GRANTED`. Both need the generation the row was armed
        under, which cannot move after a grant (a fence needs no grant, a pause needs `OPEN`).
        """
        gen = write_int(outbox.generation)
        if node.phase is NodePhase.POST_GRANT:
            run_member = _granted_run(run, PLAN_DISPATCH_FENCED, outbox.generation)
        else:
            run_member = check(
                MemberSpec(
                    "",
                    _run_key(run.run_id),
                    PLAN_DISPATCH_FENCED,
                    "#state = :open AND dispatch_generation = :gen",
                    _values(open=write_text(RunState.OPEN.value), gen=gen),
                )
            )
        self._commit(
            [
                run_member,
                _node_transition(node, NodeState.PERMITTED, {"permit_generation": gen}),
                update(
                    MemberSpec(
                        "",
                        _key(outbox.shard, f"OUTBOX#{outbox.node_id}"),
                        PLAN_DISPATCH_FENCED,
                        "#state = :open AND revision = :rev AND generation = :gen",
                        _values(
                            open=write_text(_OPEN),
                            sending=write_text(_SENDING),
                            rev=write_int(outbox.revision),
                            gen=gen,
                            one=write_int(1),
                        ),
                    ),
                    "SET #state = :sending, revision = revision + :one",
                ),
            ]
        )

    def record_send(self, receipt: SendReceipt) -> None:
        """Close the outbox row with the exact enqueue-attempt receipt."""
        node, outbox = receipt.node, receipt.outbox
        sets: dict[str, AttributeValueTypeDef] = {
            "message_id": write_text(receipt.message_id),
            "sent_at": write_text(receipt.sent_at),
        }
        if node.state is NodeState.PERMITTED:
            node_member = _node_transition(node, NodeState.DISPATCHED, sets)
        else:
            node_member = update(
                MemberSpec(
                    "",
                    _node_key(node),
                    NODE_STATE_CHANGED,
                    "dispatch_id = :dispatch",
                    _values(dispatch=write_text(node.dispatch_id), **sets),
                ),
                "SET message_id = :message_id, sent_at = :sent_at",
            )
        self._commit(
            [
                node_member,
                delete(
                    MemberSpec(
                        "",
                        _key(outbox.shard, f"OUTBOX#{outbox.node_id}"),
                        NODE_STATE_CHANGED,
                        "#state = :sending AND revision = :rev",
                        _values(sending=write_text(_SENDING), rev=write_int(outbox.revision)),
                    )
                ),
            ]
        )

    def fail_send(self, settlement: NodeSettlement, fence: FenceRunRequest | None) -> None:
        """Fail the node immutably on a provider oversize rejection, fencing a live run.

        With no fence the run already left OPEN/PAUSED (cancelled, replaced or granted), so it
        has no slot to release (S3V-5): the node still fails, its outbox row, lock and token go,
        and the transaction ConditionChecks that the run is still not live.
        """
        node = settlement.node
        if is_write_unit(node.name):
            # Slice 7 (D23's batch rule): a unit batch's oversize is an invariant on its row;
            # a `write_unit` node is never failed by a send.
            raise InvariantError(
                GRAPH_UNIT_BATCH_OVERSIZE_CODE,
                "a write_unit node reached the per-node send failure path.",
                context={"node_id": node.node_id},
            )
        members = [
            _node_transition(
                node,
                NodeState.FAILED,
                {"failure_reason": write_text(PROVIDER_PAYLOAD_OVERSIZE)},
            ),
            *_release_members(node),
            delete(
                MemberSpec(
                    "",
                    _key(due_shard(settlement.run.conflict_unit), f"OUTBOX#{node.node_id}"),
                    NODE_STATE_CHANGED,
                    "attribute_exists(pk)",
                )
            ),
            *(build_fence_run(fence) if fence is not None else [_not_live_run(node.run_id)]),
        ]
        self._commit(members)

    # --- claim (V-9, V-10) ----------------------------------------------------------------------

    def _run_claim_member(self, request: ClaimRequest) -> Member:
        run = request.run
        if request.node.phase is NodePhase.POST_GRANT:
            # S4: a POST_GRANT node is claimable only on its GRANTED run (claim predicate, r3).
            return check(
                MemberSpec(
                    "",
                    _run_key(run.run_id),
                    RUN_STATE_CHANGED,
                    "#state = :granted",
                    _values(granted=write_text(RunState.GRANTED.value)),
                )
            )
        if run.compute_started_at is None and request.claim is None:
            return update(
                MemberSpec(
                    "",
                    _run_key(run.run_id),
                    RUN_STATE_CHANGED,
                    "(#state = :open OR #state = :paused) "
                    "AND attribute_not_exists(compute_started_at)",
                    _values(
                        open=write_text(RunState.OPEN.value),
                        paused=write_text(RunState.PAUSED.value),
                        at=write_text(request.claimed_at),
                        one=write_int(1),
                    ),
                ),
                "SET compute_started_at = :at, revision = revision + :one",
            )
        return _live_run(run.run_id, RUN_STATE_CHANGED)

    def claim(self, request: ClaimRequest) -> ClaimRecord:
        """Commit a first claim or a reclaim; the first claim sets `compute_started_at`."""
        node, existing = request.node, request.claim
        claim_key = _key(f"EXEC_CLAIM#{node.node_id}", "CLAIM")
        version = 1 if existing is None else existing.lease_version + 1
        # S6 (§2.10): CLAIM is admission 1 and each RECLAIM adds one; a lease bump does not.
        record = ClaimRecord(
            node_id=node.node_id,
            run_id=node.run_id,
            lease_version=version,
            lease_expires_at=request.lease_expires_at,
            dispatch_id=node.dispatch_id,
            claimed_at=request.claimed_at if existing is None else existing.claimed_at,
            queue_enqueued_at=request.queue_enqueued_at
            if existing is None
            else existing.queue_enqueued_at,
            first_received_at=request.first_received_at
            if existing is None
            else existing.first_received_at,
            root_flow_id=request.run.root_flow_id if existing is None else existing.root_flow_id,
            admission=1 if existing is None else existing.admission + 1,
            admitted_at=request.claimed_at,
            materialization_began_at=None
            if existing is None
            else existing.materialization_began_at,
            # S6 §2.8 step 6: a RECLAIM bumps the lease in place, so recorded progress survives.
            bake_selection_started=False if existing is None else existing.bake_selection_started,
            batch_id=request.batch_id,
        )
        if existing is None:
            claim_member = put(
                MemberSpec("", {}, CLAIM_LEASE_LIVE, "attribute_not_exists(pk)"),
                {
                    **claim_key,
                    "item_kind": write_text("claim"),
                    "authority_epoch": write_int(request.run.authority_epoch),
                    "root_flow_id": write_text(request.run.root_flow_id),
                    "run_id": write_text(node.run_id),
                    # S6 §2.10: the claimed node's name, so the CLAIM projection row can name its
                    # kind (the `ActivityAdmissionElapsed` Stage and the N1 stage mapping).
                    "node": write_text(node.name),
                    "lease_version": write_int(version),
                    "lease_expires_at": write_text(record.lease_expires_at),
                    "dispatch_id": write_text(node.dispatch_id),
                    "claimed_at": write_text(record.claimed_at),
                    "admission": write_int(1),
                    "admitted_at": write_text(request.claimed_at),
                    **(
                        {}
                        if request.batch_id is None
                        else {"batch_id": write_text(request.batch_id)}
                    ),
                    **{
                        name: write_text(value)
                        for name, value in (
                            ("queue_enqueued_at", record.queue_enqueued_at),
                            ("first_received_at", record.first_received_at),
                        )
                        if value is not None
                    },
                },
            )
            node_member = update(
                MemberSpec(
                    "",
                    _node_key(node),
                    NODE_STATE_CHANGED,
                    "(#state = :permitted OR #state = :dispatched) AND dispatch_id = :dispatch "
                    "AND revision = :rev",
                    _values(
                        claimed=write_text(NodeState.CLAIMED.value),
                        permitted=write_text(NodeState.PERMITTED.value),
                        dispatched=write_text(NodeState.DISPATCHED.value),
                        dispatch=write_text(node.dispatch_id),
                        rev=write_int(node.revision),
                        one=write_int(1),
                        present=write_bool(True),
                    ),
                ),
                "SET #state = :claimed, revision = revision + :one, "
                "claim_record_present = :present",
            )
        elif node.state in SENT_NODE_STATES:
            # Slice 7 (P1): a member returned and re-armed is PERMITTED or DISPATCHED again with
            # its claim kept, so the RECLAIM also moves the node back to CLAIMED, with the first
            # claim's condition, as the re-admission after a return.
            claim_member = self._lease_bump(
                existing,
                request.lease_expires_at,
                admitted_at=request.claimed_at,
                batch_id=request.batch_id,
            )
            node_member = update(
                MemberSpec(
                    "",
                    _node_key(node),
                    NODE_STATE_CHANGED,
                    "(#state = :permitted OR #state = :dispatched) AND dispatch_id = :dispatch "
                    "AND revision = :rev",
                    _values(
                        claimed=write_text(NodeState.CLAIMED.value),
                        permitted=write_text(NodeState.PERMITTED.value),
                        dispatched=write_text(NodeState.DISPATCHED.value),
                        dispatch=write_text(node.dispatch_id),
                        rev=write_int(node.revision),
                        one=write_int(1),
                    ),
                ),
                "SET #state = :claimed, revision = revision + :one",
            )
        else:
            claim_member = self._lease_bump(
                existing,
                request.lease_expires_at,
                admitted_at=request.claimed_at,
                batch_id=request.batch_id,
            )
            node_member = check(
                MemberSpec(
                    "",
                    _node_key(node),
                    NODE_STATE_CHANGED,
                    "#state = :claimed",
                    _values(claimed=write_text(NodeState.CLAIMED.value)),
                )
            )
        self._commit([claim_member, node_member, self._run_claim_member(request)])
        return record

    @staticmethod
    def _lease_bump(  # noqa: PLR0913 -- the one lease bump, admission and batch keyword-only
        claim: ClaimRecord,
        expires_at: str,
        *,
        admitted_at: str | None = None,
        batch_id: str | None = None,
    ) -> Member:
        """Bump the claim lease version; every graph-envelope BeginTableAttempt checks it.

        A RECLAIM passes `admitted_at`, so the admission counter rises with it (S6 §2.10); a
        takeover's or a settlement's bump is not an admission and leaves the counter alone.
        """
        values = _values(
            version=write_int(claim.lease_version),
            one=write_int(1),
            expires=write_text(expires_at),
        )
        expression = "SET lease_version = lease_version + :one, lease_expires_at = :expires"
        if admitted_at is not None and batch_id is not None:
            # Slice 7 (F11): each admission of a batch member names the batch it ran in.
            values[":batch"] = write_text(batch_id)
            expression += ", batch_id = :batch"
        if admitted_at is not None:
            values[":admitted"] = write_text(admitted_at)
            expression += ", admitted_at = :admitted ADD admission :one"
        return update(
            MemberSpec(
                "",
                _key(f"EXEC_CLAIM#{claim.node_id}", "CLAIM"),
                CLAIM_LEASE_LIVE,
                "lease_version = :version",
                values,
            ),
            expression,
        )

    def release_claim_lease(
        self,
        claim: ClaimRecord,
        released_at: str,
        progress: Mapping[str, int] | None = None,
    ) -> None:
        """Expire the held lease now: one `UpdateItem` conditioned on the held lease version.

        S6 §2.3: a claimer that returns a message unacknowledged releases its lease first, so the
        redelivery reads `RECLAIM` instead of spending a receive as `LIVE_DUPLICATE`. A
        concurrent reclaimer bumped the version, so a late release is refused with
        `CLAIM_LEASE_STALE` and changes nothing. S6 §2.6: `progress` (`publish_staged`,
        `publish_total`) is set in the same write.
        """
        values = _values(released=write_text(released_at), version=write_int(claim.lease_version))
        expression = "SET lease_expires_at = :released"
        for name, count in sorted((progress or {}).items()):
            if name not in _PROGRESS_FIELDS:
                raise ValueError(f"{name} is not a claim progress count.")
            values[f":{name}"] = write_int(count)
            expression += f", {name} = :{name}"
        try:
            self._client.update_item(
                TableName=self._table,
                Key=_key(f"EXEC_CLAIM#{claim.node_id}", "CLAIM"),
                UpdateExpression=expression,
                ConditionExpression="lease_version = :version",
                ExpressionAttributeValues=values,
            )
        except ClientError as error:
            code = cast("dict[str, Any]", error.response).get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                raise GraphGuardError(CLAIM_LEASE_STALE) from None
            raise GraphStoreUnavailableError(
                f"execution-graph lease release failed: {code}"
            ) from None
        except BotoCoreError as error:
            raise GraphStoreUnavailableError(
                f"execution-graph lease release failed: {type(error).__name__}"
            ) from None

    def mark_bake_selection(self, claim: ClaimRecord) -> None:
        """Plan 4.3 (DB21): record, lease-fenced, that the fan-out's selection pass began.

        One direct `UpdateItem` on `EXEC_CLAIM#<node_id>` before the first CURRENT read, so a
        process loss between selection and the pin is provable on the next receive. A refused
        condition is `CLAIM_LEASE_STALE`: the claim is lost.
        """
        try:
            self._client.update_item(
                TableName=self._table,
                Key=_key(f"EXEC_CLAIM#{claim.node_id}", "CLAIM"),
                UpdateExpression="SET bake_selection_started = :yes",
                ConditionExpression="lease_version = :version",
                ExpressionAttributeValues=_values(
                    version=write_int(claim.lease_version), yes=write_bool(True)
                ),
            )
        except ClientError as error:
            code = cast("dict[str, Any]", error.response).get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                raise GraphGuardError(CLAIM_LEASE_STALE) from None
            raise GraphStoreUnavailableError(
                f"execution-graph bake selection mark failed: {code}"
            ) from None
        except BotoCoreError as error:
            raise GraphStoreUnavailableError(
                f"execution-graph bake selection mark failed: {type(error).__name__}"
            ) from None

    def armed_node_members(self, run: RunRecord, spec: NodeSpec, recorded_at: str) -> list[Member]:
        """Put one node already armed (plan 4.6): READY, token, lock and its open outbox.

        The shape `arming_members` gives an existing node, for a node the fan-out creates.
        """
        node = _node_item(run, spec, recorded_at)
        node["state"] = write_text(NodeState.READY.value)
        node["armed_generation"] = write_int(run.dispatch_generation)
        node["armed_at"] = write_text(recorded_at)
        record = read_node_item(node)
        return [
            put(MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"), node),
            *arming_members(run, record, recorded_at)[1:],
        ]

    def pending_node_member(self, run: RunRecord, spec: NodeSpec, recorded_at: str) -> Member:
        """Put one node PENDING, as instantiation and successors do."""
        return put(
            MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
            _node_item(run, spec, recorded_at),
        )

    def await_provider_terminal(self, request: ClaimRequest) -> None:
        """Takeover with an open attempt (r5, R4-9): park the node under a `VERIFY#`."""
        claim, node = request.claim, request.node
        if claim is None:
            raise GraphStoreUnavailableError("a takeover needs the existing claim.")
        shard = due_shard(request.run.conflict_unit)
        self._commit(
            [
                self._lease_bump(claim, request.lease_expires_at),
                _node_transition(node, NodeState.AWAITING_PROVIDER_TERMINAL, {}),
                put(
                    MemberSpec("", {}, NODE_STATE_CHANGED, "attribute_not_exists(pk)"),
                    {
                        **_key(shard, f"VERIFY#{node.node_id}"),
                        "item_kind": write_text("verify"),
                        "authority_epoch": write_int(request.run.authority_epoch),
                        "run_id": write_text(node.run_id),
                        "node": write_text(node.name),
                        "due_at": write_text(request.claimed_at),
                        **root_clock_attribute(request.run.root_clock_started_at),
                    },
                ),
                _not_live_run(node.run_id),
            ]
        )

    def _settle_members(self, settlement: NodeSettlement) -> list[Member]:
        """`settle_node`'s members: node transition, token and lock releases, lease bump."""
        node = settlement.node
        sets: dict[str, AttributeValueTypeDef] = {"settled_at": write_text(settlement.recorded_at)}
        for name, value in (
            ("effect_digest", settlement.effect_digest),
            ("failure_reason", settlement.failure_reason),
            ("effect_record", settlement.effect_record),
            ("failure_code", settlement.failure_code),
        ):
            if value is not None:
                sets[name] = write_text(value)
        members = [_node_transition(node, settlement.state, sets), *_release_members(node)]
        if settlement.claim is not None:
            members.append(self._lease_bump(settlement.claim, settlement.recorded_at))
        return members

    def reattest_materialization(self, claim: ClaimRecord, began_at: str) -> None:
        """S6 §2.4 (AGG3-17): the FR-5 re-attestation as a two-member transaction.

        An `UpdateItem` on `EXEC_CLAIM#<node_id>` conditioned on the held lease version sets
        `materialization_began_at` (a claim that already carries it, from an earlier receive,
        keeps its first instant: the member is then a ConditionCheck on the same version), and
        a `ConditionCheck` on `RUN#` requires state OPEN or PAUSED. One item's condition cannot
        check another item, so the run needs its own member.
        """
        key = _key(f"EXEC_CLAIM#{claim.node_id}", "CLAIM")
        values = _values(version=write_int(claim.lease_version))
        if claim.materialization_began_at is None:
            values[":began"] = write_text(began_at)
            claim_member = update(
                MemberSpec("", key, CLAIM_LEASE_STALE, "lease_version = :version", values),
                "SET materialization_began_at = :began",
            )
        else:
            claim_member = check(
                MemberSpec("", key, CLAIM_LEASE_STALE, "lease_version = :version", values)
            )
        self._commit([claim_member, _live_run(claim.run_id, RUN_STATE_CHANGED)])

    def settle_materialization(self, request: MaterializationSettlement) -> None:
        """S6 §2.4: the `materialize_raw` settlement, lease-fenced by `settle_node`'s bump.

        `SUCCEEDED`: `settle_node`'s 4 members, a new `RUN#` check (state OPEN or PAUSED and the
        read dispatch generation), one PENDING `write_unit:<k>` per roster unit, `ARM#` (scope
        `units`) when the roster is not empty, and `RELEASE#`: at most 5 + 11 + 2 = 18 items.
        `FAILED`: `settle_node`'s members plus `build_fence_run` while the run is live, or a
        check that it is not live (the fence settler then terminalizes it).
        """
        settlement = request.settlement
        run = settlement.run
        members = self._settle_members(settlement)
        if request.successors is not None:
            members.append(
                check(
                    MemberSpec(
                        "",
                        _run_key(run.run_id),
                        RUN_STATE_CHANGED,
                        "(#state = :open OR #state = :paused) AND dispatch_generation = :gen",
                        _values(
                            open=write_text(RunState.OPEN.value),
                            paused=write_text(RunState.PAUSED.value),
                            gen=write_int(run.dispatch_generation),
                        ),
                    )
                )
            )
            members.extend(build_successors(request.successors))
        elif request.fence is not None:
            members.extend(build_fence_run(request.fence))
        else:
            members.append(_not_live_run(run.run_id))
        self._commit(members)

    def settle_publish(
        self, request: ServingPublicationSettlementRequest
    ) -> ServingPublicationMutationResult:
        """Settle publish and freeze its one cumulative frontier coordinate atomically."""
        settlement, successors, authority = (
            request.settlement,
            request.successors,
            request.authority,
        )
        run = settlement.run
        if settlement.state is not NodeState.SUCCEEDED or run.grant_id is None:
            raise ValueError("a publish settlement is SUCCEEDED on a granted run.")
        marker_key = _serving_marker_key(run.run_id)
        existing = self._get(marker_key)
        if (
            existing is not None
            and _text(existing, "state") == "SETTLED_ELIGIBLE"
            and _text(existing, "target_digest") == request.target_digest
        ):
            return ServingPublicationMutationResult.REPLAY
        if authority.eligible_count >= authority.visible_count:
            return ServingPublicationMutationResult.FRONTIER_RACE
        members = self._settle_members(settlement)
        members.append(
            check(
                MemberSpec(
                    "",
                    _run_key(run.run_id),
                    RUN_STATE_CHANGED,
                    "#state = :granted AND grant_id = :grant",
                    _values(
                        granted=write_text(RunState.GRANTED.value),
                        grant=write_text(run.grant_id),
                    ),
                )
            )
        )
        successor_members = build_successors(successors)
        for member in successor_members:
            put_body = cast("dict[str, Any]", member.item).get("Put")
            if put_body is not None and str(
                put_body.get("Item", {}).get("sk", {}).get("S", "")
            ).startswith("ARM#"):
                put_body["Item"].update(serving_authority_identity_attributes(authority.identity))
        members.extend(successor_members)
        next_revision = authority.shard_revision + 1
        next_eligible = authority.eligible_count + 1
        members.extend(
            [
                update(
                    MemberSpec(
                        "",
                        marker_key,
                        NODE_STATE_CHANGED,
                        "#state = :visible AND run_id = :run AND grant_id = :grant "
                        "AND grant_ordinal = :ordinal AND #domain = :domain "
                        "AND target_digest = :digest",
                        _values(
                            visible=write_text("VISIBLE_UNSETTLED"),
                            settled=write_text("SETTLED_ELIGIBLE"),
                            run=write_text(authority.run_id),
                            grant=write_text(authority.grant_id),
                            ordinal=write_int(authority.grant_ordinal),
                            domain=write_text(authority.domain),
                            digest=write_text(request.target_digest),
                            at=write_text(settlement.recorded_at),
                            revision=write_int(next_revision),
                            eligible=write_int(next_eligible),
                        ),
                    ),
                    "SET #state = :settled, settled_at = :at, "
                    "eligible_revision = :revision, eligible_coordinate = :eligible",
                ),
                update(
                    MemberSpec(
                        "",
                        _serving_frontier_key(authority.domain, authority.shard),
                        NODE_STATE_CHANGED,
                        "revision = :revision AND visible_count = :visible "
                        "AND eligible_count = :eligible",
                        _values(
                            revision=write_int(authority.shard_revision),
                            visible=write_int(authority.visible_count),
                            eligible=write_int(authority.eligible_count),
                            one=write_int(1),
                        ),
                    ),
                    "ADD eligible_count :one SET revision = revision + :one",
                ),
            ]
        )
        try:
            self._commit(members)
        except GraphGuardError:
            replay = self._get(marker_key)
            if (
                replay is not None
                and _text(replay, "state") == "SETTLED_ELIGIBLE"
                and _text(replay, "target_digest") == request.target_digest
            ):
                return ServingPublicationMutationResult.REPLAY
            frontier = self._get(_serving_frontier_key(authority.domain, authority.shard))
            if frontier is not None and (
                _req_int(frontier, "revision") != authority.shard_revision
                or _req_int(frontier, "visible_count") != authority.visible_count
                or _req_int(frontier, "eligible_count") != authority.eligible_count
            ):
                return ServingPublicationMutationResult.FRONTIER_RACE
            raise
        return ServingPublicationMutationResult.APPLIED

    def settle_node(self, settlement: NodeSettlement) -> None:
        """Settle a node terminal, release its lock and token, and close its `VERIFY#`.

        Slice 7 (D34, F9): `settle_unit_set` is the one `SUCCEEDED` path for a `write_unit`
        node, so that case is refused here with `WRITE_UNIT_SETTLE_PATH` before any write.
        """
        node = settlement.node
        if is_write_unit(node.name) and settlement.state is NodeState.SUCCEEDED:
            raise InvariantError(
                WRITE_UNIT_SETTLE_PATH_CODE,
                "a write_unit node settles SUCCEEDED only through settle_unit_set.",
                context={"signal": WRITE_UNIT_SETTLE_PATH, "node_id": node.node_id},
            )
        members = self._settle_members(settlement)
        if settlement.verify_open:
            members.append(
                delete(
                    MemberSpec(
                        "",
                        _key(due_shard(settlement.run.conflict_unit), f"VERIFY#{node.node_id}"),
                        NODE_STATE_CHANGED,
                        "attribute_exists(pk)",
                    )
                )
            )
        if settlement.state is NodeState.SUPERSEDED_UNCLAIMED:
            members.append(_not_live_run(node.run_id))
        self._commit(members)

    # --- fencing (V-11, V-25) -------------------------------------------------------------------

    def fence_unsent(self, run: RunRecord, node: NodeRecord) -> None:
        """Settle a READY permitless node of a FENCING run `FENCED_UNSENT` (V-11)."""
        node_member = update(
            MemberSpec(
                "",
                _node_key(node),
                NODE_STATE_CHANGED,
                "#state = :ready AND revision = :rev AND attribute_not_exists(permit_generation)",
                _values(
                    ready=write_text(NodeState.READY.value),
                    fenced=write_text(NodeState.FENCED_UNSENT.value),
                    rev=write_int(node.revision),
                    one=write_int(1),
                ),
            ),
            "SET #state = :fenced, revision = revision + :one",
        )
        # Slice 7: a batch member has no `OUTBOX#<node_id>` of its own; its batch row is the
        # dispatcher's, which skips a member that is no longer READY with its `batch_id`.
        outbox = (
            []
            if node.batch_id is not None
            else [
                delete(
                    MemberSpec(
                        "",
                        _key(due_shard(run.conflict_unit), f"OUTBOX#{node.node_id}"),
                        NODE_STATE_CHANGED,
                        "#state = :open",
                        _values(open=write_text(_OPEN)),
                    )
                )
            ]
        )
        self._commit(
            [
                node_member,
                *_release_members(node),
                *outbox,
                check(
                    MemberSpec(
                        "",
                        _run_key(run.run_id),
                        RUN_STATE_CHANGED,
                        "#state = :fencing",
                        _values(fencing=write_text(RunState.FENCING.value)),
                    )
                ),
            ]
        )

    def fence_pending(self, run: RunRecord, node: NodeRecord) -> None:
        """Settle a never-armed node of a FENCING run `FENCED_UNSENT` (S3V-1).

        A PENDING node holds no lock, token, permit or outbox row, so this is a node CAS plus
        three checks: the run is still FENCING, its `INSTANTIATE#` obligation is gone (while it
        exists, instantiation settles the nodes it put), and its `ARM#` obligation is gone (S6
        §2.2: the arm pass deletes it alone for a run that is not live, `RUN_ARMING_OPEN`).
        """
        self._commit(
            [
                _node_transition(node, NodeState.FENCED_UNSENT, {}),
                check(
                    MemberSpec(
                        "",
                        _run_key(run.run_id),
                        RUN_STATE_CHANGED,
                        "#state = :fencing",
                        _values(fencing=write_text(RunState.FENCING.value)),
                    )
                ),
                check(
                    MemberSpec(
                        "",
                        _key(due_shard(run.conflict_unit), f"INSTANTIATE#{run.run_id}"),
                        RUN_INSTANTIATION_OPEN,
                        "attribute_not_exists(pk)",
                    )
                ),
                _arming_absent(run),
            ]
        )

    def fence_run(self, request: FenceRunRequest) -> None:
        """Release point 1: FENCING, generation + 1, slot freed, `PROMOTE#` woken."""
        self._commit(build_fence_run(request))

    def terminalize_run(self, run: RunRecord, nodes: tuple[NodeRecord, ...]) -> RunState:
        """Move a FENCING run to its terminal once every node is terminal."""
        self._commit(build_terminal(run, nodes))
        if run.fence_reason is None:
            raise GraphStoreUnavailableError("a fenced run carries no fence reason.")
        return fence_terminal_state(run.fence_reason)

    # --- operator commands (V-14, V-29) ---------------------------------------------------------

    def pause_run(self, command: RunCommand) -> None:
        """OPEN -> PAUSED and generation + 1; a pending grant refuses it (V-29)."""
        run = command.run
        self._commit(
            [
                update(
                    MemberSpec(
                        "",
                        _run_key(run.run_id),
                        RUN_STATE_CHANGED,
                        "#state = :open AND attribute_not_exists(grant_id) AND revision = :rev",
                        _values(
                            open=write_text(RunState.OPEN.value),
                            paused=write_text(RunState.PAUSED.value),
                            rev=write_int(run.revision),
                            one=write_int(1),
                        ),
                        _run_signal,
                    ),
                    "SET #state = :paused, dispatch_generation = dispatch_generation + :one, "
                    "revision = revision + :one",
                ),
                put(
                    MemberSpec("", {}, RUN_STATE_CHANGED, "attribute_not_exists(pk)"),
                    command_item(command.receipt),
                ),
            ]
        )

    def resume_run(self, command: RunCommand) -> None:
        """PAUSED -> OPEN, generation + 1, and every READY outbox row re-armed under it."""
        run = command.run
        outboxes = command.ready
        new_gen = write_int(run.dispatch_generation + 1)
        members = [
            update(
                MemberSpec(
                    "",
                    _run_key(run.run_id),
                    RUN_STATE_CHANGED,
                    "#state = :paused AND revision = :rev",
                    _values(
                        open=write_text(RunState.OPEN.value),
                        paused=write_text(RunState.PAUSED.value),
                        rev=write_int(run.revision),
                        one=write_int(1),
                    ),
                ),
                "SET #state = :open, dispatch_generation = dispatch_generation + :one, "
                "revision = revision + :one",
            ),
            put(
                MemberSpec("", {}, RUN_STATE_CHANGED, "attribute_not_exists(pk)"),
                command_item(command.receipt),
            ),
        ]
        members.extend(
            update(
                MemberSpec(
                    "",
                    _key(outbox.shard, f"OUTBOX#{outbox.node_id}"),
                    NODE_STATE_CHANGED,
                    "#state = :open AND revision = :rev",
                    _values(
                        open=write_text(_OPEN),
                        rev=write_int(outbox.revision),
                        gen=new_gen,
                        one=write_int(1),
                    ),
                ),
                "SET generation = :gen, revision = revision + :one",
            )
            for outbox in outboxes
        )
        self._commit(members)

    def set_hold(self, request: HoldRequest) -> None:
        """Set or clear the hold on the mode item; refused unless the mode reads GRAPH."""
        mode = request.mode
        values: dict[str, AttributeValueTypeDef] = {
            "graph": write_text(AuthorityMode.GRAPH.value),
            "hold": write_bool(request.hold),
            "next": write_int(mode.hold_revision + 1),
        }
        condition = "#mode = :graph AND "
        if mode.hold_revision == 0:
            condition += "attribute_not_exists(hold_revision)"
        else:
            condition += "hold_revision = :rev"
            values["rev"] = write_int(mode.hold_revision)
        expression = "SET #hold = :hold, hold_revision = :next"
        if not request.hold:
            expression += ", hold_cleared_at = :cleared"
            values["cleared"] = write_text(request.receipt.recorded_at)
        self._commit(
            [
                update(
                    MemberSpec(
                        "",
                        _key(_MODE_PK, _MODE_SK),
                        AUTOMATIC_ADMISSION_HOLD_REQUIRES_GRAPH,
                        condition,
                        _values(**values),
                    ),
                    expression,
                ),
                put(
                    MemberSpec("", {}, RUN_STATE_CHANGED, "attribute_not_exists(pk)"),
                    command_item(request.receipt),
                ),
            ]
        )

    def record_command(self, receipt: CommandReceipt) -> None:
        """Write one refusal command receipt and nothing else."""
        self._commit(
            [
                put(
                    MemberSpec("", {}, RUN_STATE_CHANGED, "attribute_not_exists(pk)"),
                    command_item(receipt),
                )
            ]
        )

    # --- promotion (V-20, V-25) -----------------------------------------------------------------

    def promote(self, commit: PromotionCommit) -> None:
        """Commit one promoter branch; every member is conditioned (r5)."""
        self._commit(build_promotion(commit))

    # --- 1b: unit batches (slice 7, §2.2, §2.5; D30-D35) -----------------------------------

    def unit_batch_fits(self, request: ArmUnitBatchRequest) -> bool:
        """Return whether the grant's one transaction is within `MAX_TRANSACTION_BYTES` (F3).

        The bound is `transaction_bytes` over the exact members `arm_unit_batch` commits, with
        each member's node and run counted at its size as read. The promoter takes the longest
        oldest-first prefix that fits, so a table of large units arms fewer members and never
        retries a transaction DynamoDB would refuse.
        """
        members = _arm_batch_members(request)
        sizes = read_sizes(*(item.run for item in request.members))
        sizes.update(read_sizes(*(item.node for item in request.members)))
        return transaction_bytes(members, sizes) <= MAX_TRANSACTION_BYTES

    def arm_unit_batch(self, request: ArmUnitBatchRequest) -> None:
        """Arm one table group's members, the counted lock and one batch outbox row (§2.2).

        Per member: PENDING -> READY with `armed_generation`, `armed_at` and `batch_id`, and its
        `TOKEN#`. Per distinct run: `RUN#` OPEN at the read generation (revision bumped), plus
        its `ARM#` delete when `retire_runs` names it. Once: `LOCK#TABLE#<k>` put on
        `attribute_not_exists(pk) OR open_members = :zero` (`DOMAIN_LOCK_HELD`), and
        `OUTBOX#<batch_id>`. At most `2 + 4n` actions, so `n <= MAX_BATCH_MEMBERS` (D32). A
        grant over the aggregate-size bound is refused before any write (F3).
        """
        if not self.unit_batch_fits(request):
            raise ValueError("a unit batch's arming transaction exceeds the 4 MB size bound.")
        self._commit(_arm_batch_members(request))

    def acquire_batch_permit(self, request: BatchPermitRequest) -> None:
        """OPEN -> SENDING with `permitted`; each permitted member READY -> PERMITTED (§2.2).

        Each member's run is checked OPEN at the generation it was armed under, so a pause or
        a generation move between the sort and the permit refuses (`PLAN_DISPATCH_FENCED`).
        At most `1 + 2n` actions.
        """
        outbox, permitted = request.outbox, request.permitted
        if not permitted:
            raise ValueError("a batch permit names at least one permitted member.")
        generations = {ref.node_id: ref.armed_generation for ref in outbox.members}
        transaction: list[Member] = [
            update(
                MemberSpec(
                    "",
                    _key(outbox.shard, f"OUTBOX#{outbox.node_id}"),
                    NODE_STATE_CHANGED,
                    "#state = :open AND revision = :rev",
                    {
                        **_values(
                            open=write_text(_OPEN),
                            sending=write_text(_SENDING),
                            rev=write_int(outbox.revision),
                            one=write_int(1),
                        ),
                        ":permitted": {
                            "L": [write_text(member.node.node_id) for member in permitted]
                        },
                    },
                ),
                "SET #state = :sending, #permitted = :permitted, revision = revision + :one",
            )
        ]
        runs: set[str] = set()
        for member in permitted:
            generation = generations.get(member.node.node_id)
            if generation is None:
                raise ValueError("a permitted member is not on the batch row.")
            transaction.append(
                _node_transition(
                    member.node, NodeState.PERMITTED, {"permit_generation": write_int(generation)}
                )
            )
            if member.run.run_id not in runs:
                runs.add(member.run.run_id)
                transaction.append(
                    _open_run_at(
                        member.run.run_id, generation, signal=PLAN_DISPATCH_FENCED, bump=False
                    )
                )
        self._commit(transaction)

    def record_batch_send(self, receipt: BatchSendReceipt) -> None:
        """Close a SENDING batch row; each listed member PERMITTED -> DISPATCHED (F7)."""
        outbox = receipt.outbox
        sets: dict[str, AttributeValueTypeDef] = {
            "message_id": write_text(receipt.message_id),
            "sent_at": write_text(receipt.sent_at),
        }
        transaction: list[Member] = []
        for node in receipt.members:
            if node.state is not NodeState.PERMITTED:
                raise ValueError("only a PERMITTED member is recorded as sent.")
            transaction.append(_node_transition(node, NodeState.DISPATCHED, sets))
        transaction.append(
            delete(
                MemberSpec(
                    "",
                    _key(outbox.shard, f"OUTBOX#{outbox.node_id}"),
                    NODE_STATE_CHANGED,
                    "#state = :sending AND revision = :rev",
                    _values(sending=write_text(_SENDING), rev=write_int(outbox.revision)),
                )
            )
        )
        self._commit(transaction)

    def delete_batch_row(self, outbox: OutboxRecord) -> None:
        """Delete an OPEN batch row none of whose members may be permitted (§2.2)."""
        self._commit(
            [
                delete(
                    MemberSpec(
                        "",
                        _key(outbox.shard, f"OUTBOX#{outbox.node_id}"),
                        NODE_STATE_CHANGED,
                        "#state = :open AND revision = :rev",
                        _values(open=write_text(_OPEN), rev=write_int(outbox.revision)),
                    )
                )
            ]
        )

    def return_unit(self, request: ReturnUnitRequest) -> None:
        """Return a CLAIMED or READY member to PENDING without holding its table (§2.5).

        One transaction: the node (removing `permit_generation` and `batch_id`, setting
        `returned_at`, `return_reason`, and `arm_alone`/`final_return` by the reason, with
        `attribute_not_exists(final_return)` when it sets it), the `TOKEN#` delete, the counted
        lock release, the claim's lease release (TableWriter only), the `ARM#` upsert and a
        `RUN#` check that the run is OPEN or PAUSED. A second `final_return` is refused
        (`NODE_STATE_CHANGED`).
        """
        run, node, reason = request.run, request.node, request.reason
        if node.state not in {NodeState.CLAIMED, NodeState.READY} or node.batch_id is None:
            raise ValueError("only a CLAIMED or READY batch member is returned.")
        values: dict[str, AttributeValueTypeDef] = _values(
            pending=write_text(NodeState.PENDING.value),
            one=write_int(1),
            from_=write_text(node.state.value),
            rev=write_int(node.revision),
            at=write_text(request.recorded_at),
            reason=write_text(reason.value),
        )
        values[":from"] = values.pop(":from_")
        sets = [
            "#state = :pending",
            "revision = revision + :one",
            "returned_at = :at",
            "return_reason = :reason",
        ]
        condition = "#state = :from AND revision = :rev"
        if sets_arm_alone(reason):
            values[":alone"] = write_bool(True)
            sets.append("arm_alone = :alone")
        if sets_final_return(reason):
            values[":final"] = write_bool(True)
            sets.append("final_return = :final")
            condition += " AND attribute_not_exists(final_return)"
        transaction: list[Member] = [
            update(
                MemberSpec("", _node_key(node), NODE_STATE_CHANGED, condition, values),
                "SET " + ", ".join(sets) + " REMOVE permit_generation, batch_id",
            ),
            _token_delete(node),
            counted_release(node.lock_key, node.batch_id, 1),
        ]
        if request.claim is not None:
            transaction.append(_lease_release(request.claim, request.recorded_at))
        transaction.append(_arm_upsert(run, request.recorded_at))
        transaction.append(_live_run(run.run_id, RUN_STATE_CHANGED))
        self._commit(transaction)

    def _unit_success_members(
        self, request: UnitSetSettlement, member: UnitSetMember
    ) -> list[Member]:
        node = member.node
        sets: dict[str, AttributeValueTypeDef] = {
            "settled_at": write_text(request.recorded_at),
            "effect_digest": write_text(member.effect_digest),
        }
        if member.effect_record is not None:
            sets["effect_record"] = write_text(member.effect_record)
        members = [_node_transition(node, NodeState.SUCCEEDED, sets), _token_delete(node)]
        if member.claim is not None:
            members.append(self._lease_bump(member.claim, request.recorded_at))
        if request.verify_open:
            members.append(
                delete(
                    MemberSpec(
                        "",
                        _key(due_shard(member.run.conflict_unit), f"VERIFY#{node.node_id}"),
                        NODE_STATE_CHANGED,
                        "attribute_exists(pk)",
                    )
                )
            )
        return members

    def settle_unit_set(self, request: UnitSetSettlement) -> tuple[str, ...]:
        """Settle a success set `SUCCEEDED`, one transaction per chunk (D34, §2.5 step 6).

        The only `SUCCEEDED` path for a `write_unit` node. Per member: the node with its
        effect, the `TOKEN#` delete and the claim's lease bump; per chunk one counted lock
        release `ADD :minus_m`. Chunks commit in member order and the last brings
        `open_members` to zero. A guard in a chunk raises `UnitSetGuardError` naming the node
        IDs every earlier chunk committed, so the caller holds a precise remainder.
        """
        if not request.members:
            raise ValueError("a success set settles at least one member.")
        for member in request.members:
            node = member.node
            if node.lock_key != request.lock_key or node.batch_id != request.batch_id:
                raise ValueError("every success-set member holds the set's table and batch.")
        committed: list[str] = []
        for chunk in success_chunks(
            request, lambda member: self._unit_success_members(request, member)
        ):
            transaction: list[Member] = []
            for member in chunk:
                transaction.extend(self._unit_success_members(request, member))
            transaction.append(counted_release(request.lock_key, request.batch_id, len(chunk)))
            try:
                self._commit(transaction)
            except GraphGuardError as guard:
                raise UnitSetGuardError(guard.signal, tuple(committed)) from None
            committed.extend(member.node.node_id for member in chunk)
        return tuple(committed)


def _promote_delete(read: PromotionRead) -> list[Member]:
    if read.wake_seq is None:
        return []
    return [
        delete(
            MemberSpec(
                "",
                _key(due_shard(read.conflict_unit), f"PROMOTE#{read.conflict_unit}"),
                PROMOTE_WAKE_CHANGED,
                "wake_seq = :k",
                _values(k=write_int(read.wake_seq)),
            )
        )
    ]


def _slot_free(read: PromotionRead) -> Member:
    return check(
        MemberSpec(
            "",
            _key(f"SLOT#{read.conflict_unit}", "SLOT"),
            PROMOTE_WAKE_CHANGED,
            "attribute_not_exists(active_run_id)",
        )
    )


def _waiter_delete(read: PromotionRead) -> list[Member]:
    """Delete the waiter on its revision; a held waiter also checks the hold it read (S3V-9).

    The promoter decided from a cleared hold. If the operator set it again after that read, the
    hold revision moved, so the commit is refused and the re-read skips the held waiter (V-20).
    """
    waiter = read.waiter
    if waiter is None:
        raise GraphStoreUnavailableError("a waiter branch read no waiter.")
    members = [
        delete(
            MemberSpec(
                "",
                _key(due_shard(read.conflict_unit), f"WAITER#{read.conflict_unit}"),
                PROMOTE_WAKE_CHANGED,
                "revision = :rev AND request_id = :rid",
                _values(rev=write_int(waiter.revision), rid=write_text(waiter.request_id)),
            )
        )
    ]
    if waiter.held:
        if read.hold_revision == 0:
            condition, values = "attribute_not_exists(hold_revision)", None
        else:
            condition = "hold_revision = :hold_rev"
            values = _values(hold_rev=write_int(read.hold_revision))
        members.append(
            check(MemberSpec("", _key(_MODE_PK, _MODE_SK), PROMOTE_WAKE_CHANGED, condition, values))
        )
    return members


def _promoted_run_members(commit: PromotionCommit, slot_revision: int) -> list[Member]:
    run = commit.new_run
    if run is None:
        raise GraphStoreUnavailableError("a promotion carried no run.")
    shard = due_shard(run.conflict_unit)
    epoch = write_int(run.authority_epoch)
    return [
        put(
            MemberSpec("", {}, PROMOTE_WAKE_CHANGED, "attribute_not_exists(pk)"),
            {
                **_run_key(run.run_id),
                "item_kind": write_text("run"),
                "state": write_text(RunState.OPEN.value),
                "authority_epoch": epoch,
                "fingerprint": write_text(run.fingerprint),
                "conflict_unit": write_text(run.conflict_unit),
                "retrieved_at": write_text(run.retrieved_at),
                "root_flow_id": write_text(run.root_flow_id),
                "generation": write_int(run.generation),
                "graph_id": write_text(run.graph_id),
                "template": write_text(run.template),
                "dispatch_generation": write_int(0),
                "revision": write_int(1),
                "receipt_request_id": write_text(run.receipt_request_id),
                "protocol_version": write_text(commit.details["protocol_version"]),
                "promoted": write_bool(True),
                **({} if run.window is None else {"window": write_text(run.window)}),
                **root_clock_attribute(run.root_clock_started_at),
            },
        ),
        # S4 (S1A-8): a waiter never becomes `A` while its co-release domain is BLOCKED.
        check(
            MemberSpec(
                "",
                _key(f"BLOCK#{INGESTION_RELEASE_DOMAIN}", "BLOCK"),
                PROMOTE_WAKE_CHANGED,
                "attribute_not_exists(pk)",
            )
        ),
        put(
            MemberSpec(
                "",
                {},
                PROMOTE_WAKE_CHANGED,
                "attribute_not_exists(pk) OR generation < :gen",
                _values(gen=write_int(run.generation)),
            ),
            {
                **_key(f"FINGERPRINT#{run.authority_epoch}#{run.fingerprint}", "FINGERPRINT"),
                "run_id": write_text(run.run_id),
                "generation": write_int(run.generation),
            },
        ),
        put(
            MemberSpec("", {}, PROMOTE_WAKE_CHANGED, "attribute_not_exists(pk)"),
            {
                **_key(shard, f"INSTANTIATE#{run.run_id}"),
                "item_kind": write_text("instantiate"),
                "authority_epoch": epoch,
                "run_id": write_text(run.run_id),
                "due_at": write_text(commit.recorded_at),
                **root_clock_attribute(run.root_clock_started_at),
            },
        ),
        update(
            MemberSpec(
                "",
                _key(f"SLOT#{run.conflict_unit}", "SLOT"),
                PROMOTE_WAKE_CHANGED,
                "attribute_not_exists(pk) OR "
                "(revision = :rev AND attribute_not_exists(active_run_id))",
                _values(
                    run=write_text(run.run_id),
                    next=write_int(slot_revision + 1),
                    rev=write_int(slot_revision),
                    epoch=epoch,
                ),
            ),
            "SET active_run_id = :run, revision = :next, authority_epoch = :epoch",
        ),
        put(
            MemberSpec("", {}, PROMOTE_WAKE_CHANGED, "attribute_not_exists(pk)"),
            {
                **_key(f"RECEIPT#{run.receipt_request_id}", "PROMOTED"),
                "item_kind": write_text("promotion"),
                "authority_epoch": epoch,
                "root_flow_id": write_text(run.root_flow_id),
                "run_id": write_text(run.run_id),
                "graph_id": write_text(run.graph_id),
                "recorded_at": write_text(commit.recorded_at),
            },
        ),
    ]


def build_promotion(commit: PromotionCommit) -> list[Member]:
    """Build one promoter branch's transaction (design.md, "The promoter", r5)."""
    read = commit.read
    branch = PromoterBranch(commit.branch)
    if branch is PromoterBranch.OCCUPIED:
        if read.slot_run_id is None or read.wake_seq is None:
            raise GraphStoreUnavailableError("the occupied branch needs a slot and a wakeup.")
        return [
            *_promote_delete(read),
            check(
                MemberSpec(
                    "",
                    _key(f"SLOT#{read.conflict_unit}", "SLOT"),
                    PROMOTE_WAKE_CHANGED,
                    "revision = :rev AND attribute_exists(active_run_id)",
                    _values(rev=write_int(read.slot_revision)),
                )
            ),
        ]
    if branch is PromoterBranch.FREE_NO_WAITER:
        return [
            *_promote_delete(read),
            _slot_free(read),
            check(
                MemberSpec(
                    "",
                    _key(due_shard(read.conflict_unit), f"WAITER#{read.conflict_unit}"),
                    PROMOTE_WAKE_CHANGED,
                    "attribute_not_exists(pk)",
                )
            ),
        ]
    if branch is PromoterBranch.SUPERSEDE_WAITER:
        receipt = commit.details
        return [
            *_promote_delete(read),
            _slot_free(read),
            *_waiter_delete(read),
            put(
                MemberSpec("", {}, PROMOTE_WAKE_CHANGED, "attribute_not_exists(pk)"),
                receipt_item(_supersession(read, receipt), "SUPERSEDED"),
            ),
        ]
    if branch is PromoterBranch.PROMOTE_WAITER:
        return [
            *_promote_delete(read),
            *_waiter_delete(read),
            *_promoted_run_members(commit, read.slot_revision),
        ]
    raise GraphStoreUnavailableError(f"branch {branch} writes nothing.")


def _supersession(read: PromotionRead, details: dict[str, str]) -> ReceiptRecord:
    waiter = read.waiter
    if waiter is None:
        raise GraphStoreUnavailableError("a supersession needs a waiter.")
    return ReceiptRecord(
        request_id=waiter.request_id,
        request_digest="superseded",
        decision="SUPERSEDED",
        reason="older_than_current",
        row=None,
        authority_epoch=int(details["authority_epoch"]),
        mode=AuthorityMode(details["mode"]),
        conflict_unit=read.conflict_unit,
        input_revision=waiter.r.isoformat(),
        fingerprint=waiter.fingerprint,
        recorded_at=details["recorded_at"],
        protocol_version=details["protocol_version"],
        contract_revision=int(details["contract_revision"]),
        root_flow_id=details.get("root_flow_id"),
    )
