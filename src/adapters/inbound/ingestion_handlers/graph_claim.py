# SPDX-License-Identifier: MIT-0
"""The graph execution claim TableWriter takes for a graph envelope in GRAPH (S3, V-9, V-10).

For graph envelopes the graph claim is the only claim (R-7). Its predicate (`decide_claim`)
reads the run, the node and any existing claim strongly:

- a first claim on an OPEN or PAUSED run commits `EXEC_CLAIM#` and moves the node to CLAIMED;
  the first claim of the run also sets `compute_started_at` on `RUN#`, the one record that
  decides compute start against `REPLACED_BEFORE_COMPUTE` (V-10);
- a claim on a FENCING or terminal run with no claim record is `EXECUTION_SUPERSEDED`: the node
  settles `SUPERSEDED_UNCLAIMED`, its lock and token are released, and the message is
  acknowledged with zero effect;
- an expired claim on such a run is a takeover: the lease version is bumped (so the first
  invocation's graph-envelope BeginTableAttempt is refused), and the node settles from its
  table-attempt facts, or parks under `VERIFY#` while the attempt is open, and the message is
  acknowledged (r5);
- a live claim leaves the duplicate unacknowledged for retry. S6 §2.3: a claimer that returns a
  message unacknowledged on purpose releases its lease first (`release_claim_lease`), so the
  redelivery reads `RECLAIM` and every node gets its real attempts;
- S6 §2.10: CLAIM records admission 1 and each RECLAIM adds one (with `admitted_at`); a lease
  bump by a takeover or a settlement is not an admission.

A `RUN#` `TransactionConflict` is retried, with a bounded exponential backoff and jitter, within
the generated maximum parallel claims per run (`max_graph_nodes`) and counted. A claim that
exhausts that bound on conflicts raises `ClaimRetryExhaustedError`, the S3 stop rule
(`design.md`, "One run record decides compute start"). A guard (a node or run that moved since
the read) is a separate count: it re-decides within the generated re-decide bound, and running
out of that bound re-raises the last guard, never the stop rule (S3V-10). Every disposition
reports the attempts it took. The claimed unit's table execution itself arrives with S6 (V-12's
GRAPH rows).
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from adapters.jobs.execution_graph.fence_settler import try_terminalize
from adapters.jobs.execution_graph.node_verifier import NodeFacts
from adapters.outbound.dynamodb.execution_graph_dispatch import envelope_digest
from domain.operations.execution_graph_dispatch import (
    CLAIM_BINDING_CONFLICT,
    CLAIM_LEASE_LIVE,
    CLAIM_LEASE_STALE,
    EXECUTION_SUPERSEDED,
    ClaimOutcome,
    ClaimView,
    GraphGuardError,
    NodeState,
    TakeoverOutcome,
    decide_claim,
    decide_takeover,
    is_write_unit,
)
from domain.operations.generated_execution_graphs import BOUNDS
from domain.shared_kernel.errors import ErrorCode, InvariantError
from ports.outbound.execution_graph_dispatch import (
    ClaimRecord,
    ClaimRequest,
    GraphDispatchStore,
    GraphTransactionConflictError,
    NodeRecord,
    NodeSettlement,
    RunRecord,
    UnitSetMember,
    UnitSetSettlement,
)

__all__ = [
    "MAX_GUARD_REDECIDES",
    "MAX_PARALLEL_CLAIMS",
    "ClaimDisposition",
    "ClaimRetryExhaustedError",
    "GraphClaimInput",
    "claim_graph_envelope",
    "conflict_backoff_ms",
    "release_claim_lease",
]

MAX_PARALLEL_CLAIMS: Final = int(BOUNDS["max_graph_nodes"])
"""The generated maximum parallel claims per run, and the claim's conflict-retry bound."""
MAX_GUARD_REDECIDES: Final = int(BOUNDS["max_redecide"])
"""How many guards (a node or run that moved since the read) one claim re-decides through."""
_BACKOFF_BASE_MS: Final = 10
_BACKOFF_CAP_MS: Final = 160


def _no_sleep(milliseconds: int) -> None:
    del milliseconds


