# SPDX-License-Identifier: MIT-0
"""Graph instantiation builds only from a `RunRequestReceipt.v1` (BF-6, V-5, V-26).

There is no second path: a caller without a receipt is refused with
`RUN_REQUEST_RECEIPT_REQUIRED` before any graph write. In epoch 0 (SHADOW) the only graph is an
immutable `SHADOW_RECORDED` record with no lock, token, permit, outbox or due obligation; it is
written in the admitting receipt's own transaction.

In epoch 1 every transaction that makes a request `A` also writes `INSTANTIATE#<run_id>`
(S1 admission, S3 promotion). `instantiate_run` then completes the graph under that obligation,
inline when the caller can and through the promoter's sweep otherwise (S3, r4/r5):

1. every node is put with `attribute_not_exists`, so a re-run is idempotent;
2. every node put and the final arming transaction ConditionCheck `RUN#` state in
   {OPEN, PAUSED} and the dispatch generation instantiation read. A failed check re-reads the
   run: a pause advanced the generation, so instantiation continues under the new one (the
   armed roots' permits stay refused until resume); a fenced or terminal run settles every
   node already put `FENCED_UNSENT` and deletes `INSTANTIATE#` in one transaction;
3. the final transaction arms the roots (READY, runtime token, domain lock, outbox) and deletes
   `INSTANTIATE#`. A root whose domain lock another run still holds leaves the obligation in
   place, so the sweep retries it after that run's node settles (V-11's positive control).

Envelope sizing runs here (FR-4): a node envelope above the provider ceiling fences the run
`oversize` before anything is armed. The size measured is the exact envelope the dispatcher will
send, the owner body plus its dispatch binding (S3V-2).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from adapters.outbound.dynamodb.execution_graph_dispatch import graph_envelope
from domain.operations.execution_graph import GraphCommandRefusedError
from domain.operations.execution_graph_dispatch import (
    DOMAIN_LOCK_HELD,
    NODE_PLAN_NOT_DEPLOYED,
    PROVIDER_CEILING_BYTES,
    RUN_NOT_INSTANTIABLE,
    FenceReason,
    GraphGuardError,
    NodePhase,
    NodeState,
    provider_accounted_bytes,
)
from domain.operations.generated_execution_graphs import BOUNDS, TEMPLATES
from domain.operations.run_request import RunState
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.execution_graph import ReceiptRecord, ShadowGraph
from ports.outbound.execution_graph_dispatch import (
    ArmRequest,
    FenceRunRequest,
    GraphDispatchStore,
    NodeSpec,
    PutNodesRequest,
    RunRecord,
)

__all__ = [
    "RUN_REQUEST_RECEIPT_REQUIRED",
    "InstantiateRequest",
    "InstantiationOutcome",
    "NodePlanUnavailableError",
    "graph_nodes",
    "ingestion_node_plan",
    "instantiate_run",
    "require_receipt",
    "shadow_graph_for",
]

RUN_REQUEST_RECEIPT_REQUIRED: Final = "RUN_REQUEST_RECEIPT_REQUIRED"
_ADMITTING: Final = frozenset({"ADMITTED", "REPLACED_BEFORE_COMPUTE", "REPLACED_ON_INVALIDATION"})
_LIVE: Final = frozenset({RunState.OPEN, RunState.PAUSED})


class NodePlanUnavailableError(DependencyError):
    """The template's dispatch plan is not deployed; `INSTANTIATE#` stays and is alarmed."""

    def __init__(self, template: str) -> None:
        super().__init__(
            ErrorCode("EXEC_GRAPH", 21), f"no node dispatch plan is deployed for {template}."
        )
        self.signal = NODE_PLAN_NOT_DEPLOYED


def require_receipt(receipt: ReceiptRecord | None, *, promoted: bool = False) -> ReceiptRecord:
    """Refuse instantiation without an admitting receipt (BF-6).

    A promoted waiter's receipt is its `QUEUED_LATEST`; the promoter's transaction made it `A`
    and recorded that on the run, so it is admitting too.
    """
    admitting = receipt is not None and (
        receipt.decision in _ADMITTING or (promoted and receipt.decision == "QUEUED_LATEST")
    )
    if receipt is None or not admitting:
        raise GraphCommandRefusedError(
            RUN_REQUEST_RECEIPT_REQUIRED, "a graph is instantiated only from an admitting receipt."
        )
    return receipt


def graph_nodes(template: str) -> tuple[str, ...]:
    """Return the template's node names, bounded by the generated graph size."""
    nodes = tuple(str(item["node"]) for item in TEMPLATES[template]["nodes"])
    if len(nodes) > int(BOUNDS["max_graph_nodes"]):
        raise GraphCommandRefusedError(
            RUN_REQUEST_RECEIPT_REQUIRED, "the template exceeds the bound."
        )
    return nodes


def shadow_graph_for(receipt: ReceiptRecord | None, template: str) -> ShadowGraph:
    """Build the one immutable epoch-0 graph a SHADOW admission records (V-5)."""
    admitted = require_receipt(receipt)
    if admitted.authority_epoch != 0 or admitted.graph_id is None or admitted.root_flow_id is None:
        raise GraphCommandRefusedError(
            RUN_REQUEST_RECEIPT_REQUIRED, "a shadow graph needs an epoch-0 admitting receipt."
        )
    return ShadowGraph(
        graph_id=admitted.graph_id,
        template=template,
        nodes=graph_nodes(template),
        fingerprint=admitted.fingerprint,
        root_flow_id=admitted.root_flow_id,
    )


