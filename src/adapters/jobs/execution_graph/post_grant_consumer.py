# SPDX-License-Identifier: MIT-0
"""The POST_GRANT consumer protocol bake and precompute share (S6 §2.8; D18, D21, D27).

The baker and the precompute worker each consume one graph node per granted run. Everything
but the work itself is the same, so it lives here once:

1. **Claim** (§2.3) with `UnreachablePostGrantFacts`, then the **replay-safe read**
   (AGG3-1): when this member's contribution is already stored, nothing is recomputed and the
   node settles from the stored one.
2. **Consumer invariant** (AGG3-14): the envelope's `grant_id`, `served_revision`, run and window
   must equal `RUN#`'s, and the `publish` node must be `SUCCEEDED` with an effect record that
   re-hashes to its digest. Otherwise the consumer produces nothing, contributes `UNAVAILABLE`
   under `RUN#`'s grant with every artifact key `post_grant_invariant`, counts one
   `GraphPostGrantInvariant`, and settles `FAILED`. So does an `InvariantError` the work raises
   that it did not classify itself (a malformed progress member, D29).
3. **Eligibility.** Nothing advanced in the whole cohort (the work's count of each member's
   `publish` effect record, ADR-140) is `NO_ELIGIBLE`, settled `SUCCEEDED`; an unverifiable
   member's effect is a consumer invariant.
4. **Work.** The node-specific executor (`graph_bake`, `graph_precompute`) returns what it did
   not produce, whether it produced anything, a release (time ran out with receives left), or a
   lost claim.
5. **Contribute and settle** through `settlement_for` (D27): `FAILED` exactly when a listed
   reason is a budget exhaustion or `post_grant_invariant`. Completion is the release sweep's
   (AGG3-7), so no consumer completes its run.

A dependency error releases the lease and leaves the message unacknowledged, so the next
receive is a `RECLAIM`; on the final receive it dead-letters for the alarm and the redrive
(§2.12). Each claimed node is traced by one `POST_GRANT_CONSUME` span under the worker's
invocation span. Every path is reachable only in `GRAPH`: the workers refuse a graph envelope
in any other mode before claiming it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, Protocol, cast

from adapters.inbound.ingestion_handlers.graph_claim import (
    ClaimDisposition,
    GraphClaimInput,
    claim_graph_envelope,
    release_claim_lease,
)
from adapters.inbound.ingestion_handlers.graph_publish import PUBLISH_EFFECT_NAMESPACE, PUBLISH_NODE
from adapters.jobs.execution_graph.node_verifier import UnreachablePostGrantFacts
from adapters.jobs.execution_graph.release import ReleaseStore, contribute
from adapters.outbound.telemetry.graph_metrics import emit_post_grant_invariant
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import POST_GRANT_CONSUME, span, trace_boundary
from domain.operations.execution_graph_dispatch import (
    GRAPH_BINDING_FIELDS,
    TERMINAL_NODE_STATES,
    GraphGuardError,
    NodeState,
)
from domain.operations.execution_graph_release import (
    BAKE_BUDGET_EXHAUSTED,
    POST_GRANT_INVARIANT,
    PRECOMPUTE_BUDGET_EXHAUSTED,
    CohortContribution,
    CohortMember,
    CohortOutcome,
    consumer_effect_record,
    serving_generation,
    settlement_for,
)
from domain.operations.generated_execution_graphs import BOUNDS, ROUTES
from domain.operations.queue_clocks import queue_attributes
from domain.operations.run_request import RunState
from domain.operations.status import canonical_authority_digest
from domain.shared_kernel.errors import DependencyError, ErrorCode, InvariantError
from ports.outbound.execution_graph_dispatch import (
    ClaimRecord,
    GraphTransactionConflictError,
    NodeRecord,
    NodeSettlement,
    RunRecord,
)
from ports.outbound.execution_graph_release import ServingExecutionContext

if TYPE_CHECKING:
    from aws_lambda_powertools import Logger

__all__ = [
    "CONSUMER_INVARIANT",
    "GRAPH_POST_GRANT_ENVELOPE_INVALID",
    "PUBLISH_NODE",
    "ConsumeResult",
    "ConsumerContext",
    "ConsumerPorts",
    "ConsumerWork",
    "LostClaim",
    "PostGrantEnvelope",
    "PostGrantEnvelopeError",
    "PostGrantRuntime",
    "Produced",
    "PublishEffect",
    "ReleaseLease",
    "SettleWithoutContribution",
    "WorkOutcome",
    "consume_post_grant",
    "max_receives",
    "post_grant_envelope",
]

CONSUMER_INVARIANT: Final = "consumer_invariant"
"""The closed `Signal` dimension `GraphPostGrantInvariant` counts a consumer invariant under."""
GRAPH_POST_GRANT_ENVELOPE_INVALID: Final = "GRAPH_POST_GRANT_ENVELOPE_INVALID"
_OWNER_FIELDS: Final = frozenset(
    {
        "schema_version",
        "post_grant",
        "grant_id",
        "granted_run",
        "conflict_unit",
        "served_revision",
        "root_flow_id",
    }
)
_MAX_REDECIDE: Final = int(BOUNDS["max_redecide"])
_FAILING_ORDER: Final = (POST_GRANT_INVARIANT, BAKE_BUDGET_EXHAUSTED, PRECOMPUTE_BUDGET_EXHAUSTED)


def max_receives(route: str) -> int:
    """Return the route queue's `maxReceiveCount` (generated `routes.<route>`, §2.1)."""
    return int(ROUTES[route]["max_receive_count"])