def conflict_backoff_ms(conflicts: int) -> int:
    """Return the jittered wait before retry `conflicts` (exponential, capped at 160 ms)."""
    ceiling = min(_BACKOFF_CAP_MS, _BACKOFF_BASE_MS * 2 ** max(conflicts - 1, 0))
    return random.randint(ceiling // 2, ceiling)  # noqa: S311 -- retry jitter, not a secret


class ClaimRetryExhaustedError(InvariantError):
    """A claim exhausted its bounded retry on `RUN#` conflicts: the S3 stop rule."""

    def __init__(self, conflicts: int) -> None:
        super().__init__(
            ErrorCode("EXEC_GRAPH", 22),
            "a graph claim exhausted its RUN# conflict retry.",
            context={"conflicts": conflicts},
        )
        self.conflicts = conflicts


@dataclass(frozen=True, slots=True)
class GraphClaimInput:
    """One received graph envelope and the instants the claim records."""

    run_id: str
    node_id: str
    dispatch_id: str
    body: str
    now: Callable[[], datetime]
    lease: timedelta
    queue_enqueued_at: str | None = None
    first_received_at: str | None = None
    sleep_ms: Callable[[int], None] = _no_sleep
    """Waits between conflict retries (TableWriter passes its runtime sleep)."""
    batch_id: str | None = None
    """Slice 7 (N1): a unit batch member's envelope batch; the node must carry it, and each
    admission records it on the claim."""


@dataclass(frozen=True, slots=True)
class ClaimDisposition:
    """TableWriter's acknowledgement decision for one graph envelope."""

    acknowledge: bool
    outcome: str
    signal: str | None = None
    claim: ClaimRecord | None = None
    conflicts: int = 0
    effects: tuple[str, ...] = field(default=())
    """Every graph write this disposition committed, for the zero-effect assertions."""
    attempts: int = 1
    """How many decide-and-commit attempts the claim took (conflicts plus guards plus one)."""


def _expired(claim: ClaimRecord | None, now: datetime) -> bool:
    return claim is not None and datetime.fromisoformat(claim.lease_expires_at) <= now


def _find(store: GraphDispatchStore, item: GraphClaimInput) -> tuple[RunRecord, NodeRecord]:
    run = store.read_run(item.run_id)
    node = None
    if run is not None:
        node = next((n for n in store.read_nodes(item.run_id) if n.node_id == item.node_id), None)
    if run is None or node is None:
        raise InvariantError(
            ErrorCode("EXEC_GRAPH", 23), "a graph envelope names a run or node that does not exist."
        )
    return run, node


def _takeover(
    context: tuple[GraphDispatchStore, NodeFacts],
    request: ClaimRequest,
) -> ClaimDisposition:
    store, facts = context
    node, claim = request.node, request.claim
    fact, digest = facts.attempt(node, claim)
    outcome = decide_takeover(fact)
    if outcome is TakeoverOutcome.AWAIT_PROVIDER_TERMINAL:
        store.await_provider_terminal(request)
        return ClaimDisposition(
            True,
            "graph_awaiting_provider_terminal",
            EXECUTION_SUPERSEDED,
            effects=("lease_bump", "awaiting_provider_terminal", "verify"),
        )
    state = {
        TakeoverOutcome.SUPERSEDE_UNCLAIMED: NodeState.SUPERSEDED_UNCLAIMED,
        TakeoverOutcome.SETTLE_SUCCEEDED: NodeState.SUCCEEDED,
        TakeoverOutcome.SETTLE_FAILED: NodeState.FAILED,
    }[outcome]
    # S6 §2.4: facts that name their failure (`MaterializationFacts`) record it on the node.
    named: object = getattr(facts, "failure_reason", None)
    reason = named if state is NodeState.FAILED and isinstance(named, str) else None
    if state is NodeState.SUCCEEDED and is_write_unit(node.name):
        # Slice 7 (D34, F8): the one SUCCEEDED path for a unit, a success set of one, from the
        # member's own claim and its attempt's digest; no effect record.
        if digest is None or node.batch_id is None:
            raise InvariantError(
                ErrorCode("EXEC_GRAPH", 24),
                "a write_unit takeover settles SUCCEEDED only from its attempt and batch.",
            )
        store.settle_unit_set(
            UnitSetSettlement(
                node.lock_key,
                node.batch_id,
                (UnitSetMember(request.run, node, digest, claim=claim),),
                request.claimed_at,
            )
        )
    else:
        store.settle_node(
            NodeSettlement(
                request.run,
                node,
                state,
                request.claimed_at,
                claim=claim,
                effect_digest=digest,
                failure_reason=reason,
            )
        )
    try_terminalize(store, node.run_id)
    return ClaimDisposition(
        True,
        "execution_superseded",
        EXECUTION_SUPERSEDED,
        effects=("lease_bump", state.value.lower(), "lock_released", "token_released"),
    )


def _decide(
    context: tuple[GraphDispatchStore, NodeFacts], item: GraphClaimInput, now: datetime
) -> ClaimDisposition:
    store, _facts = context
    run, node = _find(store, item)
    claim = store.read_claim(node.node_id)
    binding = node.dispatch_id == item.dispatch_id and node.envelope_digest == envelope_digest(
        item.body
    )
    outcome = decide_claim(
        ClaimView(
            run_state=run.state,
            phase=node.phase,
            node_state=node.state,
            binding_matches=binding and node.permit_generation is not None,
            claim_present=claim is not None,
            lease_expired=_expired(claim, now),
            batch_matches=item.batch_id is None or node.batch_id == item.batch_id,
        )
    )
    request = ClaimRequest(
        run=run,
        node=node,
        claim=claim,
        lease_expires_at=(now + item.lease).isoformat(),
        claimed_at=now.isoformat(),
        queue_enqueued_at=item.queue_enqueued_at,
        first_received_at=item.first_received_at,
        batch_id=item.batch_id,
    )
    if outcome in {ClaimOutcome.CLAIM, ClaimOutcome.RECLAIM}:
        record = store.claim(request)
        return ClaimDisposition(False, "graph_claimed", claim=record, effects=("claim",))
    if outcome is ClaimOutcome.LIVE_DUPLICATE:
        return ClaimDisposition(False, "graph_claim_live", CLAIM_LEASE_LIVE)
    if outcome is ClaimOutcome.ALREADY_SETTLED:
        return ClaimDisposition(True, "graph_already_settled")
    if outcome is ClaimOutcome.SUPERSEDE_UNCLAIMED:
        store.settle_node(
            NodeSettlement(run, node, NodeState.SUPERSEDED_UNCLAIMED, request.claimed_at)
        )
        try_terminalize(store, run.run_id)
        return ClaimDisposition(
            True,
            "execution_superseded",
            EXECUTION_SUPERSEDED,
            effects=("superseded_unclaimed", "lock_released", "token_released"),
        )
    if outcome is ClaimOutcome.TAKEOVER:
        return _takeover(context, request)
    raise InvariantError(
        ErrorCode("EXEC_GRAPH", 24),
        "a graph envelope disagrees with its node's dispatch binding.",
        context={"signal": CLAIM_BINDING_CONFLICT},
    )


def release_claim_lease(  # noqa: PLR0913 -- the held claim and the progress the write records
    store: GraphDispatchStore,
    claim: ClaimRecord,
    now: datetime,
    *,
    progress: Mapping[str, int] | None = None,
) -> bool:
    """Release the held lease before a deliberate unacknowledged return (S6 §2.3).

    Every graph claimer calls this on every deliberate unacknowledged return, so the redelivery
    reads `RECLAIM` rather than spending a receive as `LIVE_DUPLICATE`. It returns False when a
    concurrent reclaimer already bumped the lease version (`CLAIM_LEASE_STALE`): the late
    release is refused and ignored. A store failure propagates; the lease then stays live and
    the next receive is spent, which costs one receive and never an effect. `publish` passes its
    progress counts (S6 §2.6), which the same write records on the claim.
    """
    try:
        store.release_claim_lease(claim, now.isoformat(), progress)
    except GraphGuardError as guard:
        if guard.signal != CLAIM_LEASE_STALE:
            raise
        return False
    return True


def claim_graph_envelope(
    store: GraphDispatchStore, facts: NodeFacts, item: GraphClaimInput
) -> ClaimDisposition:
    """Decide and commit one graph envelope's claim disposition."""
    conflicts = guards = attempts = 0
    while True:
        attempts += 1
        try:
            found = _decide((store, facts), item, item.now())
        except GraphTransactionConflictError:
            conflicts += 1
            if conflicts >= MAX_PARALLEL_CLAIMS:
                raise ClaimRetryExhaustedError(conflicts) from None
            item.sleep_ms(conflict_backoff_ms(conflicts))
            continue
        except GraphGuardError:
            guards += 1
            if guards >= MAX_GUARD_REDECIDES:
                raise
            continue
        return ClaimDisposition(
            found.acknowledge,
            found.outcome,
            found.signal,
            found.claim,
            conflicts,
            found.effects,
            attempts,
        )