type NodePlanner = Callable[[RunRecord, ReceiptRecord], tuple[NodeSpec, ...]]


def _spine_body(node: str, run: RunRecord) -> str:
    """Render a static spine node's owner body; the dispatch binding is added at instantiation.

    The materialization inputs (manifest key and version) are read from the run's admitting
    receipt by the delivery handler (design §2.4), so the body names only the node and its run.
    """
    return json.dumps(
        {
            "schema_version": "1",
            "node": node,
            "conflict_unit": run.conflict_unit,
            "root_flow_id": run.root_flow_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


_SPINE_LOCKS: Final = {
    # `MATERIALIZE#<U>` is unique to the window, so as a reservation target it adds nothing
    # beyond `SLOT#<U>` (design §2.1); its group ID is ignored on the standard delivery queue.
    "materialize_raw": ("MATERIALIZE#{unit}", "{unit}"),
    # Never armed or dispatched: apply settles it, and reservation targets exclude its lock.
    "grant_apply": ("GRANT_APPLY#{unit}", "grant_apply#{unit}"),
}


def ingestion_node_plan(run: RunRecord, receipt: ReceiptRecord) -> tuple[NodeSpec, ...]:
    """Return the ingestion template's static PRE_GRANT spine: `materialize_raw`, `grant_apply`.

    Revision 2 (design §2.1, G1): `write_unit` is dynamic, so the `materialize_raw` settlement
    puts one node per roster unit and instantiation never does; POST_GRANT nodes are written only
    by grant apply. So only `materialize_raw` is a root.
    """
    del receipt
    specs: list[NodeSpec] = []
    for item in TEMPLATES[run.template]["nodes"]:
        phase = NodePhase(str(item.get("phase", NodePhase.PRE_GRANT.value)))
        if phase is not NodePhase.PRE_GRANT or item.get("dynamic") is True:
            continue
        name = str(item["node"])
        lock, group = _SPINE_LOCKS[name]
        specs.append(
            NodeSpec(
                name=name,
                after=tuple(str(after) for after in item["after"]),
                phase=phase,
                lock_key=lock.format(unit=run.conflict_unit),
                group_id=group.format(unit=run.conflict_unit),
                body=_spine_body(name, run),
            )
        )
    return tuple(specs)


class InstantiationOutcome(StrEnum):
    """How one instantiation pass ended."""

    ARMED = "armed"
    LOCK_WAIT = "lock_wait"
    FENCED = "fenced"
    OVERSIZE = "oversize"
    ALREADY_DONE = "already_done"


@dataclass(frozen=True, slots=True)
class InstantiateRequest:
    """One run to instantiate, its node planner and the clock."""

    run_id: str
    planner: NodePlanner
    now: Callable[[], str]


def _settle_fenced(store: GraphDispatchStore, run: RunRecord, now: str) -> InstantiationOutcome:
    nodes = store.read_nodes(run.run_id)
    store.settle_fenced_instantiation(ArmRequest(run, nodes, now))
    return InstantiationOutcome.FENCED


def _oversize(run_id: str, specs: tuple[NodeSpec, ...]) -> bool:
    return any(
        provider_accounted_bytes(graph_envelope(run_id, spec).body, {}) > PROVIDER_CEILING_BYTES
        for spec in specs
    )


def instantiate_run(store: GraphDispatchStore, request: InstantiateRequest) -> InstantiationOutcome:
    """Complete one run's graph under its `INSTANTIATE#` obligation (V-26)."""
    for _ in range(int(BOUNDS["max_redecide"])):
        run = store.read_run(request.run_id)
        if run is None:
            raise GraphCommandRefusedError(
                RUN_REQUEST_RECEIPT_REQUIRED, "instantiation names a run that does not exist."
            )
        if not store.instantiation_open(run):
            return InstantiationOutcome.ALREADY_DONE
        if run.state not in _LIVE:
            return _settle_fenced(store, run, request.now())
        receipt = require_receipt(store.read_receipt(run.receipt_request_id), promoted=run.promoted)
        # S4: POST_GRANT nodes are written only by grant apply, bound to the exact grant.
        specs = tuple(
            spec for spec in request.planner(run, receipt) if spec.phase is NodePhase.PRE_GRANT
        )
        if len(specs) > int(BOUNDS["max_graph_nodes"]):
            raise GraphCommandRefusedError(
                RUN_REQUEST_RECEIPT_REQUIRED, "the graph exceeds the bound."
            )
        if _oversize(run.run_id, specs):
            try:
                store.fence_run(FenceRunRequest(run, FenceReason.OVERSIZE, request.now()))
            except GraphGuardError:
                continue
            return InstantiationOutcome.OVERSIZE
        existing = {node.name for node in store.read_nodes(run.run_id)}
        missing = tuple(spec for spec in specs if spec.name not in existing)
        try:
            if missing:
                store.put_nodes(PutNodesRequest(run, missing, request.now()))
            roots = tuple(
                node
                for node in store.read_nodes(run.run_id)
                if not node.after and node.state is NodeState.PENDING
            )
            store.arm_roots(ArmRequest(run, roots, request.now()))
        except GraphGuardError as guard:
            if guard.signal == DOMAIN_LOCK_HELD:
                return InstantiationOutcome.LOCK_WAIT
            # RUN_NOT_INSTANTIABLE (a pause or fence moved the run) or a concurrent put:
            # re-read the run and continue under what it now says.
            continue
        return InstantiationOutcome.ARMED
    raise GraphGuardError(RUN_NOT_INSTANTIABLE)
