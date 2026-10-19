# SPDX-License-Identifier: MIT-0
"""``ProviderAttemptReconciler``: settle due nonterminal table attempts (write discipline §5).

One scheduled tick issues one bounded ``Query`` over the ``pending`` partition of
``table-attempt-reconcile`` and, for every returned base key, strongly reads the ``FENCE``, the
named ``ATTEMPT``, its exact owner, and the lease, then applies the closed §3.1 decision before any
provider or coordinator call: a clear fence is a no-op; a ``core_invalid`` pair is the hard
``CORRUPT_PROVIDER_CORE_AUTHORITY`` failure with zero mutation; repairable reconciliation metadata
is typed-blocked; a canonical pending pair is observed exactly once and then completed, parked,
recovered, cancelled, or rescheduled. Span-free by design (§10): structured operation logs through
the shared reporting seam only.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Final

import boto3
from aws_lambda_powertools import Logger

from adapters.config import table_attempt_reconciler_settings
from adapters.outbound.athena.table_attempt_start import Stage1AthenaStartClient
from adapters.outbound.athena.table_attempt_terminal import Stage1AthenaTerminalClient
from adapters.outbound.dynamodb.projection_coordinator import DynamoProjectionCoordinator
from adapters.outbound.dynamodb.stage1_discovery import DynamoStage1Discovery
from adapters.outbound.dynamodb.stage1_lifecycle_head import DynamoStage1LifecycleHeadStore
from adapters.outbound.dynamodb.stage1_writer_authority import Stage1WriterAuthorityReader
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_operation,
)
from domain.operations.stage1_lifecycle_head import (
    Stage1LifecycleProof,
    Stage1LifecycleProofRequest,
    Stage1LifecycleTransition,
    derive_stage1_lifecycle_proof,
    stage1_head_authority_document,
)
from domain.operations.stage1_operational_status import (
    InlineIdentityRequest,
    PriorOwnerEventSource,
    Stage1OwnerTransition,
    Stage1TopologyRequest,
)
from domain.operations.status import FlowState
from domain.operations.table_attempt import (
    TABLE_ATTEMPT_PREFIX,
    ActiveFence,
    AttemptObservation,
    CoreValidityRequest,
    FenceObservation,
    InlineRunOwnerObservation,
    IntentAttempt,
    ParkedAttempt,
    ParkedFence,
    PlanUnitOwnerObservation,
    SubmittedAttempt,
    TerminalAttempt,
    core_valid,
    parse_attempt,
    parse_fence,
    recompute_attempt_identity,
    reconciliation_valid,
)
from domain.operations.write_plan import IntentState
from domain.shared_kernel.errors import (
    DependencyError,
    DomainError,
    ErrorCode,
    InvariantError,
)
from ports.outbound.projection_coordinator import (
    BlockTableAttemptReconcileRequest,
    CompleteTableAttemptRequest,
    InlineRunAttemptOwner,
    MalformedReconciliationMetadataObservation,
    ParkAttemptRequest,
    QueuedUnitAttemptOwner,
    RecoverParkedAttemptSubmissionRequest,
    ReleaseTableFenceRequest,
    RequestAttemptCancellationRequest,
    RescheduleTableAttemptReconcileRequest,
    SameSnapshotRescheduleRejectedObservation,
    TableAttemptOwnerSnapshot,
)
from ports.outbound.stage1_discovery import DiscoveredKey, DueQuery
from ports.outbound.table_attempt_start import Accepted, StartTableAttemptRequest
from ports.outbound.table_attempt_terminal import ProviderTerminalObservation

if TYPE_CHECKING:
    from collections.abc import Callable

    from domain.operations.table_lease import TableLeaseRecord
    from ports.outbound.stage1_discovery import Stage1DiscoveryPort
    from ports.outbound.table_attempt_start import TableAttemptStartClient
    from ports.outbound.table_attempt_terminal import TableAttemptTerminalClient

__all__ = [
    "CORRUPT_PROVIDER_CORE_AUTHORITY",
    "PARKED_CANCEL_DUE_SECONDS",
    "ProviderAttemptReconciler",
    "handler",
]

_LOGGER = Logger(service="adlytics-provider-attempt-reconciler")
_OPERATION = "stage1_provider_attempt.reconcile"
_COMPONENT = "provider_attempt_reconciler"

CORRUPT_PROVIDER_CORE_AUTHORITY: Final = ErrorCode("PROVIDER_RECONCILE", 1)
"""The hard-failure branch: a core-invalid pending candidate; nothing is mutated."""

PARKED_CANCEL_DUE_SECONDS: Final = 780
"""§5: a parked nonterminal is cancelled at ``submitted_at + 780 s``."""

_RECONCILED_TRANSITION: Final = "semantic_inline_run_reconciled"
_CANCEL_TRANSITION: Final = "semantic_inline_run_cancel_requested"
_RUN_BEGIN_STATES: Final = frozenset({"open", "reconciled"})

# The closed per-candidate outcome vocabulary this log-only component emits (§10).
_OUTCOMES: Final = frozenset(
    {
        "clear_noop",
        "completed",
        "parked",
        "recovered_rescheduled",
        "cancelled_rescheduled",
        "rescheduled",
        "blocked",
        "reread",
        "dependency_rescheduled",
    }
)


def _now_instant() -> str:
    return f"{int(datetime.now(UTC).timestamp() * 1_000_000):020d}"


def _corrupt(description: str, table_id: str) -> InvariantError:
    return InvariantError(
        CORRUPT_PROVIDER_CORE_AUTHORITY,
        f"CORRUPT_PROVIDER_CORE_AUTHORITY: {description}",
        context={"physical_table_id": table_id},
    )


def _int(item: dict[str, object], name: str) -> int | None:
    value = item.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(item: dict[str, object], name: str) -> str | None:
    value = item.get(name)
    return value if isinstance(value, str) else None


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One due pending candidate's strongly read snapshots."""

    table_id: str
    fence: FenceObservation
    attempt: AttemptObservation
    owner: TableAttemptOwnerSnapshot
    lease: TableLeaseRecord


