# SPDX-License-Identifier: MIT-0
"""Outbound port for the ADR-075 §3.2 plan-intent/plan side of the projection coordinator.

Every request accepts only enums and validated typed identities; constructors reject
inconsistent identity tuples. No caller supplies a table name, key, sort key,
condition/update expression, attribute map, or transaction member — the adapter's
private builders derive all of them. This port now covers all twenty-four §3.2
typed operations, including the table-writer execution operations
(claim/fail/finalize), the reconciler reschedule/block/requeue family, the private
settlement expansion consumed only by the final owner-disposition builders, and
the planned-plan replacement branch of persistence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from domain.operations.execution_graph import AuthorityMode
from domain.operations.stage1_lifecycle_head import (
    HEAD_RECORD_KEY,
    STATUS_FLOW_PREFIX,
    Stage1LifecycleHeadTuple,
    Stage1LifecycleProof,
)
from domain.operations.stage1_operational_status import (
    InlineIdentityRequest,
    OwnerDisposition,
    SettleStage1AthenaUsageRequest,
    Stage1EventAuthorityRequest,
)
from domain.operations.table_attempt import (
    UNIT_RECORD_PREFIX,
    ActiveFence,
    AttemptIdentityRequest,
    AttemptObservation,
    AttemptOwnerKind,
    BindingDigestRequest,
    CancelRequestedAttempt,
    ClearFence,
    FenceObservation,
    IntentAttempt,
    NonterminalTableAttempt,
    ParkedAttempt,
    ParkedFence,
    ProtectedCaseState,
    ProtectedReconcileCase,
    ProviderTerminalState,
    RecomputedAttemptIdentity,
    ReconcileBlockIdentityRequest,
    ReconcileBlockReason,
    ReconciliationObservation,
    RunIdentityRequest,
    SanitizedStartEvidence,
    SubmittedAttempt,
    TableAttempt,
    TableAttemptError,
    TableAttemptFence,
    TerminalAttempt,
    WriterKind,
    attempt_identity,
    attempt_reservation_identity,
    binding_digest,
    parse_attempt,
    parse_fence,
    pending_reconcile_key,
    reconcile_block_identity,
    reconcile_observation_digest,
    reconcile_recovery_reference,
    reconcile_requeue_identity,
    reconciliation_valid,
    rescheduled_reconcile_due_micros,
    run_identity,
    run_record_key,
    table_attempt_partition_key,
    valid_query_execution_id,
)
from domain.operations.table_lease import TableLeaseRecord, queued_lease_holder
from domain.operations.write_plan import (
    CHILD_OUTCOME_REDUCTION_VERSION,
    MAX_PLAN_ATTEMPTS,
    META_RECORD_KEY,
    WRITE_PLAN_PREFIX,
    CanonicalUnitEntry,
    IntentAuthorityInputs,
    IntentMeta,
    IntentOrigin,
    IntentOriginKind,
    IntentState,
    PlanMeta,
    PlanStatus,
    PlanUnit,
    PlanUnitAdmission,
    PlanUnitStatus,
    SettledAttemptReference,
    SuccessorOwner,
    intent_identity,
    internal_execution_identity,
    is_canonical_digest,
    is_canonical_instant,
    plan_attempt_budget_origin,
    plan_digest_for_source_delivery,
    plan_partition_key,
    plan_settled_attempt_digest,
    roster_digest,
    unit_record_key,
    unit_verified_digest,
)

__all__ = [
    "RUN_RESERVATION_CAP",
    "AcquireTableFenceOutcome",
    "AcquireTableFenceRequest",
    "AcquireTableFenceResult",
    "ActivePlanObservation",
    "AdmitUnitOutcome",
    "AdmitUnitRequest",
    "AdmitUnitResult",
    "AthenaAttemptSettlement",
    "AttemptBudgetExhaustion",
    "AttestedUnitObservation",
    "AuthorityModeGuard",
    "BeginInlineRunOwner",
    "BeginQueuedUnitOwner",
    "BeginTableAttemptOutcome",
    "BeginTableAttemptRequest",
    "BeginTableAttemptResult",
    "BlockTableAttemptReconcileOutcome",
    "BlockTableAttemptReconcileRequest",
    "BlockTableAttemptReconcileResult",
    "ClaimPlanIntentOutcome",
    "ClaimPlanIntentRequest",
    "ClaimPlanIntentResult",
    "ClaimUnitOutcome",
    "ClaimUnitRequest",
    "ClaimUnitResult",
    "CompletePlanOutcome",
    "CompletePlanRequest",
    "CompletePlanResult",
    "CompleteTableAttemptOutcome",
    "CompleteTableAttemptRequest",
    "CompleteTableAttemptResult",
    "CurrentPointerOutcome",
    "FailAdmissionOutcome",
    "FailAdmissionRequest",
    "FailAdmissionResult",
    "FailUnitOutcome",
    "FailUnitRequest",
    "FailUnitResult",
    "FinalizeInlineOutcome",
    "FinalizeInlineRequest",
    "FinalizeInlineResult",
    "FinalizeUnitOutcome",
    "FinalizeUnitRequest",
    "FinalizeUnitResult",
    "GraphAuthorityRefusedError",
    "GraphClaimFence",
    "InlineRunAttemptOwner",
    "MalformedReconciliationMetadataObservation",
    "ModeGuardKind",
    "ParkAttemptOutcome",
    "ParkAttemptRequest",
    "ParkAttemptResult",
    "PersistInlineRunOutcome",
    "PersistInlineRunRequest",
    "PersistInlineRunResult",
    "PersistPlanOutcome",
    "PersistPlanRequest",
    "PersistPlanResult",
    "PlanHeadObservation",
    "PointerOutcomeKind",
    "ProjectionCoordinatorPort",
    "QueuedUnitAttemptOwner",
    "ReconcileBlockObservation",
    "RecordStage1RefusalOutcome",
    "RecordStage1RefusalRequest",
    "RecordStage1RefusalResult",
    "RecoverParkedAttemptSubmissionOutcome",
    "RecoverParkedAttemptSubmissionRequest",
    "RecoverParkedAttemptSubmissionResult",
    "RefusedInlineRunObservation",
    "RefusedUnitObservation",
    "ReleaseTableFenceOutcome",
    "ReleaseTableFenceRequest",
    "ReleaseTableFenceResult",
    "RequestAttemptCancellationOutcome",
    "RequestAttemptCancellationRequest",
    "RequestAttemptCancellationResult",
    "RequeueBlockedTableAttemptReconcileOutcome",
    "RequeueBlockedTableAttemptReconcileRequest",
    "RequeueBlockedTableAttemptReconcileResult",
    "ReschedulePlanIntentOutcome",
    "ReschedulePlanIntentRequest",
    "ReschedulePlanIntentResult",
    "RescheduleTableAttemptReconcileOutcome",
    "RescheduleTableAttemptReconcileRequest",
    "RescheduleTableAttemptReconcileResult",
    "SameSnapshotRescheduleRejectedObservation",
    "SettledAttemptObservation",
    "SettledUsageAuthorityObservation",
    "SourcePlanReplacementEvidence",
    "Stage1RefusalReason",
    "Stage1RefusalTag",
    "Stage1SettlementExpansion",
    "SubmitPlanIntentOutcome",
    "SubmitPlanIntentRequest",
    "SubmitPlanIntentResult",
    "TableAttemptOwnerSnapshot",
    "UnitFailureReason",
    "UnitFlowBinding",
]

_PROVIDER_TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
_COMPOUND_EVENT_COUNT: Final = 2


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


# --- Shared observations --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanHeadObservation:
    """One strongly read plan ``HEAD`` at its status-supplied ``STATUS_FLOW#`` key."""

    partition_key: str
    record_key: str
    head: Stage1LifecycleHeadTuple

    def __post_init__(self) -> None:
        _require(
            self.partition_key.startswith(STATUS_FLOW_PREFIX),
            "a plan head lives in a STATUS_FLOW# partition.",
        )
        _require(self.record_key == HEAD_RECORD_KEY, "a plan head's record key is exactly HEAD.")


@dataclass(frozen=True, slots=True)
class ActivePlanObservation:
    """The strongly read owner-active plan ``META`` a claim or reschedule conditions."""

    plan_key: str
    meta: PlanMeta

    def __post_init__(self) -> None:
        _require(
            self.plan_key
            == plan_partition_key(self.meta.plan_digest, self.meta.internal_execution_id),
            "the active plan key must reproduce from its plan digest and execution identity.",
        )
        _require(self.meta.status is PlanStatus.PLANNED, "the owner-active plan is planned.")


def _require_head_agreement(owner: SuccessorOwner, head: PlanHeadObservation | None) -> None:
    _require(
        (head is None) == (owner.last_plan_key is None),
        "a plan head observation exists exactly when the owner has a plan.",
    )
    if head is not None:
        _require(
            head.head.current_internal_execution_id == owner.latest_internal_execution_id,
            "the owner and plan HEAD must agree on the internal execution identity.",
        )


# --- Cross-table authority guards (S3, V-13, V-9) ------------------------------------------------


class ModeGuardKind(StrEnum):
    """Where a ConditionCheck on the execution-graph mode item goes (design.md, r3, R2-7)."""

    NEW_ROOT_ADMITTING = "new_root_admitting"
    """`source_delivery` intent submission: mode in {LEGACY, SHADOW} (absent reads LEGACY)."""
    FINALIZE_NOT_GRAPH = "finalize_not_graph"
    """Legacy finalize after a LEGACY_DRAINING batch read: mode != GRAPH."""


@dataclass(frozen=True, slots=True)
class AuthorityModeGuard:
    """V-13: a ConditionCheck on the graph authority's mode item in the same transaction.

    `observed` is the exact strongly read mode item (plain attributes), or None when it was
    proven absent; the adapter derives the key, table and condition from `kind`.
    """

    kind: ModeGuardKind
    observed: Mapping[str, object] | None


class GraphAuthorityRefusedError(Exception):
    """A cross-table graph member's condition failed; nothing committed.

    `AUTHORITY_MODE_DRAINING` (V-13(b)), `AUTHORITY_MODE_CHANGED` (V-13(a)), or
    `EXECUTION_SUPERSEDED` for a Begin whose claim lease a takeover bumped (V-9).
    """

    def __init__(self, signal: str) -> None:
        super().__init__(signal)
        self.signal = signal


@dataclass(frozen=True, slots=True)
class GraphClaimFence:
    """V-9 takeover fence: a graph-envelope Begin is conditioned on the claim lease version.

    The same member records the attempt on the claim, so the node verifier can re-attest it.
    `observed` is the exact strongly read `EXEC_CLAIM#` item.
    """

    node_id: str
    lease_version: int
    observed: Mapping[str, object]


# --- SubmitPlanIntentRequest ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SubmitPlanIntentRequest:
    """One generation-independent intent creation with its exact owner consumption.

    ``owner=None`` is the proven-absent optional bootstrap branch; the builder then
    puts the absent-owner form instead of condition-checking an existing revision.
    """

    plan_digest: str
    root_plan_key: str | None
    source_plan_key: str | None
    origin: IntentOrigin
    units: tuple[CanonicalUnitEntry, ...]
    operation_instant: str
    owner: SuccessorOwner | None
    mode_guard: AuthorityModeGuard | None = None
    """V-13(b): a `source_delivery` submission ConditionChecks mode in {LEGACY, SHADOW}."""

    def __post_init__(self) -> None:
        _require(is_canonical_digest(self.plan_digest), "plan digest must be 64 lowercase hex.")
        _require(is_canonical_digest(self.origin.member), "origin member must be 64 lowercase hex.")
        _require(
            is_canonical_instant(self.operation_instant),
            "operation instant must be a 20-digit epoch-microseconds instant.",
        )
        self.roster_digest()  # canonical roster or typed rejection
        if self.origin.kind is IntentOriginKind.SOURCE_DELIVERY:
            _require(
                self.plan_digest == plan_digest_for_source_delivery(self.origin.member),
                "a source-delivery plan digest must reproduce from its origin identity.",
            )
            _require(
                self.source_plan_key is None,
                "a source-delivery intent names no source plan.",
            )
        else:
            _require(
                self.source_plan_key is not None
                and self.origin.member == plan_attempt_budget_origin(self.source_plan_key),
                "a budget origin member must reproduce from its exact source plan key.",
            )
        if self.owner is None:
            _require(self.root_plan_key is None, "a bootstrap submission has no root plan yet.")
        else:
            _require(
                self.root_plan_key == self.owner.root_plan_key,
                "the intent root plan key must equal the owner's exact root.",
            )

    def roster_digest(self) -> str:
        """Derive the canonical roster digest for this exact roster."""
        return roster_digest(self.units)

    def intent_id(self) -> str:
        """Derive the sole generation-independent intent identity."""
        return intent_identity(
            IntentAuthorityInputs(
                self.root_plan_key, self.source_plan_key, self.plan_digest, self.origin, self.units
            )
        )


class SubmitPlanIntentOutcome(StrEnum):
    """Created now, or proven an exact complete-authority replay (no-op)."""

    SUBMITTED = "submitted"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class SubmitPlanIntentResult:
    """One submission's typed outcome and derived identities."""

    outcome: SubmitPlanIntentOutcome
    intent_id: str
    roster_digest: str


# --- ClaimPlanIntentRequest -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimPlanIntentRequest:
    """One due pending intent plus the exact owner snapshot the claim CASes."""

    plan_digest: str
    intent: IntentMeta
    owner: SuccessorOwner
    operation_instant: str
    active_plan: ActivePlanObservation | None
    plan_head: PlanHeadObservation | None

    def __post_init__(self) -> None:
        _require(self.intent.state is IntentState.PENDING, "only a pending intent is claimable.")
        _require(self.intent.plan_digest == self.plan_digest, "the intent names this plan digest.")
        _require(
            is_canonical_instant(self.operation_instant),
            "operation instant must be a 20-digit epoch-microseconds instant.",
        )
        _require(
            self.owner.reserved_intent_id is None,
            "a claim consumes an unreserved owner; a competing reservation rereads first.",
        )
        _require(
            (self.active_plan is None) == (self.owner.active_plan_key is None),
            "an active-plan observation exists exactly when the owner names one.",
        )
        if self.active_plan is not None:
            _require(
                self.active_plan.plan_key == self.owner.active_plan_key
                and self.active_plan.meta.origin_intent_id == self.owner.active_intent_id
                and self.active_plan.meta.generation == self.owner.last_generation,
                "the active-plan observation must agree with the owner's exact active pair.",
            )
        _require_head_agreement(self.owner, self.plan_head)

    @property
    def claimed_generation(self) -> int:
        """The one generation this claim assigns: ``owner.last_generation + 1``."""
        return self.owner.last_generation + 1


