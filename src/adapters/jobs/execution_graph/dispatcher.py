# SPDX-License-Identifier: MIT-0
"""The graph dispatcher (S3, BF-2): the only graph sender.

It reads only persisted outbox rows (`DUE#<n>/OUTBOX#<node_id>`) and sends only the exact
envelope bytes stored on the node. S6 §2.1: each node goes to its generated contract route's
queue (`RoutedGraphSender`): the FIFO routes carry `MessageGroupId=unit_key` and one logical
dispatch identity (`MessageDeduplicationId=dispatch_id`) across retries and DLQ observation;
the standard delivery route carries neither.

Per row: acquire the permit, a transaction that ConditionChecks the run state (OPEN, or GRANTED
for a POST_GRANT node) and the generation the outbox row was armed under (BF-2), then send, then
close the row with the exact
enqueue-attempt receipt. A pause, cancellation, invalidation or oversize failure that advanced
the generation first refuses the permit with `PLAN_DISPATCH_FENCED`, and the sender is never
called. A row already `SENDING` (a crash after the permit) is re-sent with the same bytes and
deduplication identity, then closed. A provider oversize rejection is an immutable node failure
that fences the run (`provider_payload_oversize`); on a run that already left OPEN/PAUSED the
node still fails and releases its lock and token, with no fence (S3V-5). A POST_GRANT oversize
is an invariant instead (S6 §2.7, D23): the row is kept and the invariant emitted every pass.
The permit for a POST_GRANT node checks `GRANTED` rather than `OPEN` (D13).

Each row is isolated (`isolation.isolated`): a conflict is retried within its bound, and any
other failure is reported once and counted, so one row never ends the pass (S3V-7).

Slice 7 (1b, §2.2 "Dispatching a unit batch"): an `OUTBOX#<batch_id>` row names its members.
OPEN: each member's node and run are read strongly and sorted (permitted, fencing, returned,
gone); returned members go back to PENDING (`run_paused`, `generation_moved`); one permit moves
the row to SENDING with its `permitted` list; with none permitted the row is deleted. SENDING:
the body is composed from the stored `permitted` list, so a re-send is byte-identical with the
same deduplication ID (F7); each listed member is re-read, and only those still PERMITTED in the
batch are recorded sent. A provider oversize on a batch is an invariant, reported every pass,
with the row kept and no member failed (D23's rule).

Deployed disabled while the mode is not GRAPH; the handler checks the mode first.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol

from adapters.jobs.execution_graph.isolation import FailureSink, ignore_failure, isolated
from adapters.outbound.dynamodb.execution_graph_dispatch import batch_body, batch_dispatch_id
from domain.operations.execution_graph_dispatch import (
    LIVE_RUN_STATES,
    PLAN_DISPATCH_FENCED,
    TABLE_LOCK_PREFIX,
    WRITE_UNIT,
    FenceReason,
    GraphGuardError,
    NodePhase,
    NodeState,
    ReturnReason,
    node_route,
)
from domain.operations.generated_execution_graphs import BOUNDS, ROUTES
from domain.operations.run_request import RunState
from domain.shared_kernel.errors import GRAPH_UNIT_BATCH_OVERSIZE_CODE, ErrorCode, InvariantError
from ports.outbound.execution_graph_dispatch import (
    BatchMemberRef,
    BatchPermitRequest,
    BatchSendReceipt,
    DueItem,
    FenceRunRequest,
    GraphDispatchStore,
    GraphMessage,
    NodeRecord,
    NodeSettlement,
    OutboxRecord,
    ReturnUnitRequest,
    RunRecord,
    SendReceipt,
    UnitMember,
)

__all__ = [
    "UNROUTED_GRAPH_NODE",
    "DispatchContext",
    "DispatchCounts",
    "GraphSender",
    "ProviderOversizeError",
    "RoutedGraphSender",
    "dispatch_shard",
]


class ProviderOversizeError(Exception):
    """The provider rejected a locally accepted envelope as oversized (non-retryable)."""


class GraphSender(Protocol):
    """Sends one message's exact bytes and returns the provider message ID (one send path)."""

    def send(self, message: GraphMessage) -> str:
        """Send the message body with its group and deduplication identity."""
        ...


@dataclass
class DispatchCounts:
    """What one dispatcher pass did."""

    rows: int = 0
    permits: int = 0
    sent: int = 0
    fenced: int = 0
    oversize: int = 0
    post_grant_invariant: int = 0
    guards: int = 0
    conflicts: int = 0
    failed: int = 0
    returned: int = 0
    """Slice 7: batch members returned to PENDING (`run_paused`, `generation_moved`)."""


_REDECIDE: Final = int(BOUNDS["max_redecide"])


