# SPDX-License-Identifier: MIT-0
"""`publish` stages, flips one generation pointer, then folds (S6 §2.6; D3, D18, D19).

TableWriter claims the `publish` node of a `GRANTED` run and calls `publish_claimed`. A run's
targets become visible to every semantic reader in one conditional write per window:

1. **Validation.** Strong reads of `RUN#` and every node: the run is `GRANTED` under the
   envelope's grant and carries its `grant_ordinal` (AGG4-16); every PRE_GRANT node is
   `SUCCEEDED`; `materialize_raw` and every `write_unit:*` effect record re-hashes to its stored
   digest, and the unit nodes are exactly the materialized roster. The targets are the
   zero-row targets plus every unit's servable targets.
2. **Stage.** Each target is classified from stored `CURRENT` state (published, superseded,
   staged) or gets its conditional `staged#<grant_id>` entry. Nothing a reader resolves moves.
3. **Flip.** One `TransactWriteItems`: the `GENERATION#<U>` / `POINTER` update with the frozen
   counts, the `RUN#` grant fence (state, grant and ordinal) and the `EXEC_CLAIM#` lease. A
   replay that finds the pointer naming this grant takes its counts from there and skips 1-3.
4. **Fold.** Every target carrying this grant's entry folds into its base; never lowered.
5. **Settlement.** `SUCCEEDED` with the counts as its effect record, and `ARM#` (scope
   `serving`), in one lease-fenced transaction.

Failure (D18 revised; AGG3-2 rule 4): `publish` never settles `FAILED`. A dependency error (or an
unclassified one) releases the lease with the progress counts and stays unacknowledged, and on
the final receive is reported `GRAPH_POST_GRANT_RETRY_EXHAUSTED` on its way to the DLQ. An
invariant (a validation mismatch, a newer pointer, a missing ordinal, a `CURRENT` without
`projected_*`) is counted on `GraphPostGrantInvariant`, reported, and dead-letters. A claim lost
at the flip acknowledges with zero effect.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

from adapters.inbound.ingestion_handlers.graph_claim import release_claim_lease
from adapters.inbound.ingestion_handlers.graph_materialize import (
    MATERIALIZE_NODE,
    materialize_effect_digest,
)
from adapters.inbound.ingestion_handlers.graph_unit import UNIT_NODE, unit_effect_digest
from adapters.outbound.dynamodb.execution_graph_dispatch import grant_fence_checks
from adapters.outbound.dynamodb.execution_graph_release import head_epoch
from adapters.outbound.dynamodb.semantic_state import (
    FlipOutcome,
    FlipRequest,
    FoldOutcome,
    GenerationFlipBarrierError,
    GenerationFlipLostError,
    GenerationPointer,
    GrantStaging,
    PublishCounts,
    PublishTarget,
    StagedTarget,
)
from adapters.outbound.telemetry.graph_metrics import emit_post_grant_invariant
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    report_error,
    report_operation,
)
from domain.operations.execution_graph_dispatch import (
    TERMINAL_NODE_STATES,
    ArmScope,
    GraphGuardError,
    NodePhase,
    NodeState,
)
from domain.operations.generated_execution_graphs import BOUNDS, ROUTES
from domain.operations.run_request import RunState
from domain.operations.status import canonical_authority_digest
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.execution_graph_dispatch import (
    ClaimRecord,
    GraphDispatchStore,
    GraphTransactionConflictError,
    NodeRecord,
    NodeSettlement,
    RunRecord,
    ServingPublicationMutationResult,
    ServingPublicationSettlementRequest,
    SuccessorRequest,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from aws_lambda_powertools import Logger

__all__ = [
    "GRAPH_POST_GRANT_RETRY_EXHAUSTED",
    "MAX_RECEIVES",
    "PUBLISH_EFFECT_NAMESPACE",
    "PUBLISH_INVARIANT",
    "PUBLISH_NODE",
    "PublicationGenerations",
    "PublishEnvelope",
    "PublishInvariantError",
    "PublishPorts",
    "PublishResult",
    "PublishRetryExhaustedError",
    "publish_claimed",
    "publish_effect_record",
]

PUBLISH_NODE: Final = "publish"
MAX_RECEIVES: Final = int(ROUTES["semantic_write"]["max_receive_count"])
PUBLISH_EFFECT_NAMESPACE: Final = "execution-graph-publish-effect/v1"
PUBLISH_INVARIANT: Final = "publish_invariant"
"""The closed `Signal` dimension `GraphPostGrantInvariant` counts a publish invariant under."""
GRAPH_POST_GRANT_RETRY_EXHAUSTED: Final = "GRAPH_POST_GRANT_RETRY_EXHAUSTED"
_MAX_REDECIDE: Final = int(BOUNDS["max_redecide"])
_OPERATION: Final = "semantic_table_writer"
_TARGET_DIGEST_NAMESPACE: Final = "execution-graph-publish-targets/v1"


class _FrontierConflictError(Exception):
    """The cumulative frontier moved through every bounded fresh retry."""


class PublishInvariantError(InvariantError):
    """A `publish` validation that cannot hold on a correct graph: zero effect, dead-lettered."""

    def __init__(self, reason: str, **context: object) -> None:
        super().__init__(
            ErrorCode("GRAPH_PUBLISH", 1),
            "a publish node's granted run or predecessors fail validation.",
            context={"reason": reason, **context},
        )
        self.reason = reason


class PublishRetryExhaustedError(DependencyError):
    """A dependency error on `publish`'s final receive; the message dead-letters (§2.12)."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(
            ErrorCode("GRAPH_PUBLISH", 2),
            "a publish node exhausted its receives on a dependency error.",
            context={"signal": GRAPH_POST_GRANT_RETRY_EXHAUSTED, "cause": type(cause).__name__},
        )
        self.signal = GRAPH_POST_GRANT_RETRY_EXHAUSTED