class ClaimPlanIntentOutcome(StrEnum):
    """Claimed now, or proven the exact same durable assignment (no-op)."""

    CLAIMED = "claimed"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class ClaimPlanIntentResult:
    """One claim's typed outcome and its assigned generation."""

    outcome: ClaimPlanIntentOutcome
    claimed_generation: int


# --- ReschedulePlanIntentRequest ------------------------------------------------------------------


def _require_exact_reservation(owner: SuccessorOwner, intent: IntentMeta) -> None:
    _require(intent.state is IntentState.CLAIMED, "the intent must be exactly claimed.")
    _require(
        owner.reserved_intent_id == intent.intent_id
        and owner.reserved_generation == intent.claimed_generation,
        "the owner reservation must name this exact intent and claimed generation.",
    )


@dataclass(frozen=True, slots=True)
class ReschedulePlanIntentRequest:
    """One exact-reservation ``claimed→pending`` retry; never invents a generation."""

    plan_digest: str
    intent: IntentMeta
    owner: SuccessorOwner
    operation_instant: str
    plan_head: PlanHeadObservation | None

    def __post_init__(self) -> None:
        _require(self.intent.plan_digest == self.plan_digest, "the intent names this plan digest.")
        _require(
            is_canonical_instant(self.operation_instant),
            "operation instant must be a 20-digit epoch-microseconds instant.",
        )
        _require_exact_reservation(self.owner, self.intent)
        _require_head_agreement(self.owner, self.plan_head)


class ReschedulePlanIntentOutcome(StrEnum):
    """Rescheduled now, or proven the exact resulting schedule bytes (no-op)."""

    RESCHEDULED = "rescheduled"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class ReschedulePlanIntentResult:
    """One reschedule's typed outcome and restored canonical schedule."""

    outcome: ReschedulePlanIntentOutcome
    retry_count: int
    due_at: str


# --- PersistPlanRequest ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SettledUsageAuthorityObservation:
    """One strongly read immutable ``USAGE#`` settlement authority beside its attempt."""

    physical_table_id: str
    attempt_id: str
    payload: str
    immutable_payload_digest: str
    item: Mapping[str, object]

    def __post_init__(self) -> None:
        _require(
            is_canonical_digest(self.immutable_payload_digest),
            "the immutable payload digest must be 64 lowercase hex characters.",
        )
        _require(
            self.item.get("partition_key") == table_attempt_partition_key(self.physical_table_id)
            and self.item.get("record_key") == f"USAGE#{self.attempt_id}",
            "the usage snapshot must carry its exact TABLE_ATTEMPT#/USAGE# keys.",
        )
        _require(
            self.item.get("payload") == self.payload
            and self.item.get("immutable_payload_digest") == self.immutable_payload_digest,
            "the usage snapshot must carry the exact byte-identical settlement authority.",
        )


@dataclass(frozen=True, slots=True)
class SourcePlanReplacementEvidence:
    """The complete strongly read source-plan evidence one replacement conditions.

    Every source plan unit, every referenced settled accepted attempt, and each
    attempt's byte-identical ``USAGE#`` settlement authority must be supplied
    whole; any aggregate mismatch is collision and has no persistence effect.
    """

    plan_key: str
    meta: PlanMeta
    unit_items: tuple[Mapping[str, object], ...]
    attempts: tuple[SettledAttemptObservation, ...]
    usage_authorities: tuple[SettledUsageAuthorityObservation, ...]

    def __post_init__(self) -> None:
        _require(
            self.plan_key
            == plan_partition_key(self.meta.plan_digest, self.meta.internal_execution_id),
            "the source plan key must reproduce from its META identity.",
        )
        _require(
            self.meta.status is PlanStatus.PLANNED,
            "only an owner-active planned plan is replaced.",
        )
        _require(
            len(self.unit_items) == self.meta.unit_count,
            "replacement conditions exactly every source plan unit.",
        )
        for unit in self.unit_items:
            record = unit.get("record_key")
            _require(
                unit.get("partition_key") == self.plan_key
                and isinstance(record, str)
                and record.startswith(UNIT_RECORD_PREFIX),
                "a source unit snapshot must carry its exact plan partition and UNIT# key.",
            )
        records = [str(unit.get("record_key")) for unit in self.unit_items]
        _require(len(set(records)) == len(records), "source unit snapshots must be distinct.")
        _require(
            len(self.attempts) <= MAX_PLAN_ATTEMPTS,
            "a plan carries at most 32 permanent attempt reservations.",
        )
        _require(
            len(self.attempts) == self.meta.settled_attempt_count,
            "attempt observations must match the complete settled-attempt count.",
        )
        references = tuple(
            SettledAttemptReference(
                attempt.physical_table_id, attempt.attempt_id, attempt.usage_authority_digest
            )
            for attempt in sorted(
                self.attempts,
                key=lambda attempt: (
                    attempt.physical_table_id.encode("utf-8"),
                    attempt.attempt_id.encode("utf-8"),
                ),
            )
        )
        if references:
            _require(
                plan_settled_attempt_digest(references) == self.meta.settled_attempt_digest,
                "attempt observations must reproduce the complete settled-attempt digest.",
            )
        else:
            _require(
                self.meta.settled_attempt_digest is None,
                "a zero settled-attempt count carries no digest.",
            )
        pairs = [(attempt.physical_table_id, attempt.attempt_id) for attempt in self.attempts]
        usage_pairs = [
            (usage.physical_table_id, usage.attempt_id) for usage in self.usage_authorities
        ]
        _require(
            sorted(usage_pairs) == sorted(pairs) and len(set(usage_pairs)) == len(usage_pairs),
            "every settled attempt carries exactly one byte-identical usage authority.",
        )
        by_pair = {
            (usage.physical_table_id, usage.attempt_id): usage for usage in self.usage_authorities
        }
        for attempt in self.attempts:
            usage = by_pair[(attempt.physical_table_id, attempt.attempt_id)]
            _require(
                usage.immutable_payload_digest == attempt.usage_authority_digest,
                "a usage authority must carry its attempt's exact settlement digest.",
            )


@dataclass(frozen=True, slots=True)
class UnitFlowBinding:
    """One status-derived public unit flow id bound to its exact roster unit key."""

    unit_key: str
    flow_id: str

    def __post_init__(self) -> None:
        _require(bool(self.unit_key.strip()), "a flow binding names its exact unit key.")
        _require(bool(self.flow_id.strip()), "a flow binding carries a nonblank flow id.")


@dataclass(frozen=True, slots=True)
class PersistPlanRequest:
    """One reservation-consuming persistence of the claimed target intent.

    With no owner-active plan this is ordinary persistence (``<=20`` actions,
    one event). With an owner-active planned plan the caller must supply the
    complete strongly read ``source_plan`` replacement evidence — every source
    plan unit and referenced settled attempt/usage authority — and the sole
    compound two-event ``N→N+2`` proof; the transaction then terminalizes the
    source plan and installs the target with exactly one owner update within
    100 actions. An owner-active plan without that evidence fails closed before
    any SDK call: fail-closed replacement rejection is never silent persistence.
    """

    plan_digest: str
    intent: IntentMeta
    units: tuple[CanonicalUnitEntry, ...]
    owner: SuccessorOwner
    unit_flows: tuple[UnitFlowBinding, ...]
    proof: Stage1LifecycleProof
    operation_instant: str
    source_plan: SourcePlanReplacementEvidence | None = None

    def __post_init__(self) -> None:
        _require(self.intent.plan_digest == self.plan_digest, "the intent names this plan digest.")
        _require(
            is_canonical_instant(self.operation_instant),
            "operation instant must be a 20-digit epoch-microseconds instant.",
        )
        _require_exact_reservation(self.owner, self.intent)
        _require(
            (self.owner.active_plan_key is None) == (self.source_plan is None),
            "planned-plan replacement requires the complete source-plan evidence "
            "exactly when the owner carries an active plan.",
        )
        _require(
            roster_digest(self.units) == self.intent.roster_digest,
            "the supplied roster must reproduce the intent's exact roster digest.",
        )
        _require(
            intent_identity(
                IntentAuthorityInputs(
                    self.intent.root_plan_key,
                    self.intent.source_plan_key,
                    self.plan_digest,
                    self.intent.origin,
                    self.units,
                )
            )
            == self.intent.intent_id,
            "META plus every unit must reconstruct the exact intent authority.",
        )
        roster_keys = [unit.unit_key for unit in self.units]
        bound_keys = [binding.unit_key for binding in self.unit_flows]
        _require(
            sorted(bound_keys) == sorted(roster_keys) and len(set(bound_keys)) == len(bound_keys),
            "flow bindings cover exactly every roster unit key once.",
        )
        flows = [binding.flow_id for binding in self.unit_flows]
        _require(len(set(flows)) == len(flows), "unit flow ids must be distinct.")
        if self.source_plan is None:
            _require(len(self.proof.events) == 1, "ordinary persistence writes exactly one event.")
            target_event = self.proof.events[0]
        else:
            self._require_replacement_evidence(self.source_plan)
            _require(
                len(self.proof.events) == _COMPOUND_EVENT_COUNT,
                "planned-plan replacement is the sole two-event compound transition.",
            )
            target_event = self.proof.events[1]
            prior = self.proof.prior_head
            resulting_compound = self.proof.resulting_head
            _require(
                prior is not None
                and resulting_compound is not None
                and resulting_compound.latest_transition_sequence
                == prior.latest_transition_sequence + 2,
                "the compound replacement advances the head once from N to N+2.",
            )
        expectations = target_event.reattest.required_child_expectations
        _require(
            expectations is not None
            and expectations.child_outcome_reduction_version == CHILD_OUTCOME_REDUCTION_VERSION,
            "the plan proof carries the version-2 required-child expectation map.",
        )
        if expectations is None:  # pragma: no cover -- excluded by the requirement above
            raise ValueError("the plan proof carries the expectation map.")
        proven = {
            (member.child_flow_id, member.owner_generation)
            for member in expectations.required_child_expectations
        }
        _require(
            proven == {(flow, self.claimed_generation) for flow in flows},
            "the proof expectation map must bind every unit flow to the claimed generation.",
        )
        resulting = self.proof.resulting_head
        _require(
            resulting is not None
            and resulting.current_internal_execution_id == self.internal_execution_id,
            "the plan HEAD must carry this persistence's exact internal execution identity.",
        )

    def _require_replacement_evidence(self, source: SourcePlanReplacementEvidence) -> None:
        _require(
            self.owner.active_plan_key == source.plan_key
            and self.owner.active_intent_id == source.meta.origin_intent_id,
            "the owner active pair must name exactly the source plan and its origin intent.",
        )
        _require(
            self.owner.latest_internal_execution_id == source.meta.internal_execution_id,
            "the owner and source plan HEAD must agree on the internal execution identity.",
        )
        _require(
            source.meta.plan_digest == self.plan_digest,
            "a planned-plan replacement stays within one plan digest.",
        )

    @property
    def claimed_generation(self) -> int:
        """The generation this persistence retains from the exact claim."""
        generation = self.intent.claimed_generation
        if generation is None:  # pragma: no cover -- excluded by the claimed-state form
            raise ValueError("a claimed intent stores its claimed generation.")
        return generation

    @property
    def internal_execution_id(self) -> str:
        """Derive the ``stage1-plan-materialization/v1`` execution identity."""
        return internal_execution_identity(self.intent.intent_id, self.claimed_generation)

    @property
    def plan_key(self) -> str:
        """Derive the normal plan key from the plan digest and execution identity."""
        return plan_partition_key(self.plan_digest, self.internal_execution_id)


class PersistPlanOutcome(StrEnum):
    """Persisted now, or proven the exact retained assignment (no-op)."""

    PERSISTED = "persisted"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class PersistPlanResult:
    """One persistence's typed outcome and derived plan identity."""

    outcome: PersistPlanOutcome
    plan_key: str
    internal_execution_id: str
    generation: int


# --- AdmitUnitRequest -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdmitUnitRequest:
    """One successful queue admission recorded exactly once against its exact owners."""

    plan_digest: str
    plan_key: str
    intent: IntentMeta
    owner: SuccessorOwner
    plan_meta: PlanMeta
    unit: PlanUnit
    deduplication_digest: str
    operation_instant: str
    proof: Stage1LifecycleProof | None

    def __post_init__(self) -> None:
        _require(self.intent.plan_digest == self.plan_digest, "the intent names this plan digest.")
        _require(
            is_canonical_instant(self.operation_instant),
            "operation instant must be a 20-digit epoch-microseconds instant.",
        )
        _require(self.intent.state is IntentState.CLAIMED, "admission requires a claimed intent.")
        _require(
            self.owner.active_plan_key == self.plan_key
            and self.owner.active_intent_id == self.intent.intent_id,
            "the owner active pair must name exactly this plan and intent.",
        )
        _require(
            self.plan_key
            == plan_partition_key(self.plan_meta.plan_digest, self.plan_meta.internal_execution_id),
            "the plan key must reproduce from the plan META identity.",
        )
        _require(self.plan_meta.status is PlanStatus.PLANNED, "admission requires a planned plan.")
        _require(
            self.plan_meta.origin_intent_id == self.intent.intent_id
            and self.plan_meta.generation == self.intent.claimed_generation,
            "the plan META must carry this intent and its exact claimed generation.",
        )
        _require(
            self.unit.admission is PlanUnitAdmission.PENDING
            and self.unit.status is PlanUnitStatus.QUEUED
            and self.unit.owner_generation == self.plan_meta.generation,
            "admission changes one pending queued unit at the plan's exact generation.",
        )
        _require(
            self.plan_meta.admitted_count < self.plan_meta.unit_count,
            "a fully admitted plan accepts no further admission.",
        )
        _require(
            is_canonical_digest(self.deduplication_digest),
            "the deduplication digest must be 64 lowercase hex characters.",
        )
        _require(
            (self.proof is not None) == self.final,
            "the admitted lifecycle proof exists exactly for the final admission.",
        )
        if self.proof is not None:
            _require(len(self.proof.events) == 1, "final admission writes exactly one event.")

    @property
    def final(self) -> bool:
        """Whether this admission reaches ``admitted_count = unit_count``."""
        return self.plan_meta.admitted_count + 1 == self.plan_meta.unit_count