def _node(store: GraphDispatchStore, outbox: OutboxRecord) -> NodeRecord | None:
    for node in store.read_nodes(outbox.run_id):
        if node.node_id == outbox.node_id:
            return node
    return None


UNROUTED_GRAPH_NODE: Final = ErrorCode("EXEC_GRAPH", 28)
"""A dispatched node whose template node has no contract route: never sent (S6 §2.1)."""


class RoutedGraphSender:
    """Send each node through the sender of its generated contract route (S6 §2.1, D4).

    The routes come from the contract's `routes` block, the one source the CDK queues and every
    claimer's final-receive test also read, so routing and infrastructure cannot drift. A node
    without a route (`grant_apply`) is never armed, so reaching one here is an invariant.
    """

    def __init__(self, template: str, senders: Mapping[str, GraphSender]) -> None:
        if frozenset(senders) != frozenset(ROUTES):
            raise ValueError("the graph dispatcher needs exactly one sender per contract route.")
        self._template = template
        self._senders = dict(senders)

    def send(self, message: GraphMessage) -> str:
        """Send the message's exact bytes through the route of the node it names."""
        route = node_route(self._template, message.node)
        if route is None:
            raise InvariantError(
                UNROUTED_GRAPH_NODE,
                "a dispatched graph node has no contract route.",
                context={"node": message.node},
            )
        return self._senders[route].send(message)


def _no_invariant_sink(node: NodeRecord) -> None:
    del node


@dataclass(frozen=True, slots=True)
class DispatchContext:
    """The store, sender and clock one dispatcher pass runs with."""

    store: GraphDispatchStore
    sender: GraphSender
    now: Callable[[], str]
    on_failure: FailureSink = ignore_failure
    on_post_grant_invariant: Callable[[NodeRecord], None] = _no_invariant_sink
    """Emits `GraphPostGrantInvariant` once per pass for a kept POST_GRANT oversize (D23)."""


def _send(
    context: DispatchContext,
    row: tuple[NodeRecord, OutboxRecord],
    counts: DispatchCounts,
) -> None:
    store, sender, now = context.store, context.sender, context.now
    node, outbox = row
    run = store.read_run(node.run_id)
    if run is None:
        return
    try:
        message_id = sender.send(GraphMessage.of(node))
    except ProviderOversizeError:
        # S6 §2.7 (D23, decides S3V2-4 and AGG3-9): a POST_GRANT oversize fails closed as an
        # invariant. A granted run has one exit (`SUCCEEDED`), so the node is never failed: it
        # stays PERMITTED with its outbox row SENDING (a flow obligation that ages into the
        # ceiling), every pass re-sends that row, and each pass emits the invariant again, so
        # the alarm stays in ALARM until the defect is fixed forward. A PRE_GRANT oversize fails
        # the node and fences a live run; an OPEN run carrying a grant has no PRE_GRANT row left
        # to send (the grant needed every PRE_GRANT node SUCCEEDED).
        if node.phase is NodePhase.POST_GRANT:
            counts.post_grant_invariant += 1
            context.on_post_grant_invariant(node)
            return
        live = run.state in LIVE_RUN_STATES and run.grant_id is None
        fence = FenceRunRequest(run, FenceReason.OVERSIZE, now()) if live else None
        try:
            store.fail_send(NodeSettlement(run, node, NodeState.FAILED, now()), fence)
        except GraphGuardError:
            counts.guards += 1
            return
        counts.oversize += 1
        return
    counts.sent += 1
    try:
        store.record_send(SendReceipt(node, outbox, message_id, now()))
    except GraphGuardError:
        counts.guards += 1


def _member(
    store: GraphDispatchStore, ref: BatchMemberRef
) -> tuple[RunRecord | None, NodeRecord | None]:
    run = store.read_run(ref.run_id)
    node = next((n for n in store.read_nodes(ref.run_id) if n.node_id == ref.node_id), None)
    return run, node


def _sort_members(
    context: DispatchContext, outbox: OutboxRecord, counts: DispatchCounts
) -> list[UnitMember]:
    """Read every member strongly and sort it; return the permitted ones in member order.

    Fencing members stay READY for `fence_unsent`; a member whose run paused or moved to a new
    generation is returned to PENDING without `arm_alone`; a member no longer READY in this
    batch is gone and skipped.
    """
    permitted: list[UnitMember] = []
    for ref in outbox.members:
        run, node = _member(context.store, ref)
        if run is None or node is None:
            continue
        if node.state is not NodeState.READY or node.batch_id != outbox.node_id:
            continue
        if run.state is RunState.FENCING:
            continue
        if run.state is RunState.OPEN and run.dispatch_generation == ref.armed_generation:
            permitted.append(UnitMember(run, node))
            continue
        if run.state in LIVE_RUN_STATES:
            reason = (
                ReturnReason.RUN_PAUSED
                if run.state is RunState.PAUSED
                else ReturnReason.GENERATION_MOVED
            )
            context.store.return_unit(ReturnUnitRequest(run, node, reason, context.now()))
            counts.returned += 1
    return permitted