class PublicationGenerations(Protocol):
    """The publications-table half of §2.6 (`DynamoPublicationStore`, plan B3)."""

    def stage_for_grant(
        self, grant: GrantStaging, target: PublishTarget, *, max_redecide: int
    ) -> StagedTarget:
        """Classify one target from stored state, or stage this grant's entry on it."""
        ...

    def read_generation_pointer(self, conflict_unit: str) -> GenerationPointer | None:
        """Strongly read one window's generation pointer."""
        ...

    def flip_generation(self, request: FlipRequest) -> FlipOutcome:
        """Flip one window's pointer to this grant, fenced by the graph members."""
        ...

    def fold_for_grant(self, grant_id: str, partition_key: str) -> FoldOutcome:
        """Fold one target's staged entry into its base."""
        ...


@dataclass(frozen=True, slots=True)
class PublishPorts:
    """What TableWriter composes for `publish` beside its graph store."""

    publications: PublicationGenerations
    graph_table: str
    """The execution-graph table the flip's fences name."""
    invariant_metric: Callable[[str], None] = field(default=emit_post_grant_invariant)
    """`GraphPostGrantInvariant`, once per invariant (alarmed under §2.11 row 5b)."""


@dataclass(frozen=True, slots=True)
class PublishEnvelope:
    """One claimed `publish` envelope: its binding and its owner body's grant."""

    run_id: str
    node_id: str
    dispatch_id: str
    grant_id: str
    granted_run: str
    conflict_unit: str
    receive_count: int


@dataclass(frozen=True, slots=True)
class PublishResult:
    """One receive's acknowledgement decision and what the root span records."""

    acknowledge: bool
    outcome: str
    node_state: NodeState | None = None
    counts: PublishCounts | None = None
    target_count: int = 0


@dataclass(frozen=True, slots=True)
class _Validated:
    run: RunRecord
    ordinal: int
    targets: tuple[PublishTarget, ...]


