# SPDX-License-Identifier: MIT-0
"""``PlanIntentReconciler``: claim, persist, send, and admit due plan intents (§5.3).

One scheduled tick issues one bounded ``Query`` over ``stage1-plan-intent-ready`` and, for every
returned pending intent, strongly reads the intent ``META`` and its complete roster, the
``SUCCESSOR_OWNER``, the optional owner-active plan and its head, and the exact plan ``HEAD``;
then it replays only the exact stored authority through the typed coordinator builders: claim,
ordinary or planned-plan-replacement persistence, and per-unit §4 message send plus admission.
A transient pre-persistence dependency reschedules the claimed intent; a definitive admission
failure terminalizes the candidate and returns the same intent to pending; a non-retryable
condition fails closed without mutation. Span-free by design (§10): the closed claim/outcome
code is the only telemetry vocabulary, emitted through the shared reporting seam.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Final, cast

import boto3
from aws_lambda_powertools import Logger

from adapters.config import plan_intent_reconciler_settings
from adapters.graph_authority import provisioned_graph_table
from adapters.outbound.dynamodb.execution_graph_dispatch import DynamoGraphDispatchStore
from adapters.outbound.dynamodb.projection_coordinator import DynamoProjectionCoordinator
from adapters.outbound.dynamodb.stage1_discovery import DynamoStage1Discovery
from adapters.outbound.dynamodb.stage1_lifecycle_head import DynamoStage1LifecycleHeadStore
from adapters.outbound.dynamodb.stage1_writer_authority import (
    Stage1WriterAuthorityReader,
    unit_entry_from_item,
)
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flushes_metrics,
    report_error,
    report_operation,
)
from domain.operations.execution_graph import AuthorityMode
from domain.operations.stage1_lifecycle_head import (
    Stage1CompoundReplacementRequest,
    Stage1LifecycleProof,
    Stage1LifecycleProofRequest,
    Stage1LifecycleTransition,
    compound_replacement_proof,
    derive_stage1_lifecycle_proof,
    stage1_head_authority_document,
)
from domain.operations.stage1_operational_status import (
    LegitimateRootSource,
    PlanIdentityRequest,
    PlanRequiredChildExpectations,
    PriorOwnerEventSource,
    RequiredChildExpectation,
    RootDiscriminator,
    Stage1OwnerTransition,
    Stage1TopologyRequest,
    UnitIdentityRequest,
    derive_plan_identity,
    derive_unit_identity,
)
from domain.operations.status import CompletionOutcome, FlowState
from domain.operations.write_plan import (
    INTENT_SEGMENT,
    WRITE_PLAN_PREFIX,
    CanonicalUnitEntry,
    IntentMeta,
    IntentState,
    PlanMeta,
    PlanStatus,
    PlanUnit,
    PlanUnitAdmission,
    PlanUnitStatus,
    SuccessorOwner,
    WriteUnitMessageRequest,
    intent_partition_key,
    internal_execution_identity,
    plan_partition_key,
    roster_digest,
    unit_message,
)
from domain.shared_kernel.errors import (
    DependencyError,
    DomainError,
    ErrorCode,
    InvariantError,
    ValidationError,
)
from ports.outbound.projection_coordinator import (
    ActivePlanObservation,
    AdmitUnitRequest,
    ClaimPlanIntentOutcome,
    ClaimPlanIntentRequest,
    FailAdmissionRequest,
    PersistPlanRequest,
    PlanHeadObservation,
    ReschedulePlanIntentRequest,
    UnitFlowBinding,
)
from ports.outbound.stage1_discovery import DiscoveredKey, DueQuery

if TYPE_CHECKING:
    from collections.abc import Callable

    from domain.operations.stage1_lifecycle_head import Stage1LifecycleHeadTuple
    from ports.outbound.stage1_discovery import Stage1DiscoveryPort

__all__ = [
    "OUTCOME_CODES",
    "PlanIntentReconciler",
    "SqsWriteUnitSender",
    "WriteUnitSender",
    "handler",
]

_LOGGER = Logger(service="adlytics-plan-intent-reconciler")
_OPERATION = "stage1_plan_intent.reconcile"
_COMPONENT = "plan_intent_reconciler"
_INVARIANT: Final = ErrorCode("PLAN_INTENT_RECONCILE", 1)
_UNIT_FLOW_SELECTOR: Final = "stage1-plan-persistence"
_LEASES_TABLE_UNUSED: Final = "unused-by-plan-intent-reconciler"
"""The reconciler has no lease authority; the coordinator still requires a distinct table name."""
"""The unit flow UUID depends only on (plan digest, unit key); the selector names persistence."""

OUTCOME_CODES: Final = frozenset(
    {
        "claimed",
        "persisted",
        "sent",
        "admitted",
        "admission_failed",
        "rescheduled",
        "exact_replay",
        "stale_winner",
        "invariant_failure",
    }
)
"""The closed §10 claim/outcome code vocabulary; no other value is emitted."""


def _now_instant() -> str:
    return f"{int(datetime.now(UTC).timestamp() * 1_000_000):020d}"


def _invariant(description: str, **context: object) -> InvariantError:
    return InvariantError(_INVARIANT, description, context=dict(context))


class WriteUnitSender:
    """The one FIFO send the reconciler performs: exact body, group, and deduplication id."""

    def send(self, body: str, group_id: str, deduplication_id: str) -> None:
        """Send one §4 unit message; failures raise ``DependencyError``."""
        raise NotImplementedError


class SqsWriteUnitSender(WriteUnitSender):
    """``sqs:SendMessage`` on the exact semantic-write FIFO queue."""

    def __init__(self, queue_url: str, client: Any) -> None:
        self._queue_url = queue_url
        self._client = client

    def send(self, body: str, group_id: str, deduplication_id: str) -> None:
        """Send the exact bytes; SQS collapses a byte-identical resend within its window."""
        try:
            self._client.send_message(
                QueueUrl=self._queue_url,
                MessageBody=body,
                MessageGroupId=group_id,
                MessageDeduplicationId=deduplication_id,
            )
        except Exception as error:  # SDK errors must not cross the boundary untyped
            raise DependencyError(
                ErrorCode("PLAN_INTENT_RECONCILE", 2),
                "The semantic-write queue send failed.",
                context={"cause": type(error).__name__},
            ) from error


@dataclass(slots=True)
class _AdmissionTiming:
    """Monotonic instants of one plan's first send and last admission in this tick."""

    sent: int = 0
    admitted: int = 0
    first_send: float | None = None
    last_admit: float | None = None

    def sent_at(self, instant: float) -> None:
        self.sent += 1
        if self.first_send is None:
            self.first_send = instant

    def admitted_at(self, instant: float) -> None:
        self.admitted += 1
        self.last_admit = instant

    def send_to_last_admit_ms(self) -> float | None:
        if self.first_send is None or self.last_admit is None:
            return None
        return round((self.last_admit - self.first_send) * 1000, 3)