class AdmitUnitOutcome(StrEnum):
    """Admitted now, or proven the exact durable admission (no-op)."""

    ADMITTED = "admitted"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class AdmitUnitResult:
    """One admission's typed outcome; ``final`` marks the intent transition."""

    outcome: AdmitUnitOutcome
    final: bool


# --- FailAdmissionRequest -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailAdmissionRequest:
    """One definitive admission failure: terminal candidate, same intent back to pending."""

    plan_digest: str
    plan_key: str
    intent: IntentMeta
    owner: SuccessorOwner
    plan_meta: PlanMeta
    operation_instant: str
    proof: Stage1LifecycleProof

    def __post_init__(self) -> None:
        _require(self.intent.plan_digest == self.plan_digest, "the intent names this plan digest.")
        _require(
            is_canonical_instant(self.operation_instant),
            "operation instant must be a 20-digit epoch-microseconds instant.",
        )
        _require(self.intent.state is IntentState.CLAIMED, "the failing intent must be claimed.")
        _require(
            self.owner.active_plan_key == self.plan_key
            and self.owner.active_intent_id == self.intent.intent_id,
            "the owner active pair must name exactly this candidate plan and intent.",
        )
        _require(
            self.plan_key
            == plan_partition_key(self.plan_meta.plan_digest, self.plan_meta.internal_execution_id),
            "the plan key must reproduce from the plan META identity.",
        )
        _require(self.plan_meta.status is PlanStatus.PLANNED, "only a planned candidate fails.")
        _require(
            self.plan_meta.origin_intent_id == self.intent.intent_id
            and self.plan_meta.generation == self.intent.claimed_generation,
            "the candidate plan must carry this intent and its exact claimed generation.",
        )
        _require(
            self.owner.latest_internal_execution_id == self.plan_meta.internal_execution_id,
            "the owner and plan HEAD must agree on the internal execution identity.",
        )
        _require(len(self.proof.events) == 1, "admission failure writes exactly one event.")


class FailAdmissionOutcome(StrEnum):
    """Terminalized now, or proven the exact durable transfer (no-op)."""

    FAILED = "failed"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class FailAdmissionResult:
    """One admission failure's typed outcome and restored schedule."""

    outcome: FailAdmissionOutcome
    retry_count: int
    due_at: str


# --- CompletePlanRequest --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttestedUnitObservation:
    """One strongly read attested plan unit with its complete exact item snapshot."""

    unit_key: str
    flow_id: str
    owner_generation: int
    item: Mapping[str, object]

    def __post_init__(self) -> None:
        _require(
            self.item.get("record_key") == unit_record_key(self.unit_key),
            "the unit snapshot must carry its exact record key.",
        )
        _require(
            self.item.get("status") == PlanUnitStatus.ATTESTED.value,
            "completion conditions only attested units.",
        )
        _require(
            self.item.get("owner_generation") == self.owner_generation
            and self.item.get("flow_id") == self.flow_id,
            "the unit snapshot must agree with its typed generation and flow id.",
        )


@dataclass(frozen=True, slots=True)
class SettledAttemptObservation:
    """One strongly read terminal, already-settled accepted Athena attempt."""

    physical_table_id: str
    attempt_id: str
    provider_state: str
    usage_authority_digest: str
    item: Mapping[str, object]

    def __post_init__(self) -> None:
        _require(
            self.provider_state in _PROVIDER_TERMINAL_STATES,
            "an accepted attempt must be provider-terminal before completion.",
        )
        _require(
            is_canonical_digest(self.usage_authority_digest),
            "the settlement authority digest must be 64 lowercase hex characters.",
        )
        _require(
            self.item.get("state") == self.provider_state
            and self.item.get("athena_usage_state") == "settled"
            and self.item.get("usage_authority_digest") == self.usage_authority_digest,
            "the attempt snapshot must carry the exact terminal, settled bytes.",
        )


@dataclass(frozen=True, slots=True)
class CompletePlanRequest:
    """The metadata-only ``planned→completed`` transition, at most 51 actions."""

    plan_digest: str
    plan_key: str
    owner: SuccessorOwner
    plan_meta: PlanMeta
    units: tuple[AttestedUnitObservation, ...]
    attempts: tuple[SettledAttemptObservation, ...]
    proof: Stage1LifecycleProof
    operation_instant: str

    def __post_init__(self) -> None:
        _require(
            is_canonical_instant(self.operation_instant),
            "operation instant must be a 20-digit epoch-microseconds instant.",
        )
        _require(
            self.owner.active_plan_key == self.plan_key
            and self.owner.active_intent_id == self.plan_meta.origin_intent_id,
            "the owner active pair must name exactly this plan and its origin intent.",
        )
        _require(
            self.plan_key
            == plan_partition_key(self.plan_meta.plan_digest, self.plan_meta.internal_execution_id),
            "the plan key must reproduce from the plan META identity.",
        )
        _require(self.plan_meta.status is PlanStatus.PLANNED, "only a planned plan completes.")
        counters = self.plan_meta
        _require(
            counters.admitted_count == counters.unit_count
            and counters.attested_count == counters.unit_count
            and counters.failed_count == 0,
            "completion requires full admission, full attestation, and zero failures.",
        )
        _require(
            self.owner.latest_internal_execution_id == self.plan_meta.internal_execution_id,
            "the owner and plan HEAD must agree on the internal execution identity.",
        )
        _require(
            len(self.units) == counters.unit_count,
            "completion conditions exactly every plan unit.",
        )
        _require(
            all(unit.owner_generation == counters.generation for unit in self.units),
            "every unit must be attested at the plan's exact generation.",
        )
        proven = sorted((unit.flow_id.encode("utf-8"), unit.flow_id) for unit in self.units)
        expected = [member.child_flow_id for member in self.plan_meta.required_child_expectations]
        _require(
            [flow for _, flow in proven] == expected,
            "unit observations must reconstruct the persisted expectation authority.",
        )
        _require(
            len(self.attempts) <= MAX_PLAN_ATTEMPTS,
            "a plan carries at most 32 permanent attempt reservations.",
        )
        _require(
            len(self.attempts) == counters.settled_attempt_count,
            "attempt observations must match the plan's complete settled-attempt count.",
        )
        pairs = [(attempt.physical_table_id, attempt.attempt_id) for attempt in self.attempts]
        _require(len(set(pairs)) == len(pairs), "attempt observations must be distinct.")
        _require(len(self.proof.events) == 1, "completion writes exactly one terminal event.")


class CompletePlanOutcome(StrEnum):
    """Completed now, or proven the exact durable completion (no-op)."""

    COMPLETED = "completed"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class CompletePlanResult:
    """One completion's typed outcome."""

    outcome: CompletePlanOutcome


# --- Table lease, fence, and provider-attempt operations (§3.2 / §5) ------------------------------

_UNIT_WRITING_STATUS = "writing"
_RUN_BEGIN_STATES = frozenset({"open", "reconciled"})
_UNIT_RESERVATION_CAP = 10
_PLAN_RESERVATION_CAP = 32
_RUN_RESERVATION_CAP = 10
RUN_RESERVATION_CAP: Final = _RUN_RESERVATION_CAP
"""The permanent inline-run Begin cap a caller may preflight (S6 §2.5 graph units)."""
_LEASE_ACQUIRED_TRANSITION = "semantic_unit_lease_acquired"
_UNIT_REFUSED_TRANSITION = "semantic_unit_refused"
_INLINE_REFUSED_TRANSITION = "semantic_inline_run_refused"
_INLINE_RECORDED_TRANSITION = "semantic_inline_run_recorded"
_INLINE_CANCEL_TRANSITION = "semantic_inline_run_cancel_requested"
_INLINE_RECONCILED_TRANSITION = "semantic_inline_run_reconciled"


def _parsed_fence(observation: FenceObservation, table_id: str) -> TableAttemptFence:
    try:
        fence = parse_fence(observation)
    except TableAttemptError as error:
        raise ValueError(f"the observed fence is outside its closed form: {error}") from error
    _require(fence.physical_table_id == table_id, "the observed fence names another table.")
    return fence


def _parsed_attempt(observation: AttemptObservation, table_id: str) -> TableAttempt:
    try:
        attempt = parse_attempt(observation)
    except TableAttemptError as error:
        raise ValueError(f"the observed attempt is outside its closed form: {error}") from error
    _require(
        attempt.core.physical_table_id == table_id,
        "the observed attempt names another table.",
    )
    return attempt


def _require_pair_bound(fence: ActiveFence | ParkedFence, attempt: NonterminalTableAttempt) -> None:
    core = attempt.core
    _require(
        fence.attempt_id == core.attempt_id
        and fence.lease_holder == core.lease_holder
        and fence.fencing_token == core.fencing_token,
        "the fence and attempt must carry byte-equal binding fields.",
    )
    if isinstance(fence, ParkedFence):
        _require(
            isinstance(attempt, ParkedAttempt) and fence.parked_at == attempt.parked_at,
            "a parked fence requires a parked attempt with a byte-equal park instant.",
        )


def _require_pending_reconciliation(observation: FenceObservation) -> None:
    rec = observation.reconciliation
    _require(
        rec.reconcile_status == "pending"
        and isinstance(rec.reconcile_after_or_block, str)
        and isinstance(rec.reconcile_attempt_count, int)
        and not isinstance(rec.reconcile_attempt_count, bool)
        and rec.reconcile_attempt_count >= 0,
        "the fence must carry one exact pending reconciliation tuple.",
    )


def _single_event_authority(proof: Stage1LifecycleProof) -> Stage1EventAuthorityRequest:
    if len(proof.events) != 1:
        raise ValueError("this operation writes exactly one lifecycle event.")
    return proof.events[0].reattest.authority


def _snapshot_text(item: Mapping[str, object], name: str) -> str | None:
    value = item.get(name)
    _require(value is None or isinstance(value, str), f"owner snapshot {name} must be text.")
    return None if value is None else str(value)


def _snapshot_int(item: Mapping[str, object], name: str, *, default: int = 0) -> int:
    value = item.get(name)
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"owner snapshot {name} must be an integer.")
    return value


# --- Owner snapshots ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueuedUnitAttemptOwner:
    """One strongly read plan-unit owner snapshot at its exact ``UNIT#`` key."""

    plan_key: str
    physical_table_id: str
    item: Mapping[str, object]

    def __post_init__(self) -> None:
        _require(bool(self.plan_key.strip()), "a queued owner names its exact active plan key.")
        _require(
            self.item.get("partition_key") == self.plan_key
            and self.item.get("record_key") == f"{UNIT_RECORD_PREFIX}{self.physical_table_id}",
            "the unit snapshot must carry its exact plan partition and UNIT# record key.",
        )

    @property
    def unit_record_key(self) -> str:
        """The exact ``UNIT#<physical table id>`` record key."""
        return f"{UNIT_RECORD_PREFIX}{self.physical_table_id}"


@dataclass(frozen=True, slots=True)
class InlineRunAttemptOwner:
    """One strongly read inline ``RUN#`` owner snapshot at its exact keys."""

    physical_table_id: str
    run_id: str
    item: Mapping[str, object]

    def __post_init__(self) -> None:
        _require(
            self.item.get("partition_key") == table_attempt_partition_key(self.physical_table_id)
            and self.item.get("record_key") == run_record_key(self.run_id)
            and self.item.get("run_id") == self.run_id,
            "the run snapshot must carry its exact TABLE_ATTEMPT#/RUN# keys and run id.",
        )


type TableAttemptOwnerSnapshot = QueuedUnitAttemptOwner | InlineRunAttemptOwner


def _require_bound_owner(
    owner: TableAttemptOwnerSnapshot, attempt: NonterminalTableAttempt
) -> None:
    core = attempt.core
    if isinstance(owner, QueuedUnitAttemptOwner):
        _require(
            core.plan_unit_owner_ref == owner.plan_key,
            "the attempt's owner binding must name this exact plan key.",
        )
    else:
        _require(
            core.inline_run_owner_ref == owner.run_id,
            "the attempt's owner binding must name this exact run id.",
        )
    _require(
        _snapshot_text(owner.item, "current_attempt_id") == core.attempt_id
        and _snapshot_text(owner.item, "lease_holder") == core.lease_holder
        and _snapshot_int(owner.item, "fencing_token", default=-1) == core.fencing_token
        and _snapshot_text(owner.item, "reserved_slot_identity") == core.reservation_identity,
        "the owner snapshot must reproduce the attempt's exact reservation binding.",
    )
    if isinstance(owner, QueuedUnitAttemptOwner):
        _require(
            _snapshot_text(owner.item, "status") == _UNIT_WRITING_STATUS
            and _snapshot_text(owner.item, "writer_execution_id") == core.writer_execution_id
            and _snapshot_int(owner.item, "ordinal", default=-1) == core.ordinal
            and 1 <= _snapshot_int(owner.item, "unit_reservation_count") <= _UNIT_RESERVATION_CAP,
            "the queued owner must be writing with this exact reservation slot.",
        )
    else:
        _require(
            _snapshot_text(owner.item, "state") in {"open", "cancel_requested", "reconciled"}
            and _snapshot_text(owner.item, "execution_id") == core.writer_execution_id
            and _snapshot_int(owner.item, "next_ordinal", default=-1) == core.ordinal
            and 1
            <= _snapshot_int(owner.item, "accepted_attempt_reservations")
            <= _RUN_RESERVATION_CAP,
            "the inline owner must be nonterminal with this exact reservation slot.",
        )


