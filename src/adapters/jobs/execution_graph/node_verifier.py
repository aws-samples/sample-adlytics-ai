# SPDX-License-Identifier: MIT-0
"""The node verifier (S3, V-9 r5): it settles a parked takeover from provider facts.

A takeover that finds the node's table attempt open moves the node to
`AWAITING_PROVIDER_TERMINAL`, writes `VERIFY#<node_id>` and acknowledges the message, so the
message never reaches the DLQ while the node still holds its domain lock. The provider terminal
then comes from the attempt's own completion or from the existing provider-attempt reconciler
and fence-recovery functions; neither needs the message. The verifier reads `VERIFY#` items
strongly, re-attests the attempt (`NodeFacts`), and once a terminal exists settles the node
(`SUCCEEDED` with the effect unpublished, because a fenced run has no grant, or `FAILED`),
releases the lock and token and deletes `VERIFY#` in one transaction. Then the run may turn
terminal. Each item is isolated, so one failure never ends the pass (S3V-7).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol

from adapters.jobs.execution_graph.fence_settler import try_terminalize
from adapters.jobs.execution_graph.isolation import FailureSink, ignore_failure, isolated
from domain.operations.execution_graph_dispatch import (
    AttemptFact,
    GraphGuardError,
    NodeState,
    is_write_unit,
)
from domain.operations.status import canonical_authority_digest
from domain.operations.table_attempt import AttemptObservation
from domain.shared_kernel.errors import ErrorCode, InvariantError
from ports.outbound.execution_graph_dispatch import (
    ClaimRecord,
    GraphDispatchStore,
    NodeRecord,
    NodeSettlement,
    UnitSetMember,
    UnitSetSettlement,
)

__all__ = [
    "MATERIALIZATION_INTERRUPTED",
    "POST_GRANT_TAKEOVER_UNREACHABLE",
    "AttemptReader",
    "ClaimAttemptFacts",
    "MaterializationFacts",
    "NodeFacts",
    "UnreachablePostGrantFacts",
    "VerifierContext",
    "VerifyCounts",
    "attempt_fact",
    "verify_shard",
]

_SUCCESS = frozenset({"successful", "no_change"})


class NodeFacts(Protocol):
    """Re-attests a node's table attempt under the existing lease and provider fence."""

    def attempt(
        self, node: NodeRecord, claim: ClaimRecord | None
    ) -> tuple[AttemptFact, str | None]:
        """Return the attempt fact and, for a terminal attempt, its effect digest."""
        ...


class AttemptReader(Protocol):
    """The strongly consistent attempt read the Stage-1 writer authority already owns."""

    def attempt(self, table_id: str, attempt_id: str) -> AttemptObservation | None:
        """Read one `ATTEMPT#` item."""
        ...


def attempt_fact(observation: AttemptObservation | None) -> tuple[AttemptFact, str | None]:
    """Classify one attempt observation; a terminal one carries its effect digest."""
    if observation is None:
        return AttemptFact.NONE, None
    if observation.terminal_at is None:
        return AttemptFact.OPEN, None
    digest = canonical_authority_digest(
        [
            "execution-graph-node-effect/v1",
            observation.attempt_id or "",
            observation.terminal_at,
            observation.owner_disposition or "",
        ]
    )
    if observation.owner_disposition in _SUCCESS:
        return AttemptFact.SUCCEEDED, digest
    return AttemptFact.FAILED, digest


@dataclass(frozen=True, slots=True)
class ClaimAttemptFacts:
    """Facts through the attempt the graph-envelope BeginTableAttempt recorded on the claim."""

    reader: AttemptReader

    def attempt(
        self, node: NodeRecord, claim: ClaimRecord | None
    ) -> tuple[AttemptFact, str | None]:
        """Read the claim's recorded attempt; no recorded attempt means none began."""
        del node
        if claim is None or claim.table_id is None or claim.attempt_id is None:
            return AttemptFact.NONE, None
        return attempt_fact(self.reader.attempt(claim.table_id, claim.attempt_id))


POST_GRANT_TAKEOVER_UNREACHABLE: Final = ErrorCode("EXEC_GRAPH", 27)
"""S6 §2.7 (G2-17): a takeover of a POST_GRANT node, which no live protocol path reaches."""