@dataclass(frozen=True, slots=True)
class AdmissionRecord:
    """The per-intent admission lifecycle record (tracing report C2); identifiers, no values."""

    plan_digest: str
    intent_id: str
    plan_flow_id: str
    sent: int
    admitted: int
    send_to_last_admit_ms: float | None
    outcome: str


def report_admission(record: AdmissionRecord) -> None:
    """Write one record per reconciled intent, so the reconciler hop joins by plan and flow."""
    report_operation(
        OperationLog(
            operation=_OPERATION,
            component=_COMPONENT,
            phase="intent",
            outcome=record.outcome,
            severity=LogSeverity.INFO,
            details={
                "plan_digest": record.plan_digest,
                "intent_id": record.intent_id,
                "flow_id": record.plan_flow_id,
                "units_sent": record.sent,
                "units_admitted": record.admitted,
                "send_to_last_admit_ms": (
                    "none" if record.send_to_last_admit_ms is None else record.send_to_last_admit_ms
                ),
            },
        ),
        _LOGGER,
    )


@dataclass(frozen=True, slots=True)
class _IntentKey:
    plan_digest: str
    intent_id: str


@dataclass(frozen=True, slots=True)
class _Ownership:
    """The strongly read owner facts one claim or reschedule conditions."""

    owner: SuccessorOwner
    active_plan: ActivePlanObservation | None
    plan_head: PlanHeadObservation | None