# --- AcquireTableFenceRequest ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AcquireTableFenceRequest:
    """One §5 lease acquisition with its exact observed lease/fence snapshots.

    ``observed_lease=None`` is the proven-absent bootstrap branch; the builder then
    creates the record at token 1 instead of CASing the exact observed snapshot.
    The optional tagged owner-lifecycle proof may carry only the one registered
    ``semantic_unit_lease_acquired`` event over an existing (claimed) unit head.
    """

    physical_table_id: str
    holder: str
    operation_instant: str
    new_expires_at: str
    observed_lease: TableLeaseRecord | None
    observed_fence: FenceObservation | None
    proof: Stage1LifecycleProof | None = None

    def __post_init__(self) -> None:
        _require(bool(self.holder.strip()), "acquisition requires a nonblank holder identity.")
        _require(
            is_canonical_instant(self.operation_instant),
            "operation instant must be a 20-digit epoch-microseconds instant.",
        )
        candidate = TableLeaseRecord(
            physical_table_id=self.physical_table_id,
            holder=self.holder,
            fencing_token=1,
            expires_at=self.new_expires_at,
        )
        _require(
            candidate.expiry_epoch_micros() > int(self.operation_instant),
            "the new lease expiry must lie after the operation instant.",
        )
        if self.observed_lease is not None:
            _require(
                self.observed_lease.physical_table_id == self.physical_table_id,
                "the observed lease names another table.",
            )
        if self.observed_fence is not None:
            _parsed_fence(self.observed_fence, self.physical_table_id)
        if self.proof is not None:
            authority = _single_event_authority(self.proof)
            _require(
                authority.transition_id == _LEASE_ACQUIRED_TRANSITION,
                "the tagged variant may put only semantic_unit_lease_acquired.",
            )
            _require(
                self.proof.prior_head is not None,
                "the lease-acquired event is forbidden for an unclaimed unit: its "
                "flow head must already exist.",
            )

    @property
    def new_fencing_token(self) -> int:
        """The monotonic token this acquisition installs: observed token plus one."""
        return 1 if self.observed_lease is None else self.observed_lease.fencing_token + 1


class AcquireTableFenceOutcome(StrEnum):
    """Acquired now, or refused with the closed §5 busy classification."""

    ACQUIRED = "acquired"
    TABLE_BUSY = "table_busy"
    TABLE_ATTEMPT_PENDING = "table_attempt_pending"


@dataclass(frozen=True, slots=True)
class AcquireTableFenceResult:
    """One acquisition's typed outcome; grant fields exist exactly when acquired."""

    outcome: AcquireTableFenceOutcome
    fencing_token: int | None = None
    expires_at: str | None = None
    held_until: str | None = None

    def __post_init__(self) -> None:
        acquired = self.outcome is AcquireTableFenceOutcome.ACQUIRED
        _require(
            acquired == (self.fencing_token is not None)
            and acquired == (self.expires_at is not None),
            "grant fields exist exactly when the fence was acquired.",
        )


# --- ReleaseTableFenceRequest ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReleaseTableFenceRequest:
    """One exact-holder/token lease release; the fence must be absent or clear."""

    physical_table_id: str
    lease: TableLeaseRecord
    observed_fence: FenceObservation | None

    def __post_init__(self) -> None:
        _require(
            self.lease.physical_table_id == self.physical_table_id,
            "the lease names another table.",
        )
        if self.observed_fence is not None:
            fence = _parsed_fence(self.observed_fence, self.physical_table_id)
            _require(
                isinstance(fence, ClearFence),
                "release is impossible while a fence is active or parked.",
            )


class ReleaseTableFenceOutcome(StrEnum):
    """Released now; holder and token are retained on the record."""

    RELEASED = "released"


@dataclass(frozen=True, slots=True)
class ReleaseTableFenceResult:
    """One release's typed outcome."""

    outcome: ReleaseTableFenceOutcome


# --- BeginTableAttemptRequest ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BeginQueuedUnitOwner:
    """The queued Begin owner: the exact unit snapshot plus its plan ``META`` snapshot."""

    unit: QueuedUnitAttemptOwner
    meta_item: Mapping[str, object]

    def __post_init__(self) -> None:
        _require(
            self.meta_item.get("partition_key") == self.unit.plan_key
            and self.meta_item.get("record_key") == META_RECORD_KEY,
            "the plan META snapshot must carry this exact plan partition.",
        )
        _require(
            _snapshot_text(self.meta_item, "status") == PlanStatus.PLANNED.value,
            "Begin requires the owner's planned plan META.",
        )
        unit_count = _snapshot_int(self.meta_item, "unit_count", default=-1)
        _require(
            unit_count >= 1
            and _snapshot_int(self.meta_item, "admitted_count", default=-1) == unit_count,
            "the conditioned full-admission counters must prove the origin intent admitted.",
        )
        _require(
            _snapshot_text(self.unit.item, "status") == _UNIT_WRITING_STATUS,
            "Begin requires the claimed unit in writing state.",
        )
        _require(
            self.unit.item.get("current_attempt_id") is None,
            "Begin requires an unreserved owner: no current attempt may be bound.",
        )
        _require(
            self.unit_reservation_count < _UNIT_RESERVATION_CAP,
            "no Begin occurs at or beyond the 10-reservation unit cap.",
        )
        _require(
            self.plan_reservation_count < _PLAN_RESERVATION_CAP,
            "no Begin occurs at or beyond the 32-reservation plan cap.",
        )

    @property
    def unit_reservation_count(self) -> int:
        """The observed permanent unit reservation count (absent is zero)."""
        return _snapshot_int(self.unit.item, "unit_reservation_count")

    @property
    def plan_reservation_count(self) -> int:
        """The observed permanent plan reservation count (absent is zero)."""
        return _snapshot_int(self.meta_item, "plan_reservation_count")


@dataclass(frozen=True, slots=True)
class BeginInlineRunOwner:
    """The inline Begin owner: the exact nonterminal ``RUN#`` snapshot."""

    run: InlineRunAttemptOwner

    def __post_init__(self) -> None:
        _require(
            _snapshot_text(self.run.item, "state") in _RUN_BEGIN_STATES,
            "inline Begin requires the run in open or reconciled state.",
        )
        _require(
            self.run.item.get("current_attempt_id") is None,
            "Begin requires an unreserved owner: no current attempt may be bound.",
        )
        _require(
            self.reservation_count < _RUN_RESERVATION_CAP,
            "no Begin occurs at or beyond the 10-reservation run cap.",
        )
        _require(self.next_ordinal >= 1, "the run's next ordinal must be a positive integer.")

    @property
    def reservation_count(self) -> int:
        """The observed permanent run reservation count (absent is zero)."""
        return _snapshot_int(self.run.item, "accepted_attempt_reservations")

    @property
    def next_ordinal(self) -> int:
        """The run's current unreserved ordinal."""
        return _snapshot_int(self.run.item, "next_ordinal", default=-1)


@dataclass(frozen=True, slots=True)
class BeginTableAttemptRequest:
    """One §5 Begin: attempt intent put, fence activation, and owner reservation.

    The attempt identity comes only from the pinned §5 step-2 constructor over the
    immutable owner/table/lease/ordinal/envelope inputs; no caller supplies an
    attempt id. The permanent reservation identity is likewise derived — the pinned
    §5 step-2 reservation constructor over the attempt id — and Begin writes it
    byte-identically to the attempt and its owner; no caller supplies it.
    """

    physical_table_id: str
    lease: TableLeaseRecord
    operation_instant: str
    observed_fence: FenceObservation | None
    owner: BeginQueuedUnitOwner | BeginInlineRunOwner
    writer_execution_id: str
    statement_kind: str
    start_envelope: str
    graph_claims: tuple[GraphClaimFence, ...] = ()
    """V-9: a graph-envelope Begin is refused once a takeover bumped any member's claim lease.

    Slice 7 (1b): one fence per execution-set member, in member order; `()` means no graph
    authority (a non-graph writer). A node's fence may appear once."""

    def __post_init__(self) -> None:
        _require(
            self.lease.physical_table_id == self.physical_table_id,
            "the lease names another table.",
        )
        _require(self.lease.parked_at is None, "Begin requires an unparked lease.")
        _require(
            is_canonical_instant(self.operation_instant),
            "operation instant must be a 20-digit epoch-microseconds instant.",
        )
        _require(
            int(self.operation_instant) < self.lease.expiry_epoch_micros(),
            "Begin requires an exact unexpired lease.",
        )
        if self.observed_fence is not None:
            fence = _parsed_fence(self.observed_fence, self.physical_table_id)
            _require(
                isinstance(fence, ClearFence),
                "Begin requires the fence absent or exactly clear.",
            )
        _require(bool(self.writer_execution_id.strip()), "Begin names its writer execution.")
        fenced = [fence.node_id for fence in self.graph_claims]
        _require(len(set(fenced)) == len(fenced), "Begin carries each node's claim fence once.")
        _require(bool(self.statement_kind.strip()), "Begin names its statement kind.")
        _require(bool(self.start_envelope.strip()), "Begin carries its canonical envelope.")
        if isinstance(self.owner, BeginQueuedUnitOwner):
            _require(
                self.owner.unit.physical_table_id == self.physical_table_id,
                "the queued owner names another table.",
            )
        else:
            _require(
                self.owner.run.physical_table_id == self.physical_table_id,
                "the inline owner names another table.",
            )
            _require(
                _snapshot_text(self.owner.run.item, "execution_id") == self.writer_execution_id,
                "the inline owner's immutable execution id must match the writer execution.",
            )
        self.identity()  # canonical constructor inputs or typed rejection

    @property
    def ordinal(self) -> int:
        """The one unreserved ordinal: plan count plus one, or the run's next ordinal."""
        if isinstance(self.owner, BeginQueuedUnitOwner):
            return self.owner.plan_reservation_count + 1
        return self.owner.next_ordinal

    @property
    def owner_kind(self) -> AttemptOwnerKind:
        """The closed §5 step-2 owner vocabulary member for this Begin."""
        if isinstance(self.owner, BeginQueuedUnitOwner):
            return AttemptOwnerKind.QUEUED_UNIT
        return AttemptOwnerKind.INLINE_RUN

    @property
    def owner_ref(self) -> str:
        """The exact active plan key or run id the attempt binds."""
        if isinstance(self.owner, BeginQueuedUnitOwner):
            return self.owner.unit.plan_key
        return self.owner.run.run_id

    def identity(self) -> RecomputedAttemptIdentity:
        """Derive the pinned §5 step-2 attempt identity (the D80 constructor)."""
        return attempt_identity(
            AttemptIdentityRequest(
                owner_kind=self.owner_kind,
                owner_ref=self.owner_ref,
                physical_table_id=self.physical_table_id,
                lease_holder=self.lease.holder,
                fencing_token=self.lease.fencing_token,
                ordinal=self.ordinal,
                start_envelope=self.start_envelope,
            )
        )

    @property
    def reservation_identity(self) -> str:
        """Derive the pinned §5 step-2 permanent-slot reservation identity."""
        return attempt_reservation_identity(self.identity().attempt_id)

    def initial_reconcile_due_micros(self) -> int:
        """Begin's initial due: exactly the lease expiry's UTC epoch microseconds."""
        return self.lease.expiry_epoch_micros()


class BeginTableAttemptOutcome(StrEnum):
    """Begun now, or proven the exact occupied slot (no-op, nothing incremented)."""

    BEGUN = "begun"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class BeginTableAttemptResult:
    """One Begin's typed outcome and derived identity."""

    outcome: BeginTableAttemptOutcome
    attempt_id: str
    client_request_token: str
    envelope_digest: str
    ordinal: int


# --- RecordStage1RefusalRequest -------------------------------------------------------------------


class Stage1RefusalTag(StrEnum):
    """Closed owner tag for a Stage-1 refusal."""

    UNIT = "unit"
    INLINE = "inline"


class Stage1RefusalReason(StrEnum):
    """The only two retryable Stage-1 refusal reasons."""

    TABLE_BUSY = "TABLE_BUSY"
    TABLE_ATTEMPT_PENDING = "TABLE_ATTEMPT_PENDING"


@dataclass(frozen=True, slots=True)
class RefusedUnitObservation:
    """The exact claimed unit, plan, intent, and owner facts the unit tag conditions."""

    plan_digest: str
    plan_key: str
    intent: IntentMeta
    owner: SuccessorOwner
    writer_execution_id: str
    unit_item: Mapping[str, object]
    meta_item: Mapping[str, object]

    def __post_init__(self) -> None:
        _require(self.intent.plan_digest == self.plan_digest, "the intent names this digest.")
        _require(
            self.intent.state is IntentState.ADMITTED,
            "a unit refusal conditions the admitted origin intent.",
        )
        _require(
            self.owner.active_plan_key == self.plan_key
            and self.owner.active_intent_id == self.intent.intent_id,
            "the owner active pair must name exactly this plan and intent.",
        )
        _require(
            self.meta_item.get("partition_key") == self.plan_key
            and self.meta_item.get("record_key") == META_RECORD_KEY
            and _snapshot_text(self.meta_item, "status") == PlanStatus.PLANNED.value
            and _snapshot_int(self.meta_item, "generation", default=-1)
            == self.intent.claimed_generation
            and _snapshot_text(self.meta_item, "origin_intent_id") == self.intent.intent_id,
            "the plan META snapshot must carry this exact planned plan and intent.",
        )
        _require(
            self.unit_item.get("partition_key") == self.plan_key
            and isinstance(self.unit_item.get("record_key"), str)
            and str(self.unit_item.get("record_key")).startswith(UNIT_RECORD_PREFIX),
            "the unit snapshot must carry its exact plan partition and UNIT# record key.",
        )
        _require(
            _snapshot_text(self.unit_item, "status") == _UNIT_WRITING_STATUS
            and _snapshot_text(self.unit_item, "writer_execution_id") == self.writer_execution_id,
            "a unit refusal conditions the exact writing unit and its writer identity.",
        )


@dataclass(frozen=True, slots=True)
class RefusedInlineRunObservation:
    """The exact open or reconciled run the inline tag conditions."""

    run: InlineRunAttemptOwner

    def __post_init__(self) -> None:
        _require(
            _snapshot_text(self.run.item, "state") in _RUN_BEGIN_STATES,
            "an inline refusal conditions the exact open or reconciled run.",
        )


