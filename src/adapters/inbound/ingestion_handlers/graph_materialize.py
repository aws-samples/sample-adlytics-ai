# SPDX-License-Identifier: MIT-0
"""`materialize_raw` runs in the delivery handler (S6 §2.4; D2, D16; AGG3-5, G2-5, G2-8).

Reachable only for a graph envelope (a body carrying `GRAPH_BINDING_FIELDS`) read in `GRAPH`.
A graph envelope read outside `GRAPH` makes zero effect, stays unacknowledged for the DLQ and is
counted once, as TableWriter's `GRAPH_DISPATCH_NOT_ACTIVE` row does.

1. **Claim.** `claim_graph_envelope` with a lease of the handler's timeout plus its visibility
   margin (900 + 60 s), so a takeover begins only after this invocation has ended. A
   `LIVE_DUPLICATE` gets `ChangeMessageVisibility` to the claim's remaining lease; a takeover
   settles from `MaterializationFacts`.
2. **Inputs.** The manifest key and version come from the run's admitting receipt. A missing
   receipt, or one bound to another run, is `FAILED` `admitting_receipt_missing`.
3. **Fetch and materialize.** The caller-supplied materializer fetches through the governed port
   under the two-member re-attestation (which sets `materialization_began_at`) and runs Semantic
   V2 with nothing servable advanced; the GRAPH feasibility callback bounds the roster at
   `GRAPH_MAX_UNITS` (11) and each unit's graph envelope at the provider ceiling, before any
   mutation. A refused re-attestation re-reads: a moved lease acknowledges with zero effect, a
   run that is no longer live settles through the takeover facts with no fence member.
4. **Settlement rule (AGG3-5, AGG3-18).** A `DependencyError` is retryable while receives remain;
   any other `AdlyticsError` is final at once; an unclassified exception is retryable until the
   final receive (`routes.delivery.max_receive_count`). A retryable outcome releases the lease
   and stays unacknowledged; a classified transient refusal (TD-61: `SIDECAR_BUSY`) also gets
   `ChangeMessageVisibility` to its short step (`SIDECAR_BUSY_RETRY_SECONDS x n`), and every
   other retryable outcome keeps the queue's visibility. A final one settles the node `FAILED`
   itself, with the fence member
   only while the run is OPEN or PAUSED (`NODE_FAILED`, or `OVERSIZE` for
   `unit_envelope_oversize`), reported once with `GRAPH_RUN_FAILED`.
5. **Settlement.** `SUCCEEDED` with the effect record (roster digest and zero-row targets), one
   PENDING `write_unit:<k>` per roster unit, `ARM#` (scope `units`) when the roster is not empty,
   `RELEASE#` always, and a `RUN#` check on the read generation. A run found FENCING at
   settlement settles `FAILED` `materialization_interrupted` through plain `settle_node`. A guard
   re-reads and re-decides up to `max_redecide`; exhausting it releases the lease and leaves the
   message unacknowledged.

Each claimed node opens exactly one `INGEST_GRAPH_MATERIALIZE` span, under the invocation's
`INGEST_HANDLER_PROCESS`, and the landing fetch's `INGEST_FETCH` nests beneath it.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from adapters.inbound.ingestion_handlers.delivery_plan import (
    FEASIBILITY_REFUSED,
    POST_AUTHORITY_INFEASIBLE,
)
from adapters.inbound.ingestion_handlers.graph_claim import (
    GraphClaimInput,
    claim_graph_envelope,
    release_claim_lease,
)
from adapters.jobs.execution_graph.node_verifier import (
    MATERIALIZATION_INTERRUPTED,
    MaterializationFacts,
)
from adapters.jobs.ingest_delivery.landing import ManifestInput
from adapters.jobs.ingest_delivery.semantic_sidecar import SIDECAR_BUSY
from adapters.outbound.dynamodb.execution_graph_dispatch import graph_envelope
from adapters.outbound.governed_materialization import (
    MANIFEST_INVALID,
    FullHistoryRefusedError,
    MaterializationClaim,
    MaterializationClaimLostError,
    graph_materialization_claim,
)
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import INGEST_GRAPH_MATERIALIZE, span, trace_boundary
from domain.operations.execution_graph import AuthorityMode
from domain.operations.execution_graph_dispatch import (
    CLAIM_LEASE_LIVE,
    GRAPH_BINDING_FIELDS,
    LIVE_RUN_STATES,
    TERMINAL_NODE_STATES,
    Acceptance,
    ArmScope,
    AttemptFact,
    EnvelopeKind,
    FenceReason,
    GraphGuardError,
    NodePhase,
    NodeState,
    table_writer_acceptance,
)
from domain.operations.execution_graph_release import INGESTION_TEMPLATE
from domain.operations.generated_execution_graphs import BOUNDS, CUTOVER, ROUTES, TEMPLATES
from domain.operations.ingestion_root import ManifestVersion, manifest_fingerprint
from domain.operations.queue_clocks import queue_attributes
from domain.operations.status import canonical_authority_digest
from domain.operations.write_plan import CanonicalUnitEntry, roster_digest
from domain.shared_kernel.errors import (
    AdlyticsError,
    DependencyError,
    ErrorCode,
    InvariantError,
)
from ports.outbound.execution_graph_dispatch import (
    ClaimRecord,
    FenceRunRequest,
    GraphDispatchStore,
    GraphTransactionConflictError,
    MaterializationSettlement,
    NodeRecord,
    NodeSettlement,
    NodeSpec,
    RunRecord,
    SuccessorRequest,
)

if TYPE_CHECKING:
    from aws_lambda_powertools import Logger

__all__ = [
    "ADMITTING_RECEIPT_MISSING",
    "DELIVERY_CLAIM_LEASE",
    "DELIVERY_QUEUE_VISIBILITY_SECONDS",
    "GRAPH_ENVELOPE_NOT_MATERIALIZE",
    "GRAPH_MAX_UNITS",
    "GRAPH_RUN_FAILED",
    "MATERIALIZE_EFFECT_NAMESPACE",
    "MATERIALIZE_NODE",
    "MAX_RECEIVES",
    "SIDECAR_BUSY_RETRY_SECONDS",
    "TRANSIENT_REFUSAL_CODES",
    "AdmittingReceiptMissingError",
    "GraphEnvelopeDelivery",
    "GraphEnvelopeRefusedError",
    "GraphMaterialization",
    "GraphRunFailedError",
    "MaterializeRequest",
    "MaterializeRuntime",
    "ZeroRowTarget",
    "classify_failure",
    "effect_record",
    "graph_envelope_delivery",
    "is_graph_envelope",
    "is_transient_refusal",
    "materialize_effect_digest",
    "materialize_graph_envelope",
    "transient_refusal_visibility_seconds",
    "unit_envelope",
    "unit_spec",
]

MATERIALIZE_NODE: Final = "materialize_raw"
MATERIALIZE_EFFECT_NAMESPACE: Final = "execution-graph-materialize-effect/v1"
_UNIT_NODE: Final = "write_unit"
GRAPH_MAX_UNITS: Final = int(BOUNDS["max_graph_nodes"]) - sum(
    1 for item in TEMPLATES[INGESTION_TEMPLATE]["nodes"] if item.get("dynamic") is not True
)
"""The graph fan-out bound (design §2.1): `N + 5 static nodes <= max_graph_nodes`, so 11."""
MAX_RECEIVES: Final = int(ROUTES["delivery"]["max_receive_count"])
"""The delivery route's receive budget; the receive that reaches it is the final one."""
DELIVERY_CLAIM_LEASE: Final = timedelta(
    seconds=int(CUTOVER["delivery_queue_timeout_seconds"])
    + int(CUTOVER["delivery_queue_visibility_margin_seconds"])
)
"""The claim lease: the handler's timeout plus its queue's visibility margin (900 + 60 s), so
the lease outlives the invocation and a takeover begins only after it has ended."""
_MAX_REDECIDE: Final = int(BOUNDS["max_redecide"])
_MAX_VISIBILITY_SECONDS: Final = 43_200
DELIVERY_QUEUE_VISIBILITY_SECONDS: Final = int(DELIVERY_CLAIM_LEASE.total_seconds())
"""The delivery queue's visibility timeout: the handler's timeout plus its margin, as the claim
lease (design §2.3). An unacknowledged return with no override waits this long."""
TRANSIENT_REFUSAL_CODES: Final = frozenset({SIDECAR_BUSY})
"""TD-61: the classified transient refusals that get a short redelivery instead of the full
visibility. Only `SIDECAR_BUSY` (`ADLY-SEMANTIC_SIDECAR-0002`, another live holder controls the
canonical partition's publication lease); every other retryable error keeps the queue's
visibility."""
SIDECAR_BUSY_RETRY_SECONDS: Final = 220
"""TD-61: the redelivery step for a `materialize_raw` refused `SIDECAR_BUSY` (owner: this module,
the delivery handler's settlement rule).

Receive `n` waits `220 x n` s instead of the queue's 960 s. With `routes.delivery`'s 3 receives,
the two waits before the final receive sum to 660 s: one full `PARTITION_LEASE_DURATION` (600 s)
plus the queue's 60 s visibility margin. So a holder that stopped without releasing has expired
before the last attempt, and the shorter schedule never fails a node the full timeout would have
let through. The waits only shorten; every receive still counts against `max_receive_count`.
"""
OPERATION: Final = "ingest_delivery"
"""The `Report` operation: the shared `Errors` metric's `Operation` dimension the
`adlytics-<env>-ingestion-alarming-errors-ingest-delivery` alarm watches."""