class PlanIntentReconciler:
    """The §5.3 reconciler over its strongly consistent reads and typed coordinator requests."""

    def __init__(  # noqa: PLR0913, PLR0917 -- four collaborators, two bounds, and a clock
        self,
        discovery: Stage1DiscoveryPort,
        reader: Stage1WriterAuthorityReader,
        coordinator: DynamoProjectionCoordinator,
        sender: WriteUnitSender,
        batch_size: int,
        max_units: int,
        now: Callable[[], str] = _now_instant,
        clock: Callable[[], float] = time.monotonic,
        mode: Callable[[], AuthorityMode] | None = None,
    ) -> None:
        self._mode = mode
        """S6 (V-17b): one strong mode read per invocation; None reads LEGACY (no graph)."""
        self._discovery = discovery
        self._reader = reader
        self._coordinator = coordinator
        self._sender = sender
        self._batch_size = batch_size
        self._max_units = max_units
        self._now = now
        self._clock = clock

    def run_once(self) -> dict[str, int]:
        """Reconcile one bounded page of due intents; return the closed outcome counters.

        The mode is read once, before the first coordinator transaction. After a
        `LEGACY_DRAINING` read, every transaction of this invocation adds 1 to `DRAIN_SEQ`, so
        the zero proof sees any claim, persist, admit or failed admission it commits (V-17b).
        The reconciler keeps finishing pre-CAS work in every mode: claim, persist and admit are
        never mode-gated (design.md, "Legacy cutover", step 2).
        """
        observed = AuthorityMode.LEGACY if self._mode is None else self._mode()
        self._coordinator.count_drain(observed)
        now = self._now()
        keys = self._discovery.ready_intents(DueQuery(now, self._batch_size))
        counters: Counter[str] = Counter()
        for key in keys:
            for code in self._reconcile_key(key):
                if code not in OUTCOME_CODES:  # pragma: no cover -- closed by construction
                    raise _invariant("an outcome code escaped the closed vocabulary.")
                counters[code] += 1
        return {"candidates": len(keys), **dict(sorted(counters.items()))}

    # -- one intent --------------------------------------------------------------------------------

    def _reconcile_key(self, key: DiscoveredKey) -> tuple[str, ...]:
        intent_key = _parse_intent_key(key)
        try:
            return self._reconcile(intent_key)
        except DomainError:
            # A lost condition means another claimant or owner advance won; reread next tick.
            return ("stale_winner",)
        except (ValidationError, InvariantError) as error:
            # Non-retryable: fail closed without mutation and keep the batch moving.
            report_error(
                error,
                Report(operation=_OPERATION, correlation_id=intent_key.intent_id, logger=_LOGGER),
            )
            return ("invariant_failure",)

    def _reconcile(self, key: _IntentKey) -> tuple[str, ...]:
        intent = self._reader.intent(key.plan_digest, key.intent_id)
        owner = self._reader.owner(key.plan_digest)
        if intent is None or owner is None:
            raise _invariant("a ready intent lost its META or SUCCESSOR_OWNER.", **vars(key))
        if intent.state is not IntentState.PENDING or owner.reserved_intent_id is not None:
            return ("stale_winner",)
        ownership = self._ownership(key.plan_digest, owner)
        active = ownership.active_plan
        if active is not None and active.meta.admitted_count < active.meta.unit_count:
            # A partially admitted active plan is resumed before any new claim (§5.3).
            return tuple(self._resume_admissions(key.plan_digest, active))
        units = self._roster(key, intent)
        codes: list[str] = []
        claimed = self._coordinator.claim_plan_intent(
            ClaimPlanIntentRequest(
                plan_digest=key.plan_digest,
                intent=intent,
                owner=owner,
                operation_instant=self._now(),
                active_plan=ownership.active_plan,
                plan_head=ownership.plan_head,
            )
        )
        codes.append(
            "claimed" if claimed.outcome is ClaimPlanIntentOutcome.CLAIMED else "exact_replay"
        )
        codes.extend(self._persist_and_admit(key, units))
        return tuple(codes)

    # -- strongly consistent reads -----------------------------------------------------------------

    def _ownership(self, plan_digest: str, owner: SuccessorOwner) -> _Ownership:
        active_plan = None
        if owner.active_plan_key is not None:
            meta = self._reader.plan_meta(owner.active_plan_key)
            if meta is None:
                raise _invariant("the owner-active plan META is absent.", plan_digest=plan_digest)
            active_plan = ActivePlanObservation(owner.active_plan_key, meta)
        plan_head = None
        if owner.last_plan_key is not None:
            if owner.latest_internal_execution_id is None:
                raise _invariant(
                    "an owner with a plan carries its execution.", plan_digest=plan_digest
                )
            head = self._plan_head(plan_digest, owner.latest_internal_execution_id)
            if head is None:
                raise _invariant("the owner's plan HEAD is absent.", plan_digest=plan_digest)
            plan_head = PlanHeadObservation(f"STATUS_FLOW#{head.flow_id}", "HEAD", head)
        return _Ownership(owner, active_plan, plan_head)

    def _plan_head(
        self, plan_digest: str, internal_execution_id: str
    ) -> Stage1LifecycleHeadTuple | None:
        flow_id = derive_plan_identity(
            PlanIdentityRequest(plan_digest, internal_execution_id)
        ).flow_id
        return self._reader.head(flow_id)

    def _roster(self, key: _IntentKey, intent: IntentMeta) -> tuple[CanonicalUnitEntry, ...]:
        partition = intent_partition_key(key.plan_digest, key.intent_id)
        items = self._reader.plan_unit_items(partition)
        if not items or len(items) > self._max_units:
            raise _invariant("the intent roster is empty or beyond the unit cap.", **vars(key))
        units = tuple(
            sorted(
                (unit_entry_from_item(item) for item in items),
                key=lambda unit: unit.unit_key.encode("utf-8"),
            )
        )
        if roster_digest(units) != intent.roster_digest:
            raise _invariant("the intent roster does not reproduce its digest.", **vars(key))
        return units

    # -- persistence -------------------------------------------------------------------------------

    def _persist_and_admit(
        self, key: _IntentKey, units: tuple[CanonicalUnitEntry, ...]
    ) -> list[str]:
        intent = self._reader.intent(key.plan_digest, key.intent_id)
        owner = self._reader.owner(key.plan_digest)
        if intent is None or owner is None or intent.state is not IntentState.CLAIMED:
            raise _invariant("the claimed intent or owner moved before persistence.", **vars(key))
        if owner.reserved_intent_id != key.intent_id:
            return ["stale_winner"]
        flows = tuple(
            UnitFlowBinding(
                unit.unit_key,
                derive_unit_identity(
                    UnitIdentityRequest(key.plan_digest, unit.unit_key, _UNIT_FLOW_SELECTOR)
                ).flow_id,
            )
            for unit in units
        )
        try:
            request = self._persist_request(key, intent, owner, units, flows)
        except DependencyError as error:
            report_error(
                error,
                Report(operation=_OPERATION, correlation_id=key.intent_id, logger=_LOGGER),
            )
            self._reschedule(key, intent, owner)
            return ["rescheduled"]
        try:
            persisted = self._coordinator.persist_plan(request)
        except DependencyError as error:
            report_error(
                error,
                Report(operation=_OPERATION, correlation_id=key.intent_id, logger=_LOGGER),
            )
            self._reschedule(key, intent, owner)
            return ["rescheduled"]
        codes = ["persisted" if persisted.outcome.value == "persisted" else "exact_replay"]
        active = self._ownership(key.plan_digest, self._required_owner(key.plan_digest)).active_plan
        if active is None:
            raise _invariant("persistence left no owner-active plan.", **vars(key))
        codes.extend(self._resume_admissions(key.plan_digest, active))
        return codes

    def _persist_request(  # noqa: PLR0913, PLR0917 -- one private request assembler
        self,
        key: _IntentKey,
        intent: IntentMeta,
        owner: SuccessorOwner,
        units: tuple[CanonicalUnitEntry, ...],
        flows: tuple[UnitFlowBinding, ...],
    ) -> PersistPlanRequest:
        generation = intent.claimed_generation
        if generation is None:  # pragma: no cover -- excluded by the claimed state
            raise _invariant("a claimed intent carries its generation.", **vars(key))
        internal_execution_id = internal_execution_identity(intent.intent_id, generation)
        plan_key = plan_partition_key(key.plan_digest, internal_execution_id)
        target_flow = derive_plan_identity(
            PlanIdentityRequest(key.plan_digest, internal_execution_id)
        ).flow_id
        expectations = PlanRequiredChildExpectations(
            target_flow,
            tuple(
                sorted(
                    (RequiredChildExpectation(binding.flow_id, generation) for binding in flows),
                    key=lambda member: member.child_flow_id.encode("utf-8"),
                )
            ),
        )
        source_plan = None
        if owner.active_plan_key is not None:
            source_plan = self._reader.replacement_evidence(owner.active_plan_key)
            source_head = self._plan_head(key.plan_digest, source_plan.meta.internal_execution_id)
            if source_head is None:
                raise _invariant("the source plan HEAD is absent.", **vars(key))
            proof = compound_replacement_proof(
                Stage1CompoundReplacementRequest(
                    plan_digest=key.plan_digest,
                    prior_head=source_head,
                    source_internal_key=(owner.active_plan_key,),
                    target_internal_key=(plan_key,),
                    successor_internal_delivery_execution_id=internal_execution_id,
                    terminal_transition=Stage1LifecycleTransition(
                        "semantic_plan_terminal",
                        FlowState.SUPERSEDED,
                        CompletionOutcome.SUPERSEDED,
                        None,
                    ),
                    successor_transition=Stage1LifecycleTransition(
                        "semantic_plan_recorded", FlowState.QUEUED, None, None
                    ),
                    source_expectations=self._expectations_of(source_plan.meta, key.plan_digest),
                    target_expectations=expectations,
                    authority_at=datetime.now(UTC),
                    clock_started_at=self._reader.flow_root_clock(source_head.flow_id),
                )
            )
        else:
            prior = (
                None
                if owner.latest_internal_execution_id is None
                else self._plan_head(key.plan_digest, owner.latest_internal_execution_id)
            )
            proof = self._recorded_proof(key, internal_execution_id, plan_key, prior, expectations)
        return PersistPlanRequest(
            plan_digest=key.plan_digest,
            intent=intent,
            units=units,
            owner=owner,
            unit_flows=flows,
            proof=proof,
            operation_instant=self._now(),
            source_plan=source_plan,
        )

    def _recorded_proof(  # noqa: PLR0913, PLR0917 -- one private proof assembler
        self,
        key: _IntentKey,
        internal_execution_id: str,
        plan_key: str,
        prior: Stage1LifecycleHeadTuple | None,
        expectations: PlanRequiredChildExpectations,
    ) -> Stage1LifecycleProof:
        topology = (
            Stage1TopologyRequest(
                Stage1OwnerTransition.FIRST_AUTOMATIC_PLAN_RECORDED,
                LegitimateRootSource(RootDiscriminator.AUTOMATIC_INGESTION_PLAN),
            )
            if prior is None
            else Stage1TopologyRequest(
                Stage1OwnerTransition.LATER_PLAN_EVENT,
                PriorOwnerEventSource(
                    prior.parent_flow_id,
                    prior.latest_event_id,
                    stage1_head_authority_document(prior),
                ),
            )
        )
        return derive_stage1_lifecycle_proof(
            Stage1LifecycleProofRequest(
                identity=PlanIdentityRequest(key.plan_digest, internal_execution_id),
                internal_key=(plan_key,),
                topology=topology,
                transition=Stage1LifecycleTransition(
                    "semantic_plan_recorded", FlowState.QUEUED, None, None
                ),
                stage="projection_plan_recorded",
                authority_at=datetime.now(UTC),
                prior_head=prior,
                clock_started_at=(
                    None if prior is None else self._reader.flow_root_clock(prior.flow_id)
                ),
                required_child_expectations=expectations,
            )
        )

    def _expectations_of(self, meta: PlanMeta, plan_digest: str) -> PlanRequiredChildExpectations:
        flow_id = derive_plan_identity(
            PlanIdentityRequest(plan_digest, meta.internal_execution_id)
        ).flow_id
        return PlanRequiredChildExpectations(
            flow_id,
            tuple(
                RequiredChildExpectation(member.child_flow_id, member.owner_generation)
                for member in meta.required_child_expectations
            ),
        )

    def _reschedule(self, key: _IntentKey, intent: IntentMeta, owner: SuccessorOwner) -> None:
        plan_head = self._ownership(key.plan_digest, owner).plan_head
        self._coordinator.reschedule_plan_intent(
            ReschedulePlanIntentRequest(
                plan_digest=key.plan_digest,
                intent=intent,
                owner=owner,
                operation_instant=self._now(),
                plan_head=plan_head,
            )
        )

    # -- admission ---------------------------------------------------------------------------------

    def _resume_admissions(self, plan_digest: str, active: ActivePlanObservation) -> list[str]:
        """Send and admit every pending unit, then write the plan's one admission record."""
        timing = _AdmissionTiming()
        codes: list[str] = []
        try:
            codes = self._admit_pending(plan_digest, active, timing)
        finally:
            report_admission(
                AdmissionRecord(
                    plan_digest=plan_digest,
                    intent_id=active.meta.origin_intent_id,
                    plan_flow_id=derive_plan_identity(
                        PlanIdentityRequest(plan_digest, active.meta.internal_execution_id)
                    ).flow_id,
                    sent=timing.sent,
                    admitted=timing.admitted,
                    send_to_last_admit_ms=timing.send_to_last_admit_ms(),
                    outcome=codes[-1] if codes else "failed",
                )
            )
        return codes

    def _admit_pending(
        self, plan_digest: str, active: ActivePlanObservation, timing: _AdmissionTiming
    ) -> list[str]:
        """Send and admit every still-pending unit of the owner-active plan, in roster order."""
        codes: list[str] = []
        plan_key = active.plan_key
        meta = active.meta
        for item in sorted(
            self._reader.plan_unit_items(plan_key),
            key=lambda item: str(item.get("unit_key", "")).encode("utf-8"),
        ):
            if item.get("admission") != PlanUnitAdmission.PENDING.value:
                continue
            owner = self._required_owner(plan_digest)
            intent = self._reader.intent(plan_digest, meta.origin_intent_id)
            fresh_meta = self._reader.plan_meta(plan_key)
            if intent is None or fresh_meta is None or fresh_meta.status is not PlanStatus.PLANNED:
                raise _invariant("the active plan moved during admission.", plan_key=plan_key)
            unit = _pending_unit(item, fresh_meta.generation)
            message = unit_message(
                WriteUnitMessageRequest(
                    plan_key=plan_key,
                    plan_digest=plan_digest,
                    origin_intent_id=meta.origin_intent_id,
                    generation=fresh_meta.generation,
                    unit_key=unit.unit.unit_key,
                )
            )
            try:
                self._sender.send(message.body, message.group_id, message.deduplication_id)
            except DependencyError as error:
                report_error(
                    error,
                    Report(
                        operation=_OPERATION,
                        correlation_id=meta.origin_intent_id,
                        logger=_LOGGER,
                    ),
                )
                # The next tick replays the byte-identical send; the intent stays claimed.
                return [*codes, "rescheduled"]
            codes.append("sent")
            timing.sent_at(self._clock())
            final = fresh_meta.admitted_count + 1 == fresh_meta.unit_count
            try:
                admitted = self._coordinator.admit_unit(
                    AdmitUnitRequest(
                        plan_digest=plan_digest,
                        plan_key=plan_key,
                        intent=intent,
                        owner=owner,
                        plan_meta=fresh_meta,
                        unit=unit,
                        deduplication_digest=message.deduplication_id,
                        operation_instant=self._now(),
                        proof=(
                            self._later_plan_proof(
                                plan_digest,
                                fresh_meta,
                                plan_key,
                                "semantic_plan_admitted",
                                FlowState.QUEUED,
                                None,
                                "projection_plan_admitted",
                            )
                            if final
                            else None
                        ),
                    )
                )
            except ValidationError:
                codes.append(self._fail_admission(plan_digest, plan_key, intent, owner, fresh_meta))
                return codes
            codes.append("admitted" if admitted.outcome.value == "admitted" else "exact_replay")
            timing.admitted_at(self._clock())
        return codes

    def _fail_admission(  # noqa: PLR0913, PLR0917 -- one private request assembler
        self,
        plan_digest: str,
        plan_key: str,
        intent: IntentMeta,
        owner: SuccessorOwner,
        meta: PlanMeta,
    ) -> str:
        self._coordinator.fail_admission(
            FailAdmissionRequest(
                plan_digest=plan_digest,
                plan_key=plan_key,
                intent=intent,
                owner=owner,
                plan_meta=meta,
                operation_instant=self._now(),
                proof=self._later_plan_proof(
                    plan_digest,
                    meta,
                    plan_key,
                    "semantic_plan_admission_failed",
                    FlowState.FAILED,
                    CompletionOutcome.EXHAUSTED,
                    "projection_plan_admission_failed",
                    typed_reason="plan_admission_failed",
                ),
            )
        )
        return "admission_failed"

    def _later_plan_proof(  # noqa: PLR0913, PLR0917 -- one private proof assembler
        self,
        plan_digest: str,
        meta: PlanMeta,
        plan_key: str,
        transition_id: str,
        state: FlowState,
        outcome: CompletionOutcome | None,
        stage: str,
        typed_reason: str | None = None,
    ) -> Stage1LifecycleProof:
        head = self._plan_head(plan_digest, meta.internal_execution_id)
        if head is None:
            raise _invariant("the plan HEAD is absent.", plan_key=plan_key)
        return derive_stage1_lifecycle_proof(
            Stage1LifecycleProofRequest(
                identity=PlanIdentityRequest(plan_digest, meta.internal_execution_id),
                internal_key=(plan_key,),
                topology=Stage1TopologyRequest(
                    Stage1OwnerTransition.LATER_PLAN_EVENT,
                    PriorOwnerEventSource(
                        head.parent_flow_id,
                        head.latest_event_id,
                        stage1_head_authority_document(head),
                    ),
                ),
                transition=Stage1LifecycleTransition(transition_id, state, outcome, typed_reason),
                stage=stage,
                authority_at=datetime.now(UTC),
                prior_head=head,
                clock_started_at=self._reader.flow_root_clock(head.flow_id),
                required_child_expectations=self._expectations_of(meta, plan_digest),
            )
        )

    def _required_owner(self, plan_digest: str) -> SuccessorOwner:
        owner = self._reader.owner(plan_digest)
        if owner is None:
            raise _invariant("the SUCCESSOR_OWNER is absent.", plan_digest=plan_digest)
        return owner


