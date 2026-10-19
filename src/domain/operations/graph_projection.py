# SPDX-License-Identifier: MIT-0
"""Status v2 projection of execution-graph events, keyed by root flow (S2).

The execution-graph store is the authority. Its committed items reach the status projection as
events (the table's stream, or a bounded rebuild read), and this module maps each one to at most
one projection row under the root flow it belongs to:

- a `graph` item becomes `GRAPH#<epoch>#<graph_id>`;
- a `receipt` item becomes `RECEIPT#<recorded_at>#<request_id>#<receipt key>`, so a root's
  receipts page in recorded order, and two receipts of one request (row 2's conflict receipt,
  a supersession) stay distinct;
- a `node` item in a terminal state becomes `NODE#<epoch>#<run_id>#<node>` (S6 part 2, design
  §2.10). Terminal states are final, so the row is immutable: a changed terminal image is a
  projection conflict, and a non-terminal image maps to nothing;
- a `claim` item becomes `CLAIM#<epoch>#<node_id>#<admission>`, one row per admission. The row
  copies only the fields an admission sets, never the lease, so a lease bump replays exactly;
- a `serving_binding` item becomes `SERVING#<epoch>#<grant_id>`.

Rows are immutable copies of closed authority fields plus the authority key they came from, so
the explanation can re-read the authority and the projection can always be rebuilt from it.
Every other item kind (mode, command, run, slot, due obligations) is not a flow event and maps to
nothing. The node, claim and serving sources carry `root_flow_id` and `authority_epoch` only once
slices 2 and 5 write them; until then those items map to nothing too. Epoch-0 rows carry the
`shadow` label and epoch-1 rows the `graph` label, so a shadow record is never presented as
graph-authority progress (V-5). The projection never writes
the graph store and never authorizes anything. Pure: no I/O, no clock, no AWS.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast

from domain.operations.execution_graph_dispatch import TERMINAL_NODE_STATES
from domain.operations.status import canonical_authority_digest
from domain.shared_kernel.errors import ErrorCode, InvariantError

__all__ = [
    "GRAPH_ROOT_PREFIX",
    "GraphProjectionConflictError",
    "GraphProjectionRow",
    "ProjectionKind",
    "epoch_label",
    "project_graph_item",
]

GRAPH_ROOT_PREFIX: Final = "XROOT#"
_GRAPH_FIELDS: Final = (
    "state",
    "authority_epoch",
    "template",
    "nodes",
    "fingerprint",
    "root_flow_id",
    "receipt_request_id",
)
_RECEIPT_FIELDS: Final = (
    "request_id",
    "decision",
    "reason",
    "row",
    "authority_epoch",
    "mode",
    "conflict_unit",
    "input_revision",
    "fingerprint",
    "recorded_at",
    "protocol_version",
    "contract_revision",
    "run_id",
    "graph_id",
    "root_flow_id",
    "retry_generation",
    "superseded_request_id",
    "legacy_plan_key",
    "caller_kind",
    "caller_reason",
    "joined_request_id",
    "retried_run_id",
    # S5: the root clock admission read, so the projection can measure root-to-decision (FR-3).
    "root_clock_started_at",
)
_NODE_FIELDS: Final = (
    "run_id",
    "node",
    "node_id",
    "state",
    "phase",
    "dispatch_id",
    "put_at",
    "armed_at",
    "settled_at",
    "effect_digest",
    # Slice 7 (1b, D35, D37; P6): why a node failed and the batch it last ran in. Absent fields
    # are skipped (`_closed`), so a pre-1b terminal image projects to the same document.
    "failure_reason",
    "failure_code",
    "batch_id",
    "return_reason",
    "arm_alone",
    "final_return",
    # Rolling serving cohort and binding disposition fields.
    "cohort_revision",
    "serving_input_revision",
    "winner_run_id",
    "binding_kind",
    "direct_binding_id",
)
_CLAIM_FIELDS: Final = (
    "run_id",
    "node",
    "dispatch_id",
    "admission",
    # Slice 7 (F11): the batch this admission ran in.
    "batch_id",
    "admitted_at",
    "claimed_at",
    "queue_enqueued_at",
    "first_received_at",
)
"""Only what `CLAIM` and `RECLAIM` set: a lease bump or release leaves the row unchanged."""
_SERVING_FIELDS: Final = (
    "binding_id",
    "conflict_unit",
    "served_revision",
    "contributor_roots",
    "bound_at",
    "artifact_revision",
    "cohort_revision",
    "serving_input_revision",
    "winner_run_id",
    "binding_kind",
    "direct_binding_id",
)
_TERMINAL_STATES: Final = frozenset(state.value for state in TERMINAL_NODE_STATES)


class ProjectionKind(StrEnum):
    """The graph items the status projection maps (S2, plus S6 part 2's three kinds)."""

    GRAPH = "graph"
    RECEIPT = "receipt"
    NODE = "node"
    CLAIM = "claim"
    SERVING = "serving"


class GraphProjectionConflictError(InvariantError):
    """A projection row already exists with other content than its authority now maps to."""

    def __init__(self, description: str) -> None:
        super().__init__(ErrorCode("EXEC_PROJECTION", 1), description)


def epoch_label(epoch: int) -> str:
    """Name the authority a record belongs to: shadow observation, or graph authority."""
    return "graph" if epoch == 1 else "shadow"


@dataclass(frozen=True, slots=True)
class GraphProjectionRow:
    """One immutable projection row and the exact authority item it copies."""

    root_flow_id: str
    sort_key: str
    kind: ProjectionKind
    authority_epoch: int
    authority_pk: str
    authority_sk: str
    document: Mapping[str, object]

    @property
    def partition_key(self) -> str:
        """The root flow's projection partition."""
        return f"{GRAPH_ROOT_PREFIX}{self.root_flow_id}"

    @property
    def digest(self) -> str:
        """The canonical digest of the copied authority fields."""
        return canonical_authority_digest(
            [self.kind.value, self.authority_pk, self.authority_sk, dict(self.document)]
        )


def _closed(item: Mapping[str, object], fields: tuple[str, ...]) -> dict[str, object]:
    document: dict[str, object] = {}
    for name in fields:
        value = item.get(name)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            entries = cast("Sequence[object]", value)
            document[name] = [str(entry) for entry in entries]
        elif isinstance(value, (bool, int)):
            document[name] = value
        else:
            document[name] = str(value)
    return document


def _epoch(item: Mapping[str, object]) -> int | None:
    value = item.get("authority_epoch")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _row(  # noqa: PLR0913 -- one keyword per row field
    *,
    root: str,
    sort_key: str,
    kind: ProjectionKind,
    epoch: int,
    key: tuple[str, str],
    document: dict[str, object],
) -> GraphProjectionRow:
    return GraphProjectionRow(
        root_flow_id=root,
        sort_key=sort_key,
        kind=kind,
        authority_epoch=epoch,
        authority_pk=key[0],
        authority_sk=key[1],
        document=document,
    )


def _execution_row(item: Mapping[str, object], root: str, epoch: int) -> GraphProjectionRow | None:
    """Map a node, claim or serving item (S6 part 2, design §2.10), or nothing."""
    kind, pk, sk = item.get("item_kind"), str(item.get("pk")), str(item.get("sk"))
    if kind == "node" and pk.startswith("RUN#") and sk.startswith("NODE#"):
        if item.get("state") not in _TERMINAL_STATES:
            return None
        run, name = pk.removeprefix("RUN#"), sk.removeprefix("NODE#")
        return _row(
            root=root,
            sort_key=f"NODE#{epoch}#{run}#{name}",
            kind=ProjectionKind.NODE,
            epoch=epoch,
            key=(pk, sk),
            document={**_closed(item, _NODE_FIELDS), "run_id": run, "node": name},
        )
    if kind == "claim" and pk.startswith("EXEC_CLAIM#") and sk == "CLAIM":
        admission = item.get("admission")
        if isinstance(admission, bool) or not isinstance(admission, int) or admission < 1:
            return None
        node_id = pk.removeprefix("EXEC_CLAIM#")
        return _row(
            root=root,
            sort_key=f"CLAIM#{epoch}#{node_id}#{admission}",
            kind=ProjectionKind.CLAIM,
            epoch=epoch,
            key=(pk, sk),
            document={"node_id": node_id, **_closed(item, _CLAIM_FIELDS)},
        )
    if kind == "serving_binding" and pk.startswith("SERVING#") and sk == "BINDING":
        grant = pk.removeprefix("SERVING#")
        return _row(
            root=root,
            sort_key=f"SERVING#{epoch}#{grant}",
            kind=ProjectionKind.SERVING,
            epoch=epoch,
            key=(pk, sk),
            document={"grant_id": grant, **_closed(item, _SERVING_FIELDS)},
        )
    return None


def project_graph_item(item: Mapping[str, object]) -> GraphProjectionRow | None:
    """Map one committed graph-store item to its projection row, or to nothing."""
    kind = item.get("item_kind")
    pk, sk = item.get("pk"), item.get("sk")
    root = item.get("root_flow_id")
    epoch = _epoch(item)
    if not isinstance(pk, str) or not isinstance(sk, str) or not isinstance(root, str):
        return None
    if not root or epoch is None:
        return None
    if kind == ProjectionKind.GRAPH.value and pk.startswith("GRAPH#") and sk == "GRAPH":
        graph_name = pk.removeprefix("GRAPH#")
        return GraphProjectionRow(
            root_flow_id=root,
            sort_key=f"GRAPH#{epoch}#{graph_name}",
            kind=ProjectionKind.GRAPH,
            authority_epoch=epoch,
            authority_pk=pk,
            authority_sk=sk,
            document={"graph_id": graph_name, **_closed(item, _GRAPH_FIELDS)},
        )
    if kind == ProjectionKind.RECEIPT.value and pk.startswith("RECEIPT#"):
        recorded_at = item.get("recorded_at")
        request = item.get("request_id")
        if not isinstance(recorded_at, str) or not isinstance(request, str):
            return None
        return GraphProjectionRow(
            root_flow_id=root,
            sort_key=f"RECEIPT#{recorded_at}#{request}#{sk}",
            kind=ProjectionKind.RECEIPT,
            authority_epoch=epoch,
            authority_pk=pk,
            authority_sk=sk,
            document={"receipt_key": sk, **_closed(item, _RECEIPT_FIELDS)},
        )
    return _execution_row(item, root, epoch)