GRAPH_RUN_FAILED: Final = "GRAPH_RUN_FAILED"
"""S6 §2.4: every final `materialize_raw` settlement is reported once with this signal."""
GRAPH_ENVELOPE_NOT_MATERIALIZE: Final = "GRAPH_ENVELOPE_NOT_MATERIALIZE"
"""A graph envelope on the delivery route whose owner body is not `materialize_raw`."""
ADMITTING_RECEIPT_MISSING: Final = "admitting_receipt_missing"
_FULL_HISTORY: Final = "full_history_not_enabled"
_DEPENDENCY_EXHAUSTED: Final = "dependency_exhausted"
_UNCLASSIFIED: Final = "unclassified_error"
_ENVELOPE_OVERSIZE: Final = "unit_envelope_oversize"
_ADMITTING: Final = frozenset({"ADMITTED", "REPLACED_BEFORE_COMPUTE", "REPLACED_ON_INVALIDATION"})


class GraphEnvelopeRefusedError(InvariantError):
    """A graph envelope the delivery handler does not accept: zero effect, unacknowledged."""

    def __init__(self, signal: str) -> None:
        super().__init__(
            ErrorCode("STAGE1_DELIVERY", 6),
            "a graph envelope reached the delivery handler where it is not accepted.",
            context={"signal": signal},
        )
        self.signal = signal