def _parse_intent_key(key: DiscoveredKey) -> _IntentKey:
    partition = key.partition_key
    if not partition.startswith(WRITE_PLAN_PREFIX) or INTENT_SEGMENT not in partition:
        raise _invariant("a ready-index key is outside the intent META key form.")
    digest, _, intent_id = partition[len(WRITE_PLAN_PREFIX) :].partition(INTENT_SEGMENT)
    if key.record_key != "META":
        raise _invariant("a ready-index key is outside the intent META key form.")
    return _IntentKey(digest, intent_id)


def _pending_unit(item: dict[str, object], generation: int) -> PlanUnit:
    return PlanUnit(
        unit=unit_entry_from_item(item),
        flow_id=str(item.get("flow_id", "")),
        owner_generation=generation,
        status=PlanUnitStatus.QUEUED,
        admission=PlanUnitAdmission.PENDING,
        admission_deduplication_digest=None,
        admitted_at=None,
        created_at=str(item.get("created_at", "")),
    )


@lru_cache(maxsize=1)
def _reconciler() -> PlanIntentReconciler:
    settings = plan_intent_reconciler_settings()
    client = boto3.client("dynamodb")
    publications = settings.publications_table
    return PlanIntentReconciler(
        discovery=DynamoStage1Discovery(publications, client),
        reader=Stage1WriterAuthorityReader(
            publications, "unused-by-plan-intent-reconciler", client
        ),
        coordinator=DynamoProjectionCoordinator(
            publications,
            _LEASES_TABLE_UNUSED,
            DynamoStage1LifecycleHeadStore(publications, client),
            client,
        ),
        sender=SqsWriteUnitSender(settings.semantic_write_queue_url, boto3.client("sqs")),
        batch_size=settings.batch_size,
        max_units=settings.max_units,
        mode=_mode_reader(client),
    )


def _mode_reader(client: object) -> Callable[[], AuthorityMode] | None:
    """Return the strong mode read, or None where the graph authority is not provisioned."""
    table = provisioned_graph_table()
    if table is None:
        return None
    store = DynamoGraphDispatchStore(table, cast("Any", client))
    return lambda: store.read_mode().mode


@flushes_metrics
@_LOGGER.inject_lambda_context
def handler(_event: dict[str, Any], _context: object) -> dict[str, int]:
    """Run one scheduled plan-intent reconciliation tick."""
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