def _permit_batch(context: DispatchContext, outbox: OutboxRecord, counts: DispatchCounts) -> bool:
    """Permit an OPEN batch row, or delete it when no member may be permitted; True to send."""
    store = context.store
    current: OutboxRecord | None = outbox
    for _ in range(_REDECIDE):
        if current is None or current.state != "OPEN":
            return current is not None
        try:
            permitted = _sort_members(context, current, counts)
            if not permitted:
                store.delete_batch_row(current)
                return False
            store.acquire_batch_permit(BatchPermitRequest(current, tuple(permitted), context.now()))
        except GraphGuardError:
            current = store.read_outbox(current.shard, current.node_id)
            continue
        counts.permits += 1
        return True
    counts.guards += 1
    return False


def _dispatch_batch(context: DispatchContext, outbox: OutboxRecord, counts: DispatchCounts) -> None:
    """Permit (when OPEN), send and close one unit batch row (§2.2, F7)."""
    store = context.store
    if outbox.state == "OPEN":
        if not _permit_batch(context, outbox, counts):
            return
        reread = store.read_outbox(outbox.shard, outbox.node_id)
        if reread is None:
            return
        outbox = reread
    lock_key = outbox.lock_key
    if lock_key is None or not lock_key.startswith(TABLE_LOCK_PREFIX):
        raise InvariantError(
            GRAPH_UNIT_BATCH_OVERSIZE_CODE,
            "a unit batch row carries no TABLE# lock key.",
            context={"batch_id": outbox.node_id},
        )
    allowed = set(outbox.permitted)
    refs = tuple(ref for ref in outbox.members if ref.node_id in allowed)
    body = batch_body(outbox.node_id, lock_key, refs)
    message = GraphMessage(
        WRITE_UNIT,
        body,
        lock_key.removeprefix(TABLE_LOCK_PREFIX),
        batch_dispatch_id(outbox.node_id, body),
    )
    try:
        message_id = context.sender.send(message)
    except ProviderOversizeError:
        # §2.2 "Oversize": a batch body is a fixed small shape, so a provider oversize is a
        # defect. The row is kept and the invariant reported on every pass; no member fails.
        raise InvariantError(
            GRAPH_UNIT_BATCH_OVERSIZE_CODE,
            "the provider refused a unit batch envelope as oversized.",
            context={"batch_id": outbox.node_id, "members": len(refs)},
        ) from None
    counts.sent += 1
    members: list[NodeRecord] = []
    for ref in refs:
        _run, node = _member(store, ref)
        if (
            node is not None
            and node.state is NodeState.PERMITTED
            and node.batch_id == outbox.node_id
        ):
            members.append(node)
    try:
        store.record_batch_send(BatchSendReceipt(outbox, tuple(members), message_id, context.now()))
    except GraphGuardError:
        counts.guards += 1


def _dispatch_row(context: DispatchContext, item: DueItem, counts: DispatchCounts) -> None:
    store = context.store
    shard, subject = item.shard, item.subject
    outbox = store.read_outbox(shard, subject)
    if outbox is None:
        return
    if outbox.members:
        _dispatch_batch(context, outbox, counts)
        return
    node = _node(store, outbox)
    run = store.read_run(outbox.run_id)
    if node is None or run is None:
        return
    if outbox.state == "OPEN":
        try:
            store.acquire_permit(run, node, outbox)
        except GraphGuardError as guard:
            if guard.signal == PLAN_DISPATCH_FENCED:
                counts.fenced += 1
            else:
                counts.guards += 1
            return
        counts.permits += 1
        outbox = store.read_outbox(shard, subject)
        node = _node(store, outbox) if outbox is not None else None
        if outbox is None or node is None:
            return
    _send(context, (node, outbox), counts)


def dispatch_shard(context: DispatchContext, shard: str) -> DispatchCounts:
    """Permit, send and close every outbox row in one due shard, each row isolated."""
    counts = DispatchCounts()
    for item in context.store.read_due(shard, "OUTBOX#"):
        counts.rows += 1
        result = isolated(
            lambda item=item: _dispatch_row(context, item, counts),
            context.on_failure,
        )
        counts.conflicts += result.conflicts
        counts.failed += 0 if result.ok else 1
    return counts