class PostGrantEnvelopeError(InvariantError):
    """A graph envelope on a consumer queue that is not exactly this consumer's owner body."""

    def __init__(self, member: CohortMember, detail: str) -> None:
        super().__init__(
            ErrorCode("GRAPH_POST_GRANT", 1),
            "a graph envelope does not carry exactly this consumer's POST_GRANT owner body.",
            context={
                "signal": GRAPH_POST_GRANT_ENVELOPE_INVALID,
                "member": member.value,
                "detail": detail,
            },
        )
        self.signal = GRAPH_POST_GRANT_ENVELOPE_INVALID


@dataclass(frozen=True, slots=True)
class PostGrantEnvelope:
    """One received POST_GRANT envelope: the dispatch binding, the owner body and its receive."""

    member: CohortMember
    run_id: str
    node_id: str
    dispatch_id: str
    grant_id: str
    granted_run: str
    conflict_unit: str
    served_revision: str
    root_flow_id: str
    body_text: str
    """The exact received body; the claim recomputes the envelope digest from it."""
    message_id: str
    receive_count: int
    sent_timestamp: str | None = None
    first_receive_timestamp: str | None = None


def _text(document: Mapping[str, object], name: str, member: CohortMember) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value.strip():
        raise PostGrantEnvelopeError(member, f"{name} is required")
    return value


def post_grant_envelope(
    record: Mapping[str, object], member: CohortMember
) -> PostGrantEnvelope | None:
    """Parse one SQS record; None when its body carries no dispatch binding (a legacy message).

    A body with any binding field must be exactly this member's owner body plus the binding;
    anything else is `PostGrantEnvelopeError`, a defect left unacknowledged for the DLQ.
    """
    raw = record.get("body")
    try:
        decoded: object = json.loads(raw) if isinstance(raw, str) else None
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    body = cast("dict[str, object]", decoded)
    if not frozenset(body) & GRAPH_BINDING_FIELDS:
        return None
    if frozenset(body) != _OWNER_FIELDS | GRAPH_BINDING_FIELDS:
        raise PostGrantEnvelopeError(member, "owner fields")
    if body.get("schema_version") != "1" or body.get("post_grant") != member.value:
        raise PostGrantEnvelopeError(member, "post_grant")
    attributes = record.get("attributes")
    typed = cast("dict[str, object]", attributes) if isinstance(attributes, dict) else {}
    receive = typed.get("ApproximateReceiveCount")
    message_id = record.get("messageId")
    if not isinstance(message_id, str) or not message_id:
        raise PostGrantEnvelopeError(member, "messageId")
    if not isinstance(receive, str) or not receive.isdecimal():
        raise PostGrantEnvelopeError(member, "ApproximateReceiveCount")
    sent, first = typed.get("SentTimestamp"), typed.get("ApproximateFirstReceiveTimestamp")
    return PostGrantEnvelope(
        member=member,
        run_id=_text(body, "run_id", member),
        node_id=_text(body, "node_id", member),
        dispatch_id=_text(body, "dispatch_id", member),
        grant_id=_text(body, "grant_id", member),
        granted_run=_text(body, "granted_run", member),
        conflict_unit=_text(body, "conflict_unit", member),
        served_revision=_text(body, "served_revision", member),
        root_flow_id=_text(body, "root_flow_id", member),
        body_text=cast("str", raw),
        message_id=message_id,
        receive_count=int(receive),
        sent_timestamp=sent if isinstance(sent, str) else None,
        first_receive_timestamp=first if isinstance(first, str) else None,
    )