@dataclass(frozen=True, slots=True)
class RecordStage1RefusalRequest:
    """One deterministic retryable refusal event plus its exact owner head advance."""

    tag: Stage1RefusalTag
    reason: Stage1RefusalReason
    refusal_instant: str
    proof: Stage1LifecycleProof
    unit: RefusedUnitObservation | None = None
    inline: RefusedInlineRunObservation | None = None

    def __post_init__(self) -> None:
        _require(
            is_canonical_instant(self.refusal_instant),
            "refusal instant must be a 20-digit epoch-microseconds instant.",
        )
        _require(
            (self.tag is Stage1RefusalTag.UNIT) == (self.unit is not None)
            and (self.tag is Stage1RefusalTag.INLINE) == (self.inline is not None),
            "exactly the tagged owner observation must be supplied.",
        )
        authority = _single_event_authority(self.proof)
        expected = (
            _UNIT_REFUSED_TRANSITION
            if self.tag is Stage1RefusalTag.UNIT
            else _INLINE_REFUSED_TRANSITION
        )
        _require(
            authority.transition_id == expected,
            "the proof must carry exactly the registered refusal event.",
        )
        _require(
            authority.typed_reason == self.reason.value.lower(),
            "the proof's typed reason must carry this exact closed refusal reason.",
        )


class RecordStage1RefusalOutcome(StrEnum):
    """Recorded now, or proven the byte-identical durable refusal event (no-op)."""

    RECORDED = "recorded"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class RecordStage1RefusalResult:
    """One refusal's typed outcome."""

    outcome: RecordStage1RefusalOutcome


# --- PersistInlineRunRequest ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PersistInlineRunRequest:
    """One atomic inline ``RUN#`` creation with its recorded event and run head.

    Public identity and topology are consumed only from the linked event-projection
    contract through the opaque proof: the run's status flow and execution bindings
    are exactly ``proof.flow_id`` and ``proof.execution_id``, and no caller field
    can supply or override a public identity, topology, surface, or status value.
    """

    writer_kind: WriterKind
    operation_id: str
    execution_id: str
    physical_table_id: str
    table_kind: str
    statement_intent_kinds: tuple[str, ...]
    pointer_finalization_payload: str | None
    created_at: str
    proof: Stage1LifecycleProof

    def __post_init__(self) -> None:
        _require(bool(self.table_kind.strip()), "the run names its exact table kind.")
        _require(
            bool(self.statement_intent_kinds)
            and all(bool(kind.strip()) for kind in self.statement_intent_kinds),
            "the immutable statement roster must be a nonempty ordered roster.",
        )
        _require(
            is_canonical_instant(self.created_at),
            "creation instant must be a 20-digit epoch-microseconds instant.",
        )
        self.run_id()  # canonical tuple or typed rejection
        authority = _single_event_authority(self.proof)
        _require(
            authority.transition_id == _INLINE_RECORDED_TRANSITION,
            "run persistence writes exactly the registered recorded event.",
        )
        identity = self.proof.events[0].reattest.identity
        _require(
            isinstance(identity, InlineIdentityRequest),
            "the proof identity must be the inline-run family.",
        )
        if isinstance(identity, InlineIdentityRequest):
            _require(
                identity.writer_kind == self.writer_kind.value
                and identity.operation_id == self.operation_id
                and identity.physical_table_id == self.physical_table_id
                and identity.internal_execution_id == self.execution_id,
                "the proof identity must reproduce this exact immutable run tuple.",
            )
        _require(
            self.proof.resulting_head is not None,
            "run persistence carries exactly one run-head effect.",
        )

    def run_id(self) -> str:
        """Derive the pinned ``run_id`` from the immutable tuple."""
        return run_identity(
            RunIdentityRequest(
                writer_kind=self.writer_kind,
                operation_id=self.operation_id,
                execution_id=self.execution_id,
                physical_table_id=self.physical_table_id,
            )
        )


class PersistInlineRunOutcome(StrEnum):
    """Created now, or proven the byte-identical durable run and event (no-op)."""

    PERSISTED = "persisted"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class PersistInlineRunResult:
    """One run persistence's typed outcome and derived identity."""

    outcome: PersistInlineRunOutcome
    run_id: str


# --- RecoverParkedAttemptSubmissionRequest --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecoverParkedAttemptSubmissionRequest:
    """One reconciler recovery of a parked submission against the exact parked pair."""

    physical_table_id: str
    fence: FenceObservation
    attempt: AttemptObservation
    query_execution_id: str
    submitted_at: str
    sanitized_start_evidence: SanitizedStartEvidence

    def __post_init__(self) -> None:
        fence = _parsed_fence(self.fence, self.physical_table_id)
        _require(isinstance(fence, ParkedFence), "recovery conditions the exact parked fence.")
        attempt = _parsed_attempt(self.attempt, self.physical_table_id)
        _require(
            isinstance(attempt, ParkedAttempt),
            "recovery updates the exact parked attempt.",
        )
        if isinstance(fence, ParkedFence) and isinstance(attempt, ParkedAttempt):
            _require_pair_bound(fence, attempt)
            _require(
                valid_query_execution_id(self.query_execution_id),
                "the recovered query execution id must be valid nonblank text.",
            )
            _require(
                is_canonical_instant(self.submitted_at),
                "submitted_at must be a 20-digit epoch-microseconds instant.",
            )
            if attempt.query_execution_id is not None:
                _require(
                    attempt.query_execution_id == self.query_execution_id
                    and attempt.submitted_at == self.submitted_at
                    and attempt.sanitized_start_evidence == self.sanitized_start_evidence,
                    "a durable query triple permits only the identical recovered bytes.",
                )
        _require_pending_reconciliation(self.fence)


class RecoverParkedAttemptSubmissionOutcome(StrEnum):
    """Recovered now, or proven the identical durable triple (no-op)."""

    RECOVERED = "recovered"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class RecoverParkedAttemptSubmissionResult:
    """One recovery's typed outcome."""

    outcome: RecoverParkedAttemptSubmissionOutcome


# --- RequestAttemptCancellationRequest ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequestAttemptCancellationRequest:
    """One write-once cancellation instant against the exact nonterminal attempt.

    A submitted attempt transitions to ``cancel_requested``; a parked attempt with
    a durable valid query identity keeps ``state=parked``. An ``intent`` attempt
    or one without a durable valid query identity is rejected before mutation.
    The inline tagged variant (the ``semantic_inline_run_cancel_requested`` proof)
    additionally changes the run ``open|reconciled -> cancel_requested`` per §3.1,
    so no further Begin is accepted against the cancelled run; a run observed in
    ``cancel_requested`` or ``terminal`` is rejected before mutation.
    """

    physical_table_id: str
    fence: FenceObservation
    attempt: AttemptObservation
    cancel_requested_at: str
    owner: TableAttemptOwnerSnapshot
    proof: Stage1LifecycleProof | None = None

    def __post_init__(self) -> None:
        _require(
            is_canonical_instant(self.cancel_requested_at),
            "cancellation instant must be a 20-digit epoch-microseconds instant.",
        )
        fence = _parsed_fence(self.fence, self.physical_table_id)
        _require(
            isinstance(fence, ActiveFence | ParkedFence),
            "cancellation conditions an active or parked fence.",
        )
        attempt = _parsed_attempt(self.attempt, self.physical_table_id)
        _require(
            not isinstance(attempt, IntentAttempt),
            "an intent attempt cannot be cancelled: acceptance is ambiguous.",
        )
        _require(
            not isinstance(attempt, CancelRequestedAttempt),
            "cancellation is write-once; the durable instant is already recorded.",
        )
        _require(
            isinstance(attempt, SubmittedAttempt | ParkedAttempt),
            "cancellation changes only a submitted or parked attempt.",
        )
        if isinstance(attempt, ParkedAttempt):
            _require(
                attempt.query_execution_id is not None and attempt.cancel_requested_at is None,
                "a parked cancellation requires the durable valid query identity "
                "and no prior cancellation instant.",
            )
        if isinstance(fence, ActiveFence | ParkedFence) and isinstance(
            attempt, SubmittedAttempt | ParkedAttempt
        ):
            _require_pair_bound(fence, attempt)
            _require_bound_owner(self.owner, attempt)
        if self.proof is not None:
            _require(
                isinstance(self.owner, InlineRunAttemptOwner),
                "only the inline tagged variant puts a cancellation event.",
            )
            _require(
                _snapshot_text(self.owner.item, "state") in _RUN_BEGIN_STATES,
                "the inline tagged variant changes only an open or reconciled run "
                "to cancel_requested.",
            )
            authority = _single_event_authority(self.proof)
            _require(
                authority.transition_id == _INLINE_CANCEL_TRANSITION,
                "the inline tagged variant puts only the registered cancellation event.",
            )


class RequestAttemptCancellationOutcome(StrEnum):
    """Requested now, or proven the identical durable first instant (no-op)."""

    REQUESTED = "requested"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class RequestAttemptCancellationResult:
    """One cancellation request's typed outcome."""

    outcome: RequestAttemptCancellationOutcome


# --- ParkAttemptRequest ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParkAttemptRequest:
    """One atomic lease/attempt/fence park at a single captured UTC instant."""

    physical_table_id: str
    lease: TableLeaseRecord
    fence: FenceObservation
    attempt: AttemptObservation
    park_instant: str

    def __post_init__(self) -> None:
        _require(
            self.lease.physical_table_id == self.physical_table_id,
            "the lease names another table.",
        )
        _require(
            is_canonical_instant(self.park_instant),
            "park instant must be a 20-digit epoch-microseconds instant.",
        )
        fence = _parsed_fence(self.fence, self.physical_table_id)
        _require(
            isinstance(fence, ActiveFence | ParkedFence),
            "parking conditions an active or parked fence.",
        )
        attempt = _parsed_attempt(self.attempt, self.physical_table_id)
        _require(
            isinstance(
                attempt, IntentAttempt | SubmittedAttempt | CancelRequestedAttempt | ParkedAttempt
            ),
            "parking changes only a nonterminal attempt.",
        )
        if isinstance(fence, ActiveFence | ParkedFence) and isinstance(
            attempt, IntentAttempt | SubmittedAttempt | CancelRequestedAttempt | ParkedAttempt
        ):
            _require_pair_bound(fence, attempt)
            _require(
                self.lease.holder == fence.lease_holder
                and self.lease.fencing_token == fence.fencing_token,
                "the lease must carry the fence's exact holder and token.",
            )
        _require_pending_reconciliation(self.fence)
        if isinstance(fence, ParkedFence):
            _require(
                fence.parked_at == self.park_instant,
                "an exact parked replay preserves the original park instant.",
            )
            _require(
                self.lease.parked_at == self.park_instant,
                "an exact parked replay carries the already-parked lease.",
            )
        else:
            _require(self.lease.parked_at is None, "a real park starts from an unparked lease.")

    @property
    def replays_parked_state(self) -> bool:
        """Whether the observed pair is already parked (an exact replay no-op)."""
        return self.fence.state == "parked"

    @property
    def old_reconcile_count(self) -> int:
        """The observed pending reconcile count ``c``."""
        count = self.fence.reconciliation.reconcile_attempt_count
        if isinstance(count, bool) or not isinstance(count, int):  # pragma: no cover
            raise ValueError("the pending reconcile count was validated at construction.")
        return count

    def new_reconcile_due_micros(self) -> int:
        """Return the §5 rescheduled due: park instant plus the exact backoff for ``c``."""
        return rescheduled_reconcile_due_micros(self.park_instant, self.old_reconcile_count)


class ParkAttemptOutcome(StrEnum):
    """Parked now, or proven the exact original instant/count/due bytes (no-op)."""

    PARKED = "parked"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class ParkAttemptResult:
    """One park's typed outcome."""

    outcome: ParkAttemptOutcome


# --- CompleteTableAttemptRequest ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompleteTableAttemptRequest:
    """One provider-terminal completion: terminal record, fence clearance, owner advance.

    ``proof`` exists exactly for the reconciler's inline variant, whose run
    transitions to resumable ``reconciled`` with the registered reconciled event;
    a live inline completion returns the run to ``open`` with no event, and a
    queued completion changes no unit lifecycle state.
    """

    physical_table_id: str
    fence: FenceObservation
    attempt: AttemptObservation
    provider_state: ProviderTerminalState
    terminal_at: str
    owner: TableAttemptOwnerSnapshot
    query_count: int | None = None
    scanned_bytes: int | None = None
    engine_ms: int | None = None
    proof: Stage1LifecycleProof | None = None

    def __post_init__(self) -> None:
        _require(
            is_canonical_instant(self.terminal_at),
            "terminal instant must be a 20-digit epoch-microseconds instant.",
        )
        for name in ("query_count", "scanned_bytes", "engine_ms"):
            value: int | None = getattr(self, name)
            _require(
                value is None or (not isinstance(value, bool) and value >= 0),
                f"{name} must be a nonnegative integer exactly when known.",
            )
        fence = _parsed_fence(self.fence, self.physical_table_id)
        _require(
            isinstance(fence, ActiveFence | ParkedFence),
            "completion clears only a matching active or parked fence.",
        )
        attempt = _parsed_attempt(self.attempt, self.physical_table_id)
        _require(
            isinstance(attempt, SubmittedAttempt | CancelRequestedAttempt | ParkedAttempt),
            "completion requires a nonterminal attempt beyond intent.",
        )
        if isinstance(attempt, SubmittedAttempt | CancelRequestedAttempt | ParkedAttempt):
            _require(
                valid_query_execution_id(attempt.query_execution_id),
                "completion requires the durable valid query execution identity.",
            )
            if isinstance(fence, ActiveFence | ParkedFence):
                _require_pair_bound(fence, attempt)
                _require_bound_owner(self.owner, attempt)
        if isinstance(self.owner, QueuedUnitAttemptOwner):
            _require(self.proof is None, "a queued completion writes no lifecycle event.")
        elif self.proof is not None:
            authority = _single_event_authority(self.proof)
            _require(
                authority.transition_id == _INLINE_RECONCILED_TRANSITION,
                "the reconciler variant puts only the registered reconciled event.",
            )

    @property
    def resulting_run_state(self) -> str:
        """The inline run's resulting state: reconciled with the proof, else open."""
        return "reconciled" if self.proof is not None else "open"


class CompleteTableAttemptOutcome(StrEnum):
    """Completed now, or proven the exact durable terminal record (no-op)."""

    COMPLETED = "completed"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class CompleteTableAttemptResult:
    """One completion's typed outcome."""

    outcome: CompleteTableAttemptOutcome