@dataclass(frozen=True, slots=True)
class UnreachablePostGrantFacts:
    """The `NodeFacts` every POST_GRANT claimer (`publish`, `precompute`, `bake`) passes.

    A takeover needs a claimed node on a run that is not live, and for a POST_GRANT node live
    means `GRANTED`. The run leaves `GRANTED` only through `complete_granted_run`, which needs
    every POST_GRANT node terminal, so no POST_GRANT takeover is reachable: asking for its
    attempt facts is an invariant, and the case fails closed.
    """

    def attempt(
        self, node: NodeRecord, claim: ClaimRecord | None
    ) -> tuple[AttemptFact, str | None]:
        """Refuse: a POST_GRANT takeover is unreachable short of a defect."""
        del claim
        raise InvariantError(
            POST_GRANT_TAKEOVER_UNREACHABLE,
            "a POST_GRANT node was taken over; its run left GRANTED with the node open.",
            context={"node": node.name},
        )


MATERIALIZATION_INTERRUPTED: Final = "materialization_interrupted"
"""S6 §2.4 (G2-5): a `materialize_raw` that may have begun committing and did not settle."""


@dataclass(frozen=True, slots=True)
class MaterializationFacts:
    """The `NodeFacts` the delivery handler's `materialize_raw` claimer passes (S6 §2.4).

    A takeover happens only on a FENCING or terminal run. The node's one fact is whether an
    earlier receive's lease-conditioned re-attestation set `materialization_began_at`: then
    publications may have committed, so the node settles `FAILED` (`failure_reason`
    `materialization_interrupted`); otherwise nothing began and it settles
    `SUPERSEDED_UNCLAIMED`. Its committed publications are inert either way: nothing servable
    moves outside `publish`, and a fenced run is never granted.
    """

    failure_reason: str = MATERIALIZATION_INTERRUPTED

    def attempt(
        self, node: NodeRecord, claim: ClaimRecord | None
    ) -> tuple[AttemptFact, str | None]:
        """Return FAILED when materialization may have begun, NONE otherwise."""
        del node
        if claim is not None and claim.materialization_began_at is not None:
            return AttemptFact.FAILED, None
        return AttemptFact.NONE, None


@dataclass
class VerifyCounts:
    """What one verifier pass did."""

    items: int = 0
    settled: int = 0
    waiting: int = 0
    guards: int = 0
    conflicts: int = 0
    failed: int = 0


@dataclass(frozen=True, slots=True)
class VerifierContext:
    """The store, attempt facts and clock one verifier pass runs with."""

    store: GraphDispatchStore
    facts: NodeFacts
    now: Callable[[], str]
    on_failure: FailureSink = ignore_failure


def _verify(context: VerifierContext, item: tuple[str, str], counts: VerifyCounts) -> None:
    store, facts, now = context.store, context.facts, context.now
    run_id, node_id = item
    run = store.read_run(run_id)
    node = next((n for n in store.read_nodes(run_id) if n.node_id == node_id), None)
    if run is None or node is None or node.state is not NodeState.AWAITING_PROVIDER_TERMINAL:
        return
    fact, digest = facts.attempt(node, store.read_claim(node.node_id))
    if fact not in {AttemptFact.SUCCEEDED, AttemptFact.FAILED}:
        counts.waiting += 1
        return
    state = NodeState.SUCCEEDED if fact is AttemptFact.SUCCEEDED else NodeState.FAILED
    try:
        if state is NodeState.SUCCEEDED and is_write_unit(node.name):
            # Slice 7 (D34, F8): a unit's one SUCCEEDED path, a success set of one with the
            # `VERIFY#` delete; no claim (NodeVerifier holds no `EXEC_CLAIM#` write) and no
            # effect record, as today.
            if digest is None or node.batch_id is None:
                raise InvariantError(
                    POST_GRANT_TAKEOVER_UNREACHABLE,
                    "a write_unit verification settles SUCCEEDED only from its attempt and batch.",
                )
            store.settle_unit_set(
                UnitSetSettlement(
                    node.lock_key,
                    node.batch_id,
                    (UnitSetMember(run, node, digest),),
                    now(),
                    verify_open=True,
                )
            )
        else:
            store.settle_node(
                NodeSettlement(run, node, state, now(), verify_open=True, effect_digest=digest)
            )
    except GraphGuardError:
        counts.guards += 1
        return
    counts.settled += 1
    try_terminalize(store, run.run_id)


def verify_shard(context: VerifierContext, shard: str) -> VerifyCounts:
    """Settle every parked node whose provider terminal now exists, each item isolated."""
    counts = VerifyCounts()
    for item in context.store.read_due(shard, "VERIFY#"):
        counts.items += 1
        run_id = item.run_id
        if run_id is None:
            continue
        result = isolated(
            lambda pair=(run_id, item.subject): _verify(context, pair, counts),
            context.on_failure,
        )
        counts.conflicts += result.conflicts
        counts.failed += 0 if result.ok else 1
    return counts