@dataclass(frozen=True, slots=True)
class PublishEffect:
    """The `publish` node's frozen counts (§2.6), which decide eligibility."""

    advanced_count: int
    advanced_curation_count: int


@dataclass(slots=True)
class ConsumerContext:
    """One claimed consumer node: the run, the claim and its budget."""

    store: ReleaseStore
    envelope: PostGrantEnvelope
    claim: ClaimRecord
    run: RunRecord
    node: NodeRecord
    now: Callable[[], datetime]
    remaining_ms: Callable[[], int]
    logger: Logger | None

    @property
    def grant_id(self) -> str:
        """The run's grant, which the envelope was checked against."""
        return cast("str", self.run.grant_id)

    @property
    def generation(self) -> str:
        """The grant's one serving generation (§2.8, "One serving revision per grant")."""
        return serving_generation(self.grant_id)

    @property
    def final(self) -> bool:
        """Whether this is the route's last receive before the dead letter (§2.1)."""
        return self.envelope.receive_count >= max_receives(self.envelope.member.value)


@dataclass(frozen=True, slots=True)
class Produced:
    """The work finished, or its final receive ran out.

    It names what it did not produce, whether it produced at least one artifact, and for DB25
    precompute the exact readiness digest plus ordered request/no-request roster.
    """

    unavailable: tuple[tuple[str, str], ...]
    produced: bool
    unit_count: int = 0
    input_effect_roster_digest: str | None = None
    request_roster: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ReleaseLease:
    """Time ran out with receives left: release the lease, leave the message unacknowledged."""

    reason: str


@dataclass(frozen=True, slots=True)
class LostClaim:
    """A lease-fenced write was refused: the claim is lost; stop with zero further effect."""


@dataclass(frozen=True, slots=True)
class SettleWithoutContribution:
    """The node settles SUCCEEDED with this effect and contributes nothing (plan 4.6).

    The V3 bake fan-out: it armed the roster tasks and the finalizer, which contributes the
    cohort's bake member at finalization, so the fan-out node itself never contributes.
    """

    record: str
    digest: str
    unit_count: int = 0


WorkOutcome = Produced | ReleaseLease | LostClaim | SettleWithoutContribution


class ConsumerWork(Protocol):
    """The node-specific half of a POST_GRANT consumer (bake or precompute)."""

    @property
    def member(self) -> CohortMember:
        """Which cohort member this work contributes as."""
        ...

    @property
    def lease(self) -> timedelta:
        """The claim lease: the worker's timeout plus its queue's visibility margin."""
        ...

    def eligible(self, effect: PublishEffect) -> bool:
        """Whether `publish` advanced anything this work consumes (§2.8 step 2)."""
        ...

    def artifact_keys(self) -> tuple[str, ...]:
        """Every artifact key a consumer invariant lists `post_grant_invariant` (cheap)."""
        ...

    def counts(self) -> Mapping[str, int]:
        """Deterministic counts recorded on the settled node (e.g. `intents_planned`)."""
        ...

    def run(self, context: ConsumerContext) -> WorkOutcome:
        """Do the work; raise `DependencyError` to release the lease and retry."""
        ...


