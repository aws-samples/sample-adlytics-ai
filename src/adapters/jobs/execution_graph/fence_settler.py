# SPDX-License-Identifier: MIT-0
"""The fence settler (S3, V-11): it finishes every FENCING run.

A run moves to FENCING through release point 1 (cancel, oversize, node failure, a grant-time
`H` refusal) or a replacing admission (rows 17 and 18); either writes `FENCE#<run_id>` in the
run's due shard. The settler reads those items strongly. For each run it settles every READY
node that holds no permit `FENCED_UNSENT`, a CAS on the node revision and on permit absence that
releases the node's domain lock and runtime token in the same transaction. Permit acquisition
checks the generation, which the FENCING transition advanced, so a node without a permit is
provably unsent. Once the run's `INSTANTIATE#` obligation is gone, every node still PENDING
(a successor nothing armed, holding no lock, token or outbox row) settles `FENCED_UNSENT` too,
by a node CAS conditioned on the run still FENCING and the obligation still absent (S3V-1);
while the obligation exists, instantiation settles the nodes it put. Nodes holding a permit
finish their send and are refused at claim (`EXECUTION_SUPERSEDED`). Once every node is
terminal and `INSTANTIATE#` is gone, the run turns terminal (`try_terminalize`), which touches
no slot (V-25). Each run is isolated, so one failure never ends the pass (S3V-7).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from adapters.jobs.execution_graph.isolation import FailureSink, ignore_failure, isolated
from domain.operations.execution_graph_dispatch import (
    GraphGuardError,
    NodeState,
    run_may_terminate,
)
from domain.operations.generated_execution_graphs import BOUNDS
from domain.operations.run_request import RunState
from ports.outbound.execution_graph_dispatch import GraphDispatchStore, RunRecord

__all__ = ["FenceCounts", "due_shards", "settle_shard", "try_terminalize"]

_SHARDS: Final = int(BOUNDS["due_shards"])


def due_shards() -> tuple[str, ...]:
    """Every fixed due shard, in order."""
    return tuple(f"DUE#{index}" for index in range(_SHARDS))


def try_terminalize(store: GraphDispatchStore, run_id: str) -> RunState | None:
    """Turn a FENCING run terminal if every node is terminal and `INSTANTIATE#` is gone."""
    run = store.read_run(run_id)
    if run is None or run.state is not RunState.FENCING:
        return None
    nodes = store.read_nodes(run_id)
    if not run_may_terminate(
        [node.state for node in nodes], instantiation_open=store.instantiation_open(run)
    ):
        return None
    try:
        return store.terminalize_run(run, nodes)
    except GraphGuardError:
        return None


@dataclass
class FenceCounts:
    """What one settler pass did."""

    runs: int = 0
    fenced_unsent: int = 0
    terminal: int = 0
    guards: int = 0
    conflicts: int = 0
    failed: int = 0


def _settle_run(store: GraphDispatchStore, run_id: str, counts: FenceCounts) -> None:
    run = store.read_run(run_id)
    if run is None or run.state is not RunState.FENCING:
        return
    counts.runs += 1
    _settle_nodes(store, run, counts)
    if try_terminalize(store, run.run_id) is not None:
        counts.terminal += 1


def _settle_nodes(store: GraphDispatchStore, run: RunRecord, counts: FenceCounts) -> None:
    instantiating = store.instantiation_open(run)
    for node in store.read_nodes(run.run_id):
        unsent = node.state is NodeState.READY and node.permit_generation is None
        unarmed = node.state is NodeState.PENDING and not instantiating
        if not (unsent or unarmed):
            continue
        try:
            if unsent:
                store.fence_unsent(run, node)
            else:
                store.fence_pending(run, node)
        except GraphGuardError:
            counts.guards += 1
            continue
        counts.fenced_unsent += 1


def settle_shard(  # noqa: PLR0913, PLR0917 -- store, shard, clock and failure sink
    store: GraphDispatchStore,
    shard: str,
    now: Callable[[], str],
    on_failure: FailureSink = ignore_failure,
) -> FenceCounts:
    """Settle every FENCING run named in one due shard, each run isolated."""
    del now
    counts = FenceCounts()
    for item in store.read_due(shard, "FENCE#"):
        result = isolated(
            lambda run_id=item.subject: _settle_run(store, run_id, counts), on_failure
        )
        counts.conflicts += result.conflicts
        counts.failed += 0 if result.ok else 1
    return counts