@dataclass(slots=True)
class _Context:
    store: GraphDispatchStore
    ports: PublishPorts
    envelope: PublishEnvelope
    claim: ClaimRecord
    now: Callable[[], datetime]
    report: Report
    progress: dict[str, int] = field(
        default_factory=lambda: {"publish_staged": 0, "publish_total": 0}
    )


def _log(ctx: _Context, outcome: str, severity: LogSeverity, **details: object) -> None:
    envelope = ctx.envelope
    report_operation(
        OperationLog(
            operation=_OPERATION,
            component="table_writer",
            phase=PUBLISH_NODE,
            outcome=outcome,
            severity=severity,
            details={
                "run_id": envelope.run_id,
                "node_id": envelope.node_id,
                "dispatch_id": envelope.dispatch_id,
                "grant_id": envelope.grant_id,
                "receive_count": envelope.receive_count,
                **details,
            },
        ),
        ctx.report.logger,
    )


# --- Validation (§2.6 "Validation", "Targets") ----------------------------------------------------


def _effect(node: NodeRecord, digest: Callable[[str], str]) -> dict[str, object]:
    record = node.effect_record
    if record is None or node.effect_digest is None or digest(record) != node.effect_digest:
        raise PublishInvariantError("effect_digest_mismatch", node=node.name)
    parsed: object = json.loads(record)
    if not isinstance(parsed, dict):
        raise PublishInvariantError("effect_record_malformed", node=node.name)
    return cast("dict[str, object]", parsed)


def _members(record: dict[str, object], name: str, node: str) -> list[object]:
    members = record.get(name)
    if not isinstance(members, list):
        raise PublishInvariantError("effect_record_malformed", node=node)
    return cast("list[object]", members)


def _targets(record: dict[str, object], name: str, node: str) -> list[PublishTarget]:
    targets: list[PublishTarget] = []
    for member in _members(record, name, node):
        if not isinstance(member, dict):
            raise PublishInvariantError("effect_record_malformed", node=node)
        fields = cast("dict[str, object]", member)
        token = fields.get("fencing_token")
        try:
            if isinstance(token, bool) or not isinstance(token, int):
                raise TypeError("a target fencing token is an integer.")
            targets.append(
                PublishTarget(
                    partition_key=str(fields["partition"]),
                    publication_id=str(fields["publication_id"]),
                    fencing_token=token,
                )
            )
        except KeyError, TypeError, ValueError:
            raise PublishInvariantError("effect_record_malformed", node=node) from None
    return targets


def _validated(ctx: _Context) -> _Validated:
    envelope = ctx.envelope
    run = ctx.store.read_run(envelope.run_id)
    if (
        run is None
        or run.state is not RunState.GRANTED
        or run.grant_id != envelope.grant_id
        or envelope.granted_run != run.run_id
        or envelope.conflict_unit != run.conflict_unit
    ):
        raise PublishInvariantError("grant_mismatch")
    if run.grant_ordinal is None:
        raise PublishInvariantError("grant_ordinal_missing")
    nodes = ctx.store.read_nodes(run.run_id)
    if not any(node.node_id == envelope.node_id and node.name == PUBLISH_NODE for node in nodes):
        raise PublishInvariantError("publish_node_mismatch")
    pre_grant = [node for node in nodes if node.phase is NodePhase.PRE_GRANT]
    if any(node.state is not NodeState.SUCCEEDED for node in pre_grant):
        raise PublishInvariantError("pre_grant_not_succeeded")
    materialize = next((node for node in pre_grant if node.name == MATERIALIZE_NODE), None)
    if materialize is None:
        raise PublishInvariantError("materialize_missing")
    record = _effect(materialize, materialize_effect_digest)
    targets = _targets(record, "zero_row_targets", materialize.name)
    units = [node for node in pre_grant if node.name.startswith(f"{UNIT_NODE}:")]
    roster = sorted(f"{UNIT_NODE}:{key}" for key in _members(record, "units", materialize.name))
    if roster != sorted(node.name for node in units):
        raise PublishInvariantError("roster_mismatch")
    for unit in units:
        targets.extend(_targets(_effect(unit, unit_effect_digest), "servable_targets", unit.name))
    by_partition: dict[str, PublishTarget] = {}
    for target in targets:
        if by_partition.setdefault(target.partition_key, target) != target:
            raise PublishInvariantError("target_conflict", partition=target.partition_key)
    ordered = tuple(sorted(by_partition.values(), key=lambda target: target.partition_key))
    return _Validated(run, run.grant_ordinal, ordered)