@dataclass(frozen=True, slots=True)
class ConsumerPorts:
    """The graph store, the clocks and the invariant metric a consumer composes."""

    store: ReleaseStore
    now: Callable[[], datetime]
    remaining_ms: Callable[[], int]
    """The invocation's remaining time (the Lambda context's), for the work's reserve."""
    invariant_metric: Callable[[str], None] = field(default=emit_post_grant_invariant)
    logger: Logger | None = None


@dataclass(frozen=True, slots=True)
class ConsumeResult:
    """One receive's acknowledgement decision and what its span records."""

    acknowledge: bool
    outcome: str
    node_state: NodeState | None = None
    failure_reason: str | None = None
    unit_count: int = 0


def _operation(member: CohortMember) -> str:
    return f"graph_{member.value}"


def _log(context: tuple[PostGrantEnvelope, Logger | None], outcome: str, **details: object) -> None:
    envelope, logger = context
    if logger is None:
        return
    report_operation(
        OperationLog(
            operation=_operation(envelope.member),
            component=f"graph_{envelope.member.value}",
            phase=envelope.member.value,
            outcome=outcome,
            severity=LogSeverity.INFO,
            details={
                "run_id": envelope.run_id,
                "node_id": envelope.node_id,
                "dispatch_id": envelope.dispatch_id,
                "grant_id": envelope.grant_id,
                "receive_count": envelope.receive_count,
                **details,
            },
        ),
        logger,
    )


def _report(ports: ConsumerPorts, envelope: PostGrantEnvelope, error: BaseException) -> None:
    if ports.logger is None:
        return
    report_error(
        error,
        Report(
            operation=_operation(envelope.member),
            correlation_id=envelope.node_id,
            logger=ports.logger,
        ),
    )


def _claim(
    ports: ConsumerPorts, envelope: PostGrantEnvelope, work: ConsumerWork
) -> ClaimDisposition:
    enqueued, received = queue_attributes(
        {
            name: value
            for name, value in (
                ("SentTimestamp", envelope.sent_timestamp),
                ("ApproximateFirstReceiveTimestamp", envelope.first_receive_timestamp),
            )
            if value is not None
        }
    )
    return claim_graph_envelope(
        ports.store,
        UnreachablePostGrantFacts(),
        GraphClaimInput(
            run_id=envelope.run_id,
            node_id=envelope.node_id,
            dispatch_id=envelope.dispatch_id,
            body=envelope.body_text,
            now=ports.now,
            lease=work.lease,
            queue_enqueued_at=None if enqueued is None else enqueued.isoformat(),
            first_received_at=None if received is None else received.isoformat(),
        ),
    )


def _matches(envelope: PostGrantEnvelope, run: RunRecord) -> bool:
    """Check the consumer invariant's binding (AGG3-14): the envelope names `RUN#`'s grant."""
    try:
        served = datetime.fromisoformat(envelope.served_revision)
    except ValueError:
        return False
    return (
        run.state is RunState.GRANTED
        and run.grant_id == envelope.grant_id
        and envelope.granted_run == run.run_id
        and envelope.conflict_unit == run.conflict_unit
        and served == datetime.fromisoformat(run.retrieved_at)
    )


def _publish_effect(nodes: tuple[NodeRecord, ...], grant_id: str) -> PublishEffect | None:
    """Return `publish`'s counts; None unless it is `SUCCEEDED` with a record that re-hashes."""
    publish = next((node for node in nodes if node.name == PUBLISH_NODE), None)
    if publish is None or publish.state is not NodeState.SUCCEEDED:
        return None
    record, digest = publish.effect_record, publish.effect_digest
    if record is None or digest is None:
        return None
    if canonical_authority_digest([PUBLISH_EFFECT_NAMESPACE, record]) != digest:
        return None
    parsed: object = json.loads(record)
    if not isinstance(parsed, dict):
        return None
    fields = cast("dict[str, object]", parsed)
    advanced, curation = fields.get("advanced_count"), fields.get("advanced_curation_count")
    if (
        fields.get("grant_id") != grant_id
        or not isinstance(advanced, int)
        or not isinstance(curation, int)
    ):
        return None
    return PublishEffect(advanced, curation)


def _failure_reason(contribution: CohortContribution) -> str | None:
    reasons = {reason for _key, reason in contribution.unavailable}
    return next((reason for reason in _FAILING_ORDER if reason in reasons), None)