class AdmittingReceiptMissingError(InvariantError):
    """The run has no admitting receipt that names its manifest version (non-retryable)."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            ErrorCode("STAGE1_DELIVERY", 7),
            "the materialize_raw run has no admitting receipt naming its manifest version.",
            context={"signal": ADMITTING_RECEIPT_MISSING, "detail": detail},
        )


class GraphRunFailedError(InvariantError):
    """One final `materialize_raw` settlement, reported once (`GRAPH_RUN_FAILED`)."""

    def __init__(self, failure_reason: str, cause: BaseException, *, fenced: bool) -> None:
        cause_code = str(cause.code) if isinstance(cause, AdlyticsError) else type(cause).__name__
        super().__init__(
            ErrorCode("STAGE1_DELIVERY", 8),
            "a materialize_raw node settled FAILED; its run is fenced or already fencing.",
            context={
                "signal": GRAPH_RUN_FAILED,
                "failure_reason": failure_reason,
                "cause": cause_code,
                "fenced": fenced,
            },
        )
        self.signal = GRAPH_RUN_FAILED


# --- The envelope ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphEnvelopeDelivery:
    """One received graph envelope on the delivery route, and its SQS receive."""

    body: str
    run_id: str
    node_id: str
    dispatch_id: str
    node: str
    """The owner body's node name; the delivery route carries only `materialize_raw`."""
    conflict_unit: str
    message_id: str
    receipt_handle: str
    receive_count: int
    queue_url: str
    queue_enqueued_at: str | None = None
    first_received_at: str | None = None


def is_graph_envelope(body: Mapping[str, object]) -> bool:
    """Return whether a body carries the three dispatch-binding fields (§2.4 Parse)."""
    return frozenset(body) >= GRAPH_BINDING_FIELDS


def _field(body: Mapping[str, object], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"a graph envelope requires a nonblank {name}.")
    return value