class ProviderAttemptReconciler:
    """The §5 reconciler over its strongly consistent reads and typed coordinator requests."""

    def __init__(  # noqa: PLR0913, PLR0917 -- the six collaborators, a batch bound, and a clock
        self,
        discovery: Stage1DiscoveryPort,
        reader: Stage1WriterAuthorityReader,
        coordinator: DynamoProjectionCoordinator,
        start: TableAttemptStartClient,
        terminal: TableAttemptTerminalClient,
        batch_size: int,
        now: Callable[[], str] = _now_instant,
    ) -> None:
        self._discovery = discovery
        self._reader = reader
        self._coordinator = coordinator
        self._start = start
        self._terminal = terminal
        self._batch_size = batch_size
        self._now = now

    def run_once(self) -> dict[str, int]:
        """Reconcile one bounded page of due candidates; return the closed outcome counters."""
        now = self._now()
        keys = self._discovery.due_pending_fences(DueQuery(now, self._batch_size))
        counters: Counter[str] = Counter()
        for key in keys:
            outcome = self._reconcile_key(key)
            if outcome not in _OUTCOMES:  # pragma: no cover -- closed by construction
                raise InvariantError(
                    CORRUPT_PROVIDER_CORE_AUTHORITY, "an outcome escaped the closed vocabulary."
                )
            counters[outcome] += 1
        return {"candidates": len(keys), **dict(sorted(counters.items()))}

    # -- one candidate -----------------------------------------------------------------------------

    def _reconcile_key(self, key: DiscoveredKey) -> str:
        if not key.partition_key.startswith(TABLE_ATTEMPT_PREFIX) or key.record_key != "FENCE":
            raise _corrupt("a pending-index key is outside the FENCE key form.", key.partition_key)
        table_id = key.partition_key[len(TABLE_ATTEMPT_PREFIX) :]
        candidate = self._observe(table_id)
        if candidate is None:
            return "clear_noop"
        try:
            return self._decide(candidate)
        except DependencyError as error:
            # Any candidate-local provider/dependency failure is reported once and rescheduled; a
            # failing reschedule itself propagates so the tick fails loudly.
            report_error(
                error,
                Report(operation=_OPERATION, correlation_id=table_id, logger=_LOGGER),
            )
            self._reschedule(table_id)
            return "dependency_rescheduled"
        except DomainError:
            # A lost condition: the pair moved under us; the next tick rereads and re-evaluates.
            return "reread"

    def _observe(self, table_id: str) -> _Candidate | None:
        fence = self._reader.fence(table_id)
        if fence is None or fence.state == "clear":
            return None
        attempt_id = fence.attempt_id
        attempt = None if attempt_id is None else self._reader.attempt(table_id, attempt_id)
        owner = None if attempt is None else self._reader.attempt_owner(attempt)
        request = CoreValidityRequest(
            table_id=table_id,
            fence=fence,
            attempt=attempt,
            recompute_identity=recompute_attempt_identity,
            plan_unit_owner=self._plan_owner_observation(owner),
            inline_run_owner=_inline_owner_observation(owner),
        )
        if attempt is None or owner is None or not core_valid(request):
            raise _corrupt("a due pending FENCE/ATTEMPT/owner triple is core-invalid.", table_id)
        lease = self._reader.lease(table_id)
        if lease is None:
            raise _corrupt("a core-valid active or parked fence has no lease record.", table_id)
        return _Candidate(table_id, fence, attempt, owner, lease)

    def _plan_owner_observation(
        self, owner: TableAttemptOwnerSnapshot | None
    ) -> PlanUnitOwnerObservation | None:
        if not isinstance(owner, QueuedUnitAttemptOwner):
            return None
        item = dict(owner.item)
        meta = self._reader.plan_meta_item(owner.plan_key)
        admitted = False
        if meta is not None:
            plan_digest = _text(meta, "plan_digest")
            intent_id = _text(meta, "origin_intent_id")
            if plan_digest is not None and intent_id is not None:
                intent = self._reader.intent(plan_digest, intent_id)
                admitted = intent is not None and intent.state is IntentState.ADMITTED
        return PlanUnitOwnerObservation(
            plan_partition_key=owner.plan_key,
            unit_record_key=owner.unit_record_key,
            plan_meta_exists=meta is not None,
            origin_intent_admitted=admitted,
            unit_status=_text(item, "status"),
            writer_execution_id=_text(item, "writer_execution_id"),
            ordinal=_int(item, "ordinal"),
            current_attempt_id=_text(item, "current_attempt_id"),
            lease_holder=_text(item, "lease_holder"),
            fencing_token=_int(item, "fencing_token"),
            unit_reservation_count=_int(item, "unit_reservation_count"),
            plan_reservation_count=_int(meta or {}, "plan_reservation_count"),
            reserved_slot_identity=_text(item, "reserved_slot_identity"),
        )

    def _decide(self, candidate: _Candidate) -> str:
        fence = candidate.fence
        if not reconciliation_valid(fence):
            return self._block(
                candidate, MalformedReconciliationMetadataObservation(fence.reconciliation)
            )
        if fence.reconciliation.reconcile_status != "pending":
            # The index page is eventually consistent; a block already won.
            return "reread"
        parsed_fence = parse_fence(fence)
        attempt = parse_attempt(candidate.attempt)
        if isinstance(parsed_fence, ActiveFence):
            return self._reconcile_active(candidate, attempt)
        if isinstance(parsed_fence, ParkedFence) and isinstance(attempt, ParkedAttempt):
            return self._reconcile_parked(candidate, attempt)
        raise _corrupt(
            "an active/parked fence carries a mismatched attempt state.", candidate.table_id
        )

    # -- active fence ------------------------------------------------------------------------------

    def _reconcile_active(self, candidate: _Candidate, attempt: object) -> str:
        if isinstance(attempt, IntentAttempt):
            # Acceptance is ambiguous: no Start replay here, park for the parked path.
            return self._park(candidate)
        if isinstance(attempt, TerminalAttempt):  # pragma: no cover -- excluded by core_valid
            raise _corrupt("an active fence names a terminal attempt.", candidate.table_id)
        query_id = getattr(attempt, "query_execution_id", None)
        if not isinstance(query_id, str):  # pragma: no cover -- closed forms carry it
            raise _corrupt("a submitted attempt carries no query identity.", candidate.table_id)
        observed = self._terminal.observe(query_id)
        if isinstance(observed, ProviderTerminalObservation):
            self._complete(candidate, observed)
            return "completed"
        now = self._now()
        if int(now) < candidate.lease.expiry_epoch_micros():
            self._reschedule_candidate(candidate)
            return "rescheduled"
        if isinstance(attempt, SubmittedAttempt):
            self._request_cancellation(candidate, now)
            self._terminal.stop(query_id)
        refreshed = self._observe(candidate.table_id)
        if refreshed is None:
            return "clear_noop"
        return self._park(refreshed)

    # -- parked fence ------------------------------------------------------------------------------

    def _reconcile_parked(self, candidate: _Candidate, attempt: ParkedAttempt) -> str:
        if attempt.query_execution_id is None:
            return self._recover_parked_submission(candidate, attempt)
        observed = self._terminal.observe(attempt.query_execution_id)
        if isinstance(observed, ProviderTerminalObservation):
            self._complete(candidate, observed)
            self._release_parked_lease(candidate.table_id)
            return "completed"
        now = self._now()
        cancel_due = int(attempt.submitted_at or "0") + PARKED_CANCEL_DUE_SECONDS * 1_000_000
        if int(now) >= cancel_due and attempt.cancel_requested_at is None:
            self._request_cancellation(candidate, now)
            self._terminal.stop(attempt.query_execution_id)
            self._reschedule(candidate.table_id)
            return "cancelled_rescheduled"
        self._reschedule_candidate(candidate)
        return "rescheduled"

    def _recover_parked_submission(self, candidate: _Candidate, attempt: ParkedAttempt) -> str:
        started = self._start.start_table_attempt(
            StartTableAttemptRequest(attempt.core.start_envelope, attempt.core.client_request_token)
        )
        if not isinstance(started, Accepted):
            self._reschedule_candidate(candidate)
            return "rescheduled"
        self._coordinator.recover_parked_attempt_submission(
            RecoverParkedAttemptSubmissionRequest(
                physical_table_id=candidate.table_id,
                fence=candidate.fence,
                attempt=candidate.attempt,
                query_execution_id=started.query_execution_id,
                submitted_at=self._now(),
                sanitized_start_evidence=started.sanitized_evidence,
            )
        )
        refreshed = self._observe(candidate.table_id)
        if refreshed is None:
            return "clear_noop"
        observed = self._terminal.observe(started.query_execution_id)
        if isinstance(observed, ProviderTerminalObservation):
            self._complete(refreshed, observed)
            self._release_parked_lease(refreshed.table_id)
            return "completed"
        self._reschedule_candidate(refreshed)
        return "recovered_rescheduled"

    # -- typed coordinator requests ----------------------------------------------------------------

    def _complete(self, candidate: _Candidate, observed: ProviderTerminalObservation) -> None:
        proof = (
            self._inline_proof(
                candidate.owner,
                _RECONCILED_TRANSITION,
                FlowState.RETRYING,
                typed_reason=observed.state.value.lower(),
            )
            if isinstance(candidate.owner, InlineRunAttemptOwner)
            else None
        )
        self._coordinator.complete_table_attempt(
            CompleteTableAttemptRequest(
                physical_table_id=candidate.table_id,
                fence=candidate.fence,
                attempt=candidate.attempt,
                provider_state=observed.state,
                terminal_at=self._now(),
                owner=candidate.owner,
                query_count=observed.query_count,
                scanned_bytes=observed.scanned_bytes,
                engine_ms=observed.engine_ms,
                proof=proof,
            )
        )

    def _park(self, candidate: _Candidate) -> str:
        self._coordinator.park_attempt(
            ParkAttemptRequest(
                physical_table_id=candidate.table_id,
                lease=candidate.lease,
                fence=candidate.fence,
                attempt=candidate.attempt,
                park_instant=self._now(),
            )
        )
        return "parked"

    def _request_cancellation(self, candidate: _Candidate, now: str) -> None:
        owner = candidate.owner
        proof = None
        if (
            isinstance(owner, InlineRunAttemptOwner)
            and owner.item.get("state") in _RUN_BEGIN_STATES
        ):
            proof = self._inline_proof(owner, _CANCEL_TRANSITION, FlowState.RUNNING)
        self._coordinator.request_attempt_cancellation(
            RequestAttemptCancellationRequest(
                physical_table_id=candidate.table_id,
                fence=candidate.fence,
                attempt=candidate.attempt,
                cancel_requested_at=now,
                owner=owner,
                proof=proof,
            )
        )

    def _release_parked_lease(self, table_id: str) -> None:
        lease = self._reader.lease(table_id)
        if lease is None or lease.parked_at is None:
            return
        self._coordinator.release_table_fence(
            ReleaseTableFenceRequest(
                physical_table_id=table_id,
                lease=lease,
                observed_fence=self._reader.fence(table_id),
            )
        )

    def _reschedule_candidate(self, candidate: _Candidate) -> None:
        try:
            self._coordinator.reschedule_table_attempt_reconcile(
                RescheduleTableAttemptReconcileRequest(
                    physical_table_id=candidate.table_id,
                    fence=candidate.fence,
                    attempt=candidate.attempt,
                    operation_instant=self._now(),
                )
            )
        except DomainError:
            reread = self._reader.fence(candidate.table_id)
            if reread is not None and reread == candidate.fence:
                # A byte-identical reread after a rejected reschedule is the same-snapshot block.
                self._block(
                    candidate,
                    SameSnapshotRescheduleRejectedObservation(candidate.fence.reconciliation),
                )
                return
            raise

    def _reschedule(self, table_id: str) -> None:
        refreshed = self._observe(table_id)
        if refreshed is not None and refreshed.fence.reconciliation.reconcile_status == "pending":
            self._reschedule_candidate(refreshed)

    def _block(
        self,
        candidate: _Candidate,
        observation: MalformedReconciliationMetadataObservation
        | SameSnapshotRescheduleRejectedObservation,
    ) -> str:
        self._coordinator.block_table_attempt_reconcile(
            BlockTableAttemptReconcileRequest(
                physical_table_id=candidate.table_id,
                fence=candidate.fence,
                attempt=candidate.attempt,
                owner=candidate.owner,
                observation=observation,
                block_instant=self._now(),
            )
        )
        return "blocked"

    # -- inline lifecycle proofs -------------------------------------------------------------------

    def _inline_proof(  # noqa: PLR0913 -- one private proof assembler
        self,
        owner: InlineRunAttemptOwner,
        transition_id: str,
        state: FlowState,
        *,
        typed_reason: str | None = None,
    ) -> Stage1LifecycleProof:
        item = dict(owner.item)
        writer_kind = _text(item, "writer_kind")
        operation_id = _text(item, "operation_id")
        execution_id = _text(item, "execution_id")
        flow_id = _text(item, "status_flow_binding")
        if writer_kind is None or operation_id is None or execution_id is None or flow_id is None:
            raise _corrupt(
                "an inline run lacks its immutable identity fields.", owner.physical_table_id
            )
        head = self._reader.head(flow_id)
        if head is None:
            raise _corrupt("an inline run has no lifecycle head.", owner.physical_table_id)
        return derive_stage1_lifecycle_proof(
            Stage1LifecycleProofRequest(
                identity=InlineIdentityRequest(
                    writer_kind, operation_id, owner.physical_table_id, execution_id
                ),
                internal_key=(writer_kind, operation_id, owner.physical_table_id, execution_id),
                topology=Stage1TopologyRequest(
                    Stage1OwnerTransition.LATER_INLINE_EVENT,
                    PriorOwnerEventSource(
                        head.parent_flow_id,
                        head.latest_event_id,
                        stage1_head_authority_document(head),
                    ),
                ),
                transition=Stage1LifecycleTransition(transition_id, state, None, typed_reason),
                stage=transition_id.removeprefix("semantic_"),
                authority_at=datetime.now(UTC),
                prior_head=head,
                clock_started_at=self._reader.flow_root_clock(flow_id),
            )
        )