@dataclass(slots=True)
class _Run:
    ports: ConsumerPorts
    envelope: PostGrantEnvelope
    work: ConsumerWork
    claim: ClaimRecord


def _release(state: _Run) -> None:
    release_claim_lease(state.ports.store, state.claim, state.ports.now())


def _settle(  # noqa: PLR0913, PLR0917 -- the run, its stored contribution and one count
    state: _Run, run: RunRecord, contribution: CohortContribution, unit_count: int
) -> ConsumeResult:
    """Contribute (replay-safe) and settle the node from the stored contribution (D27)."""
    store, envelope = state.ports.store, state.envelope
    stored = contribute(store, contribution, run).contribution
    node_state = settlement_for(stored)
    reason = _failure_reason(stored) if node_state is NodeState.FAILED else None
    record, digest = consumer_effect_record(envelope.member, stored, state.work.counts())
    return _settle_node(
        state,
        run,
        (node_state, record, digest, reason),
        (
            unit_count,
            {"contribution": stored.outcome.value, "unavailable_count": len(stored.unavailable)},
        ),
    )


def _settle_node(  # noqa: PLR0913, PLR0917 -- the run, the settlement and what it logs
    state: _Run,
    run: RunRecord,
    settlement: tuple[NodeState, str, str, str | None],
    logged: tuple[int, dict[str, object]],
) -> ConsumeResult:
    """Settle the claimed node, re-deciding on a moved revision (D27)."""
    store, envelope = state.ports.store, state.envelope
    node_state, record, digest, reason = settlement
    unit_count, details = logged
    for _ in range(_MAX_REDECIDE):
        node = next(
            (item for item in store.read_nodes(run.run_id) if item.node_id == envelope.node_id),
            None,
        )
        claim = store.read_claim(envelope.node_id)
        if node is None:
            raise InvariantError(
                ErrorCode("GRAPH_POST_GRANT", 2), "a consumer node disappeared before settling."
            )
        if claim is None or claim.lease_version != state.claim.lease_version:
            _log((envelope, state.ports.logger), "graph_claim_lost")
            return ConsumeResult(True, "graph_claim_lost")
        if node.state in TERMINAL_NODE_STATES:
            return ConsumeResult(True, "graph_already_settled", node.state, unit_count=unit_count)
        try:
            store.settle_node(
                NodeSettlement(
                    run,
                    node,
                    node_state,
                    state.ports.now().isoformat(),
                    claim=state.claim,
                    effect_digest=digest,
                    failure_reason=reason,
                    effect_record=record,
                )
            )
        except GraphGuardError, GraphTransactionConflictError:
            continue
        outcome = f"graph_{envelope.member.value}_{node_state.value.lower()}"
        _log((envelope, state.ports.logger), outcome, **details, failure_reason=reason or "none")
        return ConsumeResult(True, outcome, node_state, reason, unit_count)
    _release(state)
    return ConsumeResult(False, "graph_redecide_exhausted", NodeState.CLAIMED)


def _contribution(  # noqa: PLR0913, PLR0917 -- one contribution builder
    run: RunRecord,
    member: CohortMember,
    outcome: CohortOutcome,
    unavailable: tuple[tuple[str, str], ...],
    produced: bool,
    at: datetime,
    input_effect_roster_digest: str | None = None,
    request_roster: tuple[tuple[str, str], ...] = (),
) -> CohortContribution:
    grant = cast("str", run.grant_id)
    revision = serving_generation(grant) if produced else None
    return CohortContribution(
        grant,
        member,
        outcome,
        revision,
        at,
        run.root_flow_id,
        tuple(sorted(unavailable)),
        input_effect_roster_digest,
        request_roster,
    )


def _invariant_settlement(state: _Run, run: RunRecord, error: BaseException) -> ConsumeResult:
    """AGG3-14: produce nothing; every artifact key is `post_grant_invariant`; settle FAILED."""
    state.ports.invariant_metric(CONSUMER_INVARIANT)
    _report(state.ports, state.envelope, error)
    keys = state.work.artifact_keys() or (state.envelope.member.value,)
    contribution = _contribution(
        run,
        state.envelope.member,
        CohortOutcome.UNAVAILABLE,
        tuple((key, POST_GRANT_INVARIANT) for key in keys),
        False,
        state.ports.now(),
    )
    return _settle(state, run, contribution, len(keys))