def graph_envelope_delivery(  # noqa: PLR0913 -- one SQS record's independent facts
    body_text: str,
    body: Mapping[str, object],
    attributes: Mapping[str, str],
    *,
    message_id: str,
    receipt_handle: str,
    receive_count: int,
    queue_url: str,
) -> GraphEnvelopeDelivery:
    """Parse one graph envelope; the claim then checks its binding against the node."""
    enqueued, received = queue_attributes(attributes)
    return GraphEnvelopeDelivery(
        body=body_text,
        run_id=_field(body, "run_id"),
        node_id=_field(body, "node_id"),
        dispatch_id=_field(body, "dispatch_id"),
        node=str(body.get("node", "")),
        conflict_unit=str(body.get("conflict_unit", "")),
        message_id=message_id,
        receipt_handle=receipt_handle,
        receive_count=receive_count,
        queue_url=queue_url,
        queue_enqueued_at=None if enqueued is None else enqueued.isoformat(),
        first_received_at=None if received is None else received.isoformat(),
    )


# --- Units, envelopes and the effect record -------------------------------------------------------


def _unit_body(run: RunRecord, entry: CanonicalUnitEntry) -> str:
    """Render a unit node's owner body (design §2.5 Envelope), without the dispatch binding."""
    return json.dumps(
        {
            "schema_version": "1",
            "node": _UNIT_NODE,
            "unit": entry.to_member(),
            "conflict_unit": run.conflict_unit,
            "root_flow_id": run.root_flow_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def unit_spec(run: RunRecord, entry: CanonicalUnitEntry) -> NodeSpec:
    """Return the PENDING `write_unit:<k>` successor the settlement puts for one roster unit."""
    return NodeSpec(
        name=f"{_UNIT_NODE}:{entry.unit_key}",
        after=(MATERIALIZE_NODE,),
        phase=NodePhase.PRE_GRANT,
        lock_key=f"TABLE#{entry.unit_key}",
        group_id=entry.unit_key,
        body=_unit_body(run, entry),
    )


def unit_envelope(run: RunRecord) -> Callable[[CanonicalUnitEntry], str]:
    """Render each unit's exact graph envelope for the feasibility callback's size bound.

    These are the bytes the settlement stores and the dispatcher sends: the owner body plus its
    dispatch binding, measured as instantiation measures it.
    """

    def render(entry: CanonicalUnitEntry) -> str:
        return graph_envelope(run.run_id, unit_spec(run, entry)).body

    return render


@dataclass(frozen=True, slots=True)
class ZeroRowTarget:
    """One committed rowless publication `publish` stages (§2.6), instead of advancing it here."""

    partition: str
    publication_id: str
    fencing_token: int


def effect_record(
    units: tuple[CanonicalUnitEntry, ...], targets: tuple[ZeroRowTarget, ...]
) -> tuple[str, str]:
    """Return the canonical effect record and its digest (the materialization receipt)."""
    ordered = sorted(targets, key=lambda target: (target.partition, target.publication_id))
    record = json.dumps(
        {
            "schema_version": "1",
            "node": MATERIALIZE_NODE,
            "roster_digest": roster_digest(units),
            "units": [entry.unit_key for entry in units],
            "zero_row_targets": [
                {
                    "partition": target.partition,
                    "publication_id": target.publication_id,
                    "fencing_token": target.fencing_token,
                }
                for target in ordered
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return record, materialize_effect_digest(record)


def materialize_effect_digest(record: str) -> str:
    """Hash one `materialize_raw` effect record; `publish` re-hashes it on validation (§2.6)."""
    return canonical_authority_digest([MATERIALIZE_EFFECT_NAMESPACE, record])


# --- The runtime ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MaterializeRequest:
    """What the delivery handler's materializer needs for one claimed node."""

    manifest: ManifestInput
    claim: MaterializationClaim
    run: RunRecord
    node_id: str
    message_id: str
    receive_count: int
    now: datetime


@dataclass(frozen=True, slots=True)
class GraphMaterialization:
    """One committed materialization: the published roster and its rowless targets."""

    units: tuple[CanonicalUnitEntry, ...]
    zero_row_targets: tuple[ZeroRowTarget, ...]
    report_terminal: Callable[[float], None]
    """Write the delivery metrics and the terminal record, given the elapsed milliseconds."""


def _no_count() -> None:
    return None


@dataclass(frozen=True, slots=True)
class MaterializeRuntime:
    """The delivery handler's collaborators for the graph materialize path."""

    store: GraphDispatchStore
    mode: Callable[[], AuthorityMode]
    materialize: Callable[[MaterializeRequest], GraphMaterialization]
    visibility: Callable[[str, str, int], None]
    lease: timedelta
    logger: Logger
    now: Callable[[], datetime]
    count_delivery: Callable[[], None] = _no_count
    """`IngestDeliveries`, once per claimed receive."""
    monotonic: Callable[[], float] = time.monotonic


@dataclass(frozen=True, slots=True)
class _Claimed:
    delivery: GraphEnvelopeDelivery
    claim: ClaimRecord
    report: Report


@dataclass(frozen=True, slots=True)
class _Outcome:
    acknowledge: bool
    outcome: str
    node_state: NodeState | None = None
    failure_reason: str | None = None
    unit_count: int = 0


@dataclass(frozen=True, slots=True)
class _Current:
    run: RunRecord
    node: NodeRecord
    claim: ClaimRecord | None
    lost: bool


# --- The settlement rule --------------------------------------------------------------------------


def _failure_reason(error: AdlyticsError) -> str:
    if isinstance(error, FullHistoryRefusedError):
        return _FULL_HISTORY
    if isinstance(error, AdmittingReceiptMissingError):
        return ADMITTING_RECEIPT_MISSING
    if error.code == FEASIBILITY_REFUSED:
        return str(error.context.get("reason", "feasibility_refused"))
    if error.code == POST_AUTHORITY_INFEASIBLE:
        return "post_authority_infeasible"
    if error.code == MANIFEST_INVALID:
        return "invalid_manifest"
    return str(error.code)


def classify_failure(error: BaseException, *, final: bool) -> tuple[bool, str]:
    """Classify one outcome once, by error class: `(retryable, failure_reason)`.

    A `DependencyError` is retryable while receives remain and `dependency_exhausted` on the
    final one; any other `AdlyticsError` is final at once with its own reason; an unclassified
    exception is retryable until the final receive, then `unclassified_error`.
    """
    if isinstance(error, DependencyError):
        return not final, _DEPENDENCY_EXHAUSTED
    if isinstance(error, AdlyticsError):
        return False, _failure_reason(error)
    return not final, _UNCLASSIFIED


def is_transient_refusal(error: BaseException) -> bool:
    """TD-61: whether a retryable error is a classified transient refusal (by its exact code)."""
    return isinstance(error, DependencyError) and error.code in TRANSIENT_REFUSAL_CODES


def transient_refusal_visibility_seconds(receive_count: int) -> int:
    """TD-61: the short redelivery for a transient refusal on receive `receive_count`.

    `SIDECAR_BUSY_RETRY_SECONDS x n`, never longer than the queue's own visibility.
    """
    return min(
        SIDECAR_BUSY_RETRY_SECONDS * max(1, receive_count), DELIVERY_QUEUE_VISIBILITY_SECONDS
    )


def _log(claimed: _Claimed, outcome: str, severity: LogSeverity, **details: object) -> None:
    delivery = claimed.delivery
    report_operation(
        OperationLog(
            operation=OPERATION,
            component="ingest_handler",
            phase="graph_materialize",
            outcome=outcome,
            severity=severity,
            details={
                "run_id": delivery.run_id,
                "node_id": delivery.node_id,
                "dispatch_id": delivery.dispatch_id,
                "receive_count": delivery.receive_count,
                **details,
            },
        ),
        claimed.report.logger,
    )


def _reread(store: GraphDispatchStore, claimed: _Claimed) -> _Current:
    delivery = claimed.delivery
    run = store.read_run(delivery.run_id)
    node = None
    if run is not None:
        node = next(
            (
                item
                for item in store.read_nodes(delivery.run_id)
                if item.node_id == delivery.node_id
            ),
            None,
        )
    if run is None or node is None:
        raise InvariantError(
            ErrorCode("EXEC_GRAPH", 23), "a graph envelope names a run or node that does not exist."
        )
    claim = store.read_claim(delivery.node_id)
    lost = claim is None or claim.lease_version != claimed.claim.lease_version
    return _Current(run, node, claim, lost)


def _lost(claimed: _Claimed) -> _Outcome:
    _log(claimed, "graph_claim_lost", LogSeverity.WARNING)
    return _Outcome(True, "claim_lost")


def _exhausted(runtime: MaterializeRuntime, claimed: _Claimed) -> _Outcome:
    release_claim_lease(runtime.store, claimed.claim, runtime.now())
    _log(claimed, "graph_redecide_exhausted", LogSeverity.INFO)
    return _Outcome(False, "redecide_exhausted")


def _settle_failed(  # noqa: PLR0913, PLR0917 -- the settlement's reason and its cause
    runtime: MaterializeRuntime,
    claimed: _Claimed,
    reason: str,
    cause: BaseException,
) -> _Outcome:
    """Settle the node `FAILED` with its reason, fencing the run only while it is live."""
    fence_reason = FenceReason.OVERSIZE if reason == _ENVELOPE_OVERSIZE else FenceReason.NODE_FAILED
    for _ in range(_MAX_REDECIDE):
        current = _reread(runtime.store, claimed)
        if current.lost:
            return _lost(claimed)
        if current.node.state in TERMINAL_NODE_STATES:
            return _Outcome(True, "already_settled", current.node.state)
        now = runtime.now().isoformat()
        live = current.run.state in LIVE_RUN_STATES
        try:
            runtime.store.settle_materialization(
                MaterializationSettlement(
                    NodeSettlement(
                        current.run,
                        current.node,
                        NodeState.FAILED,
                        now,
                        claim=claimed.claim,
                        failure_reason=reason,
                    ),
                    fence=FenceRunRequest(current.run, fence_reason, now) if live else None,
                )
            )
        except GraphGuardError, GraphTransactionConflictError:
            continue
        report_error(GraphRunFailedError(reason, cause, fenced=live), claimed.report)
        return _Outcome(True, "failed", NodeState.FAILED, reason)
    return _exhausted(runtime, claimed)


def _settle_failure(
    runtime: MaterializeRuntime, claimed: _Claimed, error: BaseException
) -> _Outcome:
    retryable, reason = classify_failure(
        error, final=claimed.delivery.receive_count >= MAX_RECEIVES
    )
    if not retryable:
        return _settle_failed(runtime, claimed, reason, error)
    # S6 §2.3: a deliberate unacknowledged return releases the lease first, so the redelivery
    # reads RECLAIM rather than spending a receive as LIVE_DUPLICATE.
    report_error(error, claimed.report)
    release_claim_lease(runtime.store, claimed.claim, runtime.now())
    if is_transient_refusal(error):
        _shorten_redelivery(runtime, claimed, error)
    return _Outcome(False, "retryable", NodeState.CLAIMED)


def _shorten_redelivery(
    runtime: MaterializeRuntime, claimed: _Claimed, error: BaseException
) -> None:
    """TD-61: redeliver a transient refusal after its short step, not the full visibility.

    Availability only: if the override cannot be applied, the queue's visibility applies, which
    is the behaviour without it. The receive still counts against `max_receive_count`.
    """
    delivery = claimed.delivery
    if not delivery.queue_url:
        return
    seconds = transient_refusal_visibility_seconds(delivery.receive_count)
    code = str(error.code) if isinstance(error, AdlyticsError) else type(error).__name__
    try:
        runtime.visibility(delivery.queue_url, delivery.receipt_handle, seconds)
    # error-propagation: availability only; logged with its cause, queue visibility applies
    except Exception as failure:
        _log(
            claimed,
            "graph_transient_refusal_visibility_unavailable",
            LogSeverity.WARNING,
            error_code=code,
            cause=type(failure).__name__,
        )
        return
    _log(
        claimed,
        "graph_transient_refusal_redelivery",
        LogSeverity.INFO,
        error_code=code,
        visibility_seconds=seconds,
    )


def _interrupted(runtime: MaterializeRuntime, claimed: _Claimed, current: _Current) -> _Outcome:
    """G2-5: the run is FENCING at settlement. Plain `settle_node`, which has no run check."""
    runtime.store.settle_node(
        NodeSettlement(
            current.run,
            current.node,
            NodeState.FAILED,
            runtime.now().isoformat(),
            claim=claimed.claim,
            failure_reason=MATERIALIZATION_INTERRUPTED,
        )
    )
    _log(claimed, "graph_materialization_interrupted", LogSeverity.INFO)
    return _Outcome(True, "interrupted", NodeState.FAILED, MATERIALIZATION_INTERRUPTED)


def _settle_succeeded(
    runtime: MaterializeRuntime, claimed: _Claimed, materialized: GraphMaterialization
) -> _Outcome:
    record, digest = effect_record(materialized.units, materialized.zero_row_targets)
    for _ in range(_MAX_REDECIDE):
        current = _reread(runtime.store, claimed)
        if current.lost:
            return _lost(claimed)
        if current.node.state in TERMINAL_NODE_STATES:
            return _Outcome(True, "already_settled", current.node.state)
        try:
            if current.run.state not in LIVE_RUN_STATES:
                return _interrupted(runtime, claimed, current)
            now = runtime.now().isoformat()
            units = tuple(unit_spec(current.run, entry) for entry in materialized.units)
            runtime.store.settle_materialization(
                MaterializationSettlement(
                    NodeSettlement(
                        current.run,
                        current.node,
                        NodeState.SUCCEEDED,
                        now,
                        claim=claimed.claim,
                        effect_digest=digest,
                        effect_record=record,
                    ),
                    successors=SuccessorRequest(
                        current.run,
                        units,
                        ArmScope.UNITS if units else None,
                        True,
                        now,
                    ),
                )
            )
        except GraphGuardError, GraphTransactionConflictError:
            continue
        return _Outcome(True, "succeeded", NodeState.SUCCEEDED, unit_count=len(units))
    return _exhausted(runtime, claimed)


def _claim_lost(
    runtime: MaterializeRuntime, claimed: _Claimed, error: MaterializationClaimLostError
) -> _Outcome:
    """AGG3-5: the re-attestation was refused; re-read the claim and the run strongly."""
    for _ in range(_MAX_REDECIDE):
        current = _reread(runtime.store, claimed)
        if current.lost:
            return _lost(claimed)
        if current.node.state in TERMINAL_NODE_STATES:
            return _Outcome(True, "already_settled", current.node.state)
        if current.run.state in LIVE_RUN_STATES:
            # Both re-attested conditions hold again, which no protocol path produces: the
            # refusal is an invariant, final under the settlement rule.
            return _settle_failed(runtime, claimed, str(error.code), error)
        fact, _ = MaterializationFacts().attempt(current.node, current.claim)
        failed = fact is AttemptFact.FAILED
        state = NodeState.FAILED if failed else NodeState.SUPERSEDED_UNCLAIMED
        reason = MATERIALIZATION_INTERRUPTED if failed else None
        try:
            runtime.store.settle_node(
                NodeSettlement(
                    current.run,
                    current.node,
                    state,
                    runtime.now().isoformat(),
                    claim=claimed.claim,
                    failure_reason=reason,
                )
            )
        except GraphGuardError, GraphTransactionConflictError:
            continue
        _log(claimed, "graph_takeover_settled", LogSeverity.INFO, node_state=state.value)
        return _Outcome(True, "takeover_settled", state, reason)
    return _exhausted(runtime, claimed)


def _admitting_manifest(store: GraphDispatchStore, run: RunRecord) -> ManifestInput:
    """Read the manifest version from the run's admitting receipt, bound to this run."""
    receipt = store.read_receipt(run.receipt_request_id)
    if receipt is None:
        raise AdmittingReceiptMissingError("no receipt")
    admitting = receipt.decision in _ADMITTING or (
        run.promoted and receipt.decision == "QUEUED_LATEST"
    )
    if not admitting or receipt.fingerprint != run.fingerprint:
        raise AdmittingReceiptMissingError("receipt of another run")
    if not receipt.manifest_key or not receipt.version_id:
        raise AdmittingReceiptMissingError("receipt names no manifest version")
    version = ManifestVersion(receipt.manifest_key, receipt.version_id)
    if manifest_fingerprint(version, run.template, receipt.contract_revision) != run.fingerprint:
        raise AdmittingReceiptMissingError("manifest version of another run")
    return ManifestInput(receipt.manifest_key, receipt.version_id)


def _execute(runtime: MaterializeRuntime, claimed: _Claimed) -> _Outcome:
    started = runtime.monotonic()
    store = runtime.store
    try:
        current = _reread(store, claimed)
        manifest = _admitting_manifest(store, current.run)
        now = runtime.now()
        materialized = runtime.materialize(
            MaterializeRequest(
                manifest=manifest,
                claim=graph_materialization_claim(store, claimed.claim, now.isoformat()),
                run=current.run,
                node_id=claimed.delivery.node_id,
                message_id=claimed.delivery.message_id,
                receive_count=claimed.delivery.receive_count,
                now=now,
            )
        )
    except MaterializationClaimLostError as lost:
        return _claim_lost(runtime, claimed, lost)
    except Exception as error:
        # The settlement rule classifies every outcome, unclassified ones included.
        return _settle_failure(runtime, claimed, error)
    outcome = _settle_succeeded(runtime, claimed, materialized)
    if outcome.outcome == "succeeded":
        materialized.report_terminal((runtime.monotonic() - started) * 1000.0)
    return outcome


@trace_boundary(INGEST_GRAPH_MATERIALIZE)
def _materialize_claimed(runtime: MaterializeRuntime, claimed: _Claimed) -> bool:
    """Materialize one claimed node under its one `INGEST_GRAPH_MATERIALIZE` span."""
    delivery = claimed.delivery
    with span(
        INGEST_GRAPH_MATERIALIZE,
        dispatch_id=delivery.dispatch_id,
        run_id=delivery.run_id,
        node_id=delivery.node_id,
        conflict_unit=delivery.conflict_unit,
        receive_count=delivery.receive_count,
        stage="materialize",
        completed=0,
        total=1,
    ) as recording:
        result = _execute(runtime, claimed)
        recording.record(
            completed=1,
            outcome=result.outcome,
            node_state="none" if result.node_state is None else result.node_state.value,
            failure_reason=result.failure_reason or "none",
            unit_count=result.unit_count,
        )
    return result.acknowledge


def _hold_duplicate(runtime: MaterializeRuntime, delivery: GraphEnvelopeDelivery) -> None:
    """Hold a copy that met a live claim for the claim's remaining lease (§2.4 Duplicates)."""
    claim = runtime.store.read_claim(delivery.node_id)
    if claim is None or not delivery.queue_url:
        return
    remaining = datetime.fromisoformat(claim.lease_expires_at) - runtime.now()
    seconds = min(max(math.ceil(remaining.total_seconds()), 0), _MAX_VISIBILITY_SECONDS)
    runtime.visibility(delivery.queue_url, delivery.receipt_handle, seconds)


def materialize_graph_envelope(
    runtime: MaterializeRuntime, delivery: GraphEnvelopeDelivery
) -> bool:
    """Gate, claim and materialize one graph envelope; return whether it is acknowledged."""
    report = Report(operation=OPERATION, correlation_id=delivery.node_id, logger=runtime.logger)
    acceptance = table_writer_acceptance(EnvelopeKind.GRAPH, runtime.mode(), legacy_terminal=False)
    if acceptance is not Acceptance.GRAPH_CLAIM or delivery.node != MATERIALIZE_NODE:
        signal = (
            acceptance.value
            if acceptance is not Acceptance.GRAPH_CLAIM
            else GRAPH_ENVELOPE_NOT_MATERIALIZE
        )
        # Zero effect, left unacknowledged for the DLQ, and the invariant counted once.
        report_error(GraphEnvelopeRefusedError(signal), report)
        return False
    try:
        disposition = claim_graph_envelope(
            runtime.store,
            MaterializationFacts(),
            GraphClaimInput(
                run_id=delivery.run_id,
                node_id=delivery.node_id,
                dispatch_id=delivery.dispatch_id,
                body=delivery.body,
                now=runtime.now,
                lease=runtime.lease,
                queue_enqueued_at=delivery.queue_enqueued_at,
                first_received_at=delivery.first_received_at,
            ),
        )
        claim = disposition.claim
        if claim is None:
            if disposition.signal == CLAIM_LEASE_LIVE:
                _hold_duplicate(runtime, delivery)
            return disposition.acknowledge
        runtime.count_delivery()
        return _materialize_claimed(runtime, _Claimed(delivery, claim, report))
    except Exception as error:
        # A claim or settlement that cannot commit: the message stays for redelivery and,
        # past its receives, dead-letters (§2.12).
        report_error(error, report)
        raise