def _target_digest(targets: tuple[PublishTarget, ...]) -> str:
    return canonical_authority_digest(
        [
            _TARGET_DIGEST_NAMESPACE,
            [[item.partition_key, item.publication_id, item.fencing_token] for item in targets],
        ]
    )


def publish_effect_record(
    grant: tuple[str, int], counts: PublishCounts, target_count: int
) -> tuple[str, str]:
    """Return the `publish` node's effect record (its frozen counts) and its digest.

    Bake and precompute decide eligibility from `advanced_curation_count` here (§2.8).
    """
    grant_id, ordinal = grant
    record = json.dumps(
        {
            "schema_version": "1",
            "node": PUBLISH_NODE,
            "grant_id": grant_id,
            "grant_ordinal": ordinal,
            "target_count": target_count,
            **counts.to_record(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return record, canonical_authority_digest([PUBLISH_EFFECT_NAMESPACE, record])


# --- Stage, flip, fold, settle --------------------------------------------------------------------


def _stage_and_flip(ctx: _Context, validated: _Validated) -> PublishCounts:
    envelope, run = ctx.envelope, validated.run
    publications = ctx.ports.publications
    graph = cast("Any", ctx.store)
    grant = GrantStaging(envelope.grant_id, validated.ordinal, run.conflict_unit, ctx.now())
    staged: list[StagedTarget] = []
    for target in validated.targets:
        staged.append(publications.stage_for_grant(grant, target, max_redecide=_MAX_REDECIDE))
        ctx.progress["publish_staged"] = len(staged)
    counts = PublishCounts.classify(tuple(staged))
    target_digest = _target_digest(validated.targets)
    for _ in range(_MAX_REDECIDE):
        authority = graph.read_serving_publication_authority(run.run_id)
        flipped_at = ctx.now()
        members, marker_key, marker = graph.build_publication_flip_participant(
            authority,
            root_flow_id=run.root_flow_id,
            target_digest=target_digest,
            flipped_at=flipped_at.isoformat(),
        )
        try:
            publications.flip_generation(
                FlipRequest(
                    conflict_unit=run.conflict_unit,
                    grant_id=envelope.grant_id,
                    grant_ordinal=validated.ordinal,
                    run_id=run.run_id,
                    root_flow_id=run.root_flow_id,
                    authority_epoch=run.authority_epoch,
                    retrieved_at=run.retrieved_at,
                    revision_epoch=head_epoch(datetime.fromisoformat(run.retrieved_at)),
                    counts=counts,
                    target_count=len(validated.targets),
                    target_digest=target_digest,
                    flipped_at=flipped_at,
                    fences=grant_fence_checks(
                        ctx.ports.graph_table,
                        run_id=run.run_id,
                        grant_id=envelope.grant_id,
                        grant_ordinal=validated.ordinal,
                        node_id=envelope.node_id,
                        lease_version=ctx.claim.lease_version,
                    ),
                    graph_members=members,
                    marker_table=ctx.ports.graph_table,
                    marker_key=marker_key,
                    marker_expected=marker,
                    serving_identity=authority.identity,
                )
            )
        except GenerationFlipBarrierError:
            continue
        return counts
    raise _FrontierConflictError


def _release(ctx: _Context) -> None:
    release_claim_lease(ctx.store, ctx.claim, ctx.now(), progress=dict(ctx.progress))


def _settle(ctx: _Context, validated: _Validated, counts: PublishCounts) -> PublishResult:
    envelope = ctx.envelope
    total = len(validated.targets)
    record, digest = publish_effect_record((envelope.grant_id, validated.ordinal), counts, total)
    for _ in range(_MAX_REDECIDE):
        run = ctx.store.read_run(envelope.run_id)
        node = next(
            (
                item
                for item in ctx.store.read_nodes(envelope.run_id)
                if item.node_id == envelope.node_id
            ),
            None,
        )
        claim = ctx.store.read_claim(envelope.node_id)
        if run is None or node is None:
            raise PublishInvariantError("publish_node_mismatch")
        if claim is None or claim.lease_version != ctx.claim.lease_version:
            _log(ctx, "graph_claim_lost", LogSeverity.WARNING)
            return PublishResult(True, "graph_claim_lost")
        if node.state in TERMINAL_NODE_STATES:
            return PublishResult(True, "graph_already_settled", node.state, counts, total)
        now = ctx.now().isoformat()
        authority = ctx.store.read_serving_publication_authority(run.run_id)
        try:
            result = ctx.store.settle_publish(
                ServingPublicationSettlementRequest(
                    settlement=NodeSettlement(
                        run,
                        node,
                        NodeState.SUCCEEDED,
                        now,
                        claim=ctx.claim,
                        effect_digest=digest,
                        effect_record=record,
                    ),
                    successors=SuccessorRequest(run, (), ArmScope.SERVING, False, now),
                    authority=authority,
                    target_digest=_target_digest(validated.targets),
                )
            )
        except GraphGuardError, GraphTransactionConflictError:
            continue
        if result is ServingPublicationMutationResult.FRONTIER_RACE:
            continue
        _log(ctx, "graph_publish_succeeded", LogSeverity.INFO, **counts.to_record())
        return PublishResult(True, "graph_publish_succeeded", NodeState.SUCCEEDED, counts, total)
    # Bounded settle CAS/frontier races: the caller releases and returns `frontier_conflict`.
    raise _FrontierConflictError


def _execute(ctx: _Context) -> PublishResult:
    validated = _validated(ctx)
    ctx.progress["publish_total"] = len(validated.targets)
    counts = _stage_and_flip(ctx, validated)
    for target in validated.targets:
        ctx.ports.publications.fold_for_grant(ctx.envelope.grant_id, target.partition_key)
    return _settle(ctx, validated, counts)


def publish_claimed(  # noqa: PLR0913, PLR0917 -- one claimed node and its collaborators
    store: GraphDispatchStore,
    ports: PublishPorts,
    envelope: PublishEnvelope,
    claim: ClaimRecord,
    now: Callable[[], datetime],
    logger: Logger,
) -> PublishResult:
    """Validate, stage, flip, fold and settle one claimed `publish` node (§2.6)."""
    ctx = _Context(
        store,
        ports,
        envelope,
        claim,
        now,
        Report(operation=_OPERATION, correlation_id=envelope.node_id, logger=logger),
    )
    try:
        return _execute(ctx)
    except _FrontierConflictError:
        _release(ctx)
        _log(ctx, "frontier_conflict", LogSeverity.INFO)
        return PublishResult(False, "frontier_conflict", NodeState.CLAIMED)
    except GenerationFlipLostError as lost:
        # The run or the claim member was refused: the holder or the takeover path owns it.
        _log(ctx, "graph_claim_lost", LogSeverity.WARNING, fence=lost.position)
        return PublishResult(True, "graph_claim_lost")
    except InvariantError as invariant:
        ports.invariant_metric(PUBLISH_INVARIANT)
        report_error(invariant, ctx.report)
        _release(ctx)
        return PublishResult(False, "graph_publish_invariant", NodeState.CLAIMED)
    except Exception as error:
        # Fix-forward: never FAILED. Retry while receives remain, then dead-letter.
        final = envelope.receive_count >= MAX_RECEIVES
        report_error(PublishRetryExhaustedError(error) if final else error, ctx.report)
        _release(ctx)
        return PublishResult(False, "graph_publish_retryable", NodeState.CLAIMED)