def _from_outcome(state: _Run, run: RunRecord, outcome: Produced) -> ConsumeResult:
    if outcome.unavailable:
        kind = CohortOutcome.UNAVAILABLE
    elif outcome.produced:
        kind = CohortOutcome.SUCCEEDED
    else:
        kind = CohortOutcome.NO_ELIGIBLE
    contribution = _contribution(
        run,
        state.envelope.member,
        kind,
        outcome.unavailable,
        outcome.produced,
        state.ports.now(),
        outcome.input_effect_roster_digest,
        outcome.request_roster,
    )
    return _settle(state, run, contribution, outcome.unit_count)


def _cohort_eligible(
    state: _Run, execution: ServingExecutionContext, effect: PublishEffect
) -> bool:
    """ADR-140: whether any member of the winner's cohort advanced what this work consumes.

    A `NO_ELIGIBLE` direct binding becomes every loser's alias, so eligibility is the cohort's.
    The winner's own effect is checked first (no read). Otherwise each other member's frozen
    `publish` effect is read on that member's own grant, in cohort order, until one is eligible.
    A member whose effect cannot be verified is a consumer invariant (U2), never a silent
    `NO_ELIGIBLE`.
    """
    if state.work.eligible(effect):
        return True
    store = state.ports.store
    for member_id in execution.run_ids:
        if member_id == state.envelope.run_id:
            continue
        member = store.read_run(member_id)
        found = (
            None
            if member is None or member.grant_id is None
            else _publish_effect(store.read_nodes(member_id), member.grant_id)
        )
        if found is None:
            raise InvariantError(
                ErrorCode("GRAPH_POST_GRANT", 8),
                "a cohort member's publish node has no verifiable effect record.",
                context={
                    "signal": POST_GRANT_INVARIANT,
                    "reason": "cohort_publish_effect",
                    "member_run_id": member_id,
                },
            )
        if state.work.eligible(found):
            return True
    return False


def _execute(state: _Run) -> ConsumeResult:  # noqa: PLR0911, PLR0912 -- one ordered §2.8 lifecycle
    store, envelope = state.ports.store, state.envelope
    run = store.read_run(envelope.run_id)
    node = next(
        (item for item in store.read_nodes(envelope.run_id) if item.node_id == envelope.node_id),
        None,
    )
    if run is None or run.grant_id is None or node is None:
        raise InvariantError(
            ErrorCode("GRAPH_POST_GRANT", 3),
            "a consumer envelope names a run without a grant, or no node.",
        )
    execution = store.read_serving_execution(run.grant_id, run.run_id)
    if execution is None or execution.head.winner_run_id != run.run_id:
        _log((envelope, state.ports.logger), "graph_stale_execution_attempt")
        return ConsumeResult(True, "graph_stale_execution_attempt")
    stored = next(
        (item for item in store.read_contributions(run.grant_id) if item.member is envelope.member),
        None,
    )
    if stored is not None:
        # AGG3-1: never recompute; settle from what the first attempt recorded.
        return _settle(state, run, stored, 0)
    if not _matches(envelope, run):
        return _invariant_settlement(
            state,
            run,
            InvariantError(
                ErrorCode("GRAPH_POST_GRANT", 4),
                "a consumer envelope's grant or served revision differs from RUN#.",
                context={"signal": POST_GRANT_INVARIANT, "reason": "grant_mismatch"},
            ),
        )
    effect = _publish_effect(store.read_nodes(run.run_id), run.grant_id)
    if effect is None:
        return _invariant_settlement(
            state,
            run,
            InvariantError(
                ErrorCode("GRAPH_POST_GRANT", 5),
                "the granted run's publish node has no verifiable effect record.",
                context={"signal": POST_GRANT_INVARIANT, "reason": "publish_effect"},
            ),
        )
    try:
        eligible = _cohort_eligible(state, execution, effect)
    except InvariantError as invariant:
        return _invariant_settlement(state, run, invariant)
    if not eligible:
        return _from_outcome(state, run, Produced((), produced=False))
    context = ConsumerContext(
        store,
        envelope,
        state.claim,
        run,
        node,
        state.ports.now,
        state.ports.remaining_ms,
        state.ports.logger,
    )
    try:
        outcome = state.work.run(context)
    except DependencyError:
        raise
    except InvariantError as invariant:
        return _invariant_settlement(state, run, invariant)
    except GraphGuardError, GraphTransactionConflictError:
        raise  # concurrency, not a deterministic error: the claim fence decides
    # ADR-140 (F4): any other error repeats on every receive, so the final one settles
    # `post_grant_invariant` instead of dead-lettering; `_invariant_settlement` reports it ERROR
    # and counts the invariant signal.
    except Exception as error:
        if not context.final:
            raise  # released and retried by `_consume`, as before
        return _invariant_settlement(state, run, error)
    if isinstance(outcome, LostClaim):
        _log((envelope, state.ports.logger), "graph_claim_lost")
        return ConsumeResult(True, "graph_claim_lost")
    if isinstance(outcome, ReleaseLease):
        _release(state)
        _log((envelope, state.ports.logger), "graph_lease_released", reason=outcome.reason)
        return ConsumeResult(False, "graph_lease_released", NodeState.CLAIMED)
    if isinstance(outcome, SettleWithoutContribution):
        return _settle_node(
            state,
            run,
            (NodeState.SUCCEEDED, outcome.record, outcome.digest, None),
            (outcome.unit_count, {"contribution": "none"}),
        )
    return _from_outcome(state, run, outcome)