# --- RescheduleTableAttemptReconcileRequest -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RescheduleTableAttemptReconcileRequest:
    """One §5 pending-fence reschedule at a single captured UTC operation instant.

    Only the fence's due GSI key and count change: the request conditions the exact
    fence/attempt binding and the old canonical pending tuple, retains ``pending``
    status and any exact prior requeue audit, and sets ``new_count = c + 1`` with
    due exactly operation instant plus the closed §5 backoff for old count ``c``.
    """

    physical_table_id: str
    fence: FenceObservation
    attempt: AttemptObservation
    operation_instant: str

    def __post_init__(self) -> None:
        _require(
            is_canonical_instant(self.operation_instant),
            "operation instant must be a 20-digit epoch-microseconds instant.",
        )
        fence = _parsed_fence(self.fence, self.physical_table_id)
        _require(
            isinstance(fence, ActiveFence | ParkedFence),
            "a reschedule conditions an active or parked fence.",
        )
        attempt = _parsed_attempt(self.attempt, self.physical_table_id)
        _require(
            not isinstance(attempt, TerminalAttempt),
            "a reschedule conditions the fence's named nonterminal attempt.",
        )
        if isinstance(fence, ActiveFence | ParkedFence) and not isinstance(
            attempt, TerminalAttempt
        ):
            _require_pair_bound(fence, attempt)
        _require(
            self.fence.reconciliation.reconcile_status == "pending"
            and reconciliation_valid(self.fence),
            "a reschedule is valid only for one exact canonical pending tuple.",
        )

    @property
    def old_reconcile_count(self) -> int:
        """The observed pending reconcile count ``c``."""
        count = self.fence.reconciliation.reconcile_attempt_count
        if isinstance(count, bool) or not isinstance(count, int):  # pragma: no cover
            raise ValueError("the pending reconcile count was validated at construction.")
        return count

    def new_reconcile_due_micros(self) -> int:
        """Return the §5 rescheduled due: operation instant plus the exact backoff."""
        return rescheduled_reconcile_due_micros(self.operation_instant, self.old_reconcile_count)


class RescheduleTableAttemptReconcileOutcome(StrEnum):
    """Rescheduled now, or proven the exact resulting count/due bytes (no-op)."""

    RESCHEDULED = "rescheduled"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class RescheduleTableAttemptReconcileResult:
    """One reschedule's typed outcome and canonical resulting tuple."""

    outcome: RescheduleTableAttemptReconcileOutcome
    reconcile_attempt_count: int
    reconcile_after_or_block: str


# --- BlockTableAttemptReconcileRequest ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MalformedReconciliationMetadataObservation:
    """The exact reconciliation-field presence/type/value snapshot of one malformed tuple."""

    reconciliation: ReconciliationObservation

    @property
    def reason(self) -> ReconcileBlockReason:
        """The closed reason this observation variant derives."""
        return ReconcileBlockReason.MALFORMED_RECONCILIATION_METADATA

    def digest(self) -> str:
        """Derive the pinned observation digest for this exact snapshot."""
        return reconcile_observation_digest(
            "malformed_reconciliation_metadata", self.reconciliation
        )


@dataclass(frozen=True, slots=True)
class SameSnapshotRescheduleRejectedObservation:
    """The exact rejected/reread byte-identical pending snapshot of one refused reschedule."""

    reconciliation: ReconciliationObservation

    def __post_init__(self) -> None:
        _require(
            self.reconciliation.reconcile_status == "pending"
            and not self.reconciliation.malformed_field_names,
            "a same-snapshot rejection retains one readable pending snapshot.",
        )

    @property
    def reason(self) -> ReconcileBlockReason:
        """The closed reason this observation variant derives."""
        return ReconcileBlockReason.SAME_SNAPSHOT_RESCHEDULE_REJECTED

    def digest(self) -> str:
        """Derive the pinned observation digest for this exact snapshot."""
        return reconcile_observation_digest(
            "same_snapshot_reschedule_rejected", self.reconciliation
        )


type ReconcileBlockObservation = (
    MalformedReconciliationMetadataObservation | SameSnapshotRescheduleRejectedObservation
)


@dataclass(frozen=True, slots=True)
class BlockTableAttemptReconcileRequest:
    """One typed reconciliation block over one exact valid nonterminal attempt pair.

    The builder derives reason, observation/binding digests, the deterministic
    block id, the opaque recovery reference, the blocked GSI key, and the
    protected case; it replaces only reconciliation fields with the canonical
    blocked form and never requires a valid old pending tuple.
    """

    physical_table_id: str
    fence: FenceObservation
    attempt: AttemptObservation
    owner: TableAttemptOwnerSnapshot
    observation: ReconcileBlockObservation
    block_instant: str

    def __post_init__(self) -> None:
        _require(
            is_canonical_instant(self.block_instant),
            "block instant must be a 20-digit epoch-microseconds instant.",
        )
        fence = _parsed_fence(self.fence, self.physical_table_id)
        _require(
            isinstance(fence, ActiveFence | ParkedFence),
            "a typed block conditions an active or parked fence.",
        )
        attempt = _parsed_attempt(self.attempt, self.physical_table_id)
        _require(
            not isinstance(attempt, TerminalAttempt),
            "a typed block conditions the fence's named nonterminal attempt.",
        )
        if isinstance(fence, ActiveFence | ParkedFence) and not isinstance(
            attempt, TerminalAttempt
        ):
            _require_pair_bound(fence, attempt)
            _require_bound_owner(self.owner, attempt)
        _require(
            self.fence.reconciliation == self.observation.reconciliation,
            "the observation must be the exact snapshot of this fence's reconciliation.",
        )
        if isinstance(self.observation, MalformedReconciliationMetadataObservation):
            _require(
                not reconciliation_valid(self.fence),
                "the malformed variant blocks only a non-canonical reconciliation tuple.",
            )
        else:
            _require(
                reconciliation_valid(self.fence)
                and self.fence.reconciliation.reconcile_status == "pending",
                "a same-snapshot rejection blocks only a canonical pending tuple.",
            )

    @property
    def reason(self) -> ReconcileBlockReason:
        """The closed derived block reason."""
        return self.observation.reason

    def observation_digest(self) -> str:
        """Derive the pinned observation digest."""
        return self.observation.digest()

    def binding_digest(self) -> str:
        """Derive the §3.1 binding digest for the exact fence binding."""
        attempt_id = self.fence.attempt_id
        holder = self.fence.lease_holder
        token = self.fence.fencing_token
        if attempt_id is None or holder is None or token is None:  # pragma: no cover
            raise ValueError("the fence binding was validated at construction.")
        return binding_digest(
            BindingDigestRequest(self.physical_table_id, attempt_id, holder, token)
        )

    def block_id(self) -> str:
        """Derive the sole deterministic block identity."""
        return reconcile_block_identity(
            ReconcileBlockIdentityRequest(
                binding_digest=self.binding_digest(),
                reason=self.reason,
                observation_digest=self.observation_digest(),
                blocked_at=self.block_instant,
            )
        )

    def recovery_ref(self) -> str:
        """Derive the opaque canonical recovery reference."""
        return reconcile_recovery_reference(self.block_id())


class BlockTableAttemptReconcileOutcome(StrEnum):
    """Blocked now, or proven the byte-identical canonical blocked/case form (no-op)."""

    BLOCKED = "blocked"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class BlockTableAttemptReconcileResult:
    """One block's typed outcome and derived identities."""

    outcome: BlockTableAttemptReconcileOutcome
    reconcile_block_id: str
    recovery_ref: str


# --- RequeueBlockedTableAttemptReconcileRequest ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequeueBlockedTableAttemptReconcileRequest:
    """One recovery-broker requeue of exactly one canonical block output.

    The request requires byte equality across the protected case and the
    canonical blocked ``FENCE``/``ATTEMPT`` selectors; it performs no repair or
    correction step and rejects caller selectors, stale or terminal generations,
    and raw keys, maps, or expressions by construction.
    """

    physical_table_id: str
    case: ProtectedReconcileCase
    fence: FenceObservation
    attempt: AttemptObservation
    owner: TableAttemptOwnerSnapshot
    operation_id: str
    resolution_reason: str
    requeue_instant: str

    def __post_init__(self) -> None:
        _require(bool(self.operation_id.strip()), "requeue names its broker operation id.")
        _require(bool(self.resolution_reason.strip()), "requeue carries a sanitized reason.")
        _require(
            is_canonical_instant(self.requeue_instant),
            "requeue instant must be a 20-digit epoch-microseconds instant.",
        )
        _require(
            self.case.state is ProtectedCaseState.BLOCKED,
            "requeue consumes only a blocked protected case.",
        )
        fence = _parsed_fence(self.fence, self.physical_table_id)
        _require(
            isinstance(fence, ActiveFence | ParkedFence),
            "requeue conditions an active or parked fence.",
        )
        attempt = _parsed_attempt(self.attempt, self.physical_table_id)
        _require(
            not isinstance(attempt, TerminalAttempt),
            "requeue conditions the fence's named nonterminal attempt.",
        )
        if isinstance(fence, ActiveFence | ParkedFence) and not isinstance(
            attempt, TerminalAttempt
        ):
            _require_pair_bound(fence, attempt)
            _require_bound_owner(self.owner, attempt)
        rec = self.fence.reconciliation
        _require(
            rec.reconcile_status == "blocked" and reconciliation_valid(self.fence),
            "requeue consumes only one canonical blocked reconciliation form.",
        )
        _require(
            self.case.physical_table_id == self.physical_table_id
            and self.case.attempt_id == self.fence.attempt_id
            and rec.reconcile_recovery_ref == self.case.recovery_ref
            and rec.reconcile_block_id == self.case.reconcile_block_id
            and rec.reconcile_block_reason == self.case.reason.value
            and rec.reconcile_blocked_at == self.case.blocked_at
            and rec.reconcile_observation_digest == self.case.observation_digest
            and self.case.binding_digest == self.binding_digest(),
            "the case and canonical blocked pair must carry byte-equal selectors.",
        )

    def binding_digest(self) -> str:
        """Derive the §3.1 binding digest for the exact fence binding."""
        attempt_id = self.fence.attempt_id
        holder = self.fence.lease_holder
        token = self.fence.fencing_token
        if attempt_id is None or holder is None or token is None:  # pragma: no cover
            raise ValueError("the fence binding was validated at construction.")
        return binding_digest(
            BindingDigestRequest(self.physical_table_id, attempt_id, holder, token)
        )

    def requeue_id(self) -> str:
        """Derive the deterministic requeue identity."""
        return reconcile_requeue_identity(self.case.recovery_ref, self.operation_id)

    def pending_reconcile_key(self) -> str:
        """Derive the canonical pending due key at exactly the requeue instant."""
        return pending_reconcile_key(int(self.requeue_instant), self.binding_digest())


class RequeueBlockedTableAttemptReconcileOutcome(StrEnum):
    """Requeued now, or proven the exact prior consumption (``already_requeued``)."""

    REQUEUED = "requeued"
    ALREADY_REQUEUED = "already_requeued"


@dataclass(frozen=True, slots=True)
class RequeueBlockedTableAttemptReconcileResult:
    """One requeue's typed outcome and derived identity."""

    outcome: RequeueBlockedTableAttemptReconcileOutcome
    requeue_id: str


# --- ClaimUnitRequest -------------------------------------------------------------------------


_CLAIMABLE_UNIT_STATUSES: Final = frozenset({"queued", "failed", "writing"})
_UNIT_STARTED_TRANSITION: Final = "semantic_unit_started"


@dataclass(frozen=True, slots=True)
class ClaimUnitRequest:
    """One table-writer claim: ``queued|failed|writing → writing`` with its exact owner facts.

    A ``failed`` unit is a redrive: the same transaction decrements the plan
    ``META.failed_count`` exactly once. Permanent Begin reservation counters
    never decrement. The writer/receive identity is the §4
    ``stage1-writer-execution/v1`` constructor output supplied whole.

    A ``writing`` unit is a §8 reclaim of a dead writer's unit under a different
    writer execution: the unit must bind no nonterminal attempt
    (``current_attempt_id`` absent), and the request carries the exact prior
    ``writer_execution_id`` plus the prior §5 queued lease holder so the adapter
    can add the one leases-table condition proving that holder's lease absent,
    expired, or released. A live prior holder is the ``PRIOR_WRITER_LIVE``
    outcome, never a claim.
    """

    plan_digest: str
    plan_key: str
    physical_table_id: str
    intent: IntentMeta
    owner: SuccessorOwner
    plan_meta: PlanMeta
    unit_item: Mapping[str, object]
    writer_execution_id: str
    operation_instant: str
    plan_head: PlanHeadObservation
    proof: Stage1LifecycleProof
    prior_writer_execution_id: str | None = None
    prior_lease_holder: str | None = None

    def __post_init__(self) -> None:
        _require(self.intent.plan_digest == self.plan_digest, "the intent names this digest.")
        _require(
            is_canonical_instant(self.operation_instant),
            "operation instant must be a 20-digit epoch-microseconds instant.",
        )
        _require(
            self.intent.state is IntentState.ADMITTED,
            "a unit claim conditions the admitted origin intent.",
        )
        _require(
            self.owner.active_plan_key == self.plan_key
            and self.owner.active_intent_id == self.intent.intent_id,
            "the owner active pair must name exactly this plan and intent.",
        )
        _require(
            self.plan_key
            == plan_partition_key(self.plan_meta.plan_digest, self.plan_meta.internal_execution_id),
            "the plan key must reproduce from the plan META identity.",
        )
        _require(self.plan_meta.status is PlanStatus.PLANNED, "a claim requires a planned plan.")
        _require(
            self.plan_meta.origin_intent_id == self.intent.intent_id
            and self.plan_meta.generation == self.intent.claimed_generation,
            "the plan META must carry this intent and its exact claimed generation.",
        )
        _require(
            self.plan_meta.admitted_count == self.plan_meta.unit_count,
            "a unit claim requires the fully admitted plan.",
        )
        _require(
            self.owner.latest_internal_execution_id == self.plan_meta.internal_execution_id,
            "the owner and plan HEAD must agree on the internal execution identity.",
        )
        _require(
            self.plan_head.head.current_internal_execution_id
            == self.owner.latest_internal_execution_id,
            "the copied plan-head observation must agree with the owner.",
        )
        _require(
            self.unit_item.get("partition_key") == self.plan_key
            and self.unit_item.get("record_key") == f"{UNIT_RECORD_PREFIX}{self.physical_table_id}",
            "the unit snapshot must carry its exact plan partition and UNIT# record key.",
        )
        _require(
            _snapshot_text(self.unit_item, "status") in _CLAIMABLE_UNIT_STATUSES,
            "a claim changes only a queued, failed, or writing unit to writing.",
        )
        _require(
            _snapshot_int(self.unit_item, "owner_generation", default=-1)
            == self.plan_meta.generation,
            "the unit must carry the plan's exact generation.",
        )
        _require(bool(self.writer_execution_id.strip()), "a claim names its writer identity.")
        if self.redrive:
            _require(
                self.plan_meta.failed_count >= 1,
                "a redrive decrements a positive failed_count exactly once.",
            )
        self._require_reclaim_facts()
        authority = _single_event_authority(self.proof)
        _require(
            authority.transition_id == _UNIT_STARTED_TRANSITION,
            "a unit claim puts only the registered unit-started event.",
        )

    def _require_reclaim_facts(self) -> None:
        prior_writer = self.prior_writer_execution_id
        prior_holder = self.prior_lease_holder
        if not self.reclaim:
            _require(
                prior_writer is None and prior_holder is None,
                "prior writer facts exist only for a writing-unit reclaim.",
            )
            return
        _require(
            prior_writer is not None and prior_holder is not None,
            "a writing-unit reclaim names the prior writer execution and its lease holder.",
        )
        if prior_writer is None or prior_holder is None:  # pragma: no cover -- required above
            raise ValueError("a reclaim carries both prior writer facts.")
        _require(
            prior_writer == self.unit_item.get("writer_execution_id"),
            "the prior writer execution must equal the unit's exact durable writer.",
        )
        _require(
            prior_writer != self.writer_execution_id,
            "a reclaim happens only under a different writer execution.",
        )
        _require(
            self.unit_item.get("current_attempt_id") is None,
            "a reclaim requires a unit bound to no nonterminal attempt.",
        )
        _require(
            prior_holder
            == queued_lease_holder(self.plan_key, self.physical_table_id, prior_writer),
            "the prior lease holder must reproduce from the plan, unit, and prior writer.",
        )

    @property
    def redrive(self) -> bool:
        """Whether this claim redrives a failed unit (``failed → writing``)."""
        return _snapshot_text(self.unit_item, "status") == "failed"

    @property
    def reclaim(self) -> bool:
        """Whether this claim reclaims a dead writer's unit (``writing → writing``)."""
        return _snapshot_text(self.unit_item, "status") == "writing"