def _inline_owner_observation(
    owner: TableAttemptOwnerSnapshot | None,
) -> InlineRunOwnerObservation | None:
    if not isinstance(owner, InlineRunAttemptOwner):
        return None
    item = dict(owner.item)
    return InlineRunOwnerObservation(
        run_partition_key=str(item.get("partition_key", "")),
        run_record_key=str(item.get("record_key", "")),
        run_id=owner.run_id,
        state=_text(item, "state"),
        writer_execution_id=_text(item, "execution_id"),
        ordinal=_int(item, "next_ordinal"),
        current_attempt_id=_text(item, "current_attempt_id"),
        lease_holder=_text(item, "lease_holder"),
        fencing_token=_int(item, "fencing_token"),
        reservation_count=_int(item, "accepted_attempt_reservations"),
        reserved_slot_identity=_text(item, "reserved_slot_identity"),
    )


@lru_cache(maxsize=1)
def _reconciler() -> ProviderAttemptReconciler:
    settings = table_attempt_reconciler_settings()
    client = boto3.client("dynamodb")
    publications = settings.publications_table
    leases = settings.leases_table
    return ProviderAttemptReconciler(
        discovery=DynamoStage1Discovery(publications, client),
        reader=Stage1WriterAuthorityReader(publications, leases, client),
        coordinator=DynamoProjectionCoordinator(
            publications, leases, DynamoStage1LifecycleHeadStore(publications, client), client
        ),
        start=Stage1AthenaStartClient(),
        terminal=Stage1AthenaTerminalClient(),
        batch_size=settings.batch_size,
    )


@flushes_metrics
@_LOGGER.inject_lambda_context
def handler(_event: dict[str, Any], _context: object) -> dict[str, int]:
    """Run one scheduled reconciliation tick."""
    try:
        counters = _reconciler().run_once()
    except Exception as error:
        report_error(error, Report(operation=_OPERATION, correlation_id="", logger=_LOGGER))
        raise
    report_operation(
        OperationLog(
            operation=_OPERATION,
            component=_COMPONENT,
            phase="terminal",
            outcome="reconciled",
            severity=LogSeverity.INFO,
            details=dict(counters),
        ),
        _LOGGER,
    )
    return counters