def _consume(
    ports: ConsumerPorts, envelope: PostGrantEnvelope, work: ConsumerWork
) -> ConsumeResult:
    disposition = _claim(ports, envelope, work)
    claim = disposition.claim
    if claim is None:
        _log((envelope, ports.logger), disposition.outcome)
        return ConsumeResult(disposition.acknowledge, disposition.outcome)
    state = _Run(ports, envelope, work, claim)
    try:
        return _execute(state)
    except Exception as error:
        # A dependency (or an unclassified) failure never settles: release and retry, and on
        # the final receive the message dead-letters for the alarm and the redrive (§2.12).
        _report(ports, envelope, error)
        _release(state)
        return ConsumeResult(False, f"graph_{envelope.member.value}_retryable", NodeState.CLAIMED)


@trace_boundary(POST_GRANT_CONSUME)
def consume_post_grant(
    ports: ConsumerPorts, envelope: PostGrantEnvelope, work: ConsumerWork
) -> ConsumeResult:
    """Claim, check, work, contribute and settle one POST_GRANT node under one span (§2.8)."""
    with span(
        POST_GRANT_CONSUME,
        dispatch_id=envelope.dispatch_id,
        run_id=envelope.run_id,
        node_id=envelope.node_id,
        conflict_unit=envelope.conflict_unit,
        receive_count=envelope.receive_count,
        stage=envelope.member.value,
        completed=0,
        total=1,
        grant_id=envelope.grant_id,
        node=envelope.member.value,
    ) as recording:
        result = _consume(ports, envelope, work)
        recording.record(
            completed=1,
            outcome=result.outcome,
            node_state="none" if result.node_state is None else result.node_state.value,
            failure_reason=result.failure_reason or "none",
            unit_count=result.unit_count,
        )
    return result


@dataclass(frozen=True, slots=True)
class PostGrantRuntime:
    """A worker's composed graph consumer: the release store, its work and the wall clock."""

    store: ReleaseStore
    work: ConsumerWork
    now: Callable[[], datetime]
    logger: Logger | None = None
    invariant_metric: Callable[[str], None] = field(default=emit_post_grant_invariant)

    def consume(
        self, envelope: PostGrantEnvelope, remaining_ms: Callable[[], int]
    ) -> ConsumeResult:
        """Consume one envelope through the shared POST_GRANT protocol."""
        return consume_post_grant(
            ConsumerPorts(
                self.store,
                self.now,
                remaining_ms,
                invariant_metric=self.invariant_metric,
                logger=self.logger,
            ),
            envelope,
            self.work,
        )