class ClaimUnitOutcome(StrEnum):
    """Claimed now, proven the exact durable claim (no-op), or refused by a live prior holder."""

    CLAIMED = "claimed"
    REPLAYED = "replayed"
    PRIOR_WRITER_LIVE = "prior_writer_live"


@dataclass(frozen=True, slots=True)
class ClaimUnitResult:
    """One unit claim's typed outcome; ``redrive`` marks the one-time decrement.

    ``held_until`` is the live prior holder's exact lease expiry and exists exactly
    for the ``PRIOR_WRITER_LIVE`` outcome, mirroring ``AcquireTableFenceResult``.
    """

    outcome: ClaimUnitOutcome
    redrive: bool
    held_until: str | None = None

    def __post_init__(self) -> None:
        _require(
            (self.outcome is ClaimUnitOutcome.PRIOR_WRITER_LIVE) == (self.held_until is not None),
            "held_until exists exactly when the prior writer is live.",
        )


# --- Private settlement expansion (SettleStage1AthenaUsageRequest) --------------------------------


_MAX_SETTLED_ATTEMPTS: Final = 10


@dataclass(frozen=True, slots=True)
class AthenaAttemptSettlement:
    """One accepted provider-terminal attempt beside its owner-settlement inputs.

    The caller never supplies a category: the concept-owned mapping derives it
    from the durable owner disposition inside the private sub-builder.
    """

    attempt: AttemptObservation
    settlement: SettleStage1AthenaUsageRequest

    def __post_init__(self) -> None:
        parsed = parse_attempt(self.attempt)
        _require(
            isinstance(parsed, TerminalAttempt),
            "settlement covers only provider-terminal accepted attempts.",
        )
        _require(
            self.attempt.athena_usage_state == "unsettled",
            "settlement changes exactly unsettled Athena usage; byte-identical "
            "settled state is recognized only as exact transaction replay.",
        )
        _require(
            self.settlement.attempt_id == self.attempt.attempt_id,
            "the settlement inputs must name this exact attempt.",
        )
        _require(
            self.settlement.provider_terminal_state is not None
            and self.settlement.provider_terminal_state.value == self.attempt.state,
            "the settlement provider state must be the attempt's exact terminal state.",
        )
        _require(
            self.settlement.scanned_bytes == self.attempt.scanned_bytes
            and self.settlement.engine_ms == self.attempt.engine_ms,
            "the settlement quantities must be the attempt's exact known/missing evidence.",
        )
        _require(self.settlement.accepted, "settlement covers only accepted provider work.")


@dataclass(frozen=True, slots=True)
class Stage1SettlementExpansion:
    """The closed owner-disposition settlement roster expanded only by final builders."""

    owner_disposition: OwnerDisposition
    attempts: tuple[AthenaAttemptSettlement, ...]

    def __post_init__(self) -> None:
        _require(
            self.owner_disposition is not OwnerDisposition.NO_ACCEPTED_REQUEST,
            "settlement requires a final owner disposition over accepted work.",
        )
        _require(
            len(self.attempts) <= _MAX_SETTLED_ATTEMPTS,
            "a final owner disposition settles at most 10 referenced attempts.",
        )
        ids = [member.attempt.attempt_id for member in self.attempts]
        _require(len(set(ids)) == len(ids), "settled attempts must be distinct.")
        _require(
            all(
                member.settlement.owner_disposition is self.owner_disposition
                for member in self.attempts
            ),
            "every settlement carries this transaction's one owner disposition.",
        )
        observed = {member.settlement.observed_at for member in self.attempts}
        _require(
            len(observed) <= 1,
            "every settlement carries the one settled-disposition observation instant.",
        )


# --- FailUnitRequest --------------------------------------------------------------------------


class UnitFailureReason(StrEnum):
    """The closed §3.1 unit failure dispositions; no other reason exists."""

    STATEMENT_RETRIES_EXHAUSTED = "statement_retries_exhausted"
    PLAN_ATTEMPT_BUDGET_EXHAUSTED = "plan_attempt_budget_exhausted"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    VERIFICATION_MISMATCH = "verification_mismatch"


_PLAN_RESERVATION_MAXIMUM: Final = 32
_REJECTED_BUDGET_ORDINAL: Final = 33


@dataclass(frozen=True, slots=True)
class AttemptBudgetExhaustion:
    """The attempt-33 branch facts: the observed budget proof and successor roster.

    Queued reservation 33 performs no Athena call and creates no Begin
    reservation; the observed permanent reservation count must be exactly 32
    and the rejected ordinal exactly 33.
    """

    successor_units: tuple[CanonicalUnitEntry, ...]
    permanent_reservation_count: int
    rejected_ordinal: int

    def __post_init__(self) -> None:
        _require(bool(self.successor_units), "the successor roster reproduces the plan roster.")
        _require(
            self.permanent_reservation_count == _PLAN_RESERVATION_MAXIMUM,
            "attempt-budget exhaustion requires permanent reservations exactly 32.",
        )
        _require(
            self.rejected_ordinal == _REJECTED_BUDGET_ORDINAL,
            "attempt-budget exhaustion rejects exactly ordinal 33.",
        )


def _require_settled_history(meta: PlanMeta, prior: tuple[SettledAttemptReference, ...]) -> None:
    _require(
        len(prior) == meta.settled_attempt_count,
        "prior settled references must match the plan's complete settled-attempt count.",
    )
    if prior:
        _require(
            plan_settled_attempt_digest(prior) == meta.settled_attempt_digest,
            "prior settled references must reproduce the plan's exact settled digest.",
        )
    else:
        _require(
            meta.settled_attempt_digest is None,
            "a zero settled-attempt count carries no digest.",
        )


@dataclass(frozen=True, slots=True)
class FailUnitRequest:
    """One unit failure disposition with its atomic settlement expansion.

    The ordinary branch updates the owned unit and counters. The attempt-budget
    branch (permanent reservations exactly 32, rejected ordinal 33) carries no
    unit transaction member: the exact unexpired lease condition — whose queued
    holder is the §5 hash of ``[plan_key, unit_key, writer_execution_id]`` —
    proves the unit's writing state and writer binding transitively. It replaces
    the ordinary failure trigger with the one deterministic origin
    ``plan_attempt_budget`` successor intent ``META`` plus every successor
    ``UNIT#``, consumes the owner, and terminalizes the source plan.
    """

    plan_digest: str
    plan_key: str
    physical_table_id: str
    writer_execution_id: str
    owner: SuccessorOwner
    plan_meta: PlanMeta
    lease: TableLeaseRecord
    observed_fence: FenceObservation | None
    reason: UnitFailureReason
    settlement: Stage1SettlementExpansion
    prior_settled_attempts: tuple[SettledAttemptReference, ...]
    operation_instant: str
    proof: Stage1LifecycleProof
    unit_item: Mapping[str, object] | None = None
    budget_exhaustion: AttemptBudgetExhaustion | None = None

    def __post_init__(self) -> None:
        _require(
            is_canonical_instant(self.operation_instant),
            "operation instant must be a 20-digit epoch-microseconds instant.",
        )
        _require(
            self.owner.active_plan_key == self.plan_key
            and self.owner.active_intent_id == self.plan_meta.origin_intent_id,
            "the owner active pair must name exactly this plan and its origin intent.",
        )
        _require(
            self.plan_key
            == plan_partition_key(self.plan_meta.plan_digest, self.plan_meta.internal_execution_id),
            "the plan key must reproduce from the plan META identity.",
        )
        _require(self.plan_meta.status is PlanStatus.PLANNED, "only a planned plan's unit fails.")
        _require(
            self.plan_meta.admitted_count == self.plan_meta.unit_count,
            "the conditioned full-admission counters prove the admitted origin intent.",
        )
        _require(
            self.owner.latest_internal_execution_id == self.plan_meta.internal_execution_id,
            "the owner and plan HEAD must agree on the internal execution identity.",
        )
        _require(
            self.lease.physical_table_id == self.physical_table_id
            and self.lease.holder
            == queued_lease_holder(self.plan_key, self.physical_table_id, self.writer_execution_id),
            "the lease holder must be the §5 queued hash of this exact unit binding.",
        )
        _require(
            int(self.operation_instant) < self.lease.expiry_epoch_micros(),
            "a unit failure requires the exact unexpired lease.",
        )
        if self.observed_fence is not None:
            fence = _parsed_fence(self.observed_fence, self.physical_table_id)
            _require(
                isinstance(fence, ClearFence),
                "a unit failure requires the fence absent or exactly clear.",
            )
        _require(
            self.settlement.owner_disposition
            is (
                OwnerDisposition.EXHAUSTED_OR_UNAVAILABLE
                if self.attempt_budget_exhausted
                else OwnerDisposition.RETRYABLE_FAILURE
            ),
            "the settlement disposition must match the closed failure branch.",
        )
        _require_settled_history(self.plan_meta, self.prior_settled_attempts)
        _require(len(self.proof.events) == 1, "a unit failure writes exactly one event.")
        if self.attempt_budget_exhausted:
            self._require_budget_branch()
        else:
            self._require_ordinary_branch()

    def _require_ordinary_branch(self) -> None:
        _require(
            self.budget_exhaustion is None,
            "only the attempt-budget reason carries the successor branch.",
        )
        unit = self.unit_item
        _require(unit is not None, "the ordinary branch updates the exact writing unit.")
        if unit is None:  # pragma: no cover -- excluded by the requirement above
            raise ValueError("the ordinary branch updates the exact writing unit.")
        _require(
            unit.get("partition_key") == self.plan_key
            and unit.get("record_key") == f"{UNIT_RECORD_PREFIX}{self.physical_table_id}"
            and _snapshot_text(unit, "status") == _UNIT_WRITING_STATUS
            and _snapshot_text(unit, "writer_execution_id") == self.writer_execution_id,
            "the ordinary branch conditions the exact writing unit and writer identity.",
        )
        _require(
            self.plan_meta.failed_count < self.plan_meta.unit_count,
            "failed_count increments once within its unit-count bound.",
        )

    def _require_budget_branch(self) -> None:
        _require(
            self.unit_item is None,
            "the attempt-33 branch carries no unit transaction member.",
        )
        exhaustion = self.budget_exhaustion
        _require(
            exhaustion is not None,
            "the attempt-budget reason requires the successor roster.",
        )
        if exhaustion is None:  # pragma: no cover -- excluded by the requirement above
            raise ValueError("the attempt-budget reason requires the successor roster.")
        _require(
            roster_digest(exhaustion.successor_units) == self.plan_meta.roster_digest,
            "the successor roster must reproduce the plan's exact roster digest.",
        )

    @property
    def attempt_budget_exhausted(self) -> bool:
        """Whether this is the attempt-33 (rejected ordinal 33) branch."""
        return self.reason is UnitFailureReason.PLAN_ATTEMPT_BUDGET_EXHAUSTED

    def successor_origin(self) -> IntentOrigin:
        """Derive the deterministic ``plan_attempt_budget`` successor origin."""
        return IntentOrigin(
            IntentOriginKind.PLAN_ATTEMPT_BUDGET, plan_attempt_budget_origin(self.plan_key)
        )

    def successor_intent_id(self) -> str:
        """Derive the one deterministic successor intent identity."""
        exhaustion = self.budget_exhaustion
        if exhaustion is None:
            raise ValueError("only the attempt-budget branch derives a successor intent.")
        return intent_identity(
            IntentAuthorityInputs(
                self.owner.root_plan_key,
                self.plan_key,
                self.plan_digest,
                self.successor_origin(),
                exhaustion.successor_units,
            )
        )


class FailUnitOutcome(StrEnum):
    """Failed now, or proven the exact durable disposition (replay cannot fan out)."""

    FAILED = "failed"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class FailUnitResult:
    """One unit failure's typed outcome and optional successor identity."""

    outcome: FailUnitOutcome
    successor_intent_id: str | None = None


# --- Finalization pointer outcomes -------------------------------------------------------------


_COORDINATOR_PREFIXES: Final = (
    WRITE_PLAN_PREFIX,
    STATUS_FLOW_PREFIX,
    "TABLE_ATTEMPT#",
    "TABLE#",
    "PROVIDER_RECONCILE_BLOCK#",
)
SERVABLE_NONE_SENTINEL: Final = "NONE"


class PointerOutcomeKind(StrEnum):
    """Closed pointer finalization outcomes (§7)."""

    ADVANCED = "advanced"
    PROJECTION_SUPERSEDED = "projection_superseded"


@dataclass(frozen=True, slots=True)
class CurrentPointerOutcome:
    """One validated canonical-partition ``/CURRENT`` outcome plus its headless event.

    An advanced outcome updates the servable projection under the existing
    monotonic rule; a superseded outcome omits the advance and puts only its
    pointer event. Pointer flows are headless: the proof carries no head effect.
    """

    kind: PointerOutcomeKind
    partition_identity: str
    projected_publication_id: str
    projected_fencing_token: int
    proof: Stage1LifecycleProof
    observed_current: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        _require(bool(self.partition_identity.strip()), "a pointer names its exact partition.")
        _require(
            not any(self.partition_identity.startswith(p) for p in _COORDINATOR_PREFIXES),
            "a pointer partition lies outside every coordinator key family.",
        )
        _require(
            bool(self.projected_publication_id.strip()),
            "a pointer names its exact projected publication.",
        )
        _require(
            not isinstance(self.projected_fencing_token, bool)
            and self.projected_fencing_token >= 1,
            "a pointer carries a positive projected fencing token.",
        )
        _require(
            self.proof.resulting_head is None and self.proof.head_record_key is None,
            "pointer flows are headless; the proof carries no head effect.",
        )
        _require(len(self.proof.events) == 1, "a pointer outcome puts exactly one event.")
        _require(
            self.proof.partition_key == self.partition_identity,
            "the pointer event lives in its exact publication partition.",
        )
        advanced = self.kind is PointerOutcomeKind.ADVANCED
        _require(
            advanced == (self.observed_current is not None),
            "an advanced outcome carries its exact observed CURRENT snapshot.",
        )
        if self.observed_current is not None:
            current = self.observed_current
            _require(
                current.get("partition_key") == self.partition_identity
                and current.get("record_key") == "CURRENT"
                and current.get("publication_id") is not None,
                "the CURRENT snapshot must carry its exact keys and publication identity.",
            )
            projected = current.get("projected_publication_id")
            token = current.get("projected_fencing_token")
            monotonic = (
                projected is None
                or projected == SERVABLE_NONE_SENTINEL
                or (
                    isinstance(token, int)
                    and not isinstance(token, bool)
                    and token < self.projected_fencing_token
                )
            )
            _require(
                monotonic,
                "an equal or newer projected pointer is omitted as projection_superseded.",
            )


# --- FinalizeUnitRequest ------------------------------------------------------------------------


_MAX_POINTER_OUTCOMES: Final = 37
_FINALIZE_DISPOSITIONS: Final = frozenset(
    {OwnerDisposition.SUCCESSFUL, OwnerDisposition.NO_CHANGE, OwnerDisposition.SUPERSEDED}
)


def _require_finalize_shared(  # noqa: PLR0913, PLR0917 -- private shared validator
    lease: TableLeaseRecord,
    table_id: str,
    observed_fence: FenceObservation | None,
    pointers: tuple[CurrentPointerOutcome, ...],
    settlement: Stage1SettlementExpansion,
    operation_instant: str,
) -> None:
    _require(
        is_canonical_instant(operation_instant),
        "operation instant must be a 20-digit epoch-microseconds instant.",
    )
    _require(lease.physical_table_id == table_id, "the lease names another table.")
    _require(
        int(operation_instant) < lease.expiry_epoch_micros(),
        "finalization requires the exact unexpired lease.",
    )
    if observed_fence is not None:
        fence = _parsed_fence(observed_fence, table_id)
        _require(
            isinstance(fence, ClearFence),
            "finalization requires the fence absent or exactly clear.",
        )
    _require(
        len(pointers) <= _MAX_POINTER_OUTCOMES,
        "finalization carries at most 37 pointer outcomes.",
    )
    partitions = [pointer.partition_identity for pointer in pointers]
    _require(len(set(partitions)) == len(partitions), "pointer partitions must be distinct.")
    _require(
        settlement.owner_disposition in _FINALIZE_DISPOSITIONS,
        "finalization settles under a successful, no-change, or superseded disposition.",
    )


@dataclass(frozen=True, slots=True)
class FinalizeUnitRequest:
    """One queued-unit finalization: pointers, attestation, settlement, event, unit head."""

    plan_digest: str
    plan_key: str
    physical_table_id: str
    writer_execution_id: str
    owner: SuccessorOwner
    plan_meta: PlanMeta
    unit_item: Mapping[str, object]
    lease: TableLeaseRecord
    observed_fence: FenceObservation | None
    pointers: tuple[CurrentPointerOutcome, ...]
    settlement: Stage1SettlementExpansion
    prior_settled_attempts: tuple[SettledAttemptReference, ...]
    verified_checksums: tuple[str, ...]
    operation_instant: str
    proof: Stage1LifecycleProof
    mode_guard: AuthorityModeGuard | None = None
    """V-13(a): only after a LEGACY_DRAINING batch read, ConditionCheck mode != GRAPH."""

    def __post_init__(self) -> None:
        _require_finalize_shared(
            self.lease,
            self.physical_table_id,
            self.observed_fence,
            self.pointers,
            self.settlement,
            self.operation_instant,
        )
        _require(
            self.owner.active_plan_key == self.plan_key
            and self.owner.active_intent_id == self.plan_meta.origin_intent_id,
            "the owner active pair must name exactly this plan and its origin intent.",
        )
        _require(
            self.plan_key
            == plan_partition_key(self.plan_meta.plan_digest, self.plan_meta.internal_execution_id),
            "the plan key must reproduce from the plan META identity.",
        )
        _require(self.plan_meta.status is PlanStatus.PLANNED, "finalization needs a planned plan.")
        _require(
            self.plan_meta.admitted_count == self.plan_meta.unit_count,
            "the conditioned full-admission counters prove the admitted origin intent.",
        )
        _require(
            self.plan_meta.attested_count < self.plan_meta.unit_count,
            "attested_count increments once within its unit-count bound.",
        )
        _require(
            self.lease.holder
            == queued_lease_holder(self.plan_key, self.physical_table_id, self.writer_execution_id),
            "the lease holder must be the §5 queued hash of this exact unit binding.",
        )
        _require(
            self.unit_item.get("partition_key") == self.plan_key
            and self.unit_item.get("record_key") == f"{UNIT_RECORD_PREFIX}{self.physical_table_id}"
            and _snapshot_text(self.unit_item, "status") == _UNIT_WRITING_STATUS
            and _snapshot_text(self.unit_item, "writer_execution_id") == self.writer_execution_id,
            "finalization changes the exact writing unit under its writer identity.",
        )
        _require_settled_history(self.plan_meta, self.prior_settled_attempts)
        self.verified_digest()  # canonical checksums or typed rejection
        _require(len(self.proof.events) == 1, "unit finalization writes exactly one unit event.")
        _require(
            self.proof.resulting_head is not None,
            "unit finalization advances exactly one unit head.",
        )

    def verified_digest(self) -> str:
        """Derive the unit's ``verified_digest`` over the sorted immutable checksums."""
        return unit_verified_digest(self.verified_checksums)


class FinalizeUnitOutcome(StrEnum):
    """Finalized now, or proven the exact durable attestation (no-op)."""

    FINALIZED = "finalized"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class FinalizeUnitResult:
    """One unit finalization's typed outcome."""

    outcome: FinalizeUnitOutcome


# --- FinalizeInlineRequest ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FinalizeInlineRequest:
    """One inline-run finalization: pointers, run terminal, settlement, event, run head."""

    physical_table_id: str
    run: InlineRunAttemptOwner
    lease: TableLeaseRecord
    observed_fence: FenceObservation | None
    pointers: tuple[CurrentPointerOutcome, ...]
    settlement: Stage1SettlementExpansion
    operation_instant: str
    proof: Stage1LifecycleProof

    def __post_init__(self) -> None:
        _require_finalize_shared(
            self.lease,
            self.physical_table_id,
            self.observed_fence,
            self.pointers,
            self.settlement,
            self.operation_instant,
        )
        _require(
            self.run.physical_table_id == self.physical_table_id,
            "the run names another table.",
        )
        _require(
            _snapshot_text(self.run.item, "state") in _RUN_BEGIN_STATES,
            "only lease-held finalize_inline changes open|reconciled to terminal.",
        )
        _require(
            self.run.item.get("current_attempt_id") is None,
            "the run cannot become terminal while an attempt remains bound.",
        )
        _require(len(self.proof.events) == 1, "run finalization writes exactly one run event.")
        _require(
            self.proof.resulting_head is not None,
            "run finalization advances exactly one run head.",
        )


class FinalizeInlineOutcome(StrEnum):
    """Finalized now, or proven the exact durable run terminal (no-op)."""

    FINALIZED = "finalized"
    REPLAYED = "replayed"


@dataclass(frozen=True, slots=True)
class FinalizeInlineResult:
    """One inline finalization's typed outcome."""

    outcome: FinalizeInlineOutcome


# --- Port -----------------------------------------------------------------------------------------


@runtime_checkable
class ProjectionCoordinatorPort(Protocol):
    """Private typed transaction builders over the retained publications table."""

    def count_drain(self, observed: AuthorityMode) -> None:
        """S6 (V-17b): record this invocation's strong mode read before its first transaction.

        After `LEGACY_DRAINING`, every transaction this port commits adds 1 to `DRAIN_SEQ`.
        """
        ...

    def submit_plan_intent(self, request: SubmitPlanIntentRequest) -> SubmitPlanIntentResult:
        """Create one pending intent and consume the owner; exact replay is a no-op."""
        ...

    def claim_plan_intent(self, request: ClaimPlanIntentRequest) -> ClaimPlanIntentResult:
        """Atomically reserve the owner and claim the due pending intent."""
        ...

    def reschedule_plan_intent(
        self, request: ReschedulePlanIntentRequest
    ) -> ReschedulePlanIntentResult:
        """Consume the exact reservation and return the same intent to pending."""
        ...

    def persist_plan(self, request: PersistPlanRequest) -> PersistPlanResult:
        """Create the plan and atomically swap the owner; replay retains the generation."""
        ...

    def admit_unit(self, request: AdmitUnitRequest) -> AdmitUnitResult:
        """Record one queue admission exactly once; the last admission admits the intent."""
        ...

    def fail_admission(self, request: FailAdmissionRequest) -> FailAdmissionResult:
        """Terminalize the candidate and return the same intent to pending atomically."""
        ...

    def complete_plan(self, request: CompletePlanRequest) -> CompletePlanResult:
        """Change the plan to completed and clear the owner active pair atomically."""
        ...

    def acquire_table_fence(self, request: AcquireTableFenceRequest) -> AcquireTableFenceResult:
        """Acquire the table lease against an absent-or-clear fence, or classify busy."""
        ...

    def release_table_fence(self, request: ReleaseTableFenceRequest) -> ReleaseTableFenceResult:
        """Set the lease expiry to the canonical released instant for the exact holder."""
        ...

    def begin_table_attempt(self, request: BeginTableAttemptRequest) -> BeginTableAttemptResult:
        """Atomically create the intent attempt, activate the fence, and reserve the owner."""
        ...

    def record_stage1_refusal(
        self, request: RecordStage1RefusalRequest
    ) -> RecordStage1RefusalResult:
        """Put the deterministic refusal event and advance only its exact owner head."""
        ...

    def persist_inline_run(self, request: PersistInlineRunRequest) -> PersistInlineRunResult:
        """Atomically create the run, its recorded event authority, and the run head."""
        ...

    def recover_parked_attempt_submission(
        self, request: RecoverParkedAttemptSubmissionRequest
    ) -> RecoverParkedAttemptSubmissionResult:
        """Attach the recovered identical submission to the exact parked pair atomically."""
        ...

    def request_attempt_cancellation(
        self, request: RequestAttemptCancellationRequest
    ) -> RequestAttemptCancellationResult:
        """Record the write-once cancellation instant against the exact nonterminal attempt."""
        ...

    def park_attempt(self, request: ParkAttemptRequest) -> ParkAttemptResult:
        """Park the exact lease, attempt, and fence atomically at one captured instant."""
        ...

    def complete_table_attempt(
        self, request: CompleteTableAttemptRequest
    ) -> CompleteTableAttemptResult:
        """Record the provider terminal, clear the exact fence, and advance the owner."""
        ...

    def reschedule_table_attempt_reconcile(
        self, request: RescheduleTableAttemptReconcileRequest
    ) -> RescheduleTableAttemptReconcileResult:
        """Replace only the pending due key/count against the exact fence/attempt binding."""
        ...

    def block_table_attempt_reconcile(
        self, request: BlockTableAttemptReconcileRequest
    ) -> BlockTableAttemptReconcileResult:
        """Replace only reconciliation fields with the canonical blocked form plus its case."""
        ...

    def requeue_blocked_table_attempt_reconcile(
        self, request: RequeueBlockedTableAttemptReconcileRequest
    ) -> RequeueBlockedTableAttemptReconcileResult:
        """Return one canonical block output to canonical pending and audit the case."""
        ...

    def claim_unit(self, request: ClaimUnitRequest) -> ClaimUnitResult:
        """Change the exact queued, failed, or dead-writer unit to writing under its owner facts."""
        ...

    def fail_unit(self, request: FailUnitRequest) -> FailUnitResult:
        """Settle referenced attempts and record the closed unit failure disposition."""
        ...

    def finalize_unit(self, request: FinalizeUnitRequest) -> FinalizeUnitResult:
        """Advance pointers, attest the unit, and settle attempts in one transaction."""
        ...

    def finalize_inline(self, request: FinalizeInlineRequest) -> FinalizeInlineResult:
        """Advance pointers, terminalize the run, and settle attempts in one transaction."""
        ...
